"""启动入口：准备数据目录 → 起本机服务 → 起生成界面后台（Streamlit 子进程）→ 开窗口 → 退出时收尾。

窗口用 pywebview（Windows 用 WebView2，macOS 用 WKWebView）。窗口建不起来时自动改用默认浏览器，功能相同。

退出路径（任一种都会走到 shutdown()）：
- 关闭最后一个窗口（macOS 红色按钮、Windows 右上角 ×、Alt+F4、任务栏「关闭窗口」）、macOS ⌘Q / 菜单「退出」/ 程序坞「退出」
  → webview.start() 返回；
- 顶栏「退出」按钮 → /api/quit → 销毁窗口；
- 浏览器模式下页面关闭超过 3 分钟、Ctrl+C、SIGTERM。
shutdown() 依次：停止生成界面后台（含 FFmpeg 等子进程，确认都已退出）→ 停本机服务、释放端口 → 删除运行记录 run.json。
"""
from __future__ import annotations

import argparse
import atexit
import json
import logging
import os
import secrets
import signal
import sys
import threading
import time
import urllib.request
import webbrowser
from logging.handlers import RotatingFileHandler

from . import APP_ID, APP_NAME, DEFAULT_FONT, UPSTREAM_COMMIT, __version__
from . import paths, supervisor, userdata
from .server import AppState, ServerThread

log = logging.getLogger("svf")
BROWSER_IDLE_EXIT = 180   # 浏览器模式：页面关闭 3 分钟后退出

LOCALIZATION = {          # pywebview 自带的英文菜单和对话框，改成中文
    "global.quitConfirmation": "确定要退出吗？", "global.ok": "好", "global.quit": "退出", "global.cancel": "取消",
    "global.saveFile": "保存文件", "cocoa.menu.about": "关于", "cocoa.menu.services": "服务", "cocoa.menu.view": "显示",
    "cocoa.menu.edit": "编辑", "cocoa.menu.hide": "隐藏", "cocoa.menu.hideOthers": "隐藏其他", "cocoa.menu.showAll": "全部显示",
    "cocoa.menu.quit": "退出", "cocoa.menu.fullscreen": "进入全屏幕", "cocoa.menu.cut": "剪切", "cocoa.menu.copy": "拷贝",
    "cocoa.menu.paste": "粘贴", "cocoa.menu.selectAll": "全选", "windows.fileFilter.allFiles": "所有文件",
    "windows.fileFilter.otherFiles": "其他文件类型", "linux.openFile": "打开文件", "linux.openFiles": "打开文件",
    "linux.openFolder": "打开文件夹",
}


