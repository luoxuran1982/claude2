"""打包成免安装程序（在哪个系统上运行就打出哪个系统的包）。

    pip install pyinstaller
    python packaging/build.py

输出：dist/SentimentMonitor/。Windows 里双击 SentimentMonitor.exe：
不弹黑色控制台窗口，直接打开应用窗口；关掉应用窗口，后台随之退出。
排查问题时看日志：%APPDATA%\\SentimentMonitor\\monitor.log
"""

import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
NAME = "SentimentMonitor"

PyInstaller.__main__.run([
    "run.py",
    "--name", NAME,
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",  # 无控制台：像普通桌面程序一样
    "--add-data", f"monitor/web{os.pathsep}monitor/web",
    "--hidden-import", "monitor.server",
    "--hidden-import", "monitor.demo",
])

dist = ROOT / "dist" / NAME
if sys.platform == "darwin" and not dist.exists():
    dist = ROOT / "dist"
shutil.copy(ROOT / "README.md", dist / "README.md")
shutil.copytree(ROOT / "docs", dist / "docs", dirs_exist_ok=True)
print("完成：", dist)
