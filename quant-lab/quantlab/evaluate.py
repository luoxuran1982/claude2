"""模型/因子评估：IC、RankIC、ICIR、分层收益、多空收益、单因子检验。"""
from __future__ import annotations

import numpy as np
import pandas as pd

from .dataset import Samples


def _r(x, n=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, n) if np.isfinite(x) else None


def ic_series(score: np.ndarray, y: np.ndarray, date_idx: np.ndarray, dates) -> pd.DataFrame:
    df = pd.DataFrame({"s": score, "y": y, "d": date_idx})
    df = df[np.isfinite(df["s"]) & np.isfinite(df["y"])]
    if df.empty:
        return pd.DataFrame(columns=["ic", "rank_ic"])
    df["sr"] = df.groupby("d")["s"].rank()
    df["yr"] = df.groupby("d")["y"].rank()
    g = df.groupby("d")
    n = g.size()
    out = pd.DataFrame({"ic": g.apply(lambda x: x["s"].corr(x["y"]), include_groups=False),
                        "rank_ic": g.apply(lambda x: x["sr"].corr(x["yr"]), include_groups=False)})
    out = out[n >= 10]
    out.index = pd.DatetimeIndex(np.asarray(dates)[out.index])
    return out


def ic_summary(ic: pd.DataFrame, horizon: int, step: int) -> dict:
    def stats(col):
        s = ic[col].dropna()
        if len(s) < 2:
            return {}
        per_year = 244 / step
        return {"mean": _r(s.mean()), "std": _r(s.std()), "ir": _r(s.mean() / s.std() if s.std() > 0 else np.nan),
                "ir_annual": _r(s.mean() / s.std() * np.sqrt(per_year) if s.std() > 0 else np.nan),
                "positive": _r((s > 0).mean()), "t": _r(s.mean() / s.std() * np.sqrt(len(s)) if s.std() > 0 else np.nan, 2),
                "n": int(len(s))}
    return {"ic": stats("ic"), "rank_ic": stats("rank_ic")}


def group_returns(score, y, date_idx, dates, n_groups: int = 5, horizon: int = 5, step: int = 5) -> dict:
    """按预测分成 n 组，每组的平均未来收益；以及按不重叠持有期复利的累计曲线。"""
    df = pd.DataFrame({"s": score, "y": y, "d": date_idx})
    df = df[np.isfinite(df["s"]) & np.isfinite(df["y"])]
    if df.empty:
        return {}
    df["g"] = df.groupby("d")["s"].transform(
        lambda x: np.minimum((x.rank(pct=True) * n_groups).clip(upper=n_groups - 0.001).astype(int), n_groups - 1))
    tab = df.groupby(["d", "g"])["y"].mean().unstack()
    tab.index = pd.DatetimeIndex(np.asarray(dates)[tab.index])
    mean = tab.mean()
    every = max(1, int(np.ceil(horizon / step)))  # 只取不重叠的持有期
    nonover = tab.iloc[::every]
    curves = (1 + nonover.fillna(0)).cumprod()
    ls = (nonover[n_groups - 1] - nonover[0]).fillna(0)
    return {
        "groups": [f"第{i + 1}组" for i in range(n_groups)],
        "mean": [_r(mean.get(i), 5) for i in range(n_groups)],
        "dates": [d.strftime("%Y-%m-%d") for d in curves.index],
        "curves": [[_r(v) for v in curves[i]] if i in curves else [] for i in range(n_groups)],
        "long_short": [_r(v) for v in (1 + ls / 2).cumprod()],
        "long_short_mean": _r(ls.mean(), 5),
        "monotonic": _r(pd.Series(mean.values).corr(pd.Series(range(len(mean))), method="spearman"), 3),
    }


def factor_report(s: Samples, rows: np.ndarray | None = None) -> list[dict]:
    """单因子检验：每个因子单独作为打分时的 RankIC、ICIR、多空分组差。"""
    rows = np.arange(len(s.y)) if rows is None else rows
    y = s.y_raw[rows]
    d = s.date_idx[rows]
    yr = pd.Series(y).groupby(d).rank().to_numpy()
    out = []
    for k, name in enumerate(s.feature_names):
        x = s.X[rows, k].astype("float64")
        df = pd.DataFrame({"x": x, "y": yr, "d": d, "raw": y})
        df = df[np.isfinite(df["x"]) & np.isfinite(df["y"])]
        if df.empty:
            continue
        df["xr"] = df.groupby("d")["x"].rank()
        g = df.groupby("d")
        ics = g.apply(lambda t: t["xr"].corr(t["y"]) if len(t) > 10 else np.nan, include_groups=False).dropna()
        q = g["x"].transform(lambda t: t.rank(pct=True))
        top = df.loc[q > 0.8].groupby("d")["raw"].mean()
        bot = df.loc[q <= 0.2].groupby("d")["raw"].mean()
        spread = (top - bot).mean()
        out.append({"name": name, "rank_ic": _r(ics.mean()), "icir": _r(ics.mean() / ics.std() if ics.std() > 0 else np.nan),
                    "positive": _r((ics > 0).mean()), "spread": _r(spread, 5), "coverage": _r(len(df) / max(len(rows), 1), 3),
                    "series": [_r(v) for v in ics.rolling(12, min_periods=1).mean().iloc[::max(1, len(ics) // 120)]]})
    out.sort(key=lambda r: abs(r["rank_ic"] or 0), reverse=True)
    return out
