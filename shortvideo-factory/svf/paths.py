"""数据目录与各部件的位置。

三种运行方式下的位置：
- 源码运行（python run.py）：上游在 shortvideo-factory/upstream/，Python 用当前解释器（或环境变量 SVF_PYTHON）。
- Windows 安装版：<安装目录>/ShortVideoFactory.exe，旁边 payload/ 里是随包 Python 运行时、上游源码、字体、文档。
- macOS 安装版：AI 短视频工厂.app/Contents/MacOS/ShortVideoFactory，payload 在 Contents/Resources/payload/。

用户数据（设置、API Key、生成的视频、缓存、日志）一律放在系统的用户数据目录，不放安装目录：
- macOS：~/Library/Application Support/ShortVideoFactory/
- Windows：%LOCALAPPDATA%\\ShortVideoFactory\\
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID

PROJECT_DIR = Path(__file__).resolve().parent.parent  # 源码里的 shortvideo-factory/


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def data_dir(platform: str | None = None, environ=None, home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    override = environ.get("SVF_DATA_DIR")
    if override:
        return Path(override)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_ID
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_ID
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID


def payload_dir(executable: str | None = None, platform: str | None = None, frozen: bool | None = None) -> Path:
    """随包携带的部件所在目录（源码运行时就是 shortvideo-factory/）。"""
    frozen = is_frozen() if frozen is None else frozen
    if not frozen:
        return PROJECT_DIR
    platform = sys.platform if platform is None else platform
    exe = Path(executable or sys.executable).resolve()
    if platform == "darwin":
        return exe.parent.parent / "Resources" / "payload"   # .app/Contents/MacOS/<exe> → Contents/Resources/payload
    return exe.parent / "payload"


def upstream_dir() -> Path:
    return payload_dir() / "upstream"


def bundled_fonts_dir() -> Path:
    return payload_dir() / "fonts"


def child_script() -> Path:
    """Streamlit 子进程的入口脚本。安装包里是 payload/child/svf_child.py（与 svf/child.py 内容相同）。"""
    if is_frozen():
        return payload_dir() / "child" / "svf_child.py"
    return Path(__file__).resolve().parent / "child.py"


def runtime_python(platform: str | None = None, environ=None) -> Path:
    """运行上游 WebUI 的 Python。安装包带一份可重定位的 CPython（python-build-standalone）。"""
    environ = os.environ if environ is None else environ
    if environ.get("SVF_PYTHON"):
        return Path(environ["SVF_PYTHON"])
    if not is_frozen():
        return Path(sys.executable)
    platform = sys.platform if platform is None else platform
    rt = payload_dir() / "runtime"
    return rt / "python.exe" if platform == "win32" else rt / "bin" / "python3"


def guide_root() -> Path:
    """「使用指南」读取文档和源码的根目录：源码运行时是 shortvideo-factory/，安装包里是 payload/project/。"""
    return payload_dir() / "project" if is_frozen() else PROJECT_DIR


def web_dir() -> Path:
    return Path(__file__).resolve().parent / "web"
