"""打包成免安装程序（在哪个系统上运行就打出哪个系统的包）。

    pip install -r requirements.txt pyinstaller
    python packaging/build.py

输出：dist/AIVideoStudio/（Windows 里双击 AIVideoStudio.exe，macOS 里双击 AIVideoStudio）。
ffmpeg 随 imageio-ffmpeg 一起打进包里，不需要另装 Python 或 FFmpeg。
"""

import os
import shutil
import sys
from pathlib import Path

import PyInstaller.__main__

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)

args = [
    "run.py",
    "--name", "AIVideoStudio",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",  # 保留控制台：显示访问地址，关掉窗口即退出
    "--add-data", f"studio/web{os.pathsep}studio/web",
    "--collect-all", "imageio_ffmpeg",
    "--collect-data", "certifi",
    "--hidden-import", "studio.server",
]
PyInstaller.__main__.run(args)

dist = ROOT / "dist" / "AIVideoStudio"
shutil.copy(ROOT / "README.md", dist / "README.md")
print("完成：", dist)
if sys.platform == "darwin":
    print("首次打开如被系统拦截：右键 AIVideoStudio → 打开，或 系统设置 → 隐私与安全性 → 仍要打开。")
