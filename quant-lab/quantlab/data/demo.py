"""生成“模拟 A 股”演示数据：没有真实 K 线时用来跑通全流程、做测试。

数据里故意放了几个弱规律（短期反转、中期动量、放量反转、低波动溢价），模型应该能学到一部分；
同时包含涨跌停、停牌、晚上市、退市、送转除权，用来检验掩码和复权逻辑。**不是真实行情。**
"""
from __future__ import annotations

import numpy as np
import pandas as pd

BOARD_PREFIX = [("sh", "600", 0.40), ("sz", "000", 0.30), ("sz", "300", 0.20), ("sh", "688", 0.10)]


def generate(n_stocks: int = 300, years: float = 8.0, end: str = "2026-09-30", seed: int = 7):
    rng = np.random.default_rng(seed)
    n_days = int(years * 244)
    dates = pd.bdate_range(end=end, periods=n_days)
    T, N = n_days, n_stocks

    # 代码与板块
    symbols, limits = [], []
    counters = {p: 0 for _, p, _ in BOARD_PREFIX}
    choices = rng.choice(len(BOARD_PREFIX), size=N, p=[w for *_, w in BOARD_PREFIX])
    for c in choices:
        market, prefix, _ = BOARD_PREFIX[c]
        counters[prefix] += 1
        symbols.append(f"{market}{prefix}{counters[prefix]:03d}")
        limits.append(0.20 if prefix in ("300", "688") else 0.10)
    limits = np.array(limits)

    # 市场因子：带波动率状态切换
    regime = np.repeat(rng.choice([0.008, 0.013, 0.022], size=T // 60 + 1, p=[0.4, 0.4, 0.2]), 60)[:T]
    mkt = rng.normal(0.0006 / 0.013, 1, T) * regime
    beta = rng.uniform(0.6, 1.4, N)
    sigma = rng.uniform(0.012, 0.035, N)

    idio_hist = np.zeros((T, N))
    ret = np.zeros((T, N))
    vol_shock = rng.normal(0, 0.35, (T, N))
    for t in range(T):
        eps = rng.normal(0, 1, N) * sigma
        alpha = np.zeros(N)
        if t >= 5:
            alpha += -0.015 * idio_hist[t - 5:t].sum(0)                      # 短期反转
        if t >= 60:
            alpha += 0.002 * idio_hist[t - 60:t - 5].sum(0) / np.sqrt(55) / sigma * sigma.mean()  # 中期动量
        if t >= 1:
            alpha += -0.0015 * vol_shock[t - 1] * np.sign(idio_hist[t - 1])  # 放量后反转
        alpha += -0.02 * (sigma - sigma.mean())                              # 低波动溢价
        r = beta * mkt[t] + alpha + eps
        ret[t] = np.clip(r, -limits, limits)
        idio_hist[t] = ret[t] - beta * mkt[t]

    close = rng.uniform(5, 60, N) * np.exp(np.cumsum(np.log1p(ret), axis=0))
    prev = np.vstack([close[0] / (1 + ret[0]), close[:-1]])
    over = np.clip(ret * rng.uniform(0.1, 0.5, (T, N)) + rng.normal(0, 0.004, (T, N)), -limits, limits)
    open_ = prev * (1 + over)
    hi = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.008, (T, N))))
    lo = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.008, (T, N))))
    hi = np.minimum(hi, prev * (1 + limits))
    lo = np.maximum(lo, prev * (1 - limits))
    base_vol = rng.lognormal(16, 0.8, N)
    volume = base_vol * np.exp(vol_shock + 8 * np.abs(ret))
    # 一字涨跌停：当天振幅为 0
    lu = ret >= limits - 1e-9
    one_word = lu & (rng.random((T, N)) < 0.3)
    open_[one_word] = hi[one_word] = lo[one_word] = close[one_word]
    volume[one_word] *= 0.1
    open_, hi, lo, close = (np.round(x, 2) for x in (open_, hi, lo, close))

    # 上市/退市/停牌
    alive = np.ones((T, N), bool)
    listing = np.where(rng.random(N) < 0.25, rng.integers(0, T * 2 // 3, N), 0)
    delist = np.where(rng.random(N) < 0.06, rng.integers(T // 3, T, N), T)
    for j in range(N):
        alive[: listing[j], j] = False
        alive[delist[j]:, j] = False
    susp = rng.random((T, N)) < 0.003
    for _ in range(N // 20):  # 少数长停牌
        j, t0 = rng.integers(0, N), rng.integers(0, T - 30)
        susp[t0: t0 + rng.integers(5, 30), j] = True
    alive &= ~susp

    # 送转除权（原始价格下跳，复权因子上跳）
    factor = np.ones((T, N))
    for j in rng.choice(N, size=max(1, N // 10), replace=False):
        t0 = rng.integers(T // 4, T - 10)
        k = rng.choice([1.5, 2.0, 1.3])
        for arr in (open_, hi, lo, close):
            arr[t0:, j] = np.round(arr[t0:, j] / k, 2)
        volume[t0:, j] *= k
        factor[t0:, j] = k

    amount = volume * (open_ + close) / 2
    rows = []
    for j, sym in enumerate(symbols):
        m = alive[:, j]
        rows.append(pd.DataFrame({
            "symbol": sym, "date": dates[m], "open": open_[m, j], "high": hi[m, j], "low": lo[m, j],
            "close": close[m, j], "volume": np.round(volume[m, j]), "amount": np.round(amount[m, j], 2),
            "factor": factor[m, j],
        }))
    # 模拟指数：等权市场
    idx = 1000 * np.exp(np.cumsum(np.log1p(mkt + 0.0)))
    rows.append(pd.DataFrame({"symbol": "sh000001", "date": dates, "open": idx, "high": idx * 1.005, "low": idx * 0.995,
                              "close": idx, "volume": 1e10, "amount": 1e11, "factor": 1.0}))
    bars = pd.concat(rows, ignore_index=True)
    names = {s: f"模拟股份{i + 1:03d}" for i, s in enumerate(symbols)}
    names["sh000001"] = "模拟指数"
    return bars, names
