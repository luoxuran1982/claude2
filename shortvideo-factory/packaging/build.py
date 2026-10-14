"""在哪个系统上运行就打出哪个系统的安装包。

    pip install -r requirements.txt pyinstaller pillow uv
    python packaging/build.py

产物：
- Windows → dist/ShortVideoFactory-Setup-<版本>-x64.exe（需要 NSIS）和 dist/ShortVideoFactory-<版本>-windows-x64.zip（免安装）
- macOS   → dist/AI 短视频工厂.app 和 dist/ShortVideoFactory-<版本>-macOS-arm64.dmg

打包方式（为什么不用 PyInstaller 打整个 Streamlit）：
- 启动器（窗口 + 本机服务 + 反向代理，依赖很少）用 PyInstaller 打成 ShortVideoFactory 可执行文件；
- 上游 WebUI 运行在「随包携带的可重定位 Python」里（python-build-standalone，用 uv 下载），
  依赖按 requirements-runtime.txt（上游 uv.lock 的精确版本）正常 pip 安装，元数据、静态文件都完整，
  避开 Streamlit + PyInstaller 需要逐个收集静态文件和包元数据的老问题。上游 Windows 一键包也是这个思路。
两部分放在一起：Windows 是 <安装目录>/payload/，macOS 是 .app/Contents/Resources/payload/：
    payload/runtime/   Python 3.11 + 依赖（.pyc 已预编译）
    payload/upstream/  MoneyPrinterTurbo 源码（只含运行需要的部分）
    payload/fonts/     开源字体与许可证
    payload/child/     svf_child.py（Streamlit 子进程入口）
    payload/project/   README、docs/ 和源码（给应用内「使用指南」离线显示）
"""
from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))
from svf import APP_ID, APP_NAME, __version__  # noqa: E402
import make_icon  # noqa: E402

for _stream in (sys.stdout, sys.stderr):  # Windows CI 控制台默认不是 UTF-8
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

PY_VERSION = "3.11"          # 与上游 .python-version 一致
DIST = ROOT / "dist"
BUILD = ROOT / "build"
WIN = sys.platform == "win32"
MAC = sys.platform == "darwin"

# 上游目录里运行时不需要的部分（测试、Docker、文档图片、锁文件等），不放进安装包
UPSTREAM_EXCLUDE = {"test", "docs", ".github", "Dockerfile", "Dockerfile.gpu", "Dockerfile.claude", "docker-compose.yml",
                    "docker-compose.gpu.yml", "docker-compose.release.yml", "docker-compose.claude.yml", "uv.lock",
                    "README-en.md", "README-ja.md", ".dockerignore", ".gitignore", ".gitattributes", "webui.sh",
                    "webui.bat", ".python-version", "__pycache__", "storage", "config.toml"}
# 绝对不能出现在安装包里的文件（商业字体、来源不明的音乐）
FORBIDDEN = re.compile(r"(MicrosoftYaHei|STHeiti|UTM Kabel|resource[\\/]songs[\\/].+\.mp3)", re.I)
# 随包 Python 里用不到的标准库部分
STDLIB_STRIP = ("test", "idlelib", "tkinter", "turtledemo", "lib2to3/tests", "ensurepip")


