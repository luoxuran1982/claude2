"""打包。在 Windows 上运行得到安装程序和免安装 zip：

    pip install -r requirements.txt pyinstaller pillow
    python packaging/build.py

Windows → dist/QuantLab-Setup-<版本>-x64.exe（需要 NSIS）+ dist/QuantLab-<版本>-windows-x64.zip
其他系统 → dist/QuantLab/（onedir，可直接运行）
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
from quantlab import __version__  # noqa: E402
import make_icon  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # Windows CI 控制台默认不是 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

NAME = "QuantLab"
DIST = ROOT / "dist"


def pyinstaller(icon_dir: Path) -> Path:
    import PyInstaller.__main__
    sep = os.pathsep
    icon = icon_dir / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    args = [
        str(ROOT / "run.py"), "--name", NAME, "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'quantlab' / 'web'}{sep}quantlab/web",
        "--collect-all", "lightgbm",
        "--collect-all", "webview",
        "--collect-submodules", "sklearn",
        "--collect-submodules", "pyarrow",
        "--hidden-import", "openpyxl",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "IPython",
    ]
    PyInstaller.__main__.run(args)
    return DIST / NAME


def find_makensis() -> str | None:
    for c in (shutil.which("makensis"), r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if c and Path(c).exists():
            return c
    return None


def finish_windows(folder: Path, icon_dir: Path) -> None:
    for doc in ("安装说明.md", "使用说明.md"):
        shutil.copy(ROOT / "docs" / doc, folder / doc)
    portable = DIST / f"{NAME}-{__version__}-windows-x64"
    shutil.make_archive(str(portable), "zip", DIST, NAME)
    print("免安装版：", portable.with_suffix(".zip"))
    makensis = find_makensis()
    if not makensis:
        print("没有找到 NSIS（makensis），跳过安装程序。安装：choco install nsis")
        return
    out = DIST / f"{NAME}-Setup-{__version__}-x64.exe"
    subprocess.run([makensis, "/INPUTCHARSET", "UTF8", f"/DVERSION={__version__}", f"/DSRC={folder}",
                    f"/DOUT={out}", f"/DICON={icon_dir / 'icon.ico'}", str(ROOT / "packaging" / "installer.nsi")], check=True)
    print("安装程序：", out)


def main():
    icon_dir = make_icon.main()
    built = pyinstaller(icon_dir)
    if sys.platform == "win32":
        finish_windows(built, icon_dir)
    print("完成：", built)


if __name__ == "__main__":
    main()
