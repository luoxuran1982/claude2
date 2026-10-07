"""A 股组合回测：T+1、100 股一手、佣金（最低 5 元）、卖出印花税、滑点、涨跌停与停牌不能成交。

流程：决策日 t 收盘按模型打分排序 → t+1 开盘先卖后买，等权买入前 K 只 → 每天收盘按复权价估值。
- 持仓股票排名仍在前 K×buffer 内就继续持有（缓冲带，降低换手）
- 卖不出去（跌停/停牌）的挂单每天开盘重试；买不进（涨停/停牌）的顺延到下一只候选股
- 股票数据中断（退市）时按最后收盘价清算
- 手数按不复权价计算（真实的 100 股约束），持仓市值按复权价跟踪（除权日不跳变）
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .dataset import Samples


@dataclass
class Position:
    shares: float        # 复权股数（市值 = shares × 复权价）
    cost: float          # 买入总成本（含费用）
    entry_price: float   # 买入复权价
    entry_day: int
    raw_shares: int


def run(s: Samples, pred: np.ndarray, bt: dict, progress=lambda p, m: None) -> dict:
    p = s.panel
    T, N = len(p.dates), len(p.symbols)
    raw_open = p.raw["open"].to_numpy()
    adj_open = p.adj("open").to_numpy()
    adj_close = p.adj("close").ffill().to_numpy()
    can_buy = s.masks.can_buy.to_numpy()
    can_sell = s.masks.can_sell.to_numpy()
    traded = p.traded.to_numpy()
    last_trade = np.where(traded.any(0), T - 1 - np.argmax(traded[::-1], axis=0), -1)
    names = [p.names.get(x, "") for x in p.symbols]

    # 决策日 → 排好序的候选股
    ranked: dict[int, np.ndarray] = {}
    df = pd.DataFrame({"d": s.date_idx, "j": s.sym_idx, "p": pred})
    df = df[np.isfinite(df["p"])]
    for d, g in df.groupby("d"):
        ranked[int(s.cal_pos[d])] = g.sort_values("p", ascending=False)["j"].to_numpy()
    if not ranked:
        raise ValueError("没有样本外预测，无法回测")
    start = min(ranked)

    K = bt["top_k"]
    keep_n = int(np.ceil(K * bt["buffer"]))
    comm, min_comm, stamp, slip, lot = bt["commission"], bt["min_commission"], bt["stamp_tax"], bt["slippage"], bt["lot"]
    cash = float(bt["capital"])
    pos: dict[int, Position] = {}
    pending_sell: dict[int, str] = {}
    order: np.ndarray | None = None
    trades, nav_rows, holdings_log = [], [], []
    fees_total = 0.0

    def sell(j: int, i: int, price_adj: float, reason: str, apply_slip=True):
        nonlocal cash, fees_total
        ps = pos.pop(j)
        px = price_adj * (1 - slip if apply_slip else 1)
        value = ps.shares * px
        fee = max(value * comm, min_comm) + value * stamp
        cash += value - fee
        fees_total += fee
        pnl = value - fee - ps.cost
        trades.append({"date": i, "symbol": j, "side": "卖出", "price": px / adj_open[i, j] * raw_open[i, j]
                       if np.isfinite(raw_open[i, j]) and adj_open[i, j] > 0 else None,
                       "shares": ps.raw_shares, "value": value, "fee": fee, "pnl": pnl, "ret": pnl / ps.cost,
                       "hold_days": i - ps.entry_day, "reason": reason})
        pending_sell.pop(j, None)

    for i in range(start, T):
        # ---- 开盘：重试/执行卖出
        if order is not None:
            keep = set(order[:keep_n].tolist())
            for j in list(pos):
                if j not in keep:
                    pending_sell.setdefault(j, "调出")
        for j in list(pending_sell):
            if j in pos and can_sell[i, j]:
                sell(j, i, adj_open[i, j], pending_sell[j])
        # ---- 开盘：买入
        if order is not None:
            equity_open = cash + sum(ps.shares * (adj_open[i, j] if np.isfinite(adj_open[i, j]) else adj_close[i - 1, j])
                                     for j, ps in pos.items())
            target = equity_open / K
            slots = K - len(pos)
            for j in order:
                if slots <= 0:
                    break
                if j in pos or not can_buy[i, j] or not np.isfinite(raw_open[i, j]):
                    continue
                raw_px = raw_open[i, j] * (1 + slip)
                budget = min(target, cash)
                n_lots = int(budget // (raw_px * lot * (1 + comm)))
                if n_lots < 1:
                    continue
                value = n_lots * lot * raw_px
                fee = max(value * comm, min_comm)
                if value + fee > cash:
                    continue
                cash -= value + fee
                fees_total += fee
                adj_px = adj_open[i, j] * (1 + slip)
                pos[j] = Position(value / adj_px, value + fee, adj_px, i, n_lots * lot)
                trades.append({"date": i, "symbol": j, "side": "买入", "price": raw_px, "shares": n_lots * lot,
                               "value": value, "fee": fee, "pnl": None, "ret": None, "hold_days": None, "reason": "调入"})
                slots -= 1
            holdings_log.append({"date": i, "symbols": sorted(pos)})
            order = None
        # ---- 收盘：退市清算、止损、估值
        for j in list(pos):
            if last_trade[j] < i and last_trade[j] < T - 1:
                sell(j, i, adj_close[i, j], "数据中断清算", apply_slip=False)
        if bt["stop_loss"] > 0:
            for j, ps in pos.items():
                if adj_close[i, j] / ps.entry_price - 1 <= -bt["stop_loss"]:
                    pending_sell.setdefault(j, "止损")
        mv = sum(ps.shares * adj_close[i, j] for j, ps in pos.items())
        nav_rows.append((i, cash + mv, cash, len(pos)))
        if i in ranked and i < T - 1:
            order = ranked[i]
        if (i - start) % 100 == 0:
            progress((i - start) / max(T - start, 1), f"回测 {p.dates[i].date()}")

    nav = pd.DataFrame(nav_rows, columns=["i", "equity", "cash", "n_pos"])
    nav.index = p.dates[nav["i"].to_numpy()]
    nav = nav.drop(columns="i")
    nav["nav"] = nav["equity"] / bt["capital"]
    nav["bench"] = benchmark(s, nav.index)
    tr = pd.DataFrame(trades)
    if not tr.empty:
        tr["date"] = p.dates[tr["date"].to_numpy()].strftime("%Y-%m-%d")
        tr["name"] = [names[j] for j in tr["symbol"]]
        tr["symbol"] = [p.symbols[j] for j in tr["symbol"]]
    holdings = [{"date": p.dates[h["date"]].strftime("%Y-%m-%d"),
                 "symbols": [p.symbols[j] for j in h["symbols"]]} for h in holdings_log]
    final = [{"symbol": p.symbols[j], "name": names[j], "shares": ps.raw_shares,
              "value": round(ps.shares * adj_close[T - 1, j], 2),
              "ret": round(ps.shares * adj_close[T - 1, j] / ps.cost - 1, 4),
              "since": p.dates[ps.entry_day].strftime("%Y-%m-%d")} for j, ps in pos.items()]
    return {"nav": nav, "trades": tr, "holdings": holdings, "final_positions": final, "fees": fees_total}


def benchmark(s: Samples, index: pd.DatetimeIndex) -> pd.Series:
    """基准净值：指定指数，或股票池等权（前一天在池子里的股票当天收益的平均）。"""
    if s.bench_close is not None:
        b = s.bench_close.reindex(index).ffill()
        return b / b.dropna().iloc[0]
    close = s.panel.adj("close").ffill()
    ret = close / close.shift(1) - 1
    uni = s.masks.universe.shift(1, fill_value=False) & s.panel.traded
    daily = ret.where(uni).mean(axis=1).reindex(index).fillna(0)
    daily.iloc[0] = 0.0
    return (1 + daily).cumprod()
