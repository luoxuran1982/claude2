"""路径：安装包/源码里的只读资源，和系统用户数据目录里的可写数据。

用户数据目录（替换新版本不会丢）：
    macOS   ~/Library/Application Support/HotRadar/
    Windows %LOCALAPPDATA%\\HotRadar\\
    Linux   ~/.local/share/HotRadar/（开发和 CI 用）
可以用环境变量 HOTRADAR_DATA 指定别的目录（测试、CI 冒烟用）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    """只读资源的根目录：源码运行时是 hot-radar/，安装包里是 PyInstaller 解包目录。"""
    if frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def web_dir() -> Path:
    return bundle_root() / "hotradar" / "web"


def upstream_dir() -> Path:
    """上游 TrendRadar 的目录（含 config/ 默认配置、version 文件；源码运行时还有 trendradar/ 包）。"""
    return bundle_root() / "upstream"


def samples_dir() -> Path:
    return bundle_root() / "samples"


def ensure_upstream_importable() -> None:
    """源码运行时把 upstream/ 加进 sys.path，才能 import trendradar / mcp_server；安装包里它们已编进程序。"""
    if not frozen():
        p = str(upstream_dir())
        if p not in sys.path:
            sys.path.insert(0, p)


def data_dir(platform: str | None = None, environ=None, home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    override = environ.get("HOTRADAR_DATA")
    if override:
        return Path(override)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_ID
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_ID
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID


# 工作区：上游程序按「当前目录下的 config/ 和 output/」工作，所以每个工作区就是一个这样的目录。
#   main：正式数据，就是数据目录本身（config/、output/ 直接在数据目录下）
#   demo：离线演示，数据目录下的 demo/，与正式数据完全分开
WORKSPACES = {"main": "正式数据", "demo": "离线演示"}


def workspace_dir(name: str, root: Path | None = None) -> Path:
    root = root or data_dir()
    if name == "main":
        return root
    if name == "demo":
        return root / "demo"
    raise ValueError(f"未知工作区：{name}")
