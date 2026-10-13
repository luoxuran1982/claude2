"""启动入口：本机服务 + 原生窗口（pywebview：Windows 用 WebView2，macOS 用 WKWebView）。

没有 pywebview 或窗口组件不可用时，自动改用默认浏览器打开，功能相同（浏览器模式下页面关闭 3 分钟后退出）。

启停约定（见 docs/01 使用指南 第 1 节）：
- 关闭最后一个窗口、⌘Q / Alt+F4、界面上的「退出」→ 退出应用：停定时抓取 → 结束正在运行的抓取工作进程
  → 停 MCP 服务进程 → 停本机服务并释放端口。不做任何「关窗后继续在后台跑」的常驻。
- 最小化、切到别的应用：应用和定时抓取继续运行。
- 崩溃或被强制结束：工作进程每 2 秒检查主进程，主进程不在了就自行退出（worker.start_watchdog）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

from . import APP_NAME, __version__, configio, paths
from .server import serve

log = logging.getLogger("hotradar")
BROWSER_IDLE_EXIT = 180  # 浏览器模式：页面关闭 3 分钟后退出


def setup_logging(directory) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(directory / "hotradar.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


class Desktop:
    """给服务端用的原生能力：保存对话框。"""

    def __init__(self, webview, window):
        self.webview, self.window = webview, window

    def save_dialog(self, filename: str, file_types: tuple[str, ...]):
        kind = getattr(getattr(self.webview, "FileDialog", None), "SAVE", None)
        if kind is None:
            kind = self.webview.SAVE_DIALOG
        result = self.window.create_file_dialog(kind, save_filename=filename, file_types=file_types)
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        return result or None


def run_window(app, url: str, storage: str) -> bool:
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        log.info("没有 pywebview（%s），改用浏览器", exc)
        return False
    try:
        window = webview.create_window(f"{APP_NAME} {__version__}", url, width=1440, height=920, min_size=(900, 620),
                                       background_color="#F4F6FA", text_select=True)
        app.desktop = Desktop(webview, window)
        # 关闭最后一个窗口（macOS 红色按钮 / ⌘Q，Windows × / Alt+F4）→ webview.start() 返回 → main() 的 finally 收尾
        window.events.closed += lambda *_: (log.info("窗口已关闭"), app.quit_event.set())

        def watch_quit():  # 界面上点「退出」：关掉窗口，webview.start() 随之返回
            app.quit_event.wait()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass
        threading.Thread(target=watch_quit, name="quit-watch", daemon=True).start()
        kwargs = {"private_mode": False, "storage_path": storage}
        if sys.platform == "win32":
            kwargs["gui"] = "edgechromium"
        webview.start(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        log.exception("原生窗口启动失败，改用浏览器")
        app.desktop = None
        return False


def run_browser(app, url: str) -> None:
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"{APP_NAME} 已在浏览器打开：{url}\n关闭页面 3 分钟后自动退出，或按 Ctrl+C。", flush=True)
    app.last_seen = time.time()
    while not app.quit_event.wait(5):
        if time.time() - app.last_seen > BROWSER_IDLE_EXIT:
            log.info("页面已关闭，退出")
            break


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不用独立窗口")
    ap.add_argument("--port", type=int, default=0, help="端口（默认随机）")
    ap.add_argument("--serve-only", action="store_true", help="只启动服务（开发/测试用）")
    args = ap.parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass

    root = paths.data_dir()
    setup_logging(root)
    copied = configio.ensure_workspace(paths.workspace_dir("main", root))
    if copied:
        log.info("首次使用，已复制上游默认配置：%s", "、".join(copied))
    app, httpd = serve(root, args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    app.runner.start_scheduler()
    log.info("%s %s 启动，端口 %s，数据目录 %s", APP_NAME, __version__, port, root)

    try:
        if args.serve_only:
            print(url, flush=True)
            # 只在开发/测试用的 --serve-only 模式下把带令牌的地址写进数据目录，供自动化测试读取（无控制台的安装版拿不到 stdout）
            configio.atomic_write(root / ".serve-only-url", url.encode("utf-8"))
            app.quit_event.wait()
        elif args.browser or not run_window(app, url, str(root / "webview")):
            run_browser(app, url)
    except KeyboardInterrupt:
        pass
    finally:
        shutdown(app, httpd)


def shutdown(app, httpd) -> None:
    """正常退出的收尾：停定时线程和工作进程（确认已退出）→ 停 HTTP 服务并释放端口。
    设置和数据在改动时已保存，这里不删任何文件。"""
    app.quit_event.set()
    try:
        app.runner.shutdown()
    finally:
        try:
            httpd.shutdown()
        finally:
            httpd.server_close()
    log.info("已退出（工作进程已结束，服务已停止，端口 %s 已释放）", httpd.server_address[1])
