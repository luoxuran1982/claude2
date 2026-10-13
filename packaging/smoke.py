"""CI 冒烟测试：启动打包好的程序（不是源码），检查

1. 只启动服务（--no-browser）：主要接口可用，使用指南的文档和手写源码都在包里；
   发出退出指令后进程结束、端口不再监听；
2. 默认的窗口模式：窗口能否创建（失败时程序会改用浏览器并写日志，这里打印出来）；
   页面「退出」后窗口关闭、进程结束、端口不再监听。

    python packaging/smoke.py
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

for stream in (sys.stdout, sys.stderr):
    try:
        stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from studio import guide  # noqa: E402

if sys.platform.startswith("win"):
    EXE = ROOT / "dist" / "AIVideoStudio" / "AIVideoStudio.exe"
elif sys.platform == "darwin":
    EXE = ROOT / "dist" / "AI 知识视频工坊.app" / "Contents" / "MacOS" / "AIVideoStudio"
else:
    EXE = ROOT / "dist" / "AIVideoStudio" / "AIVideoStudio"


def port_open(port):
    s = socket.socket()
    s.settimeout(2)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def launch(port, extra):
    tmp = Path(tempfile.mkdtemp(prefix="studio-smoke-"))
    env = dict(os.environ, STUDIO_DATA_DIR=str(tmp / "data"), STUDIO_URL_FILE=str(tmp / "url.txt"))
    proc = subprocess.Popen([str(EXE), "--port", str(port)] + extra, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 120
    while time.time() < deadline:
        if (tmp / "url.txt").is_file() and (tmp / "url.txt").read_text(encoding="utf-8").strip():
            break
        if proc.poll() is not None:
            print_log(tmp)
            sys.exit(f"程序提前退出（{proc.returncode}）")
        time.sleep(0.5)
    else:
        proc.kill()
        print_log(tmp)
        sys.exit("120 秒内没有拿到访问地址")
    token = (tmp / "url.txt").read_text(encoding="utf-8").strip().split("?t=")[-1]
    return proc, token, tmp


def print_log(tmp):
    f = tmp / "data" / "studio.log"
    if f.is_file():
        print("---- studio.log ----")
        print(f.read_text(encoding="utf-8", errors="replace"))


def call(port, token, method, path):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers={"X-Studio-Token": token})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read()


def quit_and_check(proc, port, token, tmp):
    call(port, token, "POST", "/api/shutdown")
    try:
        proc.wait(40)
    except subprocess.TimeoutExpired:
        proc.kill()
        print_log(tmp)
        sys.exit("退出指令后 40 秒进程仍在")
    time.sleep(1)
    if port_open(port):
        sys.exit("退出后端口仍在监听")
    print("已退出：退出码", proc.returncode, "，端口已释放")


# ---------------- 1. 只启动服务
port = 18765
proc, token, tmp = launch(port, ["--no-browser"])
boot = json.loads(call(port, token, "GET", "/api/bootstrap"))
print("版本", boot["version"], "FFmpeg", boot["ffmpeg"])
assert boot["ffmpeg"], "包里没有 FFmpeg"
docs = {d["path"] for d in json.loads(call(port, token, "GET", "/api/guide/docs"))["docs"]}
want = {d["path"] for d in guide.list_docs()}
assert docs == want, f"包里的文档和仓库不一致：缺 {want - docs}"
srcs = {f["path"] for f in json.loads(call(port, token, "GET", "/api/guide/sources"))["files"]}
want_src = {f["path"] for f in guide.list_sources()}
assert want_src <= srcs, f"包里缺源码：{want_src - srcs}"
assert len(call(port, token, "GET", "/api/guide/source.zip")) > 10000
procs = json.loads(call(port, token, "GET", "/api/status/processes"))
assert procs["processes"][0]["pid"] == proc.pid, procs
print("文档", len(docs), "篇；源码", len(srcs), "个；进程", [(p["pid"], p["mib"]) for p in procs["processes"]])
quit_and_check(proc, port, token, tmp)

# ---------------- 2. 默认窗口模式
port = 18766
proc, token, tmp = launch(port, [])
time.sleep(8)  # 等窗口建好（或失败后改用浏览器）
procs = json.loads(call(port, token, "GET", "/api/status/processes"))
print("窗口模式：mode =", procs["mode"], "；进程", [(p["role"], p["pid"], p["mib"]) for p in procs["processes"]])
if procs["mode"] != "window":
    print("::warning::窗口在 CI 上没有建起来，程序已按设计改用浏览器")
    print_log(tmp)
quit_and_check(proc, port, token, tmp)
print("冒烟测试通过")
