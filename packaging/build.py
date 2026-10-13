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

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

# 应用内「使用指南」要离线显示全部文档和手写源码：
# 文档（README.md、docs/）原样打包；PyInstaller 默认只带 .pyc，所以把手写源码按白名单复制到 source/ 再打包。
from studio import guide  # noqa: E402

SRC_STAGE = ROOT / "build" / "source_stage"
shutil.rmtree(SRC_STAGE, ignore_errors=True)
for f in guide.list_sources():
    dst = SRC_STAGE / f["path"]
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / f["path"], dst)
print("打包手写源码：", len(guide.list_sources()), "个文件")
print("打包文档：", [d["path"] for d in guide.list_docs()])

args = [
    "run.py",
    "--name", "AIVideoStudio",
    "--noconfirm",
    "--clean",
    "--onedir",
    "--console",  # 保留控制台：显示访问地址，关掉窗口即退出
    "--add-data", f"studio/web{os.pathsep}studio/web",
    "--add-data", f"README.md{os.pathsep}.",
    "--add-data", f"docs{os.pathsep}docs",
    "--add-data", f"{SRC_STAGE}{os.pathsep}source",
    "--hidden-import", "psutil",
    "--collect-all", "imageio_ffmpeg",
    "--collect-data", "certifi",
    "--hidden-import", "studio.server",
]
PyInstaller.__main__.run(args)

dist = ROOT / "dist" / "AIVideoStudio"
shutil.copy(ROOT / "README.md", dist / "README.md")

# 自检：文档和源码确实进了包
internal = dist / "_internal" if (dist / "_internal").is_dir() else dist
missing = [d["path"] for d in guide.list_docs() if not (internal / d["path"]).is_file()]
missing += ["source/" + f["path"] for f in guide.list_sources() if not (internal / "source" / f["path"]).is_file()]
if missing:
    sys.exit("打包缺少文件：" + ", ".join(missing))
print("文档和源码已打包：", internal)
print("完成：", dist)
if sys.platform == "darwin":
    print("首次打开如被系统拦截：右键 AIVideoStudio → 打开，或 系统设置 → 隐私与安全性 → 仍要打开。")
