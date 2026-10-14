"""在线数据源测试：AKShare 与富途 OpenD 都用假的模块（夹具），不访问网络、不需要 OpenD。"""
from __future__ import annotations

import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantlab.data import akshare_source, futu_source, online  # noqa: E402
from quantlab.data.online import SourceError  # noqa: E402
from quantlab.data.store import DataStore, symbol_source  # noqa: E402

DATES = pd.bdate_range("2024-01-02", periods=60)


def raw_frame(seed: int, dates=DATES, split_at: int | None = None) -> pd.DataFrame:
    """不复权价；split_at 处 10 送 10（价格减半）。返回含 hfq 收盘价的真值。"""
    rng = np.random.default_rng(seed)
    close = 10 * np.exp(np.cumsum(rng.normal(0, 0.01, len(dates))))
    factor = np.ones(len(dates))
    if split_at is not None:
        close[split_at:] /= 2
        factor[split_at:] = 2.0
    return pd.DataFrame({"date": dates, "open": close, "high": close * 1.01, "low": close * 0.99, "close": close,
                         "volume": 1e6, "amount": close * 1e6, "factor": factor})


class FakeAk:
    """模拟 akshare 的几个函数（返回和真实接口一样的中文列名）。"""

    def __init__(self, frames: dict[str, pd.DataFrame], fail: dict[str, Exception] | None = None):
        self.frames, self.fail, self.calls = frames, fail or {}, []

    def stock_info_a_code_name(self):
        return pd.DataFrame({"code": ["600000", "000001", "300750", "510300"], "name": ["浦发银行", "平安银行", "宁德时代", "ETF"]})

    def index_stock_cons_csindex(self, symbol):
        return pd.DataFrame({"成分券代码": ["600000", "1"], "成分券名称": ["浦发银行", "平安银行"],
                             "交易所": ["上海证券交易所", "深圳证券交易所"]})

    def stock_zh_a_hist(self, symbol, period, start_date, end_date, adjust):
        self.calls.append((symbol, start_date, end_date, adjust))
        if symbol in self.fail:
            raise self.fail[symbol]
        df = self.frames.get(symbol)
        if df is None:
            return pd.DataFrame()
        df = df[(df["date"] >= pd.Timestamp(start_date)) & (df["date"] <= pd.Timestamp(end_date))]
        k = df["factor"] if adjust == "hfq" else (df["factor"] / df["factor"].iloc[-1] if adjust == "qfq" else 1.0)
        return pd.DataFrame({"日期": df["date"].dt.date, "股票代码": symbol, "开盘": df["open"] * k, "收盘": df["close"] * k,
                             "最高": df["high"] * k, "最低": df["low"] * k, "成交量": df["volume"] / 100, "成交额": df["amount"]})

    def stock_zh_a_daily(self, symbol, start_date, end_date, adjust):
        raise ConnectionError("sina down too")

    def stock_zh_index_daily_em(self, symbol, start_date, end_date):
        df = raw_frame(99)
        return df.drop(columns="factor").assign(date=df["date"].dt.strftime("%Y-%m-%d"))


def no_sleep(_s):
    pass