def run(cmd, **kw):
    print("$", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


def uv() -> str:
    exe = shutil.which("uv")
    if not exe:
        raise SystemExit("需要 uv：pip install uv")
    return exe


# ---------------- 1. 随包 Python 运行时 ----------------

def build_runtime(payload: Path) -> Path:
    install_dir = BUILD / "uv-python"
    run([uv(), "python", "install", PY_VERSION, "--install-dir", install_dir, "--no-bin"])
    # 安装目录里是 cpython-3.11.x-<平台>/（另有不带补丁号的同名链接），取补丁号最大的真实目录
    homes = [p for p in install_dir.glob(f"cpython-{PY_VERSION}.*") if p.is_dir() and not p.is_symlink()]
    if not homes:
        raise SystemExit(f"uv 没有装好 Python {PY_VERSION}：{list(install_dir.iterdir())}")
    home = max(homes, key=lambda p: [int(x) for x in re.findall(r"\d+", p.name.split("-")[1])])
    print("python-build-standalone：", home)
    rt = payload / "runtime"
    shutil.rmtree(rt, ignore_errors=True)
    shutil.copytree(home, rt, symlinks=True)
    stdlib = rt / "Lib" if WIN else rt / "lib" / f"python{PY_VERSION}"
    (stdlib / "EXTERNALLY-MANAGED").unlink(missing_ok=True)   # 允许往这份独立 Python 里装包
    for part in STDLIB_STRIP:
        shutil.rmtree(stdlib / part, ignore_errors=True)
    for tk in ("tcl", "lib/tcl8.6", "lib/tk8.6", "lib/itcl4.2.4", "lib/thread2.8.9"):
        shutil.rmtree(rt / tk, ignore_errors=True)
    py = rt / "python.exe" if WIN else rt / "bin" / "python3"
    run([uv(), "pip", "install", "--python", py, "--no-cache", "--link-mode", "copy",
         "-r", ROOT / "requirements-runtime.txt"])
    prune_site_packages(stdlib / "site-packages")
    # 冒烟：关键依赖都能导入，FFmpeg 可执行
    run([py, "-c", "import streamlit, moviepy, edge_tts, openai, litellm, imageio_ffmpeg, PIL, toml, loguru;"
                   "print('edge-tts:', edge_tts.__version__); assert edge_tts.__version_info__ >= (7, 3, 1), 'edge-tts 太旧';"
                   "import subprocess; exe = imageio_ffmpeg.get_ffmpeg_exe(); print('ffmpeg:', exe);"
                   "subprocess.run([exe, '-version'], check=True, capture_output=True)"])
    return py


# 运行时用不到、而且路径很长的第三方文件：Windows 路径上限 260 字符，NSIS 打包和运行都会出错
PRUNE_DIRS = {"guardrail_benchmarks"}
PRUNE_PATHS = ("streamlit/.agents", "litellm/proxy/_experimental/out")   # Streamlit 给 AI 助手的示例、LiteLLM 代理服务器的网页界面
PRUNE_TESTS_IN = {"pandas", "numpy", "pyarrow", "litellm"}
MAX_REL_PATH = 170   # payload 内相对路径上限：留出 C:\\Users\\<用户名>\\AppData\\Local\\Programs\\ShortVideoFactory\\payload\\ 的长度


def prune_site_packages(site: Path) -> None:
    removed = 0
    for d in sorted(site.rglob("*"), key=lambda p: -len(p.parts)):
        if not d.is_dir() or d.is_symlink() or not d.exists():
            continue
        top = d.relative_to(site).parts[0]
        if d.name in PRUNE_DIRS or (d.name == "tests" and top in PRUNE_TESTS_IN):
            shutil.rmtree(d, ignore_errors=True)
            removed += 1
    for rel in PRUNE_PATHS:
        if (site / rel).exists():
            shutil.rmtree(site / rel, ignore_errors=True)
            removed += 1
    print(f"删掉运行时用不到的目录 {removed} 个")


def check_path_lengths(payload: Path) -> None:
    too_long = [str(p.relative_to(payload)) for p in payload.rglob("*") if len(str(p.relative_to(payload))) > MAX_REL_PATH]
    if too_long:
        raise SystemExit(f"有 {len(too_long)} 个路径太长（Windows 260 字符限制），例如：{too_long[:5]}")


# ---------------- 2. 上游源码、字体、子进程入口、使用指南资料 ----------------

def copy_upstream(payload: Path) -> None:
    dst = payload / "upstream"
    shutil.rmtree(dst, ignore_errors=True)

    def ignore(directory, names):
        return {n for n in names if n in UPSTREAM_EXCLUDE or n.endswith(".pyc")}
    shutil.copytree(ROOT / "upstream", dst, ignore=ignore)


def stage_project(payload: Path) -> int:
    """把 README、UPSTREAM、docs/（含附件）、字体许可证和源码原样复制到 payload/project/，给「使用指南」离线显示。"""
    from svf import guide
    staging = payload / "project"
    shutil.rmtree(staging, ignore_errors=True)
    items = guide.list_docs(ROOT) + guide.list_assets(ROOT) + guide.list_sources(ROOT)
    for item in items:
        target = staging / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / item["path"], target)
    print(f"使用指南资料：{len(items)} 个文件 → {staging}")
    return len(items)


def build_payload() -> tuple[Path, Path]:
    payload = BUILD / "payload"
    payload.mkdir(parents=True, exist_ok=True)
    py = build_runtime(payload)
    copy_upstream(payload)
    shutil.rmtree(payload / "fonts", ignore_errors=True)
    shutil.copytree(ROOT / "fonts", payload / "fonts")
    (payload / "child").mkdir(exist_ok=True)
    shutil.copy2(ROOT / "svf" / "child.py", payload / "child" / "svf_child.py")
    stage_project(payload)
    # 预编译 .pyc（unchecked-hash：不依赖文件时间，安装目录只读也不用再写缓存）
    subprocess.run([str(py), "-m", "compileall", "-q", "-j", "0", "--invalidation-mode", "unchecked-hash",
                    str(payload / "runtime"), str(payload / "upstream"), str(payload / "child")], check=False)
    check_path_lengths(payload)
    bad = [str(p.relative_to(payload)) for p in payload.rglob("*") if FORBIDDEN.search(str(p.relative_to(payload)))]
    if bad:
        raise SystemExit(f"安装包里不能有这些文件（版权）：{bad}")
    return payload, py


