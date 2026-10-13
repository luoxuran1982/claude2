#!/usr/bin/env python3
"""启动 AI 知识视频工坊。

    python run.py              # 默认：独立窗口（pywebview：Windows 用 WebView2，macOS 用 WKWebView）
                               # 窗口组件不可用时自动改用默认浏览器，原因写进数据目录的 studio.log
    python run.py --browser    # 直接用默认浏览器打开
    python run.py --port 8765 --no-browser   # 只启动服务（开发、测试用）

退出：关闭最后一个窗口、⌘Q / Alt+F4、页面右上角「退出」、Ctrl+C，都会取消任务、
结束本应用启动的子进程、停止后台服务，然后进程退出。
"""

import argparse
import logging
import os
import sys
import threading
import time
import webbrowser
from logging.handlers import RotatingFileHandler

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本")

# 打包或从其他目录启动时也能找到 studio 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

APP_NAME = "AI 知识视频工坊"
BROWSER_IDLE_EXIT = 180  # 浏览器模式：页面关闭（3 分钟没有任何请求）后自动退出后台
log = logging.getLogger("studio")


def setup_logging(directory):
    handlers = [RotatingFileHandler(os.path.join(str(directory), "studio.log"), maxBytes=1_000_000, backupCount=2, encoding="utf-8")]
    if sys.stderr is not None:  # 打包成窗口程序时没有控制台
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", handlers=handlers)


def run_window(app, url, quit_event, storage, version):
    """在独立窗口里打开。窗口关闭后返回 True；窗口组件不可用时返回 False（调用方改用浏览器）。"""
    from studio import procinfo

    try:
        import webview  # pywebview
    except Exception as exc:  # noqa: BLE001
        log.info("没有 pywebview（%s），改用浏览器", exc)
        return False
    try:
        window = webview.create_window(f"{APP_NAME} {version}", url, width=1440, height=900, min_size=(900, 600))
        # 关闭最后一个窗口（macOS 红色按钮 / ⌘Q，Windows × / Alt+F4）→ webview.start() 返回 → main() 收尾
        window.events.closed += lambda *_: (log.info("窗口已关闭"), quit_event.set())

        def close_from_page():
            # 页面上点「退出」：先关窗口，webview.start() 随即返回
            quit_event.set()
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                log.exception("关闭窗口失败")

        app.shutdown_cb = close_from_page
        procinfo.set_mode("window")
        kwargs = {"private_mode": False, "storage_path": storage}
        if sys.platform == "win32":
            kwargs["gui"] = "edgechromium"
        log.info("打开窗口")
        webview.start(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        log.exception("窗口启动失败，改用浏览器")
        procinfo.set_mode("browser")
        app.shutdown_cb = quit_event.set
        return False


def run_browser(app, url, quit_event):
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    print(f"已在浏览器打开：{url}\n点页面右上角「退出」、按 Ctrl+C，或关闭页面 3 分钟后自动退出。", flush=True)
    app.last_seen = time.time()
    while not quit_event.wait(2):
        if time.time() - app.last_seen > BROWSER_IDLE_EXIT and not app.jobs.busy():
            log.info("页面已关闭 %s 秒，自动退出", BROWSER_IDLE_EXIT)
            break


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=0, help="端口，默认随机")
    ap.add_argument("--browser", action="store_true", help="用默认浏览器打开，不用独立窗口")
    ap.add_argument("--no-browser", action="store_true", help="只启动服务，不打开窗口或浏览器")
    ap.add_argument("--window", action="store_true", help="（旧参数，现在默认就是独立窗口）")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("缺少 Pillow：请先运行 pip install -r requirements.txt")

    from studio import __version__, paths, procinfo
    from studio.media import find_ffmpeg
    from studio.server import serve

    setup_logging(paths.data_dir())
    app, httpd = serve(args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    quit_event = threading.Event()
    app.shutdown_cb = quit_event.set  # 页面「退出」→ /api/shutdown → 这里
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()
    log.info("%s %s 启动，端口 %s，数据目录 %s，FFmpeg %s", APP_NAME, __version__, port, paths.data_dir(), find_ffmpeg() or "未找到")
    print(f"{APP_NAME} {__version__}")
    print(f"数据目录：{paths.data_dir()}")
    print(f"打开：{url}", flush=True)
    url_file = os.environ.get("STUDIO_URL_FILE")  # 冒烟测试用：窗口程序没有控制台输出
    if url_file:
        with open(url_file, "w", encoding="utf-8") as fh:
            fh.write(url)

    # 终端关闭（SIGHUP）或被结束（SIGTERM）时也走正常退出流程
    import signal

    for name in ("SIGTERM", "SIGHUP", "SIGINT"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), lambda *_: quit_event.set())
            except (ValueError, OSError):
                pass

    try:
        if args.no_browser:
            while not quit_event.wait(1):
                pass
        elif args.browser or not run_window(app, url, quit_event, str(paths.sub("webview")), __version__):
            procinfo.set_mode("browser")
            run_browser(app, url, quit_event)
    except KeyboardInterrupt:
        pass
    finally:
        # 收尾：取消任务 → 结束本应用启动的子进程（FFmpeg、配音）和守护进程 → 停止 HTTP 服务、释放端口
        try:
            app.jobs.cancel()
        except Exception:  # noqa: BLE001
            pass
        procinfo.shutdown()
        httpd.shutdown()
        httpd.server_close()
        log.info("已退出（子进程已结束，端口 %s 已释放）", port)
        print("已退出。", flush=True)


if __name__ == "__main__":
    main()
