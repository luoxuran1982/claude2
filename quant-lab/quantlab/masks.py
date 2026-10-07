"""掩码（mask）：决定每一天哪些股票“能进股票池”“能买”“能卖”。

时间约定（贯穿全系统）：
    t 日收盘后算因子、出预测  →  t+1 日开盘按预测下单  →  持有 H 天  →  t+1+H 日开盘卖出
所以：股票池掩码看 t 日；买卖掩码看 t+1 日开盘（一字涨停买不进、一字跌停卖不出、停牌都不能交易）。
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .data.store import Panel
from .market import classify, is_st, limit_rate_frame

LIMIT_TOL = 0.003  # 判断开盘是否在涨跌停价的相对容差（价格四舍五入到分造成的误差）


@dataclass
class Masks:
    universe: pd.DataFrame   # t 日是否进股票池
    can_buy: pd.DataFrame    # 当天开盘能否买入
    can_sell: pd.DataFrame   # 当天开盘能否卖出
    limit_rate: pd.DataFrame


def build(panel: Panel, *, boards=None, min_listed_days: int = 60, min_amount: float = 1e7,
          min_price: float = 0.0, exclude_st: bool = True) -> Masks:
    traded = panel.traded
    syms = panel.symbols
    lim = limit_rate_frame(panel.dates, syms, panel.names)
    adj_open = panel.adj("open")
    adj_close = panel.adj("close")
    prev_close = adj_close.ffill().shift(1)
    gap = adj_open / prev_close - 1
    open_up = gap >= lim - LIMIT_TOL
    open_down = gap <= -lim + LIMIT_TOL
    # 新股上市前 5 天无涨跌停限制
    listed_days = traded.cumsum()
    no_limit = listed_days <= 5
    can_buy = traded & (~open_up | no_limit)
    can_sell = traded & (~open_down | no_limit)

    stock = pd.Series({s: classify(s)[0] == "stock" and (boards is None or classify(s)[1] in boards) for s in syms})
    if exclude_st:
        stock &= pd.Series({s: not is_st(panel.names.get(s)) for s in syms})
    amt = panel.raw["amount"].where(traded, 0.0).rolling(20, min_periods=10).mean()
    uni = traded & (listed_days >= min_listed_days) & (amt >= min_amount)
    if min_price > 0:
        uni &= panel.raw["close"] >= min_price
    uni &= np.broadcast_to(stock.to_numpy(), uni.shape)
    return Masks(uni.fillna(False), can_buy.fillna(False), can_sell.fillna(False), lim)


def forward_return(panel: Panel, horizon: int, price: str = "open") -> pd.DataFrame:
    """t 日的标签：t+1 开盘买入、t+1+H 开盘卖出的收益（price='close' 时用 t 收盘到 t+H 收盘）。"""
    if price == "close":
        c = panel.adj("close").ffill()
        return c.shift(-horizon) / c.where(panel.traded) - 1
    o = panel.adj("open")
    entry = o.shift(-1).where(panel.traded.shift(-1, fill_value=False))
    exit_ = o.ffill().shift(-(horizon + 1))
    return exit_ / entry - 1
