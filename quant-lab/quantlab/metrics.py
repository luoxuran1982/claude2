"""绩效指标。"""
from __future__ import annotations

import numpy as np
import pandas as pd

DAYS = 244  # A 股一年约 244 个交易日


def _r(x, n=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, n) if np.isfinite(x) else None


def drawdown(nav: pd.Series) -> pd.Series:
    return nav / nav.cummax() - 1


def perf(nav: pd.Series, bench: pd.Series | None = None) -> dict:
    nav = nav.dropna()
    if len(nav) < 2:
        return {}
    ret = nav.pct_change().dropna()
    years = len(ret) / DAYS
    total = nav.iloc[-1] / nav.iloc[0] - 1
    cagr = (nav.iloc[-1] / nav.iloc[0]) ** (1 / years) - 1 if years > 0 and nav.iloc[-1] > 0 else np.nan
    vol = ret.std() * np.sqrt(DAYS)
    down = ret[ret < 0].std() * np.sqrt(DAYS)
    dd = drawdown(nav)
    mdd = dd.min()
    end = dd.idxmin()
    begin = nav.loc[:end].idxmax()
    under = (dd < 0).astype(int)
    longest = int(under.groupby((under == 0).cumsum()).sum().max()) if under.any() else 0
    out = {
        "total_return": _r(total), "cagr": _r(cagr), "volatility": _r(vol),
        "sharpe": _r(ret.mean() / ret.std() * np.sqrt(DAYS) if ret.std() > 0 else np.nan, 3),
        "sortino": _r(ret.mean() * DAYS / down if down > 0 else np.nan, 3),
        "max_drawdown": _r(mdd), "max_dd_start": begin.strftime("%Y-%m-%d"), "max_dd_end": end.strftime("%Y-%m-%d"),
        "longest_dd_days": longest,
        "calmar": _r(cagr / abs(mdd) if mdd < 0 else np.nan, 3),
        "win_rate_daily": _r((ret > 0).mean()), "days": int(len(ret)),
        "start": nav.index[0].strftime("%Y-%m-%d"), "end": nav.index[-1].strftime("%Y-%m-%d"),
    }
    if bench is not None:
        b = bench.reindex(nav.index).ffill().dropna()
        if len(b) > 1:
            bret = b.pct_change().reindex(ret.index).fillna(0)
            ex = ret - bret
            byears = years
            bcagr = (b.iloc[-1] / b.iloc[0]) ** (1 / byears) - 1 if byears > 0 else np.nan
            cov = np.cov(ret, bret)
            beta = cov[0, 1] / cov[1, 1] if cov[1, 1] > 0 else np.nan
            out.update({
                "bench_total_return": _r(b.iloc[-1] / b.iloc[0] - 1), "bench_cagr": _r(bcagr),
                "bench_max_drawdown": _r(drawdown(b).min()),
                "excess_cagr": _r(cagr - bcagr),
                "info_ratio": _r(ex.mean() / ex.std() * np.sqrt(DAYS) if ex.std() > 0 else np.nan, 3),
                "tracking_error": _r(ex.std() * np.sqrt(DAYS)),
                "beta": _r(beta, 3), "alpha": _r((ret.mean() - beta * bret.mean()) * DAYS),
                "excess_win_rate": _r((ex > 0).mean()),
            })
    return out


def trade_stats(trades: pd.DataFrame, nav: pd.DataFrame, fees: float) -> dict:
    if trades is None or trades.empty:
        return {"n_trades": 0}
    sells = trades[trades["side"] == "卖出"]
    buys = trades[trades["side"] == "买入"]
    years = max(len(nav) / DAYS, 1e-9)
    avg_eq = nav["equity"].mean()
    gross_win = sells.loc[sells["pnl"] > 0, "pnl"].sum()
    gross_loss = -sells.loc[sells["pnl"] < 0, "pnl"].sum()
    return {
        "n_trades": int(len(trades)), "n_buys": int(len(buys)), "n_round_trips": int(len(sells)),
        "trade_win_rate": _r((sells["pnl"] > 0).mean()) if len(sells) else None,
        "avg_trade_return": _r(sells["ret"].mean()) if len(sells) else None,
        "avg_hold_days": _r(sells["hold_days"].mean(), 1) if len(sells) else None,
        "profit_factor": _r(gross_win / gross_loss if gross_loss > 0 else np.nan, 3),
        "turnover_annual": _r(trades["value"].sum() / 2 / avg_eq / years, 2),
        "fees_total": _r(fees, 2), "fees_ratio_annual": _r(fees / avg_eq / years),
        "avg_positions": _r(nav["n_pos"].mean(), 1),
        "cash_ratio_avg": _r((nav["cash"] / nav["equity"]).mean()),
    }


def monthly_table(nav: pd.Series) -> dict:
    m = nav.resample("ME").last()
    first = nav.iloc[0]
    rets = m.pct_change()
    rets.iloc[0] = m.iloc[0] / first - 1
    years = sorted(set(rets.index.year))
    table = {str(y): [None] * 12 for y in years}
    for d, v in rets.items():
        table[str(d.year)][d.month - 1] = _r(v)
    annual = nav.resample("YE").last()
    ar = annual.pct_change()
    ar.iloc[0] = annual.iloc[0] / first - 1
    return {"years": [str(y) for y in years], "table": table, "annual": {str(d.year): _r(v) for d, v in ar.items()}}
