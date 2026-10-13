"""在哪个系统上运行就打出哪个系统的安装包。

    pip install -r requirements.txt -c constraints.txt pyinstaller pillow
    python packaging/build.py

Windows → dist/HotRadar-Setup-<版本>-x64.exe（需要 NSIS）和 dist/HotRadar-<版本>-windows-x64.zip（免安装）
macOS   → dist/热点雷达.app 和 dist/HotRadar-<版本>-macOS-arm64.dmg
Linux   → dist/HotRadar/（只用于在容器里试打包）
"""
from __future__ import annotations

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
from hotradar import APP_NAME, UPSTREAM_VERSION, __version__  # noqa: E402
import make_icon  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # Windows CI 控制台默认不是 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

NAME = "HotRadar"
DIST = ROOT / "dist"


def stage_project() -> Path:
    """把「使用指南」清单里的全部文件（文档、附件、外壳源码、上游源码、样例）原样复制到 build/project/，
    打进安装包：网页、上游默认配置和样例从这里读，使用指南离线显示，源码 zip 也从这里打。"""
    from hotradar import guide
    staging = ROOT / "build" / "project"
    shutil.rmtree(staging, ignore_errors=True)
    items = guide.list_docs(ROOT) + guide.list_assets(ROOT) + guide.list_sources(ROOT)
    for item in items:
        target = staging / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        src = guide.ci_workflow(ROOT) if item["path"] == "ci/hot-radar.yml" else ROOT / item["path"]
        shutil.copy2(src, target)
    # 网页文件必须在（guide 清单已包含 hotradar/web/*，这里再确认一次）
    for f in ("index.html", "app.js", "guide.js", "md.js", "zoom.js", "style.css", "icon.svg"):
        assert (staging / "hotradar" / "web" / f).is_file(), f
    print(f"打包资料：{len(items)} 个文件 → {staging}")
    return staging


def pyinstaller(icon_dir: Path) -> Path:
    import PyInstaller.__main__
    sep = os.pathsep
    project = stage_project()
    icon = icon_dir / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    up = ROOT / "upstream"
    args = [
        str(ROOT / "run.py"), "--name", NAME, "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--paths", str(up),
        "--add-data", f"{project}{sep}project",
        # 上游运行时按「模块文件旁边」读取的数据文件
        "--add-data", f"{up / 'trendradar' / 'storage' / 'schema.sql'}{sep}trendradar/storage",
        "--add-data", f"{up / 'trendradar' / 'storage' / 'rss_schema.sql'}{sep}trendradar/storage",
        "--add-data", f"{up / 'trendradar' / 'storage' / 'ai_filter_schema.sql'}{sep}trendradar/storage",
        "--add-data", f"{up / 'config'}{sep}config",  # 上游提示词的兜底位置（正常从数据目录读）
        "--collect-submodules", "trendradar",
        "--collect-submodules", "mcp_server",
        "--collect-all", "litellm",
        "--collect-all", "fastmcp",
        "--collect-all", "mcp",
        "--collect-data", "tiktoken_ext",
        "--hidden-import", "tiktoken_ext.openai_public",
        "--hidden-import", "psutil",
        "--hidden-import", "feedparser",
        "--hidden-import", "json_repair",
        "--collect-data", "certifi",
        "--collect-all", "webview",
        "--copy-metadata", "fastmcp",
        "--copy-metadata", "mcp",
        "--copy-metadata", "litellm",
        "--exclude-module", "tkinter",
        "--exclude-module", "boto3",
        "--exclude-module", "botocore",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "app.hotradar"]
    PyInstaller.__main__.run(args)
    return DIST / (f"{NAME}.app" if sys.platform == "darwin" else NAME)


def install_note() -> str:
    return (ROOT / "docs" / "安装说明.md").read_text(encoding="utf-8")


def finish_mac(app: Path) -> None:
    plist_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist_path.read_bytes())
    info.update({
        "CFBundleDisplayName": APP_NAME, "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.news",
        "NSHumanReadableCopyright": f"GPL-3.0 · 基于 TrendRadar {UPSTREAM_VERSION}",
    })
    plist_path.write_bytes(plistlib.dumps(info))
    # 改了 Info.plist 必须重新签名（无开发者证书时用临时签名）
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    final = DIST / f"{APP_NAME}.app"
    if final.exists():
        shutil.rmtree(final)
    app.rename(final)
    arch = platform.machine()
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    subprocess.run(["ditto", str(final), str(staging / final.name)], check=True)
    (staging / "Applications").symlink_to("/Applications")
    (staging / "安装说明（先看这里）.md").write_text(install_note(), encoding="utf-8")
    dmg = DIST / f"{NAME}-{__version__}-macOS-{arch}.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(staging),
                    "-ov", "-format", "UDZO", str(dmg)], check=True)
    print("完成：", final, dmg)


def find_makensis() -> str | None:
    for c in (shutil.which("makensis"), r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if c and Path(c).exists():
            return c
    return None


def finish_windows(folder: Path, icon_dir: Path) -> None:
    (folder / "安装说明.md").write_text(install_note(), encoding="utf-8")
    portable = DIST / f"{NAME}-{__version__}-windows-x64"
    shutil.make_archive(str(portable), "zip", DIST, NAME)
    print("免安装版：", portable.with_suffix(".zip"))
    makensis = find_makensis()
    if not makensis:
        if os.environ.get("CI"):
            # CI 上必须出安装程序，不能悄悄只出免安装版
            raise SystemExit("没有找到 NSIS（makensis），无法生成安装程序")
        print("没有找到 NSIS（makensis），跳过安装程序。安装：choco install nsis")
        return
    out = DIST / f"{NAME}-Setup-{__version__}-x64.exe"
    subprocess.run([makensis, "/INPUTCHARSET", "UTF8", f"/DVERSION={__version__}", f"/DSRC={folder}",
                    f"/DOUT={out}", f"/DICON={icon_dir / 'icon.ico'}", str(ROOT / "packaging" / "installer.nsi")], check=True)
    print("安装程序：", out)


def main():
    icon_dir = make_icon.main()
    built = pyinstaller(icon_dir)
    if sys.platform == "darwin":
        finish_mac(built)
    elif sys.platform == "win32":
        finish_windows(built, icon_dir)
    else:
        print("完成：", built)


if __name__ == "__main__":
    main()
