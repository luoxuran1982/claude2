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
