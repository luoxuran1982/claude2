"""路径：安装包/源码里的只读资源，和系统用户数据目录里的可写数据。

用户数据目录（替换新版本、卸载都不会删）：
    macOS   ~/Library/Application Support/QlibStudio/
    Windows %LOCALAPPDATA%\\QlibStudio\\
    Linux   ~/.local/share/QlibStudio/（开发和 CI 用）
可以用环境变量 QLIBSTUDIO_DATA 指定别的目录（测试、CI 冒烟用）。

数据目录里的内容：
    settings.json        界面和连接设置
    datasets/<编号>/      每个数据集一个 Qlib 格式目录（calendars/ instruments/ features/）+ meta.json + quality.json
    futu-cache/          富途 OpenD 下载的日 K 缓存（每只股票一个 CSV，下次只补缺的日期）
    downloads/           社区数据集下载中的文件（支持断点续传）
    runs/<编号>/          每次实验的配置、日志、结果（result.json、export.json）
    jobs/                工作进程的日志和进度文件
    webview/             窗口组件的本机存储（缩放档位等）
    qlibstudio.log       应用日志
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from . import APP_ID


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")


def bundle_root() -> Path:
    if frozen():
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def project_dir() -> Path:
    """完整源码树：源码运行时就是 qlib-studio/；安装包里是解包目录下的 project/
    （packaging/build.py 原样复制进去的全部手写源码、文档、上游源码和样例）。"""
    if frozen():
        return bundle_root() / "project"
    return bundle_root()


def web_dir() -> Path:
    return project_dir() / "qlibstudio" / "web"


def upstream_dir() -> Path:
    return project_dir() / "upstream"


def samples_dir() -> Path:
    return project_dir() / "samples"


def ensure_upstream_importable() -> None:
    """源码运行时把 upstream/ 和 upstream/scripts/ 加进 sys.path，才能 import qlib 和 dump_bin；
    安装包里它们已编进程序。"""
    if frozen():
        return
    for p in (upstream_dir(), upstream_dir() / "scripts"):
        s = str(p)
        if s not in sys.path:
            sys.path.insert(0, s)


def data_dir(platform: str | None = None, environ=None, home: Path | None = None) -> Path:
    platform = sys.platform if platform is None else platform
    environ = os.environ if environ is None else environ
    home = Path.home() if home is None else Path(home)
    override = environ.get("QLIBSTUDIO_DATA")
    if override:
        return Path(override)
    if platform == "win32":
        return Path(environ.get("LOCALAPPDATA") or home / "AppData" / "Local") / APP_ID
    if platform == "darwin":
        return home / "Library" / "Application Support" / APP_ID
    return Path(environ.get("XDG_DATA_HOME") or home / ".local" / "share") / APP_ID
