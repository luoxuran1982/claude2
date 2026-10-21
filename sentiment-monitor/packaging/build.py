"""打包成免安装程序（在哪个系统上运行就打出哪个系统的包）。

    pip install pyinstaller
    python packaging/build.py

输出：dist/SentimentMonitor/。Windows 里双击 SentimentMonitor.exe：
不弹黑色控制台窗口，直接打开应用窗口；关掉应用窗口，后台随之退出。
排查问题时看日志：%APPDATA%\\SentimentMonitor\\monitor.log
"""

import os
import shutil
import subprocess
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

sys.path.insert(0, str(ROOT))
from monitor.guide import SOURCE_PATTERNS  # noqa: E402

# 「使用指南」：README.md 和 docs/ 全部打进包里（guide/），离线可读
data = [
    "--add-data", f"monitor/web{os.pathsep}monitor/web",
    "--add-data", f"monitor/catalog_tested.json{os.pathsep}monitor",  # v0.3：订阅目录的实测记录
    "--add-data", f"README.md{os.pathsep}guide",
    "--add-data", f"docs{os.pathsep}guide/docs",
]
# 「源码」页：PyInstaller 默认只带 .pyc，这里把手写源码原文件按白名单打进 source/
src_files = sorted({f.relative_to(ROOT).as_posix() for pat in SOURCE_PATTERNS for f in ROOT.glob(pat)
                    if f.is_file() and "__pycache__" not in f.parts})
for rel in src_files:
    parent = Path(rel).parent.as_posix()
    data += ["--add-data", f"{rel}{os.pathsep}" + (f"source/{parent}" if parent != "." else "source")]
print("打包源码文件：", len(src_files))

hidden = ["monitor.server", "monitor.demo", "monitor.guide", "monitor.procinfo", "monitor.desktop"]
collect = []
try:
    import webview  # noqa: F401  v0.4：原生窗口

    collect = ["--collect-all", "webview"]
except ImportError:
    print("提示：未安装 pywebview，打出的包会改用 Edge/Chrome 窗口")
try:
    import psutil  # noqa: F401

    hidden.append("psutil")
except ImportError:
    print("提示：未安装 psutil，运行状态页改用系统接口读取内存（Windows 上无法列出窗口子进程）")

PyInstaller.__main__.run([
    "run.py",
    "--name", NAME,
    "--noconfirm",
    "--clean",
    "--onedir",
    "--windowed",  # 无控制台：像普通桌面程序一样
    *data,
    *[x for h in hidden for x in ("--hidden-import", h)],
    *collect,
])

if sys.platform == "darwin":
    # 程序坞、菜单栏、⌘Q 菜单里显示的名字和版本（PyInstaller 默认用英文程序名、版本 0.0.0）
    import plistlib

    from monitor import __version__

    plist = ROOT / "dist" / f"{NAME}.app" / "Contents" / "Info.plist"
    if plist.exists():
        info = plistlib.loads(plist.read_bytes())
        info.update(CFBundleName="舆情监控", CFBundleDisplayName="舆情监控", CFBundleShortVersionString=__version__,
                    CFBundleVersion=__version__, CFBundleIdentifier="com.claude.sentiment-monitor")
        plist.write_bytes(plistlib.dumps(info))
        subprocess.run(["codesign", "--force", "--deep", "-s", "-", str(plist.parents[1])], check=False)

dist = ROOT / "dist" / NAME
if sys.platform == "darwin" and not dist.exists():
    dist = ROOT / "dist"
shutil.copy(ROOT / "README.md", dist / "README.md")
shutil.copytree(ROOT / "docs", dist / "docs", dirs_exist_ok=True)
print("完成：", dist)
