"""跨平台的数据目录与资源路径。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID


def resource_dir() -> Path:
    """打包后资源在 PyInstaller 解包目录里，源码运行时就是本包目录。"""
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) / "quantlab" if base else Path(__file__).resolve().parent


def data_dir(platform: str | None = None, environ=None, home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    override = environ.get("QUANTLAB_DATA")
    if override:
        return Path(override)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_ID
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_ID
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID
