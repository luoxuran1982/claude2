"""打包产物冒烟测试（CI 用）：启动打包好的程序（服务模式），检查主要接口、
使用指南文档和源码都在包里，然后发退出指令，确认进程退出、端口不再监听。

    python packaging/smoke.py dist/SentimentMonitor/SentimentMonitor.exe
    python packaging/smoke.py dist/SentimentMonitor.app/Contents/MacOS/SentimentMonitor
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = 18848


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=5) as r:
        return r.read()


def main():
    exe = sys.argv[1]
    data = tempfile.mkdtemp(prefix="sm-smoke-")
    p = subprocess.Popen([exe, "--no-browser", "--port", str(PORT), "--data-dir", data])
    try:
        for _ in range(60):
            try:
                ping = json.loads(get("/api/ping"))
                break
            except Exception:  # noqa: BLE001
                time.sleep(1)
        else:
            raise SystemExit("程序没有启动")
        print("ping:", ping)
        assert b"<html" in get("/").lower()
        docs = json.loads(get("/api/guide/docs"))["docs"]
        want = 1 + len(list((ROOT / "docs").rglob("*.md")))
        print("使用指南文档：", [d["path"] for d in docs])
        assert len(docs) == want, f"包里的文档 {len(docs)} 篇，仓库里 {want} 篇"
        srcs = json.loads(get("/api/guide/sources"))["files"]
        paths = [f["path"] for f in srcs]
        print("源码文件：", len(paths))
        assert "monitor/server.py" in paths and "run.py" in paths, "包里缺少源码原文件"
        text = json.loads(get("/api/guide/source?path=monitor/server.py"))["text"]
        assert "class App" in text
        assert len(get("/api/guide/source.zip")) > 10000
        procs = json.loads(get("/api/processes"))
        print("进程：", procs["processes"])
        assert procs["processes"][0]["pid"] == p.pid or procs["processes"][0]["pid"] > 0
        req = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/shutdown", data=b"{}", method="POST",
                                     headers={"X-Monitor": "1", "Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5).read()
        p.wait(timeout=30)
        print("退出码：", p.returncode)
        s = socket.socket()
        try:
            busy = s.connect_ex(("127.0.0.1", PORT)) == 0
        finally:
            s.close()
        assert not busy, "退出后端口仍在监听"
        print("冒烟测试通过")
    finally:
        if p.poll() is None:
            p.kill()
            raise SystemExit("程序没有在退出指令后结束")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
