#!/usr/bin/env python3
"""启动舆情监控系统：启动后台服务并打开页面；关闭页面后后台自动退出。

    python run.py                 # 启动并打开应用窗口
    python run.py --port 8848     # 指定端口（默认 8848，被占用时自动换）
    python run.py --no-browser    # 只启动后台，不打开页面
    python run.py --demo          # 启动后写入一批演示数据
"""

import argparse
import json
import logging
import logging.handlers
import os
import signal
import sys
import threading
import time
import urllib.request

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def existing_instance(port):
    """端口上已经有一个本程序在运行时返回 True。"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read()).get("app") == "sentiment-monitor"
    except Exception:  # noqa: BLE001
        return False


def setup_logging():
    from monitor import paths

    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = logging.handlers.RotatingFileHandler(paths.log_path(), maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    fh.setFormatter(fmt)
    root.addHandler(fh)
    if sys.stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        root.addHandler(sh)


def main():
    from monitor.lifecycle import hide_console_if_frozen

    hide_console_if_frozen()
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass

    ap = argparse.ArgumentParser(description="舆情监控系统")
    ap.add_argument("--port", type=int, default=8848)
    ap.add_argument("--no-browser", action="store_true", help="不自动打开页面")
    ap.add_argument("--demo", action="store_true", help="写入演示数据")
    ap.add_argument("--data-dir", help="数据目录（默认在用户目录下）")
    args = ap.parse_args()
    if args.data_dir:
        os.environ["MONITOR_DATA_DIR"] = args.data_dir

    from monitor import APP_NAME, __version__, paths
    from monitor.lifecycle import BrowserWindow, Watchdog, on_console_close
    from monitor.server import App

    setup_logging()
    log = logging.getLogger("monitor")

    # 单实例：已经在运行就只打开页面
    if existing_instance(args.port):
        url = f"http://127.0.0.1:{args.port}/"
        log.info("程序已在运行，直接打开页面：%s", url)
        if not args.no_browser:
            BrowserWindow().open(url)
        return

    app = App()
    try:
        httpd = app.make_server(args.port)
    except OSError:
        httpd = app.make_server(0)  # 端口被别的程序占用，换随机端口
    url = f"http://127.0.0.1:{app.port}/"

    print(f"{APP_NAME} v{__version__}")
    print(f"数据目录：{paths.data_dir()}")
    print(f"页面地址：{url}")
    print("关闭页面后后台会自动退出；也可以在页面右上角点“退出”或在此按 Ctrl+C。", flush=True)

    if args.demo:
        from monitor.demo import seed_demo

        log.info("写入演示数据 %d 条", seed_demo(app))

    app.collector.start()

    browser = BrowserWindow()
    app.on_shutdown.append(browser.close)
    watchdog = Watchdog(app.bus, browser, app.store.settings, app.shutdown)
    watchdog.start()

    if not args.no_browser:
        cfg = app.store.settings()
        threading.Timer(0.3, lambda: browser.open(url, cfg["browser"], cfg["app_window"])).start()

    def on_signal(*_):
        app.shutdown("收到退出信号")

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)  # Windows 控制台 Ctrl+Break
    on_console_close(lambda: (browser.close(), app.shutdown("控制台窗口被关闭")))

    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        watchdog.stop_event.set()
        browser.close()
        httpd.server_close()
        app.store.close()
        log.info("已退出")
        time.sleep(0.2)


if __name__ == "__main__":
    main()
