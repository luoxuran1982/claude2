"""实验结果导出（JSON）测试：结构、长度一致、与实验结果数值一致、HTTP 下载。"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
import urllib.request
import warnings
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore", RuntimeWarning)

from quantlab import __version__, experiment, export  # noqa: E402
from quantlab.data import demo  # noqa: E402
from quantlab.data.store import DataStore  # noqa: E402


class TestExport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.store, cls.runs = DataStore(cls.tmp), experiment.Runs(cls.tmp)
        bars, names = demo.generate(80, 4, seed=3)
        cls.store.save_frame(bars, "demo", names, "demo", demo=True)
        cls.rid = experiment.run(cls.store, cls.runs, {"dataset": "demo", "model": {"types": ["ridge"]},
                                                       "walk": {"retrain_days": 126}})

    def test_structure_and_values(self):
        doc = export.build(self.runs, self.store, self.rid)
        self.assertEqual(export.validate(doc), [])
        self.assertEqual(doc["format"], "quantlab.experiment-result")
        self.assertEqual(doc["format_version"], export.FORMAT_VERSION)
        self.assertEqual(doc["app"]["version"], __version__)
        doc = json.loads(json.dumps(doc, ensure_ascii=False, allow_nan=False))   # 合法 JSON，没有 NaN
        s = self.runs.summary(self.rid)
        nav = self.runs.nav(self.rid)
        r = export.frame(doc)   # 日收益复利回去等于净值
        self.assertEqual(len(r), len(nav) - 1)
        self.assertAlmostEqual(float((1 + r["strategy"]).prod()), nav["nav"].iloc[-1] / nav["nav"].iloc[0], places=5)
        self.assertAlmostEqual(float((1 + r["benchmark"]).prod()), nav["bench"].iloc[-1] / nav["bench"].iloc[0], places=5)
        self.assertAlmostEqual(doc["metrics"]["annual_return"], s["perf"]["cagr"])
        self.assertAlmostEqual(doc["metrics"]["sharpe"], s["perf"]["sharpe"])
        self.assertAlmostEqual(doc["metrics"]["max_drawdown"], s["perf"]["max_drawdown"])
        self.assertAlmostEqual(doc["metrics"]["rank_ic_mean"], s["ic"]["rank_ic"]["mean"])
        self.assertEqual(len(doc["ic"]["dates"]), len(doc["ic"]["rank_ic"]))
        self.assertTrue(doc["ic"]["dates"])
        self.assertEqual(doc["universe"]["n_symbols"], s["n_stocks"])
        self.assertEqual(doc["periods"]["test"]["start"], s["perf"]["start"])
        self.assertLess(doc["periods"]["train"]["start"], doc["periods"]["test"]["start"])
        self.assertEqual(doc["dataset"]["sources"], {"演示数据": s["n_stocks"]})
        self.assertEqual(doc["config"]["model"]["types"], ["ridge"])

    def test_old_run_without_universe(self):
        p = self.runs.path(self.rid) / "summary.json"
        original = p.read_text(encoding="utf-8")
        s = json.loads(original)
        s.pop("universe_symbols")
        p.write_text(json.dumps(s), encoding="utf-8")
        try:
            doc = export.build(self.runs, self.store, self.rid)
            self.assertTrue(doc["universe"]["symbols"])
            self.assertEqual(export.validate(doc), [])
        finally:
            p.write_text(original, encoding="utf-8")

    def test_validate_catches_problems(self):
        doc = export.build(self.runs, self.store, self.rid)
        doc["daily_returns"]["strategy"].pop()
        self.assertTrue(export.validate(doc))
        self.assertTrue(export.validate({}))

    def test_http_download(self):
        from quantlab.server import serve
        app, httpd = serve(self.tmp, 0)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            url = f"http://127.0.0.1:{httpd.server_address[1]}/api/export?id={self.rid}&kind=result"
            with urllib.request.urlopen(url) as r:
                self.assertIn("application/json", r.headers["Content-Type"])
                self.assertIn(".json", r.headers["Content-Disposition"])
                doc = json.loads(r.read())
            self.assertEqual(export.validate(doc), [])
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    unittest.main()
