"""启动入口：本机服务 + 原生窗口（pywebview；Windows 用 WebView2）。窗口不可用时自动改用浏览器。"""
from __future__ import annotations

import argparse
import atexit
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import APP_NAME, __version__
from .paths import data_dir
from .server import serve
from .sysinfo import stop_children

log = logging.getLogger("quantlab")
BROWSER_IDLE_EXIT = 600  # 浏览器模式：页面关闭 10 分钟且没有任务时退出后台


def setup_logging(directory) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(directory / "quantlab.log", maxBytes=2_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


class Desktop:
    def __init__(self, webview, window):
        self.webview, self.window = webview, window

    def _kind(self, name: str, legacy: str):
        kind = getattr(getattr(self.webview, "FileDialog", None), name, None)
        return kind if kind is not None else getattr(self.webview, legacy)

    def folder_dialog(self):
        result = self.window.create_file_dialog(self._kind("FOLDER", "FOLDER_DIALOG"))
        if isinstance(result, (list, tuple)):
            result = result[0] if result else None
        return result or None

    def save_dialog(self, filename: str):
        types = {".csv": ("CSV 表格 (*.csv)",), ".zip": ("ZIP 压缩包 (*.zip)",)}.get(filename[-4:], ("JSON (*.json)",))
        result = self.window.create_file_dialog(self._kind("SAVE", "SAVE_DIALOG"), save_filename=filename, file_types=types)
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
        window = webview.create_window(APP_NAME, url, width=1480, height=940, min_size=(1000, 680),
                                       background_color="#F4F6FA", text_select=True)
        app.desktop = Desktop(webview, window)
        app.mode = "窗口模式"
        try:  # 关闭最后一个（也是唯一的）窗口：立即开始收尾，不等 webview.start() 返回
            window.events.closed += lambda: app.quit_event.set()
        except Exception:  # noqa: BLE001
            pass
        kwargs = {"private_mode": False, "storage_path": storage}
        if sys.platform == "win32":
            kwargs["gui"] = "edgechromium"
        webview.start(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        log.exception("原生窗口启动失败，改用浏览器")
        app.desktop = None
        app.mode = "服务模式"
        return False


def run_browser(app, url: str) -> None:
    app.mode = "浏览器模式"
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"{APP_NAME} 已在浏览器打开：{url}\n关闭页面 10 分钟后自动退出，或按 Ctrl+C。", flush=True)
    app.last_seen = time.time()
    while not app.quit_event.wait(5):
        if time.time() - app.last_seen > BROWSER_IDLE_EXIT and not app.jobs.busy():
            log.info("页面已关闭，退出")
            break


def shutdown(app, httpd, wait: float = 5.0) -> None:
    """退出收尾（可重复调用）：取消计算任务 → 停止子进程 → 关闭本机服务释放端口。不删除任何数据或设置。"""
    if getattr(app, "_shut", False):
        return
    app._shut = True
    app.quit_event.set()
    if app.jobs.cancel():
        log.info("退出：取消正在运行的任务")
        deadline = time.time() + wait
        while app.jobs.busy() and time.time() < deadline:
            time.sleep(0.1)
    n = stop_children()
    if n:
        log.info("退出：结束了 %d 个子进程", n)
    try:
        httpd.shutdown()
    except Exception:  # noqa: BLE001
        pass
    try:
        httpd.server_close()
    except Exception:  # noqa: BLE001
        pass
    log.info("已退出（端口已释放）%s", "；仍有任务线程未结束，强制结束进程" if app.jobs.busy() else "")


def selftest(out: str) -> None:
    """打包后验证 LightGBM / sklearn / pyarrow 等依赖都能用：演示数据 → 三模型集成实验。"""
    import json
    import tempfile
    import traceback

    from . import experiment
    from .data import demo
    from .data.store import DataStore
    result = {"ok": False, "version": __version__}
    try:
        root = Path(tempfile.mkdtemp(prefix="quantlab-selftest-"))
        store, runs = DataStore(root), experiment.Runs(root)
        bars, names = demo.generate(80, 4, seed=1)
        store.save_frame(bars, "selftest", names, "selftest", demo=True)
        rid = experiment.run(store, runs, {"dataset": "selftest", "model": {"types": ["lgbm", "ridge", "mlp"]},
                                           "walk": {"retrain_days": 126}})
        s = runs.summary(rid)
        result.update(ok=True, run=rid, cagr=s["perf"].get("cagr"), rank_ic=s["ic"]["rank_ic"].get("mean"),
                      trades=s["trade_stats"].get("n_trades"), picks=len(runs.picks(rid)["picks"]))
    except Exception as exc:  # noqa: BLE001
        result["error"] = f"{exc}\n{traceback.format_exc()}"
    Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=1), encoding="utf-8")
    sys.exit(0 if result["ok"] else 1)


def main(argv=None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ("cli", "demo", "import", "run", "factors", "list"):
        from .cli import main as cli_main
        return cli_main(argv[1:] if argv[0] == "cli" else argv)
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不用独立窗口")
    ap.add_argument("--port", type=int, default=0, help="端口（默认随机）")
    ap.add_argument("--serve-only", action="store_true", help="只启动服务（开发/测试用）")
    ap.add_argument("--url-file", metavar="PATH", help="把带令牌的访问地址写到文件（测试用）")
    ap.add_argument("--selftest", metavar="OUT.json", help="自检：生成小份演示数据跑一遍完整实验，结果写到文件后退出")
    args = ap.parse_args(argv)
    if args.selftest:
        return selftest(args.selftest)
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    directory = data_dir()
    setup_logging(directory)
    app, httpd = serve(directory, args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    atexit.register(shutdown, app, httpd, 1.0)
    log.info("%s %s 启动，数据目录 %s，PID %d", APP_NAME, __version__, directory, os.getpid())
    if args.url_file:
        Path(args.url_file).write_text(url, encoding="utf-8")
    try:
        if args.serve_only:
            print(url, flush=True)
            app.quit_event.wait()
        elif args.browser or not run_window(app, url, str(directory / "webview")):
            run_browser(app, url)
    except KeyboardInterrupt:
        pass
    finally:
        busy = app.jobs.busy()
        shutdown(app, httpd)
        if busy or app.jobs.busy():
            # 计算线程可能卡在 LightGBM 等原生代码里，不能让它拖住退出：确认收尾后直接结束进程
            logging.shutdown()
            os._exit(0)
