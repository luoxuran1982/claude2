"""模拟的富途 OpenD（只用于自动化测试和离线演示，不连任何服务器）。

接口和 futu-api 的 OpenQuoteContext 一样（request_history_kline、get_history_kl_quota、close），
行情是按代码生成的确定性随机游走，带一次 2:1 送转（用来检验复权因子）。额度总数默认 100 只，按「不同股票」计。
设置环境变量 QLIBSTUDIO_FAKE_FUTU=1 后，应用的「富途 OpenD」功能改用它（界面会标明「模拟 OpenD」）。
"""
from __future__ import annotations

import json
import os
import types
import zlib

import numpy as np
import pandas as pd

fake_module = types.SimpleNamespace(
    RET_OK=0, RET_ERROR=-1,
    KLType=types.SimpleNamespace(K_DAY="K_DAY"),
    AuType=types.SimpleNamespace(NONE="NONE", QFQ="QFQ", HFQ="HFQ"),
)


class FakeQuoteContext:
    def __init__(self, total_quota: int = 100, fail_codes: set[str] | None = None, used: list[str] | None = None):
        self.total = total_quota
        self.used = list(used or [])
        self.fail_codes = fail_codes or set()
        self.requests: list[tuple] = []
        self.closed = False

    def _bars(self, code: str) -> pd.DataFrame:
        seed = zlib.crc32(code.encode())
        rng = np.random.default_rng(seed)
        dates = pd.bdate_range("2019-01-02", "2026-09-30")
        ret = rng.normal(0.0003, 0.02, len(dates)).clip(-0.1, 0.1)
        close = 10 * np.exp(np.cumsum(np.log1p(ret)))
        split = len(dates) // 2
        raw = close.copy()
        raw[split:] /= 2.0  # 2:1 送转：不复权价减半
        factor = np.where(np.arange(len(dates)) >= split, 2.0, 1.0)
        vol = rng.lognormal(15, 0.5, len(dates))
        df = pd.DataFrame({"time_key": dates.strftime("%Y-%m-%d 00:00:00"), "open": raw * (1 + rng.normal(0, 0.003, len(dates))),
                           "close": raw, "volume": np.round(vol * factor), "name": f"模拟{code[-6:]}"})
        df["high"] = df[["open", "close"]].max(axis=1) * 1.01
        df["low"] = df[["open", "close"]].min(axis=1) * 0.99
        df["turnover"] = df["volume"] * (df["open"] + df["close"]) / 2
        df["_factor"] = factor
        return df.round({"open": 2, "close": 2, "high": 2, "low": 2})

    def request_history_kline(self, code, start=None, end=None, ktype=None, autype=None, max_count=1000,
                              page_req_key=None, **_):
        self.requests.append((code, start, end, autype, page_req_key))
        if code in self.fail_codes:
            return -1, "Unknown stock 未知股票", None
        if code not in self.used:
            if len(self.used) >= self.total:
                return -1, "历史K线额度不足", None
            self.used.append(code)
        df = self._bars(code)
        d = pd.to_datetime(df["time_key"])
        df = df[(d >= pd.Timestamp(start)) & (d <= pd.Timestamp(end))].reset_index(drop=True)
        if autype == "HFQ":
            for c in ("open", "close", "high", "low"):
                df[c] = df[c] * df["_factor"]
        df = df.drop(columns="_factor")
        page = int(page_req_key or 0)
        chunk = df.iloc[page * max_count:(page + 1) * max_count]
        nxt = page + 1 if (page + 1) * max_count < len(df) else None
        return 0, chunk.reset_index(drop=True), (str(nxt) if nxt is not None else None)

    def get_history_kl_quota(self, get_detail=False):
        detail = [{"code": c, "name": f"模拟{c[-6:]}", "request_time": "2026-10-14 10:00:00"} for c in self.used]
        return 0, (len(self.used), self.total - len(self.used), detail if get_detail else [])

    def close(self):
        self.closed = True
        state = os.environ.get("QLIBSTUDIO_FAKE_FUTU_STATE")
        if state:
            with open(state, "w", encoding="utf-8") as f:
                json.dump(self.used, f)


_shared: FakeQuoteContext | None = None


def factory(host=None, port=None):
    """同一进程里共用一个模拟 OpenD，这样额度在多次调用之间累计（像真的一样）。"""
    global _shared
    if _shared is None:
        used = []
        state = os.environ.get("QLIBSTUDIO_FAKE_FUTU_STATE")  # 工作进程每次都是新进程：额度记在文件里，跨进程累计
        if state and os.path.isfile(state):
            with open(state, encoding="utf-8") as f:
                used = json.load(f)
        _shared = FakeQuoteContext(used=used)
    _shared.closed = False
    return _shared, fake_module
