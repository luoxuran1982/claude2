"""冒烟测试（CI 在 macOS 和 Windows 上对打好的程序运行；也可对源码运行）。

    python packaging/smoke.py dist/QlibStudio/QlibStudio.exe                       # Windows
    python packaging/smoke.py "dist/Qlib 量化研究台.app/Contents/MacOS/QlibStudio"   # macOS
    python packaging/smoke.py --source                                              # 源码（python run.py）

检查：能启动；页面和接口可用；文档和源码在包里（含源码 zip、路径穿越被拦截）；用小样例数据（模拟 A 股）
在工作进程里跑通一次 LightGBM 实验（Qlib 因子 → 训练 → 回测 → 报告）；工作进程出现在进程列表、结束后消失；
任务可以取消；模拟 OpenD 查额度和下载；导入 QuantLab 交换格式样例并对比；退出后所有进程结束、端口不再监听。
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

    def get(self, path: str, raw=False, ok=True):
        try:
            with urllib.request.urlopen(self.base + path, timeout=60) as r:
                data, ctype, code = r.read(), r.headers.get("Content-Type", ""), r.status
        except urllib.error.HTTPError as e:
            if ok:
                raise
            return e.code
        if not ok:
            return code
        if raw:
            return data
        return json.loads(data) if "json" in ctype else data.decode("utf-8")

    def post(self, path: str, body: dict):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-QlibStudio-Token": self.token})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            return {"_status": e.code, **json.loads(e.read() or b"{}")}

    def run_job(self, kind: str, params: dict, timeout: int = 900, seen_pids: set | None = None) -> dict:
        j = self.post("/api/job/start", {"kind": kind, "params": params})
        assert j.get("id"), j
        if seen_pids is not None:
            seen_pids.add(j["pid"])
        listed = False
        for _ in range(timeout):
            v = self.get("/api/job?id=" + j["id"])
            if v["running"] and not listed:
                procs = self.get("/api/processes")
                listed = j["pid"] in [p["pid"] for p in procs["processes"]]
            if not v["running"]:
                break
            time.sleep(1)
        else:
            raise SystemExit(f"{kind} 超过 {timeout} 秒没有结束")
        if not v["result"].get("ok"):
            print(self.get("/api/job-log?id=" + j["id"]))
            raise SystemExit(f"{kind} 失败：{v['result'].get('error')}")
        procs = self.get("/api/processes")
        assert j["pid"] not in [p["pid"] for p in procs["processes"]], "工作进程结束后仍在进程列表里"
        print(f"  {kind}：{v['result'].get('summary')}（工作进程 PID {j['pid']}{'，运行时出现在进程列表' if listed else ''}）")
        return v


def main() -> None:
    args = sys.argv[1:]
    source = "--source" in args
    exe = next((a for a in args if not a.startswith("--")), None)
    cmd = [sys.executable, str(ROOT / "run.py")] if source else [exe]
    data = Path(tempfile.mkdtemp(prefix="qlibstudio-smoke-"))
    port = free_port()
    env = dict(os.environ, QLIBSTUDIO_DATA=str(data), QLIBSTUDIO_FAKE_FUTU="1")
    print("程序：", cmd, "\n数据目录：", data, "\n端口：", port, flush=True)
    t0 = time.time()
    proc = subprocess.Popen(cmd + ["--serve-only", "--port", str(port)], env=env)
    pids: set[int] = set()
    ok = False
    try:
        for _ in range(240):
            if listening(port):
                break
            if proc.poll() is not None:
                raise SystemExit(f"程序启动后就退出了（退出码 {proc.returncode}）")
            time.sleep(1)
        else:
            raise SystemExit("240 秒内没有启动")
        print(f"启动用时 {time.time() - t0:.1f} 秒")
        url_file = data / ".serve-only-url"
        for _ in range(30):
            if url_file.is_file():
                break
            time.sleep(0.5)
        token = url_file.read_text(encoding="utf-8").split("t=", 1)[1]
        c = Client(port, token)
        st = c.get("/api/status")
        print("版本：", st["version"], "上游 Qlib：", st["upstream"]["version"], st["upstream"]["commit"])
        page = c.get("/", raw=True).decode("utf-8")
        assert "Qlib 量化研究台" in page and "zoom-select" in page, "首页内容不对"
        for f in ("/app.js", "/guide.js", "/md.js", "/zoom.js", "/style.css", "/icon.svg", "/vendor/echarts.min.js"):
            c.get(f, raw=True)
        cat = c.get("/api/catalog")
        assert {"lgbm", "xgb", "catboost", "linear"} <= set(cat["models"]), cat["models"]

        # 使用指南：文档、源码、附件都在包里
        g = c.get("/api/guide")
        docs = {d["path"] for d in g["docs"]}
        need = {"README.md", "UPSTREAM.md", "docs/00 项目阅读入口.md", "docs/01 使用指南.md", "docs/02 软件框架与系统架构.md",
                "docs/03 源码阅读与重建指南.md", "docs/04 与 QuantLab 的对比.md"}
        assert need <= docs, f"缺少文档：{need - docs}"
        srcs = {s["path"] for s in g["sources"]}
        for s in ("qlibstudio/app.py", "qlibstudio/experiment.py", "upstream/qlib/__init__.py", "upstream/LICENSE",
                  "upstream/scripts/dump_bin.py", "ci/qlib-studio.yml"):
            assert s in srcs, f"缺少源码 {s}"
        assert "怎样缩放界面" in c.get("/api/guide/doc?path=" + urllib.parse.quote("docs/01 使用指南.md"))
        assert "def main" in c.get("/api/guide/source?path=qlibstudio/app.py")
        assert c.get("/api/guide/source?path=" + urllib.parse.quote("../../etc/passwd"), ok=False) == 404, "路径穿越没有被拦截"
        z = zipfile.ZipFile(BytesIO(c.get("/api/guide/source.zip", raw=True)))
        names = z.namelist()
        assert "qlib-studio/upstream/LICENSE" in names and "qlib-studio/qlibstudio/server.py" in names
        print(f"使用指南：{len(g['docs'])} 篇文档，{len(g['sources'])} 个源码文件，{len(g['assets'])} 个附件；源码 zip {len(names)} 个文件")

        # 小样例数据 → LightGBM 实验（在工作进程里跑上游 Qlib 全流程）
        print("任务：")
        v = c.run_job("demo", {"n_stocks": 60, "years": 3, "seed": 11}, seen_pids=pids)
        ds = v["result"]["dataset"]["id"]
        v = c.run_job("experiment", {"config": {"dataset": ds, "model": "lgbm", "quick": True, "strategy": {"topk": 10, "n_drop": 2}}},
                      seen_pids=pids)
        run_id = v["result"]["run_id"]
        r = c.get("/api/run?id=" + run_id)
        assert r["metrics"]["days"] > 50 and r["ic"]["n_days"] > 50, r["metrics"]
        trades = c.get("/api/run/trades?id=" + run_id)
        assert trades and all(t["shares"] % 100 == 0 for t in trades), "成交明细为空或不是 100 股整数倍"
        assert c.get("/api/run/positions?id=" + run_id), "没有持仓记录"
        print(f"  报告：年化 {r['metrics']['annual_return']}，最大回撤 {r['metrics']['max_drawdown']}，RankIC {r['ic']['rank_ic_mean']}，"
              f"成交 {len(trades)} 笔")

        # 取消：开一个实验，确认工作进程起来后取消
        j = c.post("/api/job/start", {"kind": "experiment", "params": {"config": {"dataset": ds, "model": "catboost"}}})
        pids.add(j["pid"])
        time.sleep(4)
        assert c.post("/api/job/cancel", {})["cancelled"], "取消失败"
        v = c.get("/api/job?id=" + j["id"])
        assert not v["running"] and v["status"] == "已取消", v["status"]
        assert j["pid"] not in [p["pid"] for p in c.get("/api/processes")["processes"]], "取消后工作进程还在"
        print("  取消：工作进程已结束")

        # 模拟 OpenD：额度、下载、缓存命中
        v = c.run_job("futu-check", {}, seen_pids=pids)
        assert v["result"]["quota"]["total"] == 100
        v = c.run_job("futu-download", {"codes": "600000, 000001", "start": "2024-01-01", "end": "2026-09-30"}, seen_pids=pids)
        assert len(v["result"]["download"]["downloaded"]) == 2, v["result"]["download"]
        v = c.run_job("futu-download", {"codes": "600000", "start": "2024-06-01", "end": "2026-09-30", "build": False}, seen_pids=pids)
        assert v["result"]["download"]["skipped"] == ["SH.600000"], "缓存没有命中"

        # 真实 futu-api（安装包里能加载）：连一个「不是 OpenD」的端口，应在握手超时后给出中文原因
        dummy = socket.socket()
        dummy.bind(("127.0.0.1", 0))
        dummy.listen()
        j = c.post("/api/job/start", {"kind": "futu-check", "params": {"force_real": True, "port": dummy.getsockname()[1]}})
        pids.add(j["pid"])
        for _ in range(90):
            v = c.get("/api/job?id=" + j["id"])
            if not v["running"]:
                break
            time.sleep(1)
        dummy.close()
        assert not v["result"]["ok"] and "握手" in v["result"]["error"], v["result"]
        print("  真实 futu-api：已加载；端口上不是 OpenD 时提示：", v["result"]["error"][:40], "…")

        # 与 QuantLab 对比：导入样例交换格式并和刚才的实验并排
        sample = c.get("/api/guide/file?path=" + urllib.parse.quote("samples/quantlab-result-example.json"), raw=True)
        imp = c.post("/api/compare/import", {"content": sample.decode("utf-8")})
        assert imp.get("key", "").startswith("file:"), imp
        cmp_ = c.get("/api/compare?keys=" + urllib.parse.quote(f"{imp['key']},run:{run_id}"))
        assert len(cmp_["items"]) == 2, cmp_
        # QuantLab 1.2.0 真实导出的文件（quantlab.experiment-result 1.0）也能直接导入
        ql = c.get("/api/guide/file?path=" + urllib.parse.quote("samples/quantlab-1.2.0-export-demo.json"), raw=True)
        imp2 = c.post("/api/compare/import", {"content": ql.decode("utf-8")})
        assert imp2.get("app") == "QuantLab" and imp2.get("key", "").startswith("file:"), imp2
        cmp2 = c.get("/api/compare?keys=" + urllib.parse.quote(f"{imp2['key']},run:{run_id}"))
        assert len(cmp2["items"]) == 2 and cmp2["items"][0]["settings"]["top_k"] == 20, cmp2
        bad = c.post("/api/compare/import", {"content": json.dumps({"perf": {}, "ic": {}})})
        assert bad.get("_status") == 400 and "导出实验结果" in bad.get("error", ""), bad
        print("  对比：交换格式样例和 QuantLab 1.2.0 导出文件都已导入并与实验并排；格式不对时给出中文原因")

        procs = c.get("/api/processes")
        print("进程：", [(p["pid"], p["role"][:16], p["mem_mib"]) for p in procs["processes"]], procs["metric"], procs["unit"])

        # 退出：进程结束、端口释放、工作进程不残留
        c.post("/api/quit", {})
        proc.wait(60)
        time.sleep(1)
        assert not listening(port), "退出后端口仍在监听"
        try:
            import psutil
            alive = [p for p in pids if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]
        except ImportError:
            alive = []
        assert not alive, f"退出后工作进程仍在：{alive}"
        print(f"退出：主进程退出码 {proc.returncode}，端口 {port} 已释放，{len(pids)} 个工作进程都已结束")
        ok = True
    finally:
        if proc.poll() is None:
            proc.kill()
        if not ok:
            log = data / "qlibstudio.log"
            if log.is_file():
                print("---- qlibstudio.log ----\n" + log.read_text(encoding="utf-8", errors="replace")[-6000:])
    print(f"冒烟测试通过（共 {time.time() - t0:.0f} 秒）")


if __name__ == "__main__":
    main()
