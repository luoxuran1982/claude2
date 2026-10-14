"""导出实验结果（JSON），给「Qlib 量化研究台」等其他程序读取对比。

格式说明见 docs/04 实验结果导出格式.md；字段改动时同时改 FORMAT_VERSION 和文档。
"""
from __future__ import annotations

import time

import numpy as np
import pandas as pd

from . import APP_NAME, __version__
from .data.store import DataStore, symbol_source

FORMAT = "quantlab.experiment-result"
FORMAT_VERSION = "1.0"


def _num(v, n=8):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, n) if np.isfinite(v) else None


def _universe(runs, run_id: str, summary: dict) -> list[str]:
    syms = summary.get("universe_symbols")
    if syms:
        return list(syms)
    try:   # 1.2.0 之前的实验没记录股票池：用有样本外预测的股票代替
        return sorted(runs.predictions(run_id)["symbol"].astype(str).unique().tolist())
    except (LookupError, OSError, KeyError):
        return []


def build(runs, store: DataStore | None, run_id: str) -> dict:
    s = runs.summary(run_id)
    cfg = runs.config(run_id)
    nav = runs.nav(run_id)
    perf, ic = s.get("perf") or {}, s.get("ic") or {}
    folds = [f for f in s.get("folds") or []]
    trained = [f for f in folds if not f.get("skipped")]
    ret = nav["nav"].pct_change()
    bret = nav["bench"].pct_change()
    dates = nav.index.strftime("%Y-%m-%d").tolist()
    universe = _universe(runs, run_id, s)

    ds_info = {"id": s.get("dataset"), "title": s.get("dataset_title"), "demo": bool(s.get("demo_data"))}
    if store is not None and s.get("dataset"):
        try:
            meta = store.meta(s["dataset"])
            per = {m["symbol"]: m for m in meta.get("symbols", [])}
            srcs: dict[str, int] = {}
            for sym in universe or per:
                if sym in per:
                    src = symbol_source(meta, per[sym])[0]
                    srcs[src] = srcs.get(src, 0) + 1
            ds_info.update(start=meta.get("start"), end=meta.get("end"), updated=meta.get("updated") or meta.get("created"),
                           sources=srcs, adjust=sorted({per[x].get("adjust", "") for x in universe if x in per}))
        except (LookupError, ValueError, OSError):
            ds_info["missing"] = True   # 数据集已删除，只导出实验里保存的信息

    ics = s.get("ic_series") or {}
    rank = ic.get("rank_ic") or {}
    pic = ic.get("ic") or {}
    tstats = s.get("trade_stats") or {}
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "app": {"name": APP_NAME, "version": __version__},
        "run": {"id": s.get("id"), "name": s.get("name"), "created": s.get("created"),
                "app_version": s.get("version"), "elapsed_seconds": s.get("elapsed")},
        "dataset": ds_info,
        "config": cfg,
        "periods": {
            "data": {"start": (cfg.get("period") or {}).get("start") or ds_info.get("start"),
                     "end": (cfg.get("period") or {}).get("end") or ds_info.get("end")},
            "train": {"start": trained[0]["train_start"] if trained else None,
                      "end": trained[-1]["train_end"] if trained else None},
            "test": {"start": perf.get("start"), "end": perf.get("end"), "days": perf.get("days")},
            "folds": [{k: f.get(k) for k in ("fold", "train_start", "train_end", "test_start", "test_end",
                                             "n_train", "n_valid", "n_test", "skipped")} for f in folds],
        },
        "universe": {"rules": cfg.get("universe"), "n_symbols": len(universe), "symbols": universe,
                     "benchmark": s.get("benchmark")},
        "model": {"types": s.get("models"), "horizon": s.get("horizon"), "n_features": s.get("n_features"),
                  "features": (cfg.get("features") or {}).get("resolved"),
                  "label": cfg.get("label"), "walk": cfg.get("walk")},
        "daily_returns": {
            "dates": dates[1:],
            "strategy": [_num(v) for v in ret.iloc[1:]],
            "benchmark": [_num(v) for v in bret.iloc[1:]],
        },
        "nav": {"dates": dates, "strategy": [_num(v) for v in nav["nav"] / nav["nav"].iloc[0]],
                "benchmark": [_num(v) for v in nav["bench"] / nav["bench"].iloc[0]]},
        "ic": {"dates": ics.get("dates", []), "ic": ics.get("ic", []), "rank_ic": ics.get("rank_ic", []),
               "horizon": s.get("horizon"), "step": (cfg.get("walk") or {}).get("step")},
        "metrics": {
            "annual_return": perf.get("cagr"), "total_return": perf.get("total_return"),
            "annual_volatility": perf.get("volatility"), "sharpe": perf.get("sharpe"), "sortino": perf.get("sortino"),
            "max_drawdown": perf.get("max_drawdown"), "calmar": perf.get("calmar"),
            "win_rate_daily": perf.get("win_rate_daily"),
            "benchmark_annual_return": perf.get("bench_cagr"), "benchmark_max_drawdown": perf.get("bench_max_drawdown"),
            "excess_annual_return": perf.get("excess_cagr"), "information_ratio": perf.get("info_ratio"),
            "tracking_error": perf.get("tracking_error"), "beta": perf.get("beta"), "alpha": perf.get("alpha"),
            "ic_mean": pic.get("mean"), "icir": pic.get("ir"), "rank_ic_mean": rank.get("mean"), "rank_icir": rank.get("ir"),
            "rank_ic_positive_ratio": rank.get("positive"),
            "turnover_annual": tstats.get("turnover_annual"), "fees_ratio_annual": tstats.get("fees_ratio_annual"),
            "n_trades": tstats.get("n_trades"), "trade_win_rate": tstats.get("trade_win_rate"),
        },
        "assumptions": {
            "trading_days_per_year": 244,
            "returns": "日收益 = 当日收盘净值 / 前一日收盘净值 − 1（已扣佣金、印花税、滑点）",
            "a_share_rules": "T+1、涨停买不进/跌停卖不出、停牌不交易、一手 100 股",
            "ic": "每个调仓日：模型得分与未来 horizon 日收益的截面相关（IC 为皮尔逊，RankIC 为斯皮尔曼）",
        },
    }


def validate(doc: dict) -> list[str]:
    """检查导出文件结构（测试和对方程序都可以用）。返回问题列表，空 = 通过。"""
    problems = []
    for key in ("format", "format_version", "app", "run", "config", "periods", "universe", "daily_returns", "ic", "metrics"):
        if key not in doc:
            problems.append(f"缺少字段 {key}")
    if problems:
        return problems
    if doc["format"] != FORMAT:
        problems.append("format 不对")
    dr = doc["daily_returns"]
    if not (len(dr["dates"]) == len(dr["strategy"]) == len(dr["benchmark"])):
        problems.append("daily_returns 各序列长度不一致")
    if dr["dates"] != sorted(dr["dates"]):
        problems.append("daily_returns.dates 没有按时间排序")
    ic = doc["ic"]
    if not (len(ic["dates"]) == len(ic["ic"]) == len(ic["rank_ic"])):
        problems.append("ic 各序列长度不一致")
    for k in ("annual_return", "sharpe", "max_drawdown"):
        if k not in doc["metrics"]:
            problems.append(f"metrics 缺少 {k}")
    return problems


def frame(doc: dict) -> pd.DataFrame:
    """读取方便函数：daily_returns → DataFrame（index=日期，列 strategy/benchmark）。"""
    dr = doc["daily_returns"]
    return pd.DataFrame({"strategy": dr["strategy"], "benchmark": dr["benchmark"]}, index=pd.to_datetime(dr["dates"]))