class TestOnlineCommon(unittest.TestCase):
    def test_plan_incremental(self):
        existing = {"sh600000": {"date": pd.Timestamp("2024-03-01"), "start": pd.Timestamp("2024-01-02")},
                    "sz000001": {"date": pd.Timestamp("2024-03-29"), "start": pd.Timestamp("2024-01-02")}}
        p = online.plan(existing, ["sh600000", "sz000001", "sz300750"], "2024-01-02", "2024-03-30")  # 3/30 是周六
        self.assertEqual(p.up_to_date, ["sz000001"])
        self.assertEqual([(t.symbol, t.start, t.incremental) for t in p.tasks],
                         [("sh600000", "2024-03-01", True), ("sz300750", "2024-01-02", False)])

    def test_retry_backoff_and_message(self):
        waits, n = [], [0]

        def flaky():
            n[0] += 1
            if n[0] < 3:
                raise ConnectionError("Max retries exceeded")
            return "ok"
        self.assertEqual(online.with_retry(flaky, akshare_source.explain, sleep=waits.append), "ok")
        self.assertEqual(waits, [2.0, 4.0])
        with self.assertRaises(SourceError) as cm:
            online.with_retry(lambda: (_ for _ in ()).throw(ConnectionError("timed out")), akshare_source.explain,
                              sleep=waits.append)
        self.assertIn("网络连接失败", str(cm.exception))

    def test_rate_limiter(self):
        t, waits = [0.0], []
        lim = online.RateLimiter(0.5, sleep=lambda s: (waits.append(round(s, 3)), t.__setitem__(0, t[0] + s)),
                                 clock=lambda: t[0])
        lim.wait()
        t[0] += 0.2
        lim.wait()
        self.assertEqual(waits, [0.3])

    def test_explain_messages(self):
        self.assertIn("没有安装 akshare", str(akshare_source.explain(ImportError("x"))))
        self.assertIn("403/429", str(akshare_source.explain(RuntimeError("HTTP 429 Too Many Requests"))))
        self.assertIn("改版", str(akshare_source.explain(KeyError("klines"))))
        self.assertTrue(akshare_source.explain(ConnectionError("x")).retry)

    def test_parse_codes(self):
        good, bad = online.parse_codes("600519, 000001；sz300750 830799.BJ abc")
        self.assertEqual(good, ["sh600519", "sz000001", "sz300750", "bj830799"])
        self.assertEqual(bad, ["abc"])


class TestAkshare(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DataStore(Path(self.tmp.name))
        self.frames = {"600000": raw_frame(1, split_at=30), "000001": raw_frame(2)}

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, ak):
        return akshare_source.AkshareFetcher(module=ak, interval=0, sleep=no_sleep)

    def test_factor_mode_and_store(self):
        ak = FakeAk(self.frames)
        res = online.download(self.store, self.fetcher(ak), ["sh600000", "sz000001", "sh000300"], start="2024-01-02",
                              end="2024-03-25", adjust="factor", ds_id="ak", title="AK 测试", names={"sh600000": "浦发银行"})
        self.assertEqual(res.downloaded, 3)
        df = self.store.symbol_bars("ak", "sh600000")
        self.assertAlmostEqual(df["volume"].iloc[0], 1e6)     # 手 → 股
        # 复权价连续：后复权收盘 = 不复权 × 因子
        adj = df["close"] * df["factor"]
        self.assertLess(abs(adj.pct_change().iloc[30]), 0.05)
        self.assertAlmostEqual(df["factor"].iloc[-1] / df["factor"].iloc[0], 2.0, places=6)
        meta = self.store.meta("ak")
        m = {x["symbol"]: x for x in meta["symbols"]}
        self.assertEqual(m["sh600000"]["source"], "AKShare")
        self.assertEqual(m["sh600000"]["adjust"], "复权因子列")
        self.assertTrue(m["sh600000"]["updated"])
        self.assertEqual(meta["sources"], {"AKShare": 3})
        # 和通达信导入的数据一样能用：能加载成宽表
        panel = self.store.panel("ak")
        self.assertIn("sh600000", panel.symbols)

    def test_incremental_skips_up_to_date(self):
        ak = FakeAk(self.frames)
        f = self.fetcher(ak)
        online.download(self.store, f, ["sh600000", "sz000001"], start="2024-01-02", end="2024-02-15", adjust="hfq",
                        ds_id="ak", title="AK")
        ak.calls.clear()
        res = online.download(self.store, f, ["sh600000", "sz000001"], start="2024-01-02", end="2024-03-25",
                              adjust="hfq", ds_id="ak", title="")
        self.assertEqual(res.downloaded, 2)
        self.assertTrue(all(c[1] == "20240215" for c in ak.calls), ak.calls)   # 从本地最后一天接着下
        self.assertEqual(self.store.symbol_bars("ak", "sh600000")["date"].max(), pd.Timestamp("2024-03-25"))
        self.assertEqual(self.store.meta("ak")["title"], "AK")
        ak.calls.clear()
        res = online.download(self.store, f, ["sh600000", "sz000001"], start="2024-01-02", end="2024-03-25",
                              adjust="hfq", ds_id="ak", title="")
        self.assertEqual((res.downloaded, res.up_to_date, ak.calls), (0, 2, []))
        self.assertTrue(self.store.meta("ak")["symbols"][0].get("checked"))

    def test_qfq_change_triggers_full_redownload(self):
        ak = FakeAk({"600000": raw_frame(1)})
        f = self.fetcher(ak)
        online.download(self.store, f, ["sh600000"], start="2024-01-02", end="2024-02-15", adjust="qfq", ds_id="ak", title="AK")
        ak.frames["600000"] = raw_frame(1, split_at=50)    # 之后发生除权：前复权历史价格整体变了
        ak.calls.clear()
        res = online.download(self.store, f, ["sh600000"], start="2024-01-02", end="2024-03-25", adjust="qfq", ds_id="ak", title="")
        self.assertEqual([c[1] for c in ak.calls], ["20240215", "20240102"])
        self.assertEqual(res.downloaded, 1)
        df = self.store.symbol_bars("ak", "sh600000")
        self.assertEqual(len(df), len(DATES[DATES <= "2024-03-25"]))
        self.assertTrue(any("整段重新下载" in x for x in self.store.meta("ak")["issues"]))

    def test_failures_are_reported(self):
        ak = FakeAk(self.frames, fail={"000001": ConnectionError("Max retries exceeded")})
        res = online.download(self.store, self.fetcher(ak), ["sh600000", "sz000001", "sz300750"], start="2024-01-02",
                              end="2024-03-25", adjust="none", ds_id=None, title="AK")
        self.assertEqual(res.downloaded, 1)
        self.assertEqual(len(res.failed), 2)
        self.assertIn("网络连接失败", res.failed[0])
        self.assertIn("没有数据", res.failed[1])

    def test_all_fail_raises_chinese(self):
        ak = FakeAk({}, fail={c: ConnectionError("Max retries") for c in ("600000", "000001")})
        with self.assertRaises(SourceError) as cm:
            online.download(self.store, self.fetcher(ak), ["sh600000", "sz000001"], start="2024-01-02",
                            end="2024-03-25", adjust="hfq", ds_id=None, title="AK", max_consecutive_fail=2)
        self.assertIn("网络连接失败", str(cm.exception))

    def test_universe(self):
        f = self.fetcher(FakeAk(self.frames))
        syms, names, _ = akshare_source.resolve_universe(f, {"scope": "all"})
        self.assertEqual(syms, ["sh600000", "sz000001", "sz300750"])   # ETF 去掉
        syms, names, notes = akshare_source.resolve_universe(f, {"scope": "index", "index": "000300", "benchmarks": ["sh000300"]})
        self.assertEqual(syms, ["sh600000", "sz000001", "sh000300"])
        self.assertIn("幸存者偏差", notes[0])
        with self.assertRaises(ValueError):
            akshare_source.resolve_universe(f, {"scope": "custom", "codes": ""})

    def test_old_dataset_source_fallback(self):
        self.assertEqual(symbol_source({"source": "D:/new_tdx/vipdoc", "created": "2024-01-01 00:00:00"}, {}),
                         ("通达信", "2024-01-01 00:00:00"))
        self.assertEqual(symbol_source({"demo": True, "created": "x"}, {})[0], "演示数据")


