"""把行情宽表变成机器学习用的样本矩阵。

样本 = (决策日 t, 股票 s)，只取 t 日在股票池里的股票。决策日从最后一个交易日往前每隔 step 天取一个，
保证最新一天一定是决策日（用于“今日选股”）。
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd

from . import features as F
from . import masks as M
from .data.store import DataStore, Panel, symbols_where

log = logging.getLogger("quantlab")
Progress = Callable[[float, str], None]


@dataclass
class Samples:
    panel: Panel
    masks: M.Masks
    decision_dates: pd.DatetimeIndex
    feature_names: list[str]
    X: np.ndarray            # (n, f) float32
    y: np.ndarray            # 变换后的训练标签
    y_raw: np.ndarray        # 原始未来收益（评估用）
    date_idx: np.ndarray     # 行 → decision_dates 的下标
    sym_idx: np.ndarray      # 行 → panel.symbols 的下标
    tradable: np.ndarray     # 次日开盘能买入
    cal_pos: np.ndarray      # decision_dates 在完整交易日历中的位置
    benchmark: str | None = None
    bench_close: pd.Series | None = None
    meta: dict = field(default_factory=dict)

    @property
    def dates(self) -> np.ndarray:
        return self.decision_dates.to_numpy()[self.date_idx]


def cs_normalize(df: pd.DataFrame, how: str) -> pd.DataFrame:
    """截面标准化（每天单独做，不跨日期，不会引入未来信息）。"""
    if how == "rank":
        return df.rank(axis=1, pct=True) - 0.5
    if how == "zscore":
        med = df.median(axis=1)
        mad = (df.sub(med, axis=0)).abs().median(axis=1) * 1.4826
        lo, hi = med - 5 * mad, med + 5 * mad
        x = df.clip(lo, hi, axis=0)
        return x.sub(x.mean(axis=1), axis=0).div(x.std(axis=1).replace(0, np.nan), axis=0).clip(-5, 5)
    return df


def decision_dates(dates: pd.DatetimeIndex, start, end, step: int) -> pd.DatetimeIndex:
    pos = np.arange(len(dates) - 1, -1, -step)[::-1]
    sel = dates[pos]
    if start:
        sel = sel[sel >= pd.Timestamp(start)]
    if end:
        sel = sel[sel <= pd.Timestamp(end)]
    return sel


_cache_lock = threading.Lock()
_cache: dict = {}


def build(store: DataStore, cfg: dict, progress: Progress = lambda p, m: None) -> Samples:
    meta = store.meta(cfg["dataset"])
    u, lab, w = cfg["universe"], cfg["label"], cfg["walk"]
    feats = cfg["features"]["resolved"]
    key = (cfg["dataset"], meta.get("revision"), repr(u), cfg["period"].get("start"), cfg["period"].get("end"),
           tuple(feats), cfg["features"]["normalize"], repr(lab), w["step"], w["train_tradable_only"],
           cfg["backtest"].get("benchmark"))
    with _cache_lock:
        if key in _cache:
            progress(0.30, "使用缓存的因子数据")
            return _cache[key]

    progress(0.01, "加载行情")
    stocks = symbols_where(meta, ("stock",), set(u["boards"]))
    if not stocks:
        raise ValueError("所选板块在数据集中没有股票")
    bench = cfg["backtest"].get("benchmark") or "auto"
    extra = [bench] if bench != "auto" and bench in {m["symbol"] for m in meta["symbols"]} else []
    full = store.panel(cfg["dataset"], cfg["period"].get("start") or None, cfg["period"].get("end") or None,
                       symbols=stocks + extra)
    panel = full.subset([s for s in full.symbols if s in set(stocks)])
    bench_close = full.adj("close")[extra[0]].reindex(panel.dates) if extra else None
    progress(0.05, f"{len(panel.symbols)} 只股票，{len(panel.dates)} 个交易日；计算掩码")
    masks = M.build(panel, boards=set(u["boards"]), min_listed_days=u["min_listed_days"],
                    min_amount=u["min_amount"], min_price=u["min_price"], exclude_st=u["exclude_st"])
    ddates = decision_dates(panel.dates, cfg["period"].get("start"), cfg["period"].get("end"), w["step"])
    if len(ddates) < 20:
        raise ValueError(f"决策日太少（{len(ddates)} 个），请扩大时间范围或缩短调仓周期")
    uni = masks.universe.loc[ddates].to_numpy()
    di, sj = np.nonzero(uni)
    if len(di) == 0:
        raise ValueError("股票池为空：请放宽成交额/上市天数等条件")
    progress(0.07, f"{len(ddates)} 个决策日，{len(di):,} 个样本；计算因子")

    ctx = F.Ctx(panel, masks.universe)
    X = np.empty((len(di), len(feats)), dtype="float32")
    uni_df = masks.universe.loc[ddates]
    for k, name in enumerate(feats):
        wide = F.compute(ctx, name).loc[ddates].where(uni_df)
        wide = cs_normalize(wide, cfg["features"]["normalize"])
        X[:, k] = wide.to_numpy(dtype="float64")[di, sj]
        if k % 5 == 4 or k == len(feats) - 1:
            progress(0.07 + 0.23 * (k + 1) / len(feats), f"因子 {k + 1}/{len(feats)}：{name}")
    del ctx

    fwd = M.forward_return(panel, lab["horizon"], lab["price"]).loc[ddates].where(uni_df)
    if lab["excess"]:
        fwd = fwd.sub(fwd.mean(axis=1), axis=0)
    y_raw_full = M.forward_return(panel, lab["horizon"], lab["price"]).loc[ddates].to_numpy()[di, sj]
    y = cs_normalize(fwd, lab["transform"] if lab["transform"] != "raw" else "none").to_numpy()[di, sj]
    tradable = masks.can_buy.shift(-1, fill_value=False).loc[ddates].to_numpy()[di, sj]
    cal_pos = panel.dates.get_indexer(ddates)
    s = Samples(panel, masks, ddates, feats, X, y.astype("float32"), y_raw_full.astype("float32"),
                di.astype("int32"), sj.astype("int32"), tradable, cal_pos,
                benchmark=extra[0] if extra else None, bench_close=bench_close)
    with _cache_lock:
        _cache.clear()
        _cache[key] = s
    return s


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
