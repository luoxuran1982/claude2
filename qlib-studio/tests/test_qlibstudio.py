"""Qlib 量化研究台外壳的单元测试与集成测试（全部离线：富途 OpenD 用模拟对象，社区数据集用本机 HTTP 服务）。

    python packaging/build_ext.py          # 先编译上游 Cython 扩展（只需一次）
    python -m unittest discover -s tests -v
需要 requirements.txt 里的依赖。集成测试会真正运行上游 Qlib（因子、训练、回测）和独立的工作进程。
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qlibstudio import __version__, compare, experiment, guide, paths, procinfo  # noqa: E402
from qlibstudio.data import community, convert, datasets, futu, quality, readers, sources  # noqa: E402
from qlibstudio.data.futu_fake import FakeQuoteContext, fake_module  # noqa: E402
from qlibstudio.server import serve  # noqa: E402

_SHARED: dict = {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def shared_demo() -> tuple[Path, str]:
    """整个测试只生成一次的小演示数据集（60 只 × 3 年），给多个测试共用。"""
    if "root" not in _SHARED:
        root = Path(tempfile.mkdtemp(prefix="qlibstudio-shared-"))
        d = datasets.root(root) / "demo1"
        d.mkdir()
        convert.build(sources.from_demo(60, 3.0, seed=3), d, {"title": "测试演示", "source": "demo"})
        _SHARED["root"] = root
    return _SHARED["root"], "demo1"


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="qlibstudio-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PathsTest(unittest.TestCase):
    def test_data_dir_per_platform(self):
        home = Path("/home/u")
        self.assertEqual(paths.data_dir("darwin", {}, home), home / "Library" / "Application Support" / "QlibStudio")
        self.assertEqual(paths.data_dir("win32", {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, home),
                         Path(r"C:\Users\u\AppData\Local") / "QlibStudio")
        self.assertEqual(paths.data_dir("linux", {}, home), home / ".local" / "share" / "QlibStudio")
        self.assertEqual(paths.data_dir("win32", {"QLIBSTUDIO_DATA": "/x"}, home), Path("/x"))


class ConvertTest(TempDir):
    def test_demo_to_qlib_format(self):
        root, ds = shared_demo()
        d = datasets.path(root, ds)
        meta = datasets.meta(root, ds)
        self.assertEqual(meta["source"], "demo")
        self.assertGreater(meta["n_stocks"], 50)
        self.assertIn("SH000001", meta["benchmarks"])
        for f in ("open", "close", "high", "low", "volume", "amount", "vwap", "factor", "change", "limit_rate"):
            self.assertIn(f, meta["fields"])
        pools = {p["id"] for p in datasets.pools(d)}
        self.assertTrue({"all", "stocks", "main"} <= pools)
        stocks = (d / "instruments" / "stocks.txt").read_text(encoding="utf-8")
        self.assertNotIn("SH000001", stocks, "指数不应进股票池")
        cal = datasets.calendar(d)
        self.assertGreater(len(cal), 600)
        # 二进制格式：第一个数是日历位置，之后是值；复权价 = 不复权价 × 因子
        some = sorted((d / "features").iterdir())[5]
        close = quality.read_bin(some / "close.day.bin", len(cal))
        self.assertTrue(np.isfinite(close).sum() > 100)

    def test_limit_rate_by_board(self):
        df = pd.DataFrame({"date": pd.to_datetime(["2020-08-20", "2020-08-21", "2020-08-24", "2020-08-25"]),
                           "open": 10.0, "high": 10.5, "low": 9.5, "close": 10.0, "volume": 1000.0, "amount": 10000.0})
        out, _ = convert.prepare("sz300001", df, adjusted=False)
        self.assertEqual(out["limit_rate"].tolist(), [0.10, 0.10, 0.20, 0.20], "创业板 2020-08-24 起 20%")
        out, _ = convert.prepare("sh600001", df, adjusted=False, name="*ST 测试")
        self.assertTrue((out["limit_rate"] == 0.05).all(), "主板 ST 5%")
        out, _ = convert.prepare("sh688001", df, adjusted=False)
        self.assertTrue((out["limit_rate"] == 0.20).all())

    def test_unadjusted_files_get_estimated_factor(self):
        """通达信 .day（不复权）里有一次 10 送 10：应识别出除权，复权后收盘价连续。"""
        dates = pd.bdate_range("2023-01-02", periods=200)
        close = np.full(200, 20.0) * np.cumprod(1 + np.random.default_rng(1).normal(0, 0.01, 200))
        close[120:] /= 2.0
        df = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                           "volume": 1e6, "amount": close * 1e6})
        tdx = self.tmp / "vipdoc" / "sh" / "lday"
        tdx.mkdir(parents=True)
        readers.write_tdx_day(tdx / "sh600123.day", df, "sh600123")
        # 再放一个中文列名的 CSV（多只股票在一个文件里）
        csv = pd.DataFrame({"代码": ["000001"] * 200 + ["300750"] * 200, "日期": list(dates.strftime("%Y-%m-%d")) * 2,
                            "开盘": 10.0, "最高": 10.2, "最低": 9.8, "收盘": 10.0, "成交量": 1000, "成交额": 1e4})
        csv.to_csv(self.tmp / "日线.csv", index=False, encoding="utf-8-sig")
        series, problems = sources.from_files([str(self.tmp / "vipdoc"), str(self.tmp / "日线.csv"), str(self.tmp / "没有.csv")])
        self.assertEqual({s.symbol for s in series}, {"sh600123", "sz000001", "sz300750"})
        self.assertTrue(any("找不到" in p for p in problems))
        d = self.tmp / "ds"
        d.mkdir()
        meta = convert.build(series, d, {"title": "导入", "source": "tdx"})
        self.assertEqual(meta["n_stocks"], 3)
        q = json.loads((d / "quality.json").read_text(encoding="utf-8"))
        adj = {x["symbol"]: x["adjust"] for x in q["import"]}
        self.assertIn("估算除权 1 次", adj["sh600123"])
        cal = datasets.calendar(d)
        c = quality.read_bin(d / "features" / "sh600123" / "close.day.bin", len(cal))
        c = c[np.isfinite(c)]
        self.assertLess(np.abs(c[1:] / c[:-1] - 1).max(), 0.06, "复权后不应再有 50% 的跳空")
        self.assertEqual(q["summary"]["suspect_exright"], 0)


class QualityTest(TempDir):
    def _write(self, d: Path, sym: str, start: int, vals, field="close"):
        f = d / "features" / sym
        f.mkdir(parents=True, exist_ok=True)
        np.hstack([start, vals]).astype("<f").tofile(f / f"{field}.day.bin")

    def test_flags(self):
        d = self.tmp
        (d / "calendars").mkdir()
        cal = pd.bdate_range("2024-01-01", periods=120).strftime("%Y-%m-%d")
        (d / "calendars" / "day.txt").write_text("\n".join(cal), encoding="utf-8")
        ok = np.linspace(10, 11, 120)
        self._write(d, "sh600001", 0, ok)
        self._write(d, "sh600001", 0, np.ones(120), "factor")
        gap = ok.copy()
        gap[60:] /= 2  # 没有复权因子的除权跳空
        self._write(d, "sh600002", 0, gap)
        susp = ok.copy()
        susp[30:60] = np.nan  # 30 天停牌
        self._write(d, "sh600003", 0, susp)
        self._write(d, "sh600004", 0, ok[:50])  # 50 天后中断
        q = quality.check(d)
        rows = {r["symbol"]: r for r in q["rows"]}
        self.assertEqual(rows["SH600002"]["suspect_exright"], 1)
        self.assertEqual(rows["SH600003"]["missing"], 30)
        self.assertEqual(rows["SH600003"]["long_suspend"], 1)
        self.assertTrue(rows["SH600004"]["stale"])
        self.assertEqual(q["summary"]["level"], "需注意")
        self.assertTrue(any("复权提示" in i["text"] for i in q["issues"]))


class FutuTest(TempDir):
    def test_codes(self):
        self.assertEqual(futu.parse_codes("600000，000001.SZ sz300750\n600000"), ["SH.600000", "SZ.000001", "SZ.300750"])
        with self.assertRaises(futu.FutuError) as e:
            futu.to_futu("830799")
        self.assertIn("北交所", str(e.exception))
        self.assertEqual(futu.missing_ranges(None, "2024-01-01", "2024-12-31"), [("2024-01-01", "2024-12-31")])
        self.assertEqual(futu.missing_ranges(("2024-03-01", "2024-06-30"), "2024-01-01", "2024-12-31"),
                         [("2024-01-01", "2024-02-29"), ("2024-07-01", "2024-12-31")])
        self.assertEqual(futu.missing_ranges(("2024-01-01", "2024-12-31"), "2024-02-01", "2024-03-01"), [])

    def test_download_cache_and_quota(self):
        ctx = FakeQuoteContext(total_quota=3, fail_codes={"SZ.000002"})
        fac = lambda h, p: (ctx, fake_module)  # noqa: E731
        r = futu.download(self.tmp, ["SH.600000", "SZ.000001", "SZ.000002"], "2024-01-01", "2024-12-31", factory=fac)
        self.assertEqual({x["code"] for x in r["downloaded"]}, {"SH.600000", "SZ.000001"})
        self.assertIn("代码无效", r["failed"][0]["reason"])
        self.assertEqual(r["quota_before"]["remain"], 3)
        n_req = len(ctx.requests)
        # 已覆盖的区间不再请求
        r2 = futu.download(self.tmp, ["SH.600000"], "2024-03-01", "2024-06-30", factory=fac)
        self.assertEqual(r2["skipped"], ["SH.600000"])
        self.assertEqual(len(ctx.requests), n_req)
        # 扩展到 2025：只请求缺的那段
        futu.download(self.tmp, ["SH.600000"], "2024-01-01", "2025-03-31", factory=fac)
        self.assertTrue(all(req[1] >= "2025-01-01" for req in ctx.requests[n_req:]), ctx.requests[n_req:])
        # 后复权因子：模拟数据中途 2:1 送转，因子从 1 变到 2，复权价连续
        df = futu.Cache(self.tmp).frame("SH.600000")
        self.assertAlmostEqual(df["factor"].iloc[0], 1.0, places=3)
        self.assertAlmostEqual(df["factor"].iloc[-1], 2.0, places=2)
        # 额度不够时直接说明原因
        with self.assertRaises(futu.FutuError) as e:
            futu.download(self.tmp, ["SH.600010", "SH.600011"], "2024-01-01", "2024-12-31", factory=fac)
        self.assertIn("额度只剩", str(e.exception))
        series = futu.series_from_cache(self.tmp, ["SH.600000", "SZ.000001"])
        d = self.tmp / "ds"
        d.mkdir()
        meta = convert.build(series, d, {"title": "富途", "source": "futu"})
        self.assertEqual(meta["n_stocks"], 2)

    def test_opend_not_running(self):
        port = free_port()  # 没有程序监听
        with self.assertRaises(futu.FutuError) as e:
            futu.probe("127.0.0.1", port, timeout=1)
        self.assertIn("连不上富途 OpenD", str(e.exception))
        self.assertIn("额度", futu.explain("历史K线额度不足"))
        # 端口上有程序但不是 OpenD：真实 futu-api 会一直等握手，研究台限时后给出原因
        srv = socket.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen()
        old = futu.CONNECT_TIMEOUT
        futu.CONNECT_TIMEOUT = 3
        try:
            with self.assertRaises(futu.FutuError) as e:
                futu.real_context("127.0.0.1", srv.getsockname()[1])
            self.assertIn("握手", str(e.exception))
        finally:
            futu.CONNECT_TIMEOUT = old
            srv.close()
        self.assertIn("行情权限", futu.explain("No right to get the quote"))


class RangeHandler(BaseHTTPRequestHandler):
    payload = b""

    def log_message(self, *a):
        pass

    def do_GET(self):
        data = self.payload
        rng = self.headers.get("Range")
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            data = data[start:]
        else:
            self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class CommunityTest(TempDir):
    def _tarball(self) -> bytes:
        root, ds = shared_demo()
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            d = datasets.path(root, ds)
            for sub in ("calendars", "instruments", "features"):
                tar.add(d / sub, arcname=f"./qlib_bin/{sub}")
        return buf.getvalue()

    def test_download_resume_and_import(self):
        payload = self._tarball()
        RangeHandler.payload = payload
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), RangeHandler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/qlib_bin.tar.gz"
            dl = self.tmp / "downloads"
            dl.mkdir()
            (dl / "qlib_bin.tar.gz.part").write_bytes(payload[:1000])  # 模拟上次下到一半
            f = community.download(dl, url)
            self.assertEqual(f.read_bytes(), payload, "续传后文件应完整")
        finally:
            httpd.shutdown()
        d = self.tmp / "ds"
        d.mkdir()
        meta = community.import_archive(f, d, {"title": "社区", "source": "community"})
        self.assertGreater(meta["n_stocks"], 50)
        self.assertTrue((d / "instruments" / "stocks.txt").is_file())

    def test_errors_are_explained(self):
        port = free_port()
        with self.assertRaises(community.DownloadError) as e:
            community.download(self.tmp, f"http://127.0.0.1:{port}/x.tar.gz", timeout=3)
        self.assertIn("拒绝连接", str(e.exception))
        bad = self.tmp / "bad.tar.gz"
        bad.write_bytes(b"not a tar")
        with self.assertRaises(ValueError) as e:
            community.import_archive(bad, self.tmp / "d1", {})
        self.assertIn("压缩包损坏", str(e.exception))
        evil = self.tmp / "evil.tar"
        with tarfile.open(evil, "w") as tar:
            info = tarfile.TarInfo("../../escape.txt")
            info.size = 3
            tar.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaises(ValueError):
            community.import_archive(evil, self.tmp / "d2", {})
        self.assertFalse((self.tmp.parent / "escape.txt").exists())


class ExperimentTest(unittest.TestCase):
    def test_validation_and_segments(self):
        root, ds = shared_demo()
        with self.assertRaises(ValueError) as e:
            experiment.normalize({"dataset": ds, "segments": {"train": ["2025-01-01", "2025-06-30"], "valid": ["2024-01-01", "2024-06-30"],
                                                               "test": ["2025-07-01", "2025-12-31"]}}, root)
        self.assertIn("顺序", str(e.exception))
        with self.assertRaises(ValueError):
            experiment.normalize({"dataset": ds, "model": "lstm"}, root)
        cfg = experiment.normalize({"dataset": ds}, root)
        seg = cfg["segments"]
        self.assertLess(seg["train"][1], seg["valid"][0])
        self.assertLess(seg["valid"][1], seg["test"][0])
        cal = datasets.calendar(datasets.path(root, ds))
        folds = experiment.plan_rolling(cal, seg, 40)
        self.assertGreater(len(folds), 2)
        self.assertEqual(folds[0]["test"][0], seg["test"][0])
        self.assertEqual(folds[-1]["test"][1], seg["test"][1])
        for f in folds:  # 每段：训练 < 验证 < 测试，不重叠
            self.assertLess(f["train"][1], f["valid"][0])
            self.assertLess(f["valid"][1], f["test"][0])

    def test_run_lightgbm_and_linear(self):
        root, ds = shared_demo()
        for model in ("lgbm", "linear"):
            out = root / "runs" / f"t_{model}"
            res = experiment.run({"dataset": ds, "model": model, "quick": True, "strategy": {"topk": 10, "n_drop": 2},
                                  "benchmark": "equal" if model == "linear" else "auto"}, root, out)
            self.assertGreater(res["metrics"]["days"], 50)
            self.assertIsNotNone(res["ic"]["rank_ic_mean"])
            self.assertEqual(len(res["groups"]["mean"]), 5)
            trades = json.loads((out / "trades.json").read_text(encoding="utf-8"))
            self.assertTrue(trades)
            self.assertTrue(all(t["shares"] % 100 == 0 for t in trades), "一手 100 股")
            pos = json.loads((out / "positions.json").read_text(encoding="utf-8"))
            self.assertLessEqual(max(len(p["holdings"]) for p in pos), 10)
            exp = compare.load(out / "export.json")
            self.assertEqual(exp["app"], "Qlib 量化研究台")
            self.assertFalse((out / "mlruns").exists())
            if model == "lgbm":
                self.assertTrue(res["importance"] and not res["importance"][0]["name"].startswith("Column_"))


class CompareTest(unittest.TestCase):
    def test_sample_and_side_by_side(self):
        sample = compare.load(ROOT / "samples" / "quantlab-result-example.json")
        self.assertEqual(sample["app"], "QuantLab")
        other = json.loads(json.dumps(sample))
        other["app"] = "Qlib 量化研究台"
        other["daily"]["dates"] = other["daily"]["dates"][50:]
        other["daily"]["return"] = other["daily"]["return"][50:]
        other["daily"]["bench_return"] = other["daily"]["bench_return"][50:]
        res = compare.side_by_side([sample, compare.validate(other)])
        self.assertEqual(res["common"]["start"], other["daily"]["dates"][0])
        self.assertTrue(any("共同区间" in w for w in res["warnings"]))
        a, b = res["items"]
        self.assertEqual(len(a["curve"]["dates"]), len(b["curve"]["dates"]))
        self.assertAlmostEqual(a["metrics_common"]["total_return"], b["metrics_common"]["total_return"], places=9)

    def test_bad_files(self):
        for obj, word in (({"perf": {}, "ic": {}}, "导出实验结果"), ({"format": "quant-experiment-result", "format_version": 9}, "升级"),
                          ({"format": "quant-experiment-result", "format_version": 1, "daily": {"dates": ["2024-01-02"], "return": []}}, "长度")):
            with self.assertRaises(compare.FormatError) as e:
                compare.validate(obj)
            self.assertIn(word, str(e.exception))
        with self.assertRaises(compare.FormatError):
            compare.load(b"{not json")


class GuideTest(unittest.TestCase):
    def test_lists_and_zip(self):
        idx = guide.index(ROOT)
        self.assertEqual(idx["version"], __version__)
        docs = {d["path"] for d in idx["docs"]}
        self.assertIn("docs/04 与 QuantLab 的对比.md", docs)
        srcs = {s["path"]: s["group"] for s in idx["sources"]}
        self.assertEqual(srcs.get("qlibstudio/experiment.py"), "shell")
        self.assertEqual(srcs.get("upstream/qlib/backtest/exchange.py"), "upstream")
        self.assertNotIn("qlibstudio/web/vendor/echarts.min.js", srcs, "第三方压缩库不算手写源码")
        self.assertIsNone(guide.read_source("../run.py", ROOT))
        names = zipfile.ZipFile(io.BytesIO(guide.source_zip(ROOT))).namelist()
        self.assertIn("qlib-studio/upstream/qlib/__init__.py", names)


class ServerTest(TempDir):
    def setUp(self):
        super().setUp()
        self.app, self.httpd = serve(self.tmp, 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def tearDown(self):
        self.app.jobs.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def req(self, method, path, body=None, token=True, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        h = {"Content-Type": "application/json", "Host": host or f"127.0.0.1:{self.port}"}
        if token:
            h["X-QlibStudio-Token"] = self.app.token
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        return r.status, (json.loads(data) if "json" in (r.getheader("Content-Type") or "") else data.decode())

    def test_security(self):
        self.assertEqual(self.req("GET", "/api/status", host="evil.com:80")[0], 403)
        self.assertEqual(self.req("POST", "/api/quit", {}, token=False)[0], 403)
        self.assertEqual(self.req("GET", "/api/guide/source?path=..%2F..%2Fetc%2Fpasswd")[0], 404)
        self.assertEqual(self.req("GET", "/api/run?id=..%2Fx")[0], 400)
        self.assertEqual(self.req("POST", "/api/settings", {"changes": {"futu_host": "10.0.0.1"}})[0], 400)
        s, page = self.req("GET", "/")
        self.assertEqual(s, 200)
        self.assertIn("zoom-select", page)

    def test_job_in_worker_process_and_cancel(self):
        s, j = self.req("POST", "/api/job/start", {"kind": "demo", "params": {"n_stocks": 30, "years": 2}})
        self.assertEqual(s, 202)
        pid = j["pid"]
        s, procs = self.req("GET", "/api/processes")
        self.assertIn(pid, [p["pid"] for p in procs["processes"]])
        self.assertEqual(self.req("POST", "/api/job/start", {"kind": "demo", "params": {}})[0], 409, "同一时间只跑一个任务")
        for _ in range(180):
            v = self.req("GET", "/api/job?id=" + j["id"])[1]
            if not v["running"]:
                break
            time.sleep(1)
        self.assertTrue(v["result"]["ok"], v)
        s, ds = self.req("GET", "/api/datasets")
        self.assertEqual(len(ds["datasets"]), 1)
        s, procs = self.req("GET", "/api/processes")
        self.assertNotIn(pid, [p["pid"] for p in procs["processes"]], "结束的工作进程不应留在列表里")
        # 取消
        s, j = self.req("POST", "/api/job/start", {"kind": "experiment", "params": {"config": {"dataset": ds["datasets"][0]["id"], "model": "catboost"}}})
        time.sleep(2)
        self.assertTrue(self.req("POST", "/api/job/cancel", {})[1]["cancelled"])
        v = self.req("GET", "/api/job?id=" + j["id"])[1]
        self.assertEqual(v["status"], "已取消")
        try:
            import psutil
            self.assertFalse(psutil.pid_exists(j["pid"]) and psutil.Process(j["pid"]).status() != psutil.STATUS_ZOMBIE)
        except ImportError:
            pass

    def test_compare_import(self):
        content = (ROOT / "samples" / "quantlab-result-example.json").read_text(encoding="utf-8")
        s, r = self.req("POST", "/api/compare/import", {"content": content})
        self.assertEqual(s, 200, r)
        s, items = self.req("GET", "/api/compare/items")
        self.assertEqual(items["items"][0]["app"], "QuantLab")
        s, res = self.req("GET", "/api/compare?keys=" + r["key"])
        self.assertEqual(len(res["items"]), 1)


class ProcinfoTest(unittest.TestCase):
    def test_snapshot(self):
        s = procinfo.snapshot({})
        self.assertEqual(s["processes"][0]["pid"], os.getpid())
        self.assertEqual(s["unit"], "MiB")
        self.assertTrue(s["backend_note"].startswith("无独立后台"))
        self.assertTrue(s["external"])


def tearDownModule():
    if "root" in _SHARED:
        shutil.rmtree(_SHARED["root"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
