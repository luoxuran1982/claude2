"""v0.4：原生应用窗口（pywebview：macOS 用系统自带的 WKWebView，Windows 用 WebView2），和其他应用一样。

为什么从「借 Edge/Chrome 的 --app 窗口」改成原生窗口：窗口归本应用自己，程序坞里是应用自己的图标，
⌘Q / 关闭窗口由应用自己处理；不再要求电脑上装 Chrome，也不用另起一整个浏览器。
没有 pywebview 或窗口组件不可用（例如 Windows 缺 WebView2）时，run.py 退回到原来的 Edge/Chrome 窗口。

嵌进应用的网页引擎不具备浏览器的几样能力，这里补上：
- 下载（导出 CSV、下载源码 zip）：打开 pywebview 的 ALLOW_DOWNLOADS，下载时弹出系统的「存储为」对话框；
- 新窗口链接（资讯原文、官方文档，target=_blank）：交给系统默认浏览器打开（OPEN_EXTERNAL_LINKS_IN_BROWSER）；
- 桌面通知：网页的 Notification 接口在嵌入式引擎里不可用，改由后台调用系统通知（notify）。

启停：关闭窗口（红色按钮）、⌘Q、程序坞「退出」→ 窗口的 closing 事件里同步收尾（App.stop_now：停采集、停服务、
关数据库）→ 进程结束。不能等 webview.start() 返回再收尾：macOS 处理「退出」时可能直接结束进程。
页面右上角「退出」→ App.shutdown() → 这里的 watch_quit 关掉窗口 → 同样收尾。
"""

from __future__ import annotations

import logging
import subprocess
import sys
import threading

from . import APP_NAME, __version__, paths

log = logging.getLogger("monitor.desktop")


def available() -> bool:
    try:
        import webview  # noqa: F401
    except Exception:  # noqa: BLE001 - 没装、或装了但缺系统组件
        return False
    return True


def run_window(app, url: str) -> bool:
    """在主线程打开原生窗口并阻塞到窗口关闭。成功打开过返回 True；打不开返回 False（调用方改用浏览器）。"""
    try:
        import webview
    except Exception as exc:  # noqa: BLE001
        log.info("没有 pywebview（%s），改用 Edge/Chrome 窗口", exc)
        return False
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
        webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
        window = webview.create_window(
            f"{APP_NAME} v{__version__}", url, width=1440, height=900, min_size=(900, 600), text_select=True,
        )
        app.native_window = True
        app.collector.notifier = notify

        closing = threading.Event()

        def on_closing():
            closing.set()  # 告诉 watch_quit：窗口已经在关了，不要再去关它
            # closing 事件在主线程同步执行：红色关闭按钮、⌘Q、程序坞「退出」都会先经过这里，
            # 返回后 macOS 可能直接结束进程，所以收尾要在这里做完（v0.4 实测：只挂 closed 时日志缺「已退出」）
            log.info("应用窗口正在关闭")
            app.stop_now("应用窗口已关闭")
            return True

        window.events.closing += on_closing
        window.events.closed += lambda *_: app.shutdown("应用窗口已关闭")

        def watch_quit():  # 页面上点「退出」或收到退出信号：关掉窗口，webview.start() 随之返回
            app._stopping.wait()
            if closing.is_set():
                return
            try:
                window.destroy()
            except Exception:  # noqa: BLE001
                pass

        threading.Thread(target=watch_quit, name="quit-watch", daemon=True).start()
        kwargs = {"private_mode": False, "storage_path": str(paths.data_dir() / "webview")}
        if sys.platform == "win32":
            kwargs["gui"] = "edgechromium"
        log.info("已用原生窗口打开：%s", url)
        webview.start(**kwargs)
        return True
    except Exception:  # noqa: BLE001
        log.exception("原生窗口启动失败，改用 Edge/Chrome 窗口")
        app.native_window = False
        app.collector.notifier = None
        return False


def notify(title: str, message: str) -> None:
    """系统通知。macOS 用自带的 osascript（文字作为参数传入，不拼进脚本，避免引号出错）；其他系统暂不支持。"""
    if sys.platform != "darwin":
        return
    script = "on run argv\ndisplay notification (item 2 of argv) with title (item 1 of argv)\nend run"
    try:
        subprocess.Popen(["osascript", "-e", script, title, message[:200]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        log.debug("系统通知不可用", exc_info=True)
