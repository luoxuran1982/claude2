"""核心逻辑测试：python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import warnings
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
warnings.simplefilter("ignore", RuntimeWarning)

from quantlab import backtest, config, dataset, experiment, masks, trainer  # noqa: E402
from quantlab import features as F  # noqa: E402
from quantlab.data import adjust, demo, readers  # noqa: E402
from quantlab.data.store import DataStore, Panel  # noqa: E402
from quantlab.market import classify, normalize_symbol  # noqa: E402


def make_bars(n=60, start=10.0, seed=0, date0="2024-01-02"):
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(date0, periods=n)
    close = start * np.exp(np.cumsum(rng.normal(0, 0.01, n)))
    open_ = close * (1 + rng.normal(0, 0.003, n))
    return pd.DataFrame({"date": dates, "open": open_.round(2), "high": (np.maximum(open_, close) * 1.01).round(2),
                         "low": (np.minimum(open_, close) * 0.99).round(2), "close": close.round(2),
                         "volume": rng.integers(1e5, 1e6, n).astype(float), "amount": rng.uniform(1e7, 5e7, n)})


class TestMarket(unittest.TestCase):
    def test_normalize(self):
        cases = {"600000.SH": "sh600000", "SZ000001": "sz000001", "sh.600519": "sh600519", "000001": "sz000001",
                 "300750.XSHE": "sz300750", 600036: "sh600036", "830799.BJ": "bj830799", "abc": None, "688981": "sh688981"}
        for raw, want in cases.items():
            self.assertEqual(normalize_symbol(raw), want, raw)
        self.assertEqual(normalize_symbol("000001", "sh"), "sh000001")

    def test_classify(self):
        self.assertEqual(classify("sh600000"), ("stock", "sh_main"))
        self.assertEqual(classify("sh688001"), ("stock", "star"))
        self.assertEqual(classify("sz300750"), ("stock", "chinext"))
        self.assertEqual(classify("sh000300"), ("index", None))
        self.assertEqual(classify("sz399006"), ("index", None))
        self.assertEqual(classify("sh510300")[0], "fund")
        self.assertEqual(classify("bj830799"), ("stock", "bse"))


class TestReaders(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())

    def test_tdx_day_roundtrip(self):
        df = make_bars()
        d = self.tmp / "vipdoc" / "sh" / "lday"
        d.mkdir(parents=True)
        readers.write_tdx_day(d / "sh600000.day", df, "sh600000")
        readers.write_tdx_day(d / "sh510300.day", df.assign(open=df.open / 3, high=df.high / 3, low=df.low / 3, close=df.close / 3), "sh510300")
        items = {s.symbol: s for p in readers.scan(self.tmp) for s in readers.read_any(p)}
        self.assertEqual(set(items), {"sh600000", "sh510300"})
        got = items["sh600000"].df
        self.assertEqual(len(got), len(df))
        np.testing.assert_allclose(got["close"], df["close"], atol=1e-9)
        np.testing.assert_allclose(items["sh510300"].df["close"], (df["close"] / 3).round(3), atol=1e-3)  # ETF 价格 /1000
        self.assertFalse(items["sh600000"].adjusted)

    def test_tdx_export_txt(self):
        df = make_bars(20)
        lines = ["600519 贵州茅台 日线 前复权", "      日期\t    开盘\t    最高\t    最低\t    收盘\t    成交量\t    成交额"]
        for r in df.itertuples():
            lines.append(f"{r.date:%Y/%m/%d}\t{r.open:.2f}\t{r.high:.2f}\t{r.low:.2f}\t{r.close:.2f}\t{int(r.volume)}\t{r.amount:.2f}")
        lines.append("数据来源:通达信")
        p = self.tmp / "SH#600519.txt"
        p.write_bytes("\r\n".join(lines).encode("gbk"))
        [s] = readers.read_any(p)
        self.assertEqual((s.symbol, s.name, s.adjusted), ("sh600519", "贵州茅台", True))
        self.assertEqual(len(s.df), 20)
        np.testing.assert_allclose(s.df["close"], df["close"])

    def test_generic_multi_symbol_csv(self):
        a, b = make_bars(15, seed=1), make_bars(15, seed=2)
        rows = pd.concat([a.assign(代码="600000.SH", 名称="浦发银行"), b.assign(代码="000001.SZ", 名称="平安银行")])
        rows = rows.rename(columns={"date": "交易日期", "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
                                    "volume": "成交量", "amount": "成交额"})
        rows["交易日期"] = rows["交易日期"].dt.strftime("%Y-%m-%d")
        p = self.tmp / "all.csv"
        rows.to_csv(p, index=False, encoding="utf-8-sig")
        got = {s.symbol: s for s in readers.read_any(p)}
        self.assertEqual(set(got), {"sh600000", "sz000001"})
        self.assertEqual(got["sz000001"].name, "平安银行")
        np.testing.assert_allclose(got["sh600000"].df["close"], a["close"])

    def test_tushare_units_and_factor(self):
        a = make_bars(15)
        ts = pd.DataFrame({"ts_code": "600000.SH", "trade_date": a["date"].dt.strftime("%Y%m%d"), "open": a.open, "high": a.high,
                           "low": a.low, "close": a.close, "vol": a.volume / 100, "amount": a.amount / 1000, "adj_factor": 2.0})
        p = self.tmp / "daily.csv"
        ts.to_csv(p, index=False)
        [s] = readers.read_any(p)
        np.testing.assert_allclose(s.df["volume"], a["volume"], rtol=1e-9)
        np.testing.assert_allclose(s.df["amount"], a["amount"], rtol=1e-6)
        self.assertTrue((s.df["factor"] == 2.0).all())

    def test_minute_aggregation(self):
        times = pd.date_range("2024-01-02 09:31", periods=4, freq="min").append(pd.date_range("2024-01-03 09:31", periods=4, freq="min"))
        df = pd.DataFrame({"datetime": times.strftime("%Y-%m-%d %H:%M"), "open": [1, 2, 3, 4, 5, 6, 7, 8], "high": [2, 3, 4, 5, 6, 7, 8, 9],
                           "low": [0.5, 1, 2, 3, 4, 5, 6, 7], "close": [1.5, 2.5, 3.5, 4.5, 5.5, 6.5, 7.5, 8.5], "volume": 100, "amount": 1000})
        p = self.tmp / "sz000002.csv"
        df.to_csv(p, index=False)
        [s] = readers.read_any(p)
        self.assertEqual(len(s.df), 2)
        self.assertEqual(s.df["open"].tolist(), [1, 5])
        self.assertEqual(s.df["close"].tolist(), [4.5, 8.5])
        self.assertEqual(s.df["volume"].tolist(), [400, 400])


class TestImport(unittest.TestCase):
    def test_tdx_folder_with_name_table(self):
        tmp = Path(tempfile.mkdtemp())
        d = tmp / "vipdoc" / "sz" / "lday"
        d.mkdir(parents=True)
        df = make_bars(80)
        split = df.copy()
        split.loc[40:, ["open", "high", "low", "close"]] /= 2
        readers.write_tdx_day(d / "sz000001.day", split, "sz000001")
        readers.write_tdx_day(d / "sz000002.day", df, "sz000002")
        (tmp / "名称.csv").write_text("代码,名称\n000001,平安银行\n000002,*ST万科\n", encoding="gbk")
        store = DataStore(tmp / "data")
        meta = store.import_path(tmp, "t")
        self.assertEqual(meta["adjust_events"], 1)
        names = {m["symbol"]: m["name"] for m in store.meta(meta["id"])["symbols"]}
        self.assertEqual(names, {"sz000001": "平安银行", "sz000002": "*ST万科"})
        p = store.panel(meta["id"])
        m = masks.build(p, min_listed_days=0, min_amount=0)
        self.assertFalse(m.universe["sz000002"].any())   # ST 被排除
        adj = p.adj("close")["sz000001"]
        self.assertLess(abs(adj.iloc[40] / adj.iloc[39] - 1), 0.05)  # 复权后连续


class TestAdjust(unittest.TestCase):
    def test_split_detected_limit_move_not(self):
        df = make_bars(40)
        df.loc[10:, ["open", "high", "low", "close"]] = df.loc[10:, ["open", "high", "low", "close"]] / 2  # 10 送 10
        prev = df.loc[19, "close"]
        df.loc[20, ["open", "close", "high", "low"]] = round(prev * 1.10, 2)  # 一字涨停，不是除权
        f, events = adjust.estimate_factor("sh600000", df)
        self.assertEqual([d.strftime("%Y-%m-%d") for d in events], [df.loc[10, "date"].strftime("%Y-%m-%d")])
        adj_close = df["close"] * f
        self.assertLess(abs(adj_close[10] / adj_close[9] - 1), 0.05)


class PanelCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.store = DataStore(cls.tmp)
        bars, names = demo.generate(60, 3, seed=3)
        cls.store.save_frame(bars, "t", names, "t", demo=True)


class TestNoLookahead(PanelCase):
    def test_features_do_not_use_future(self):
        full = self.store.panel("t")
        stocks = [s for s in full.symbols if classify(s)[0] == "stock"]
        full = full.subset(stocks)
        cut = full.dates[len(full.dates) - 120]
        part = self.store.panel("t", end=cut).subset(stocks)
        cf, cp = F.Ctx(full), F.Ctx(part)
        traded = part.traded.loc[cut]
        cols = traded[traded].index
        for name in F.REGISTRY:
            a = F.compute(cf, name).loc[cut, cols].to_numpy()
            b = F.compute(cp, name).loc[cut, cols].to_numpy()
            np.testing.assert_allclose(a, b, rtol=1e-7, atol=1e-9, equal_nan=True, err_msg=name)

    def test_label_alignment(self):
        p = self.store.panel("t")
        fwd = masks.forward_return(p, 5, "open")
        o = p.adj("open")
        s = p.symbols[0]
        i = 50
        if p.traded[s].iloc[i + 1]:
            want = o[s].ffill().iloc[i + 6] / o[s].iloc[i + 1] - 1
            self.assertAlmostEqual(fwd[s].iloc[i], want, places=10)


class TestMasks(unittest.TestCase):
    def test_limit_up_open_cannot_buy(self):
        df = make_bars(80)
        df.loc[70, ["open", "high", "low", "close"]] = round(df.loc[69, "close"] * 1.1, 2)
        df.loc[71, ["open", "high", "low", "close"]] = round(df.loc[70, "close"] * 0.9, 2)
        p = _panel({"sh600000": df})
        m = masks.build(p, min_listed_days=10, min_amount=0)
        d70, d71 = p.dates[70], p.dates[71]
        self.assertFalse(m.can_buy.loc[d70, "sh600000"])
        self.assertTrue(m.can_sell.loc[d70, "sh600000"])
        self.assertFalse(m.can_sell.loc[d71, "sh600000"])
        self.assertTrue(m.can_buy.loc[p.dates[60], "sh600000"])


def _panel(series: dict[str, pd.DataFrame]) -> Panel:
    long = pd.concat([df.assign(symbol=s, factor=df.get("factor", 1.0)) for s, df in series.items()])
    wide = {f: long.pivot(index="date", columns="symbol", values=f) for f in ("open", "high", "low", "close", "volume", "amount", "factor")}
    factor = wide.pop("factor").ffill().bfill()
    syms = list(factor.columns)
    return Panel(pd.DatetimeIndex(factor.index), syms, {s: "" for s in syms}, wide, factor)


class TestTrainer(unittest.TestCase):
    def test_folds_have_purge_gap(self):
        cal = np.arange(0, 1500, 5)
        for horizon in (1, 5, 20):
            folds = trainer.plan_folds(cal, horizon, train_days=500, retrain_days=60, min_train_days=250)
            self.assertGreater(len(folds), 5)
            covered = np.concatenate([f["test"] for f in folds])
            self.assertEqual(len(covered), len(set(covered)))  # 测试段不重叠
            for f in folds:
                self.assertLessEqual(cal[f["train"]].max() + horizon + 1, cal[f["test"]].min())
                self.assertLessEqual(cal[f["train"]].max() - cal[f["train"]].min(), 500)


class TestBacktest(unittest.TestCase):
    def setUp(self):
        n = 30
        self.a = make_bars(n, 10, 1)
        self.b = make_bars(n, 20, 2)
        self.c = make_bars(n, 30, 3)
        # c 在第 6 天一字涨停（买不进）
        self.c.loc[6, ["open", "high", "low", "close"]] = round(self.c.loc[5, "close"] * 1.1, 2)
        self.p = _panel({"sh600001": self.a, "sh600002": self.b, "sh600003": self.c})
        self.m = masks.build(self.p, min_listed_days=0, min_amount=0)

    def samples(self, scores_by_day: dict[int, list[float]]):
        days = sorted(scores_by_day)
        dd = self.p.dates[days]
        di, sj, pred = [], [], []
        for k, d in enumerate(days):
            for j, v in enumerate(scores_by_day[d]):
                di.append(k); sj.append(j); pred.append(v)
        s = dataset.Samples(self.p, self.m, pd.DatetimeIndex(dd), [], np.zeros((len(di), 0), "float32"),
                            np.zeros(len(di)), np.zeros(len(di)), np.array(di), np.array(sj), np.ones(len(di), bool),
                            np.array(days))
        return s, np.array(pred, dtype=float)

    def bt(self, **kw):
        cfg = dict(config.DEFAULTS["backtest"], capital=100_000, top_k=2, buffer=1.0)
        cfg.update(kw)
        return cfg

    def test_accounting_and_rules(self):
        s, pred = self.samples({5: [0.1, 0.5, 0.9], 15: [0.9, 0.1, 0.5]})
        res = backtest.run(s, pred, self.bt())
        tr, nav = res["trades"], res["nav"]
        first = tr[tr["date"] == self.p.dates[6].strftime("%Y-%m-%d")]
        self.assertEqual(set(first["symbol"]), {"sh600002", "sh600001"})  # 600003 涨停买不进，顺延到 600001
        self.assertTrue((tr["shares"] % 100 == 0).all())
        self.assertTrue((tr["fee"] >= 5 - 1e-9).all())
        sells = tr[tr["side"] == "卖出"]
        self.assertEqual(set(sells["symbol"]), {"sh600002"})  # 第 15 天 600002 排名最后被调出
        v = sells.iloc[0]["value"]
        self.assertAlmostEqual(sells.iloc[0]["fee"], max(v * 0.00025, 5) + v * 0.0005, places=6)
        self.assertEqual(nav.index[0], self.p.dates[5])
        self.assertAlmostEqual(nav["equity"].iloc[0], 100_000)
        # 期末权益 = 现金 + 持仓市值
        last = nav.iloc[-1]
        mv = sum(p["value"] for p in res["final_positions"])
        self.assertAlmostEqual(last["equity"], last["cash"] + mv, delta=1.0)
        # 资金守恒：总盈亏 = 交易盈亏 + 浮动盈亏
        self.assertGreater(last["cash"], 0)

    def test_buffer_keeps_holdings(self):
        s, pred = self.samples({5: [0.9, 0.5, 0.1], 10: [0.5, 0.1, 0.9]})
        res = backtest.run(s, pred, self.bt(top_k=1, buffer=2.0))
        sells = res["trades"][res["trades"]["side"] == "卖出"]
        self.assertTrue(sells.empty)  # 600001 排名第 2，仍在前 1×2 内，不卖


class TestEndToEnd(unittest.TestCase):
    def test_demo_experiment(self):
        tmp = Path(tempfile.mkdtemp())
        store, runs = DataStore(tmp), experiment.Runs(tmp)
        bars, names = demo.generate(150, 5, seed=11)
        store.save_frame(bars, "demo", names, "demo", demo=True)
        cfg = {"dataset": "demo", "model": {"types": ["lgbm", "ridge"]}, "walk": {"retrain_days": 126},
               "backtest": {"benchmark": "sh000001"}}
        rid = experiment.run(store, runs, cfg)
        s = runs.summary(rid)
        self.assertGreater(s["ic"]["rank_ic"]["mean"], 0)   # 演示数据里埋了可学习的规律
        self.assertEqual(s["benchmark"], "sh000001")
        for f in ("config.json", "nav.csv", "trades.csv", "predictions.parquet", "picks.json", "models/lgbm.pkl"):
            self.assertTrue((runs.path(rid) / f).exists(), f)
        pr = runs.predictions(rid)
        first_test = pd.Timestamp(s["folds"][0]["test_start"])
        self.assertGreaterEqual(pd.to_datetime(pr["date"]).min(), first_test)  # 只有样本外预测
        picks = runs.picks(rid)
        self.assertEqual(picks["date"], s["latest_date"])
        self.assertTrue(picks["picks"])
        json.dumps(s, allow_nan=False)
        fac = experiment.factor_study(store, {"dataset": "demo"})
        self.assertGreater(len(fac["factors"]), 50)

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            config.normalize({"dataset": "x", "model": {"types": []}})
        cfg = config.normalize({"dataset": "x", "backtest": {"rebalance_days": 10}})
        self.assertEqual(cfg["walk"]["step"], 10)


class TestServer(unittest.TestCase):
    def test_api(self):
        from quantlab.server import serve
        import threading
        tmp = Path(tempfile.mkdtemp())
        app, httpd = serve(tmp, 0)
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def get(path):
            with urllib.request.urlopen(base + path) as r:
                return json.loads(r.read())

        def post(path, body, token=app.token):
            req = urllib.request.Request(base + path, json.dumps(body).encode(), method="POST",
                                         headers={"Content-Type": "application/json", "X-QuantLab-Token": token})
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read())
        try:
            self.assertEqual(get("/api/status")["datasets"], [])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/api/demo", {}, token="wrong")
            self.assertEqual(cm.exception.code, 403)
            post("/api/demo", {"n_stocks": 40, "years": 3})
            for _ in range(60):
                job = get("/api/job")["job"]
                if job["status"] != "running":
                    break
                time.sleep(0.5)
            self.assertEqual(job["status"], "done")
            syms = get("/api/symbols?id=demo&q=sh6")["symbols"]
            self.assertTrue(syms)
            bars = get(f"/api/bars?id=demo&symbol={syms[0]['symbol']}")
            self.assertEqual(len(bars["dates"]), len(bars["ohlc"]))
            self.assertIn("lgbm", get("/api/catalog")["models"])
            with self.assertRaises(urllib.error.HTTPError) as cm:
                post("/api/import", {"path": str(tmp / "nope")})
            self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
