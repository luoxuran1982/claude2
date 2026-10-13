"""在哪个系统上运行就打出哪个系统的安装包。

    pip install -r requirements.txt pyinstaller
    python packaging/build.py

Windows → dist/AIVideoStudio-Setup-<版本>-x64.exe（需要 NSIS：choco install nsis）
          dist/AIVideoStudio-<版本>-windows-x64.zip（免安装）
          dist/AIVideoStudio/AIVideoStudio.exe（解开的程序目录，冒烟测试用）
macOS   → dist/AI 知识视频工坊.app 和 dist/AIVideoStudio-<版本>-macOS-<arm64>.dmg

程序是窗口程序（pywebview：Windows 用 WebView2，macOS 用 WKWebView），没有控制台窗口；
FFmpeg 随 imageio-ffmpeg 打进包里，不需要另装 Python 或 FFmpeg。
应用内「使用指南」要离线显示全部文档和手写源码：README.md、docs/ 原样打包；
PyInstaller 默认只带 .pyc，所以把手写源码按白名单复制到 source/ 再打包，最后自检。
"""

import os
import platform
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "packaging"))

from studio import __version__, guide  # noqa: E402
import make_icon  # noqa: E402

NAME = "AIVideoStudio"
APP_NAME = "AI 知识视频工坊"
DIST = ROOT / "dist"
INSTALL_NOTE = f"""{APP_NAME} {__version__} 安装说明

macOS（Apple 芯片）
1. 把「{APP_NAME}.app」拖进「应用程序」文件夹。
2. 第一次打开：在「应用程序」里右键（或按住 Control 点按）→「打开」→ 再点「打开」。
   如果仍被拦截：「系统设置 → 隐私与安全性」，在下方点「仍要打开」。
3. 之后在「应用程序」或启动台里双击即可。
4. 退出：⌘Q、菜单「退出」、程序坞右键「退出」或点窗口红色按钮关闭最后一个窗口，后台和子进程都会结束。
5. 卸载：把「{APP_NAME}.app」拖进废纸篓。数据在 ~/Library/Application Support/AIVideoStudio/，不会被删除。

Windows（x64）
1. 双击 AIVideoStudio-Setup-{__version__}-x64.exe。SmartScreen 提示时点「更多信息 → 仍要运行」。
2. 装到 %LOCALAPPDATA%\\Programs\\AIVideoStudio\\，不需要管理员权限；开始菜单和桌面有快捷方式。
3. 退出：点窗口右上角 ×、Alt+F4 或任务栏右键「关闭窗口」，后台和子进程都会结束。
4. 卸载：「设置 → 应用」或开始菜单里的「卸载{APP_NAME}」。数据在 %APPDATA%\\AIVideoStudio\\，保留。
5. 免安装 zip：解压后双击 AIVideoStudio.exe。

窗口打不开时会自动改用默认浏览器，原因写在数据目录的 studio.log。
详细说明见应用内「使用指南」。
"""


def stage_sources():
    stage = ROOT / "build" / "source_stage"
    shutil.rmtree(stage, ignore_errors=True)
    files = guide.list_sources()
    for f in files:
        dst = stage / f["path"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / f["path"], dst)
    print("打包手写源码：", len(files), "个文件")
    print("打包文档：", [d["path"] for d in guide.list_docs()])
    return stage


def pyinstaller(icon_dir):
    import PyInstaller.__main__

    sep = os.pathsep
    stage = stage_sources()
    icon = icon_dir / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
    args = [
        str(ROOT / "run.py"),
        "--name", NAME,
        "--noconfirm",
        "--clean",
        "--onedir",
        "--windowed",  # 窗口程序，没有控制台
        "--icon", str(icon),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build" / "pyinstaller"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'studio' / 'web'}{sep}studio/web",
        "--add-data", f"{ROOT / 'README.md'}{sep}.",
        "--add-data", f"{ROOT / 'docs'}{sep}docs",
        "--add-data", f"{stage}{sep}source",
        "--collect-all", "imageio_ffmpeg",
        "--collect-all", "webview",
        "--collect-data", "certifi",
        "--hidden-import", "studio.server",
        "--hidden-import", "psutil",
        "--exclude-module", "tkinter",
    ]
    if sys.platform == "darwin":
        args += ["--osx-bundle-identifier", "app.aivideostudio"]
    PyInstaller.__main__.run(args)
    return DIST / (f"{NAME}.app" if sys.platform == "darwin" else NAME)


def check_bundle(root):
    """自检：文档和源码确实进了包。"""
    candidates = [root / "_internal", root, root / "Contents" / "Resources", root / "Contents" / "Frameworks"]
    data = next((c for c in candidates if (c / "docs").is_dir()), None)
    if data is None:
        sys.exit(f"打包缺少 docs/：{root}")
    missing = [d["path"] for d in guide.list_docs() if not (data / d["path"]).is_file()]
    missing += ["source/" + f["path"] for f in guide.list_sources() if not (data / "source" / f["path"]).is_file()]
    if missing:
        sys.exit("打包缺少文件：" + ", ".join(missing))
    print("文档和源码已打包：", data)


def finish_mac(app):
    plist_path = app / "Contents" / "Info.plist"
    info = plistlib.loads(plist_path.read_bytes())
    info.update({
        "CFBundleDisplayName": APP_NAME, "CFBundleName": APP_NAME,
        "CFBundleShortVersionString": __version__, "CFBundleVersion": __version__,
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.video",
    })
    plist_path.write_bytes(plistlib.dumps(info))
    # 改了 Info.plist 必须重新签名（没有开发者证书时用临时签名）
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(app)], check=True)
    final = DIST / f"{APP_NAME}.app"
    if final.exists():
        shutil.rmtree(final)
    app.rename(final)
    check_bundle(final)
    staging = ROOT / "build" / "dmg"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    subprocess.run(["ditto", str(final), str(staging / final.name)], check=True)
    (staging / "Applications").symlink_to("/Applications")
    (staging / "安装说明（先看这里）.txt").write_text(INSTALL_NOTE, encoding="utf-8")
    dmg = DIST / f"{NAME}-{__version__}-macOS-{platform.machine()}.dmg"
    dmg.unlink(missing_ok=True)
    subprocess.run(["hdiutil", "create", "-volname", APP_NAME, "-srcfolder", str(staging), "-ov", "-format", "UDZO", str(dmg)], check=True)
    print("完成：", final, dmg)


def find_makensis():
    for c in (shutil.which("makensis"), r"C:\Program Files (x86)\NSIS\makensis.exe", r"C:\Program Files\NSIS\makensis.exe"):
        if c and Path(c).exists():
            return c
    return None


def finish_windows(folder, icon_dir):
    check_bundle(folder)
    shutil.copy(ROOT / "README.md", folder / "README.md")
    (folder / "安装说明.txt").write_text(INSTALL_NOTE, encoding="utf-8-sig")
    portable = DIST / f"{NAME}-{__version__}-windows-x64"
    shutil.make_archive(str(portable), "zip", DIST, NAME)
    print("免安装版：", f"{portable}.zip")
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
    if sys.platform == "darwin":
        finish_mac(built)
    elif sys.platform == "win32":
        finish_windows(built, icon_dir)
    else:
        check_bundle(built)
        print("完成：", built)


if __name__ == "__main__":
    main()
