"""不复权数据的除权除息近似处理。

A 股每天涨跌有上限，所以“开盘价 / 前收盘”超出涨跌停范围的跳空，几乎只能是除权（送转、配股、大额分红）。
在这些日子把跳空当作复权因子，相当于假设除权当天隔夜收益为 0。小额现金分红（1%~3%）检测不到，会留下
很小的偏差；有准确复权因子时（例如 Tushare adj_factor 列）优先用准确因子。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from ..market import CHINEXT_REFORM, classify

NEW_LISTING_DAYS = 5   # 新股上市前 5 天不设涨跌幅
MARGIN = 0.02          # 涨跌停之外再留 2% 余量，避免误判


def limit_rate_series(symbol: str, dates: pd.Series) -> np.ndarray:
    _, board = classify(symbol)
    if board == "star":
        return np.full(len(dates), 0.20)
    if board == "bse":
        return np.full(len(dates), 0.30)
    if board == "chinext":
        return np.where(dates.to_numpy() >= np.datetime64(CHINEXT_REFORM), 0.20, 0.10)
    return np.full(len(dates), 0.10)


def estimate_factor(symbol: str, df: pd.DataFrame) -> tuple[np.ndarray, list[pd.Timestamp]]:
    """返回 (后复权因子, 识别出的除权日列表)。复权价 = 原始价 × 因子。"""
    n = len(df)
    factor = np.ones(n)
    if n < 2:
        return factor, []
    prev_close = df["close"].to_numpy()[:-1]
    open_ = df["open"].to_numpy()[1:]
    gap = open_ / prev_close - 1
    rate = limit_rate_series(symbol, df["date"])[1:]
    event = (np.abs(gap) > rate + MARGIN)
    event[: max(NEW_LISTING_DAYS - 1, 0)] = False
    step = np.ones(n)
    step[1:][event] = prev_close[event] / open_[event]
    factor = np.cumprod(step)
    dates = list(df["date"].iloc[1:][event])
    return factor, dates
