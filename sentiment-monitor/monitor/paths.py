"""路径：运行数据放在用户目录，不写程序目录（打包后的程序目录可能只读）。"""

import os
import sys
from pathlib import Path

APP_DIR_NAME = "SentimentMonitor"

IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def data_dir() -> Path:
    override = os.environ.get("MONITOR_DATA_DIR")
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


def db_path() -> Path:
    return data_dir() / "monitor.db"


def log_path() -> Path:
    return data_dir() / "monitor.log"


def browser_profile_dir() -> Path:
    p = data_dir() / "browser-profile"
    p.mkdir(parents=True, exist_ok=True)
    return p


def web_dir() -> Path:
    # PyInstaller 打包后资源在 sys._MEIPASS 下
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    return root / "monitor" / "web"


def bundle_root() -> Path:
    """程序文件所在的根目录：源码运行时是项目目录，打包后是 PyInstaller 解包目录。"""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))


def guide_root() -> Path:
    """「使用指南」文档根目录：里面有 README.md 和 docs/。打包时放在 guide/ 下。"""
    if getattr(sys, "frozen", False):
        return bundle_root() / "guide"
    return bundle_root()


def source_root() -> Path:
    """应用内「源码」页读取的手写源码根目录。打包时 .py 原文件放在 source/ 下（否则只有 .pyc）。"""
    if getattr(sys, "frozen", False):
        return bundle_root() / "source"
    return bundle_root()