class FakeCtx:
    def __init__(self, ft, frames, used=3, remain=1, recent=("SZ.000001",), logged=True, fail=None):
        self.ft, self.frames, self.used, self.remain, self.recent = ft, frames, used, remain, list(recent)
        self.logged, self.fail, self.calls, self.closed = logged, fail, [], False

    def get_global_state(self):
        return 0, {"qot_logined": self.logged}

    def get_history_kl_quota(self, get_detail=False):
        return 0, (self.used, self.remain, [{"code": c, "name": "", "request_time": "2024-03-01 10:00:00"} for c in self.recent])

    def request_history_kline(self, code, start=None, end=None, ktype=None, autype=None, max_count=1000, page_req_key=None):
        self.calls.append((code, start, autype, page_req_key))
        if self.fail:
            return -1, self.fail, None
        df = self.frames[code]
        df = df[(df["date"] >= pd.Timestamp(start)) & (df["date"] <= pd.Timestamp(end))]
        k = df["factor"] if autype == "hfq" else (df["factor"] / df["factor"].iloc[-1] if autype == "qfq" else 1.0)
        out = pd.DataFrame({"code": code, "time_key": df["date"].dt.strftime("%Y-%m-%d 00:00:00"), "open": df["open"] * k,
                            "close": df["close"] * k, "high": df["high"] * k, "low": df["low"] * k,
                            "volume": df["volume"], "turnover": df["amount"]})
        page = 0 if page_req_key is None else page_req_key
        part = out.iloc[page * 25:(page + 1) * 25]   # 分页：每页 25 根，测试翻页
        nxt = page + 1 if (page + 1) * 25 < len(out) else None
        return 0, part, nxt

    def close(self):
        self.closed = True


