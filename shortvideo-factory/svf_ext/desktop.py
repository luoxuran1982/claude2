"""「打开目录」：用系统的文件管理器打开文件夹。

上游用 webbrowser.open("file://…") 打开任务目录和缓存目录，在 Windows 上会静默失败（没有任何反应）。
这里按平台改用系统自己的打开方式：Windows os.startfile，macOS open，Linux xdg-open。
"""
from __future__ import annotations

import os
import subprocess
import sys


def open_command(path: str, platform: str | None = None) -> list[str] | None:
    """返回打开 path 用的命令；Windows 用 os.startfile，不走命令行，返回 None。"""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return None
    if platform == "darwin":
        return ["open", path]
    return ["xdg-open", path]


def open_path(path: str, platform: str | None = None, startfile=None, popen=None) -> bool:
    """打开文件夹（或文件）。成功发出打开请求返回 True；失败时记日志并返回 False，不抛异常。"""
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
        try:
            from loguru import logger
            logger.warning(f"打开目录失败：{path}，{type(exc).__name__}: {exc}")
        except ImportError:
            pass
        return False