def setup_logging(directory) -> None:
    (directory / "logs").mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [RotatingFileHandler(directory / "logs" / "launcher.log", maxBytes=1_000_000,
                                                           backupCount=2, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers, force=True)
    logging.getLogger("tornado.access").setLevel(logging.WARNING)   # 每个请求一行太吵，只记错误


def focus_existing(rec: dict) -> bool:
    """已有一个实例在运行：请它把窗口调到前面，然后本次启动直接退出。"""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{rec['shell_port']}/api/focus", data=b"{}", method="POST",
                                     headers={"X-SVF-Token": rec["token"], "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:  # noqa: BLE001
        return False


class Shell:
    def __init__(self, args):
        self.args = args
        self.data = paths.data_dir()
        self.window = None
        self.webview = None
        self._shutdown_done = threading.Event()
        self._shutdown_lock = threading.Lock()

    # ---------- 窗口 ----------
    def run_window(self, url: str) -> bool:
        try:
            import webview
        except Exception as exc:  # noqa: BLE001
            log.info("没有 pywebview（%s），改用浏览器", exc)
            return False
        try:
            self.webview = webview
            webview.settings["ALLOW_DOWNLOADS"] = True              # 允许下载视频、源码 zip
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            self.window = webview.create_window(
                f"{APP_NAME} {__version__}", url, width=1440, height=940, min_size=(960, 640),
                background_color="#F4F6FA", text_select=True)
            self.window.events.closed += self._on_closed
            self.window.events.loaded += lambda *_: log.info("窗口已创建并载入页面")
            self.state.on_quit = self.request_quit
            self.state.on_focus = self.focus
            self.state.window_mode = "独立窗口（pywebview）"
            kwargs = {"private_mode": False, "storage_path": str(self.data / "webview"), "localization": LOCALIZATION}
            if sys.platform == "win32":
                kwargs["gui"] = "edgechromium"
            webview.start(**kwargs)
            return True
        except Exception:  # noqa: BLE001
            log.exception("原生窗口启动失败，改用浏览器")
            self.window = None
            self.state.on_quit = self.state.quit_event.set
            return False

    def _on_closed(self, *_):
        log.info("窗口已关闭")
        self.state.quit_event.set()

    def request_quit(self):
        """顶栏「退出」：关掉窗口，webview.start() 返回后由 main 收尾。"""
        log.info("收到退出请求")
        self.state.quit_event.set()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:  # noqa: BLE001
                log.exception("关闭窗口失败")

    def focus(self):
        if self.window is not None:
            try:
                self.window.restore()
                self.window.show()
            except Exception:  # noqa: BLE001
                pass

    def run_browser(self, url: str) -> None:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
        print(f"{APP_NAME} 已在浏览器打开：{url}\n关闭页面 3 分钟后自动退出，或按 Ctrl+C。", flush=True)
        self.state.window_mode = "浏览器"
        while not self.state.quit_event.wait(5):
            if time.time() - self.state.last_seen > BROWSER_IDLE_EXIT:
                log.info("页面已关闭超过 %s 秒，退出", BROWSER_IDLE_EXIT)
                break

    # ---------- 主流程 ----------
    def main(self) -> int:
        a = self.args
        setup_logging(self.data)
        log.info("%s %s 启动（上游 MoneyPrinterTurbo %s），PID %s，数据目录 %s", APP_NAME, __version__, UPSTREAM_COMMIT,
                 os.getpid(), self.data)
        stale = supervisor.cleanup_stale(self.data)
        if stale == "running":
            rec = supervisor.read_run_record(self.data) or {}
            if focus_existing(rec):
                log.info("已有实例在运行（PID %s），已切到前台，本次退出", rec.get("launcher_pid"))
                print(f"{APP_NAME} 已经在运行（PID {rec.get('launcher_pid')}）", flush=True)
                return 0
            log.warning("运行记录里的实例没有响应，按遗留处理")
            if supervisor.same_process(rec.get("webui_pid"), rec.get("webui_started")):
                supervisor.kill_tree(rec["webui_pid"])
            supervisor.clear_run_record(self.data)
        elif stale:
            log.warning(stale)

        upstream = paths.upstream_dir()
        prep = userdata.prepare(self.data, upstream, paths.bundled_fonts_dir())
        log.info("用户数据已就绪：%s", prep)

        webui = supervisor.WebUI(paths.runtime_python(), paths.child_script(), upstream, self.data, DEFAULT_FONT,
                                 port=a.webui_port or None)
        self.state = AppState(self.data, webui, userdata.ShellSettings(self.data), secrets.token_urlsafe(18))
        self.server = ServerThread(self.state, a.port)
        port = self.server.start()
        url = f"http://127.0.0.1:{port}/"
        log.info("本机服务：%s", url)
        atexit.register(self.shutdown)
        if not a.no_webui:
            webui.start()
        self.write_record()
        threading.Thread(target=self._record_loop, name="run-record", daemon=True).start()
        if hasattr(signal, "SIGTERM"):
            try:
                signal.signal(signal.SIGTERM, lambda *_: self.state.quit_event.set())
            except ValueError:
                pass
        try:
            if a.serve_only:
                print(json.dumps({"url": url, "port": port, "token": self.state.token, "pid": os.getpid(),
                                  "data": str(self.data)}, ensure_ascii=False), flush=True)
                while not self.state.quit_event.wait(1):
                    pass
            elif a.browser or not self.run_window(url):
                if not self.state.quit_event.is_set():
                    self.run_browser(url)
        except KeyboardInterrupt:
            pass
        finally:
            self.shutdown()
        return 0

    def write_record(self) -> None:
        w = self.state.webui
        rec = {"app": APP_ID, "version": __version__, "launcher_pid": os.getpid(),
               "launcher_started": supervisor._create_time(os.getpid()), "shell_port": self.state.port,
               "token": self.state.token, "webui_pid": w.pid(), "webui_port": w.port,
               "webui_started": supervisor._create_time(w.pid()) if w.pid() else None,
               "written_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        try:
            supervisor.write_run_record(self.data, rec)
        except OSError:
            log.exception("写运行记录失败")

    def _record_loop(self):
        last = None
        while not self.state.quit_event.wait(2):
            pid = self.state.webui.pid()
            if pid != last:
                last = pid
                self.write_record()

    def shutdown(self) -> None:
        """正常退出的收尾：停后台（确认进程已退出）→ 停本机服务（释放端口）→ 删运行记录。不删任何用户数据。"""
        with self._shutdown_lock:
            if self._shutdown_done.is_set():
                return
            self.state.quit_event.set()
            log.info("开始退出收尾")
            result = self.state.webui.stop()
            if result.get("alive"):
                log.error("仍有进程未退出：%s", result["alive"])
            self.server.stop()
            port = self.state.port
            released = not supervisor.port_open(port) if port else True
            supervisor.clear_run_record(self.data)
            log.info("已退出：后台进程 %s 已结束，生成界面端口 %s %s，本机服务端口 %s %s",
                     result.get("stopped"), self.state.webui.port, "已释放" if result.get("port_released") else "仍被占用",
                     port, "已释放" if released else "仍被占用")
            self._shutdown_done.set()


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=f"{APP_NAME} {__version__}")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不用独立窗口")
    ap.add_argument("--serve-only", action="store_true", help="只启动服务，不开窗口（测试/CI 用）；会打印地址和令牌")
    ap.add_argument("--port", type=int, default=0, help="本机服务端口（默认随机）")
    ap.add_argument("--webui-port", type=int, default=0, help="生成界面后台端口（默认随机）")
    ap.add_argument("--no-webui", action="store_true", help="不启动生成界面后台（只测外壳）")
    ap.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    return ap.parse_args(argv)


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    sys.exit(Shell(parse_args(argv)).main())
