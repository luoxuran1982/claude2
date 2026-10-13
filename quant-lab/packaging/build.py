"""打包。在 Windows 上运行得到安装程序和免安装 zip：

    pip install -r requirements.txt pyinstaller pillow
    python packaging/build.py

Windows → dist/QuantLab-Setup-<版本>-x64.exe（需要 NSIS）+ dist/QuantLab-<版本>-windows-x64.zip
macOS   → dist/QuantLab.app + dist/QuantLab-<版本>-macos-arm64.dmg
其他系统 → dist/QuantLab/（onedir，可直接运行）
README、docs/ 全部文档和手写源码 zip 都会打进包里，供应用内「使用指南」离线阅读。
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


def stage_guide() -> tuple[Path, Path]:
    """把使用指南的资料放进打包目录：README + docs/（含附件）原样复制；手写源码打成 zip（打包后只有 .pyc，靠它在应用内看源码）。"""
    from quantlab import guide
    stage = ROOT / "build" / "guide"
    shutil.rmtree(stage, ignore_errors=True)
    docs = stage / "projectdocs"
    (docs / "docs").mkdir(parents=True)
    shutil.copy(ROOT / "README.md", docs / "README.md")
    shutil.copytree(ROOT / "docs", docs / "docs", dirs_exist_ok=True)
    src = stage / "projectsrc"
    src.mkdir(parents=True)
    (src / guide.SRC_ZIP_NAME).write_bytes(guide.build_source_zip(ROOT))
    print("使用指南：", len(guide.list_docs(docs)), "篇文档，", len(guide.source_files(ROOT)), "个源码文件")
    return docs, src


def pyinstaller(icon_dir: Path) -> Path:
    import PyInstaller.__main__
    sep = os.pathsep
    docs, src = stage_guide()
    icon = icon_dir / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    args = [
        str(ROOT / "run.py"), "--name", NAME, "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'quantlab' / 'web'}{sep}quantlab/web",
        "--add-data", f"{docs}{sep}projectdocs",
        "--add-data", f"{src}{sep}projectsrc",
        "--osx-bundle-identifier", "com.claude.quantlab",
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
    for doc in ("00 项目阅读入口.md", "01 使用指南.md", "安装说明.md"):
        if (ROOT / "docs" / doc).exists():
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


def finish_macos() -> None:
    """dist/QuantLab.app → dist/QuantLab-<版本>-macos-arm64.dmg（里面有 QuantLab.app 和「应用程序」快捷方式）。"""
    app = DIST / f"{NAME}.app"
    if not app.exists():
        print("没有找到", app)
        return
    stage = ROOT / "build" / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    subprocess.run(["ditto", str(app), str(stage / app.name)], check=True)
    os.symlink("/Applications", stage / "Applications")
    out = DIST / f"{NAME}-{__version__}-macos-arm64.dmg"
    out.unlink(missing_ok=True)
    for attempt in range(3):  # CI 上 hdiutil 偶尔报 "Resource busy"，重试
        r = subprocess.run(["hdiutil", "create", "-volname", NAME, "-srcfolder", str(stage), "-ov", "-format", "UDZO", str(out)])
        if r.returncode == 0:
            break
        import time
        time.sleep(5 * (attempt + 1))
    else:
        raise SystemExit("hdiutil 生成 dmg 失败")
    print("macOS 安装包：", out)


def main():
    icon_dir = make_icon.main()
    built = pyinstaller(icon_dir)
    if sys.platform == "win32":
        finish_windows(built, icon_dir)
    elif sys.platform == "darwin":
        finish_macos()
    print("完成：", built)


if __name__ == "__main__":
    main()
