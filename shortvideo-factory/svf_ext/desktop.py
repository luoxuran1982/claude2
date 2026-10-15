"""「打开目录」：用系统的文件管理器打开文件夹。

1.0.2 在生成界面后台（Streamlit 子进程）里直接调用 os.startfile，用户在 Windows 上点了仍然没有反应。
这个子进程是后台进程：启动时带 CREATE_NO_WINDOW（没有控制台和窗口）、放在作业对象里，按钮回调跑在 Streamlit 的
工作线程上（没有初始化 COM），也不是前台进程。打开「文件」（播放视频）时系统直接启动播放器，所以正常；打开「文件夹」要
经资源管理器（COM 委托 + 新窗口要抢前台），在这种进程里会静默失败或窗口被压在后面。

1.0.3 起改为：子进程通过本机 HTTP 请外壳主进程打开（POST /api/open-path，见 svf/server.py 的 OpenPathHandler）。
外壳主进程拥有应用窗口、运行在用户桌面的前台，外壳只允许打开数据目录之内的路径。
外壳不可用（单独运行上游、没有 SVF_SHELL_URL）时，退回本进程按平台打开：Windows os.startfile，macOS open，Linux xdg-open。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.error
import urllib.request


def open_command(path: str, platform: str | None = None) -> list[str] | None:
    """返回打开 path 用的命令；Windows 用 os.startfile，不走命令行，返回 None。"""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return None
    if platform == "darwin":
        return ["open", path]
    return ["xdg-open", path]


def _log(msg: str) -> None:
    try:
        from loguru import logger
        logger.warning(msg)
    except ImportError:
        pass


def open_local(path: str, platform: str | None = None, startfile=None, popen=None) -> bool:
    """在本进程里按平台打开。成功发出打开请求返回 True；失败时记日志并返回 False，不抛异常。"""
    platform = sys.platform if platform is None else platform
    path = os.path.abspath(str(path))
    try:
        cmd = open_command(path, platform)
        if cmd is None:
            (startfile or getattr(os, "startfile"))(path)
        else:
            (popen or subprocess.Popen)(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception as exc:  # noqa: BLE001
        _log(f"打开目录失败：{path}，{type(exc).__name__}: {exc}")
        return False


def shell_endpoint() -> tuple[str, str] | None:
    url, token = os.environ.get("SVF_SHELL_URL", ""), os.environ.get("SVF_SHELL_TOKEN", "")
    return (url.rstrip("/"), token) if url and token else None


def ask_shell(path: str, endpoint: tuple[str, str], check_only: bool = False, timeout: float = 20) -> dict:
    """请外壳打开文件夹。返回外壳的应答 {"ok", "path", "error"}；连不上外壳时 {"ok": False, "unreachable": True}。"""
    url, token = endpoint
    body = json.dumps({"path": path, "check_only": check_only}).encode("utf-8")
    req = urllib.request.Request(url + "/api/open-path", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "X-SVF-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            data = json.loads(exc.read() or b"{}")
        except ValueError:
            data = {}
        return {"ok": False, "path": data.get("path", path), "error": data.get("error") or f"外壳拒绝了请求（{exc.code}）",
                "status": exc.code}
    except (OSError, ValueError) as exc:
        return {"ok": False, "path": path, "error": f"连不上外壳本机服务：{exc}", "unreachable": True}


def open_folder(path: str, platform: str | None = None, startfile=None, popen=None, endpoint=None) -> dict:
    """打开文件夹，返回 {"ok": bool, "path": 完整路径, "error": 原因, "via": "shell" / "local"}，不抛异常。"""
    path = os.path.abspath(str(path))
    endpoint = shell_endpoint() if endpoint is None else endpoint
    if endpoint:
        res = ask_shell(path, endpoint)
        if not res.get("unreachable"):
            res.setdefault("path", path)
            res.setdefault("error", "")
            res["via"] = "shell"
            if not res.get("ok"):
                _log(f"外壳没能打开目录：{res.get('path')}，{res.get('error')}")
            return res
        _log(f"{res['error']}，改在生成界面后台里直接打开")
    if not os.path.exists(path):
        return {"ok": False, "path": path, "error": "文件夹不存在（可能已被删除）", "via": "local"}
    ok = open_local(path, platform, startfile, popen)
    return {"ok": ok, "path": path, "error": "" if ok else "系统没能打开文件夹（详见日志）", "via": "local"}


def open_path(path: str, platform: str | None = None, startfile=None, popen=None) -> bool:
    """兼容 1.0.2 的接口：只返回成败。"""
    return bool(open_folder(path, platform, startfile, popen).get("ok"))
