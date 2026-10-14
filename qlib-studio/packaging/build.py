"""在哪个系统上运行就打出哪个系统的安装包。

    pip install -r requirements.txt pyinstaller pillow
    python packaging/build_ext.py        # 先编译上游的两个 Cython 扩展
    python packaging/build.py

Windows → dist/QlibStudio-Setup-<版本>-x64.exe（需要 NSIS）和 dist/QlibStudio-<版本>-windows-x64.zip（免安装）
macOS   → dist/Qlib 量化研究台.app 和 dist/QlibStudio-<版本>-macOS-arm64.dmg
Linux   → dist/QlibStudio/（只用于在容器里试打包）
"""
from __future__ import annotations

import importlib
import os
import platform
import plistlib
import shutil
import subprocess
import sys
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UP = ROOT / "upstream"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
from qlibstudio import APP_NAME, UPSTREAM_VERSION, __version__  # noqa: E402
import make_icon  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # Windows CI 控制台默认不是 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

NAME = "QlibStudio"
DIST = ROOT / "dist"
# 上游里研究台用不到、而且依赖没装的部分（PyTorch 深度模型、强化学习、报告绘图、超参搜索等），不打包
SKIP_PREFIXES = ("qlib.rl", "qlib.contrib.rl", "qlib.contrib.meta", "qlib.contrib.tuner", "qlib.contrib.report",
                 "qlib.contrib.online", "qlib.contrib.rolling", "qlib.tests", "qlib.contrib.data.highfreq",
                 "qlib.contrib.strategy.optimizer", "qlib.contrib.torch")


def stage_project() -> Path:
    """把「使用指南」清单里的全部文件（文档、附件、外壳源码、上游源码、样例）原样复制到 build/project/，
    打进安装包：网页从这里读，使用指南离线显示，源码 zip 也从这里打。"""
    from qlibstudio import guide
    staging = ROOT / "build" / "project"
    shutil.rmtree(staging, ignore_errors=True)
    items = guide.list_docs(ROOT) + guide.list_assets(ROOT) + guide.list_sources(ROOT)
    for item in items:
        target = staging / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        src = guide.ci_workflow(ROOT) if item["path"] == f"ci/{guide.CI_NAME}" else ROOT / item["path"]
        shutil.copy2(src, target)
    # ECharts 是压缩过的第三方库，不在「源码」清单里，单独复制
    vend = staging / "qlibstudio" / "web" / "vendor"
    vend.mkdir(parents=True, exist_ok=True)
    for f in (ROOT / "qlibstudio" / "web" / "vendor").iterdir():
        shutil.copy2(f, vend / f.name)
    for f in ("index.html", "app.js", "guide.js", "md.js", "zoom.js", "style.css", "icon.svg", "vendor/echarts.min.js"):
        assert (staging / "qlibstudio" / "web" / f).is_file(), f
    print(f"打包资料：{len(items)} 个文件 → {staging}")
    return staging


def qlib_modules() -> list[str]:
    """上游 qlib 里能在打包环境中导入的全部模块（上游按字符串动态加载模块，PyInstaller 静态分析找不全）。"""
    sys.path.insert(0, str(UP))
    sys.path.insert(0, str(UP / "scripts"))
    mods = []
    for p in sorted((UP / "qlib").rglob("*.py")):
        rel = p.relative_to(UP).with_suffix("")
        name = ".".join(rel.parts)
        if name.endswith(".__init__"):
            name = name[: -len(".__init__")]
        if name.startswith(SKIP_PREFIXES) or ".pytorch_" in name or "pytorch" in name.rsplit(".", 1)[-1]:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            try:
                importlib.import_module(name)
            except Exception as exc:  # noqa: BLE001
                print(f"  跳过 {name}：{type(exc).__name__}: {str(exc)[:80]}")
                continue
        mods.append(name)
    print(f"上游 qlib 模块：{len(mods)} 个")
    return mods


def pyinstaller(icon_dir: Path) -> Path:
    import PyInstaller.__main__
    sep = os.pathsep
    project = stage_project()
    icon = icon_dir / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    args = [
        str(ROOT / "run.py"), "--name", NAME, "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--paths", str(UP), "--paths", str(UP / "scripts"),
        "--add-data", f"{project}{sep}project",
        "--hidden-import", "dump_bin",
        "--collect-submodules", "qlibstudio",
        "--collect-all", "lightgbm", "--collect-all", "catboost",
        # xgboost 用 PyInstaller 自带钩子（收 xgboost.dll/libxgboost 和 VERSION）；--collect-all 会去导入 xgboost.testing（要 pytest）
        "--hidden-import", "xgboost", "--collect-data", "xgboost", "--collect-binaries", "xgboost",
        "--hidden-import", "sklearn.linear_model",  # sklearn、scipy 用 PyInstaller 自带钩子（不收它们的测试模块）
        "--collect-all", "mlflow", "--copy-metadata", "mlflow-skinny",
        "--collect-all", "futu", "--collect-submodules", "gym", "--copy-metadata", "gym",
        "--collect-all", "setuptools_scm", "--copy-metadata", "setuptools_scm",
        "--collect-submodules", "ruamel.yaml", "--collect-submodules", "redis_lock",
        "--hidden-import", "psutil", "--collect-data", "certifi",
        "--collect-all", "webview",
        "--exclude-module", "tkinter", "--exclude-module", "torch", "--exclude-module", "tensorflow",
        "--exclude-module", "matplotlib", "--exclude-module", "IPython", "--exclude-module", "jupyter",
        "--exclude-module", "notebook", "--exclude-module", "plotly", "--exclude-module", "cvxpy",
        "--exclude-module", "nvidia", "--exclude-module", "pytest", "--exclude-module", "hypothesis",
    ]
    for m in qlib_modules():
        args += ["--hidden-import", m]
    try:
        importlib.import_module("vcs_versioning")
        args += ["--collect-all", "vcs_versioning", "--copy-metadata", "vcs-versioning"]
    except ImportError:
        pass
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "app.qlibstudio"]
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
        "LSApplicationCategoryType": "public.app-category.finance",
        "NSHumanReadableCopyright": f"MIT · 基于微软 Qlib {UPSTREAM_VERSION}",
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
    for attempt in range(4):  # hdiutil 在 CI 上偶尔报「资源忙」，重试
        r = subprocess.run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(staging),
                            "-ov", "-format", "UDZO", str(dmg)])
        if r.returncode == 0:
            break
        print(f"hdiutil 失败（第 {attempt + 1} 次），稍后重试")
        import time
        time.sleep(10 * (attempt + 1))
    else:
        raise SystemExit("hdiutil 生成 dmg 失败")
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
    libs = UP / "qlib" / "data" / "_libs"
    if not any(p.suffix in (".so", ".pyd") for p in libs.iterdir()):
        raise SystemExit("上游 Cython 扩展还没编译：先运行 python packaging/build_ext.py")
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
