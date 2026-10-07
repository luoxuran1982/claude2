#!/usr/bin/env python3
"""启动 AI 知识视频工坊。

    python run.py              # 启动并用默认浏览器打开
    python run.py --window     # 用独立窗口打开（需要 pip install pywebview）
    python run.py --port 8765 --no-browser
"""

import argparse
import os
import sys
import threading
import webbrowser

if sys.version_info < (3, 9):
    sys.exit("需要 Python 3.9 或更高版本")

# 打包或从其他目录启动时也能找到 studio 包
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    ap = argparse.ArgumentParser(description="AI 知识视频工坊")
    ap.add_argument("--port", type=int, default=0, help="端口，默认随机")
    ap.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    ap.add_argument("--window", action="store_true", help="用 pywebview 独立窗口打开")
    args = ap.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass
    try:
        import PIL  # noqa: F401
    except ImportError:
        sys.exit("缺少 Pillow：请先运行 pip install -r requirements.txt")

    from studio import __version__, paths
    from studio.media import find_ffmpeg
    from studio.server import serve

    app, httpd = serve(args.port)
    port = httpd.server_address[1]
    url = f"http://127.0.0.1:{port}/?t={app.token}"
    print(f"AI 知识视频工坊 {__version__}")
    print(f"数据目录：{paths.data_dir()}")
    print(f"FFmpeg：{find_ffmpeg() or '未找到（pip install imageio-ffmpeg）'}")
    print(f"打开：{url}")
    print("关闭本窗口或按 Ctrl+C 退出。", flush=True)

    if args.window:
        try:
            import webview  # pywebview

            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            webview.create_window("AI 知识视频工坊", url, width=1440, height=900)
            webview.start()
            httpd.shutdown()
            return
        except ImportError:
            print("未安装 pywebview，改用浏览器打开。")

    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
