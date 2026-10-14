"""工作进程：读 jobs/<编号>/job.json，执行一个任务，把进度、日志和结果写回同一目录后退出。

由主进程启动：QlibStudio --worker <job.json> --parent <主进程 PID>（源码运行时是 python run.py --worker …）。
任务种类见 jobs.KINDS。异常收尾：每 2 秒检查主进程（PID + 启动时间，防止 PID 被复用），
主进程崩溃或被强制结束后，工作进程在 2 秒内自行退出。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

from . import paths
from .data import datasets


def _parent_alive_checker(parent_pid: int):
    try:
        import psutil
        proc = psutil.Process(parent_pid)
        created = proc.create_time()

        def alive() -> bool:
            try:
                return proc.is_running() and proc.create_time() == created
            except psutil.Error:
                return False
        return alive
    except Exception:  # noqa: BLE001
        if sys.platform == "win32":
            return lambda: True

        def alive_posix() -> bool:
            try:
                os.kill(parent_pid, 0)
                return os.getppid() == parent_pid
            except OSError:
                return False
        return alive_posix


def start_watchdog(parent_pid: int, interval: float = 2.0) -> None:
    if parent_pid <= 0:
        return
    alive = _parent_alive_checker(parent_pid)

    def loop():
        while True:
            time.sleep(interval)
            if not alive():
                print(f"[Qlib 量化研究台] 主进程（PID {parent_pid}）已不在，工作进程自行退出", flush=True)
                os._exit(3)
    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


class Reporter:
    """进度写进 progress.json（覆盖写，带最近几步的说明）。"""

    def __init__(self, job_dir: Path):
        self.file = job_dir / "progress.json"
        self.steps: list[dict] = []
        self.last = 0.0
        self.last_msg = ""

    def __call__(self, p: float, msg: str) -> None:
        p = max(0.0, min(1.0, float(p)))
        now = time.time()
        new = msg != self.last_msg
        if new:
            self.steps.append({"t": time.strftime("%H:%M:%S"), "msg": msg})
            self.steps = self.steps[-40:]
            self.last_msg = msg
            print(f"[进度 {p * 100:5.1f}%] {msg}", flush=True)
        if new or now - self.last > 0.25:
            self.last = now
            datasets.write_json(self.file, {"p": round(p, 4), "msg": msg, "steps": self.steps, "time": now})


def _futu_factory():
    if os.environ.get("QLIBSTUDIO_FAKE_FUTU") == "1":
        from .data import futu_fake
        return futu_fake.factory
    return None


def _new_dataset_dir(root: Path) -> Path:
    d = datasets.root(root) / datasets.new_id("d")
    d.mkdir(parents=True)
    return d


def _fail_cleanup(d: Path | None) -> None:
    import shutil
    if d is not None and d.exists() and not (d / "meta.json").exists():
        shutil.rmtree(d, ignore_errors=True)


def execute(job: dict, rep: Reporter) -> dict:  # noqa: C901
    kind, p = job["kind"], job.get("params") or {}
    root = Path(job["data_root"])
    from .data import community, convert, futu, quality, sources
    ds_dir = None
    try:
        if kind == "demo":
            n, years = int(p.get("n_stocks", 300)), float(p.get("years", 6))
            if not (20 <= n <= 1000 and 1.5 <= years <= 15):
                raise ValueError("演示数据：股票数 20~1000，年数 1.5~15")
            rep(0.02, f"生成模拟 A 股：{n} 只 × {years:g} 年")
            series = sources.from_demo(n, years, seed=int(p.get("seed", 7)))
            ds_dir = _new_dataset_dir(root)
            meta = convert.build(series, ds_dir, {"title": p.get("title") or f"演示数据（模拟 A 股 {n} 只 × {years:g} 年）",
                                                  "source": "demo", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                  "note": "模拟数据，不是真实行情；用来跑通流程和测试"}, rep)
            return {"ok": True, "dataset": meta, "summary": f"已生成数据集「{meta['title']}」：{meta['n_stocks']} 只股票，{meta['start']} ~ {meta['end']}"}
        if kind == "import":
            files = [str(x) for x in (p.get("paths") or []) if str(x).strip()]
            if not files:
                raise ValueError("请填写要导入的文件或文件夹路径")
            rep(0.01, f"扫描 {len(files)} 个路径")
            series, problems = sources.from_files(files, rep)
            mode = p.get("adjust", "auto")
            if mode == "adjusted":
                for s in series:
                    s.adjusted = True
            elif mode == "raw":
                for s in series:
                    s.adjusted = False
            if not series:
                raise ValueError("没有读到任何日线数据。" + "；".join(problems[:5]))
            ds_dir = _new_dataset_dir(root)
            src = "tdx" if any(str(s.source).lower().endswith(".day") for s in series) else "csv"
            meta = convert.build(series, ds_dir, {"title": p.get("title") or f"导入数据（{len(series)} 只）", "source": src,
                                                  "created": time.strftime("%Y-%m-%d %H:%M:%S"), "paths": files,
                                                  "read_problems": problems[:50]}, rep)
            return {"ok": True, "dataset": meta, "problems": problems[:50],
                    "summary": f"已导入 {meta['n_symbols']} 只（{meta['start']} ~ {meta['end']}）" + (f"，{len(problems)} 个文件读取有问题" if problems else "")}
        if kind == "futu-check":
            rep(0.1, f"连接 OpenD {p.get('host', futu.HOST)}:{p.get('port', futu.PORT)}")
            q = futu.check(root, p.get("host", futu.HOST), int(p.get("port", futu.PORT)), factory=_futu_factory())
            return {"ok": True, "quota": q, "fake": _futu_factory() is not None,
                    "summary": f"已连上 OpenD：历史 K 线额度已用 {q['used']} / 共 {q['total']}，剩余 {q['remain']}"}
        if kind == "futu-download":
            codes = futu.parse_codes(str(p.get("codes", "")))
            if not codes:
                raise ValueError("请填写股票代码，例如 600000, 000001, 300750")
            start, end = str(p.get("start") or "2019-01-01"), str(p.get("end") or time.strftime("%Y-%m-%d"))
            r = futu.download(root, codes, start, end, p.get("host", futu.HOST), int(p.get("port", futu.PORT)),
                              factory=_futu_factory(), progress=lambda x, m: rep(0.05 + 0.6 * x, m))
            got = [c for c in codes if c not in {f["code"] for f in r["failed"]}]
            out = {"ok": True, "download": r, "fake": _futu_factory() is not None}
            if p.get("build", True) and got:
                series = futu.series_from_cache(root, got, start, end)
                if series:
                    ds_dir = _new_dataset_dir(root)
                    meta = convert.build(series, ds_dir, {"title": p.get("title") or f"富途日 K（{len(series)} 只）",
                                                          "source": "futu", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                                                          "codes": got, "fake_opend": out["fake"]},
                                         lambda x, m: rep(0.65 + 0.35 * x, m))
                    out["dataset"] = meta
            out["summary"] = (f"下载 {len(r['downloaded'])} 只，缓存命中（没请求）{len(r['skipped'])} 只，失败 {len(r['failed'])} 只"
                              + (f"；额度剩余 {r['quota_after']['remain']}" if r.get("quota_after") else ""))
            return out
        if kind in ("community-download", "community-import"):
            if kind == "community-download":
                archive = community.download(root / "downloads", str(p.get("url") or community.URL), rep)
            else:
                archive = Path(str(p.get("path", "")).strip()).expanduser()
            tag = ""
            tagf = root / "downloads" / "release-tag.txt"
            if kind == "community-download" and tagf.is_file():
                tag = tagf.read_text(encoding="utf-8").strip()
            ds_dir = _new_dataset_dir(root)
            meta = community.import_archive(archive, ds_dir, {
                "title": p.get("title") or ("社区 A 股数据集" + (f"（{tag}）" if tag else "")), "source": "community",
                "created": time.strftime("%Y-%m-%d %H:%M:%S"), "release": tag, "url": community.URL if kind == "community-download" else None,
                "archive": str(archive)}, rep)
            if kind == "community-download" and p.get("delete_archive", True):
                archive.unlink(missing_ok=True)
            return {"ok": True, "dataset": meta, "summary": f"已导入社区数据集：{meta['n_symbols']} 只，{meta['start']} ~ {meta['end']}"}
        if kind == "quality":
            d = datasets.path(root, str(p.get("dataset", "")))
            q = quality.check(d, max_symbols=int(p.get("max_symbols") or 0) or None, progress=rep)
            old = datasets.read_json(d / "quality.json", {}) or {}
            q["import"] = old.get("import", [])
            datasets.write_json(d / "quality.json", q)
            return {"ok": True, "quality": q["summary"], "summary": f"质量检查：{q['summary']['level']}"}
        if kind == "experiment":
            from . import experiment
            run_id = datasets.new_id("r")
            out_dir = root / "runs" / run_id
            res = experiment.run(p.get("config") or {}, root, out_dir, rep)
            m = res["metrics"]
            return {"ok": True, "run_id": run_id,
                    "summary": f"「{res['name']}」完成：年化 {fmt_pct(m.get('annual_return'))}，最大回撤 {fmt_pct(m.get('max_drawdown'))}，"
                               f"RankIC {res['ic'].get('rank_ic_mean')}，用时 {res['elapsed']} 秒"}
        raise ValueError(f"未知任务：{kind}")
    except BaseException:
        _fail_cleanup(ds_dir)
        raise


def fmt_pct(x) -> str:
    return "—" if x is None else f"{x * 100:.1f}%"


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="QlibStudio --worker")
    ap.add_argument("job")
    ap.add_argument("--parent", type=int, default=0)
    a = ap.parse_args(argv)
    job_file = Path(a.job)
    jd = job_file.parent
    log = open(jd / "log.txt", "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stderr = log
    os.environ.setdefault("TQDM_DISABLE", "1")  # 上游 dump_bin / 回测的进度条不写进日志
    start_watchdog(a.parent)
    job = json.loads(job_file.read_text(encoding="utf-8"))
    print(f"[Qlib 量化研究台] 工作进程 PID {os.getpid()} 开始：{job['title']}（{job['id']}）", flush=True)
    paths.ensure_upstream_importable()
    rep = Reporter(jd)
    code = 0
    from .data.community import DownloadError
    from .data.futu import FutuError
    try:
        res = execute(job, rep)
        rep(1.0, res.get("summary", "完成"))
    except (ValueError, LookupError, FutuError, DownloadError, RuntimeError) as exc:
        traceback.print_exc()
        res = {"ok": False, "error": str(exc)}
        code = 1
    except BaseException as exc:  # noqa: BLE001
        traceback.print_exc()
        res = {"ok": False, "error": f"内部错误：{type(exc).__name__}: {exc}（完整堆栈见任务日志）"}
        code = 2
    datasets.write_json(jd / "result.json", res)
    print(f"[Qlib 量化研究台] 工作进程结束：{'成功' if res.get('ok') else res.get('error')}", flush=True)
    try:
        log.flush()
    except Exception:  # noqa: BLE001
        pass
    os._exit(code)  # 不等上游可能残留的非守护线程（futu-api、joblib）
