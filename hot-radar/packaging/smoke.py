"""安装包冒烟测试（CI 在 macOS 和 Windows 上对打好的程序运行；也可对源码运行）。

    python packaging/smoke.py dist/HotRadar/HotRadar.exe            # Windows
    python packaging/smoke.py "dist/热点雷达.app/Contents/MacOS/HotRadar"  # macOS
    python packaging/smoke.py --source                               # 源码（python run.py）
    python packaging/smoke.py --source --live                        # 另外真实访问 newsnow 抓一次（需要网络）

检查：能启动、主要页面和接口可用、文档和源码在包里、离线演示跑通上游全流程并生成报告、
工作进程出现在进程列表、MCP 服务能起停、退出后所有进程结束且端口不再监听。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from io import BytesIO
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", port)) == 0


class Client:
    def __init__(self, port: int, token: str = ""):
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def get(self, path: str, raw=False):
        """raw=True 返回字节；JSON 接口返回对象；其余（文档、源码、日志）返回文字。"""
        with urllib.request.urlopen(self.base + path, timeout=30) as r:
            data = r.read()
            ctype = r.headers.get("Content-Type", "")
        if raw:
            return data
        return json.loads(data) if "json" in ctype else data.decode("utf-8")

    def post(self, path: str, body: dict):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-HotRadar-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read() or b"{}")}


def main() -> None:
    args = sys.argv[1:]
    source = "--source" in args
    live = "--live" in args
    exe = next((a for a in args if not a.startswith("--")), None)
    cmd = [sys.executable, str(ROOT / "run.py")] if source else [exe]
    data = Path(tempfile.mkdtemp(prefix="hotradar-smoke-"))
    port = free_port()
    env = dict(os.environ, HOTRADAR_DATA=str(data))
    print("程序：", cmd, "\n数据目录：", data, "\n端口：", port, flush=True)
    proc = subprocess.Popen(cmd + ["--serve-only", "--port", str(port)], env=env)
    worker_pids: set[int] = set()
    ok = False
    try:
        for _ in range(180):
            if listening(port):
                break
            if proc.poll() is not None:
                raise SystemExit(f"程序启动后就退出了（退出码 {proc.returncode}）")
            time.sleep(1)
        else:
            raise SystemExit("180 秒内没有启动")
        url_file = data / ".serve-only-url"
        for _ in range(30):
            if url_file.is_file():
                break
            time.sleep(0.5)
        token = url_file.read_text(encoding="utf-8").split("t=", 1)[1]
        c = Client(port, token)
        st = c.get("/api/status")
        print("版本：", st["version"], "上游：", st["upstream"]["version"])
        page = c.get("/", raw=True).decode("utf-8")
        assert "热点雷达" in page and "zoom-select" in page, "首页内容不对"
        for f in ("/app.js", "/guide.js", "/md.js", "/zoom.js", "/style.css", "/icon.svg"):
            c.get(f, raw=True)
        cfg = c.get("/api/config")
        assert len(cfg["values"]["platforms.sources"]) == 11, "默认平台不是 11 个"
        assert (data / "config" / "config.yaml").is_file(), "首次启动没有复制默认配置"

        # 使用指南：文档、源码、附件都在包里
        g = c.get("/api/guide")
        docs = {d["path"] for d in g["docs"]}
        need = {"README.md", "UPSTREAM.md", "docs/00 项目阅读入口.md", "docs/01 使用指南.md", "docs/02 软件框架与系统架构.md",
                "docs/03 源码阅读与重建指南.md", "docs/04 与舆情监控系统的对比.md"}
        assert need <= docs, f"缺少文档：{need - docs}"
        srcs = {s["path"] for s in g["sources"]}
        for s in ("hotradar/app.py", "hotradar/worker.py", "upstream/trendradar/__main__.py", "upstream/LICENSE", "ci/hot-radar.yml"):
            assert s in srcs, f"缺少源码 {s}"
        assert "怎样缩放界面" in c.get("/api/guide/doc?path=" + urllib.parse.quote("docs/01 使用指南.md"))
        assert "def main" in c.get("/api/guide/source?path=hotradar/app.py")
        z = zipfile.ZipFile(BytesIO(c.get("/api/guide/source.zip", raw=True)))
        assert "hot-radar/upstream/LICENSE" in z.namelist() and "hot-radar/hotradar/server.py" in z.namelist()
        print(f"使用指南：{len(g['docs'])} 篇文档，{len(g['sources'])} 个源码文件，{len(g['assets'])} 个附件；源码 zip {len(z.namelist())} 个文件")

        # 离线演示：在工作进程里跑上游全流程
        r = c.post("/api/run", {"task": "demo"})
        assert r.get("started"), r
        wpid = r["job"]["pid"]
        worker_pids.add(wpid)
        procs = c.get("/api/processes")
        print("进程：", [(p["pid"], p["role"][:12], p["mem_mib"]) for p in procs["processes"]])
        assert wpid in [p["pid"] for p in procs["processes"]], "进程列表里没有工作进程"
        for _ in range(300):
            st = c.get("/api/status")
            if not st["job"]:
                break
            time.sleep(1)
        last = st["last_run"]
        print("离线演示：", last["status"], last["summary"], f"{last['duration_sec']} 秒")
        if last["status"] != "ok":
            print(c.get("/api/run-log?id=" + last["id"]))
            raise SystemExit("离线演示没有成功")
        rep = c.get("/report/demo/latest/current.html", raw=True).decode("utf-8")
        assert "热点新闻分析" in rep, "报告内容不对"
        procs = c.get("/api/processes")
        assert wpid not in [p["pid"] for p in procs["processes"]], "工作进程结束后仍在进程列表里"

        # MCP 服务能起停
        mport = free_port()
        m = c.post("/api/mcp", {"enabled": True, "port": mport})
        assert m.get("running"), m
        worker_pids.add(m["pid"])
        for _ in range(120):
            if listening(mport):
                break
            if not c.get("/api/status")["mcp"]["running"]:
                print(c.get("/api/mcp-log"))
                raise SystemExit("MCP 服务启动失败")
            time.sleep(1)
        else:
            print(c.get("/api/mcp-log"))
            raise SystemExit("MCP 服务 120 秒内没有监听端口")
        print("MCP 服务：已监听", mport)
        m = c.post("/api/mcp", {"enabled": False})
        assert not m["running"]
        for _ in range(20):
            if not listening(mport):
                break
            time.sleep(0.5)
        assert not listening(mport), "MCP 停止后端口仍在监听"

        if live:  # 真实抓取（需要能访问 newsnow）
            r = c.post("/api/run", {"task": "crawl"})
            worker_pids.add(r["job"]["pid"])
            for _ in range(600):
                st = c.get("/api/status")
                if not st["job"]:
                    break
                time.sleep(1)
            last = st["last_run"]
            print("真实抓取：", last["status"], last["summary"], last.get("platforms_failed"))
            print("\n".join(c.get("/api/run-log?id=" + last["id"]).splitlines()[-40:]))

        # 退出：进程结束、端口释放、工作进程不残留
        c.post("/api/quit", {})
        proc.wait(60)
        time.sleep(1)
        assert not listening(port), "退出后端口仍在监听"
        try:
            import psutil
            alive = [p for p in worker_pids if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]
        except ImportError:
            alive = []
        assert not alive, f"退出后工作进程仍在：{alive}"
        print(f"退出：主进程退出码 {proc.returncode}，端口 {port} 已释放，工作进程已全部结束")
        ok = True
    finally:
        if proc.poll() is None:
            proc.kill()
        if not ok:
            log = data / "hotradar.log"
            if log.is_file():
                print("---- hotradar.log ----\n" + log.read_text(encoding="utf-8", errors="replace")[-6000:])
    print("冒烟测试通过")


if __name__ == "__main__":

    main()