def fake_futu(ctx_kwargs, frames):
    ft = types.SimpleNamespace(RET_OK=0, KLType=types.SimpleNamespace(K_DAY="K_DAY"),
                               AuType=types.SimpleNamespace(QFQ="qfq", HFQ="hfq", NONE=None),
                               SysConfig=types.SimpleNamespace(set_all_thread_daemon=lambda v: None))
    ft.made = []

    def ctx_factory(host, port):
        c = FakeCtx(ft, frames, **ctx_kwargs)
        ft.made.append(c)
        return c
    ft.OpenQuoteContext = ctx_factory
    return ft


class TestFutu(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = DataStore(Path(self.tmp.name))
        self.frames = {"SH.600000": raw_frame(1, split_at=30), "SZ.000001": raw_frame(2), "SZ.300750": raw_frame(3)}

    def tearDown(self):
        self.tmp.cleanup()

    def fetcher(self, **kw):
        ft = fake_futu(kw, self.frames)
        f = futu_source.FutuFetcher(module=ft, interval=0, sleep=no_sleep)
        f.check_port = lambda: None    # 不真的连端口
        return f, ft

    def test_codes(self):
        self.assertEqual(futu_source.to_futu("sh600000"), "SH.600000")
        self.assertEqual(futu_source.from_futu("SZ.000001"), "sz000001")
        self.assertIsNone(futu_source.from_futu("HK.00700"))

    def test_quota_and_budget(self):
        f, ft = self.fetcher(used=3, remain=1, recent=("SZ.000001",))
        q = f.quota()
        self.assertEqual((q["used"], q["remain"], q["recent"][0]["symbol"]), (3, 1, "sz000001"))
        res = online.download(self.store, f, ["sh600000", "sz000001", "sz300750"], start="2024-01-02", end="2024-03-25",
                              adjust="factor", ds_id="fu", title="富途", budget=f.budget)
        # 000001 最近 30 天下过（不占额度），剩余 1 个额度给 600000，300750 跳过
        self.assertEqual(res.downloaded, 2)
        self.assertEqual(res.skipped_quota, ["sz300750"])
        df = self.store.symbol_bars("fu", "sh600000")
        self.assertEqual(len(df), len(DATES[DATES <= "2024-03-25"]))     # 翻页拼接完整
        self.assertAlmostEqual(df["factor"].iloc[-1] / df["factor"].iloc[0], 2.0, places=6)
        m = {x["symbol"]: x for x in self.store.meta("fu")["symbols"]}
        self.assertEqual(m["sh600000"]["source"], "富途 OpenD")
        f.close()
        self.assertTrue(ft.made[0].closed)

    def test_not_running(self):
        f = futu_source.FutuFetcher(host="127.0.0.1", port=free_port(), module=fake_futu({}, {}))
        with self.assertRaises(SourceError) as cm:
            f.quota()
        self.assertIn("OpenD 没有运行", str(cm.exception))

    def test_not_logged_in(self):
        f, _ = self.fetcher(logged=False)
        with self.assertRaises(SourceError) as cm:
            f.quota()
        self.assertIn("没有登录", str(cm.exception))

    def test_no_permission_is_fatal(self):
        f, _ = self.fetcher(fail="No right to get the quote")
        with self.assertRaises(SourceError) as cm:
            online.download(self.store, f, ["sh600000", "sz000001"], start="2024-01-02", end="2024-03-25",
                            adjust="qfq", ds_id=None, title="x")
        self.assertIn("行情权限", str(cm.exception))

    def test_quota_error_message(self):
        self.assertIn("额度不足", str(futu_source.explain_text("history kline quota exceeded")))
        self.assertIn("北交所", str(_raises(lambda: self.fetcher()[0].daily("bj830799", "2024-01-02", "2024-02-01", "qfq"))))

    def test_bad_port(self):
        with self.assertRaises(ValueError):
            futu_source.FutuFetcher(port="abc")


def _raises(fn):
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


if __name__ == "__main__":
    unittest.main()
