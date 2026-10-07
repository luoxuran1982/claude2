"""前后台一起开、一起关。

打开：启动本机服务后，优先用 Edge / Chrome 的“应用窗口”模式（--app）打开页面，
      使用独立的浏览器配置目录，这样浏览器进程就是我们启动的子进程；找不到时用系统默认浏览器。
关闭：页面通过 SSE 长连接 /api/events 与后台保持连接。
      ① 应用窗口进程退出，或 ② 所有页面断开超过宽限时间（默认 10 秒，留给刷新页面），后台自动退出。
      反过来，在页面点“退出”或控制台按 Ctrl+C，后台退出时也会关掉它启动的应用窗口。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser

from . import paths

log = logging.getLogger("monitor.lifecycle")


def _win_app_path(exe: str) -> str:
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(root, rf"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\{exe}") as k:
                    v = winreg.QueryValue(k, None)
                    if v and os.path.isfile(v):
                        return v
            except OSError:
                continue
    except ImportError:
        pass
    return ""


def find_browser(pref: str = "auto"):
    """返回 (名称, 可执行文件路径)；找不到返回 (None, None)。"""
    order = {"edge": ["edge"], "chrome": ["chrome"]}.get(pref, ["edge", "chrome"])
    cands = {"edge": [], "chrome": []}
    if paths.IS_WIN:
        pf = [os.environ.get(k) for k in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA")]
        for base in filter(None, pf):
            cands["edge"].append(os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"))
            cands["chrome"].append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
        cands["edge"].insert(0, _win_app_path("msedge.exe"))
        cands["chrome"].insert(0, _win_app_path("chrome.exe"))
    elif paths.IS_MAC:
        cands["edge"].append("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")
        cands["chrome"].append("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    else:
        cands["edge"] += [shutil.which(x) or "" for x in ("microsoft-edge", "microsoft-edge-stable")]
        cands["chrome"] += [shutil.which(x) or "" for x in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")]
    for name in order:
        for p in cands[name]:
            if p and os.path.isfile(p):
                return name, p
    return None, None


class BrowserWindow:
    def __init__(self):
        self.proc = None
        self.started = 0.0

    def open(self, url: str, pref="auto", app_window=True) -> str:
        if pref == "none":
            return "none"
        if pref != "default" and app_window:
            name, exe = find_browser(pref)
            if exe:
                args = [
                    exe, f"--app={url}", f"--user-data-dir={paths.browser_profile_dir()}",
                    "--no-first-run", "--no-default-browser-check", "--disable-background-mode",
                    "--disable-features=msEdgeStartupBoost,Translate", "--window-size=1440,900",
                ]
                kw = {}
                if paths.IS_WIN:
                    kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
                try:
                    self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
                    self.started = time.time()
                    log.info("已用 %s 应用窗口打开：%s", name, url)
                    return name
                except OSError as e:
                    log.warning("启动 %s 失败：%s，改用默认浏览器", name, e)
        webbrowser.open(url)
        log.info("已用默认浏览器打开：%s", url)
        return "default"

    def exited(self) -> bool:
        """应用窗口是否已被用户关闭。启动 5 秒内就退出的视为把窗口交给了已在运行的浏览器，不算关闭。"""
        if not self.proc or self.proc.poll() is None:
            return False
        return self.started and (time.time() - self.started) > 5

    def close(self):
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
            except OSError:
                pass


class Watchdog(threading.Thread):
    """页面全部关闭后让后台退出。"""

    def __init__(self, bus, browser: BrowserWindow, get_settings, on_exit):
        super().__init__(name="watchdog", daemon=True)
        self.bus = bus
        self.browser = browser
        self.get_settings = get_settings
        self.on_exit = on_exit
        self.stop_event = threading.Event()

    def check(self, now=None) -> str:
        now = now or time.time()
        cfg = self.get_settings()
        if not cfg.get("auto_exit", True):
            return ""
        if self.bus.clients() == 0:
            if self.browser.exited():
                return "应用窗口已关闭"
            grace = max(3, int(cfg.get("exit_grace_sec", 10)))
            if self.bus.ever_connected and now - self.bus.last_disconnect >= grace:
                return f"页面已全部关闭 {grace} 秒"
        return ""

    def run(self):
        while not self.stop_event.wait(1):
            try:
                reason = self.check()
            except Exception:  # noqa: BLE001
                log.exception("watchdog")
                continue
            if reason:
                log.info("自动退出：%s", reason)
                self.on_exit(reason)
                return


def hide_console_if_frozen():
    """打包成无控制台程序时，把 stdout/stderr 指向空，避免 print 报错。"""
    if getattr(sys, "frozen", False) and sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


_console_handler = None  # 保持引用，防止被回收


def on_console_close(callback):
    """Windows：用户直接点控制台窗口的 ×、注销或关机时，也执行退出流程（关掉应用窗口）。"""
    global _console_handler
    if not paths.IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

        def handler(event):
            if event in (2, 5, 6):  # CTRL_CLOSE_EVENT / LOGOFF / SHUTDOWN
                try:
                    callback()
                finally:
                    time.sleep(1.5)
                return True
            return False

        _console_handler = HANDLER(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_handler, True)
    except Exception:  # noqa: BLE001
        log.debug("SetConsoleCtrlHandler 不可用", exc_info=True)