# ---------------- 3. 启动器（PyInstaller） ----------------

def pyinstaller(icon_dir: Path) -> Path:
    import PyInstaller.__main__
    sep = os.pathsep
    icon = icon_dir / ("icon.icns" if MAC else "icon.ico")
    args = [
        str(ROOT / "run.py"), "--name", APP_ID, "--noconfirm", "--clean", "--onedir", "--windowed",
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(BUILD / "pyinstaller"), "--specpath", str(BUILD),
        "--add-data", f"{ROOT / 'svf' / 'web'}{sep}svf/web",
        "--hidden-import", "psutil",
        "--collect-submodules", "tornado",
        "--collect-all", "webview",
        "--exclude-module", "tkinter",
        "--exclude-module", "streamlit",
    ]
    if MAC:
        args += ["--osx-bundle-identifier", "app.shortvideofactory"]
    PyInstaller.__main__.run(args)
    return DIST / (f"{APP_ID}.app" if MAC else APP_ID)


def dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file() and not f.is_symlink())


def finish_mac(app: Path, payload: Path) -> Path:
    res = app / "Contents" / "Resources"
    shutil.rmtree(res / "payload", ignore_errors=True)
    run(["ditto", payload, res / "payload"])
    plist_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist_path.read_bytes())
    info.update({
        "CFBundleDisplayName": APP_NAME, "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.video",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSHumanReadableCopyright": "基于 MoneyPrinterTurbo（MIT）",
    })
    plist_path.write_bytes(plistlib.dumps(info))
    # 加了 payload、改了 Info.plist，必须重新签名（没有开发者证书时用临时签名）
    run(["codesign", "--force", "--deep", "--sign", "-", app])
    subprocess.run(["codesign", "--verify", "--verbose=1", str(app)], check=False)   # 只打印结果，临时签名下不作硬性要求
    final = DIST / f"{APP_NAME}.app"
    if final.exists():
        shutil.rmtree(final)
    app.rename(final)
    arch = platform.machine()
    staging = BUILD / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    run(["ditto", final, staging / final.name])
    (staging / "Applications").symlink_to("/Applications")
    shutil.copy(ROOT / "docs" / "01 使用指南.md", staging / "使用指南（安装与首次打开）.md")
    dmg = DIST / f"{APP_ID}-{__version__}-macOS-{arch}.dmg"
    dmg.unlink(missing_ok=True)
    run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", staging, "-ov", "-format", "UDZO", dmg])
    print("完成：", final, dmg)
    return final


def find_makensis() -> str | None:
    for c in (shutil.which("makensis"), r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if c and Path(c).exists():
            return c
    return None


def finish_windows(folder: Path, payload: Path, icon_dir: Path) -> Path:
    shutil.rmtree(folder / "payload", ignore_errors=True)
    shutil.copytree(payload, folder / "payload", symlinks=False)
    shutil.copy(ROOT / "docs" / "01 使用指南.md", folder / "使用指南.md")
    portable = DIST / f"{APP_ID}-{__version__}-windows-x64"
    shutil.make_archive(str(portable), "zip", DIST, APP_ID)
    print("免安装版：", f"{portable}.zip")
    makensis = find_makensis()
    if not makensis:
        print("没有找到 NSIS（makensis），跳过安装程序。安装：choco install nsis")
        return folder
    out = DIST / f"{APP_ID}-Setup-{__version__}-x64.exe"
    run([makensis, "/V2", "/INPUTCHARSET", "UTF8", f"/DVERSION={__version__}", f"/DSRC={folder}",
         f"/DOUT={out}", f"/DICON={icon_dir / 'icon.ico'}", ROOT / "packaging" / "installer.nsi"])
    print("安装程序：", out)
    return folder


def main():
    DIST.mkdir(exist_ok=True)
    icon_dir = make_icon.main()
    payload, _ = build_payload()
    built = pyinstaller(icon_dir)
    if MAC:
        final = finish_mac(built, payload)
    elif WIN:
        final = finish_windows(built, payload, icon_dir)
    else:
        shutil.rmtree(built / "payload", ignore_errors=True)
        shutil.copytree(payload, built / "payload", symlinks=True)
        final = built
        print("完成（Linux 仅供测试）：", built)
    info = {"version": __version__, "app": str(final), "payload_mib": round(dir_size(payload) / 2**20, 1),
            "files": {p.name: round(p.stat().st_size / 2**20, 1) for p in DIST.iterdir() if p.is_file()}}
    (DIST / "build-info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
