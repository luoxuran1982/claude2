"""跨平台路径：运行数据放在用户目录，不写入程序目录（打包后的程序目录可能只读）。"""

import os
import sys
from pathlib import Path

APP_DIR_NAME = "AIVideoStudio"

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def data_dir() -> Path:
    override = os.environ.get("STUDIO_DATA_DIR")
    if override:
        base = Path(override)
    elif IS_WIN:
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / APP_DIR_NAME
    elif IS_MAC:
        base = Path.home() / "Library" / "Application Support" / APP_DIR_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / APP_DIR_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def sub(*parts) -> Path:
    p = data_dir().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def projects_dir() -> Path:
    return sub("projects")


def library_dir() -> Path:
    return sub("library")


def music_dir() -> Path:
    return sub("music")


def fonts_dir() -> Path:
    return sub("fonts")


def cache_dir(name: str) -> Path:
    return sub("cache", name)


def private_dir() -> Path:
    p = sub("private")
    if not IS_WIN:
        try:
            os.chmod(p, 0o700)
        except OSError:
            pass
    return p


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def web_dir() -> Path:
    return package_dir() / "web"


def bundled_bin_dir() -> Path:
    """打包版可把 ffmpeg 放在程序目录的 bin/ 下。"""
    root = Path(getattr(sys, "_MEIPASS", package_dir().parent))
    return root / "bin"
