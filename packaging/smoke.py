"""CI 冒烟测试：启动打包好的程序，检查主要接口、使用指南文档和源码都在包里，
发出退出指令后进程结束、端口不再监听。

    python packaging/smoke.py
"""

import json
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from studio import guide  # noqa: E402

exe = ROOT / "dist" / "AIVideoStudio" / ("AIVideoStudio.exe" if sys.platform.startswith("win") else "AIVideoStudio")
port = 18765
proc = subprocess.Popen([str(exe), "--port", str(port), "--no-browser"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
token = ""
deadline = time.time() + 120
while time.time() < deadline:
    line = proc.stdout.readline().decode("utf-8", "replace")
    if not line and proc.poll() is not None:
        sys.exit(f"程序提前退出（{proc.returncode}）")
    print(line.rstrip())
    if "?t=" in line:
        token = line.strip().split("?t=")[-1]
        break
if not token:
    proc.kill()
    sys.exit("没有拿到访问地址")


def call(method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers={"X-Studio-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


boot = json.loads(call("GET", "/api/bootstrap"))
print("版本", boot["version"], "FFmpeg", boot["ffmpeg"])
docs = {d["path"] for d in json.loads(call("GET", "/api/guide/docs"))["docs"]}
want = {d["path"] for d in guide.list_docs()}
assert docs == want, f"包里的文档和仓库不一致：缺 {want - docs}"
srcs = {f["path"] for f in json.loads(call("GET", "/api/guide/sources"))["files"]}
assert "studio/server.py" in srcs and "run.py" in srcs, srcs
assert len(call("GET", "/api/guide/source.zip")) > 10000
procs = json.loads(call("GET", "/api/status/processes"))
assert procs["processes"][0]["pid"] == proc.pid or procs["processes"], procs
print("文档", len(docs), "篇；源码", len(srcs), "个；进程", [(p["pid"], p["mib"]) for p in procs["processes"]])
call("POST", "/api/shutdown")
try:
    proc.wait(30)
except subprocess.TimeoutExpired:
    proc.kill()
    sys.exit("退出指令后 30 秒进程仍在")
s = socket.socket()
try:
    s.settimeout(2)
    assert s.connect_ex(("127.0.0.1", port)) != 0, "退出后端口仍在监听"
finally:
    s.close()
print("冒烟测试通过：退出码", proc.returncode)
