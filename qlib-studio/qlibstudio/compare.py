"""与 QuantLab 对比：读取「实验结果交换格式」JSON，并排比较。

交换格式（format = "quant-experiment-result"，format_version = 1）由 Qlib 量化研究台定义，
QuantLab 1.2.0 的「导出实验结果」按同一格式输出；本应用每次实验也导出一份（runs/<编号>/export.json），
所以两边的结果可以放在一起比。完整字段说明见 docs/04 与 QuantLab 的对比.md「交换格式」一节，
样例见 samples/quantlab-result-example.json。

    {
      "format": "quant-experiment-result", "format_version": 1,
      "app": "QuantLab" | "Qlib 量化研究台", "app_version": "1.2.0",
      "run_id": "...", "name": "...", "created": "2026-10-14 10:00:00",
      "dataset": {"title": "...", "source": "...", "n_stocks": 300, "start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},
      "period": {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"},          # 样本外回测区间
      "settings": {"factors": "...", "n_features": 158, "models": ["lgbm"], "horizon": 1, "top_k": 20,
                   "rebalance": "...", "deal_price": "...", "costs": "...", "benchmark": "..."},
      "metrics": {"total_return": 0.12, "annual_return": 0.05, "volatility": 0.2, "sharpe": 0.8,
                  "max_drawdown": -0.15, "excess_annual_return": 0.03, "info_ratio": 0.6,
                  "ic_mean": 0.03, "rank_ic_mean": 0.04, "icir": 0.3, "rank_icir": 0.35,
                  "turnover": 0.1, "win_rate_daily": 0.52},
      "daily": {"dates": [...], "return": [...], "bench_return": [...]},   # 扣费后的组合日收益、基准日收益（小数）
      "ic": {"dates": [...], "ic": [...], "rank_ic": [...]}               # 每个决策日的 IC / RankIC
    }
所有收益都用小数（0.05 = 5%）；缺少的指标写 null。必填：format、format_version、app、period、daily.dates、daily.return。
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

FORMAT = "quant-experiment-result"
VERSION = 1
METRIC_LABELS = {
    "total_return": ("累计收益", "pct"), "annual_return": ("年化收益（复利）", "pct"), "volatility": ("年化波动", "pct"),
    "sharpe": ("夏普比率", "num"), "max_drawdown": ("最大回撤", "pct"), "excess_annual_return": ("年化超额收益", "pct"),
    "info_ratio": ("信息比率", "num"), "ic_mean": ("IC 均值", "num"), "rank_ic_mean": ("RankIC 均值", "num"),
    "icir": ("ICIR", "num"), "rank_icir": ("RankICIR", "num"), "turnover": ("日均换手", "pct"),
    "win_rate_daily": ("日胜率", "pct"),
}


class FormatError(ValueError):
    pass


def _f(x):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def export_from_result(res: dict) -> dict:
    """本应用的 result.json → 交换格式。"""
    m, ic, cfg, d = res.get("metrics", {}), res.get("ic", {}), res.get("config", {}), res.get("daily", {})
    ret = d.get("return") or []
    bret = d.get("bench_return") or []
    ex = [a - b for a, b in zip(ret, bret) if a is not None and b is not None]
    ir = None
    if len(ex) > 2 and np.std(ex) > 0:
        ir = float(np.mean(ex) / np.std(ex, ddof=1) * np.sqrt(244))
    st = cfg.get("strategy", {})
    costs = cfg.get("costs", {})
    return {
        "format": FORMAT, "format_version": VERSION, "app": "Qlib 量化研究台", "app_version": res.get("app_version"),
        "engine": f"Qlib {res.get('qlib', {}).get('version', '')}",
        "run_id": res.get("id"), "name": res.get("name"), "created": res.get("created"),
        "dataset": {"title": res.get("dataset", {}).get("title"), "source": res.get("dataset", {}).get("source"),
                    "start": res.get("dataset", {}).get("start"), "end": res.get("dataset", {}).get("end")},
        "period": {"start": m.get("start"), "end": m.get("end")},
        "settings": {"factors": cfg.get("factors"), "models": [cfg.get("model")], "horizon": cfg.get("label_horizon"),
                     "top_k": st.get("topk"), "rebalance": f"每日 TopkDropout（每天最多换 {st.get('n_drop')} 只）",
                     "deal_price": {"close": "当日收盘价", "open": "当日开盘价"}.get(cfg.get("deal_price"), cfg.get("deal_price")),
                     "costs": f"买 {costs.get('open_cost')}、卖 {costs.get('close_cost')}（含印花税）、最低 {costs.get('min_cost')} 元",
                     "limit": {"board": "按板块涨跌停（主板 10%、ST 5%、创业板/科创板 20%、北交所 30%）",
                               "qlib": "上游默认 ±9.5%", "none": "不限制"}.get(cfg.get("limit"), cfg.get("limit")),
                     "benchmark": res.get("benchmark"), "pool": cfg.get("pool"),
                     "n_features": {"Alpha158": 158, "Alpha360": 360}.get(cfg.get("factors")),
                     "rolling": (f"每 {(cfg.get('rolling') or {}).get('step_days')} 个交易日重训，"
                                 + ("训练窗口扩展" if (cfg.get("rolling") or {}).get("expanding", True) else "训练窗口固定长度滑动"))
                                if (cfg.get("rolling") or {}).get("enabled") else "不滚动（一次切分）"},
        "metrics": {"total_return": m.get("total_return"), "annual_return": m.get("annual_return"),
                    "volatility": m.get("volatility"), "sharpe": m.get("sharpe"), "max_drawdown": m.get("max_drawdown"),
                    "excess_annual_return": m.get("excess_annual_return"), "info_ratio": ir,
                    "ic_mean": ic.get("ic_mean"), "rank_ic_mean": ic.get("rank_ic_mean"), "icir": ic.get("icir"),
                    "rank_icir": ic.get("rank_icir"), "turnover": m.get("turnover_daily"),
                    "win_rate_daily": m.get("win_rate_daily")},
        "daily": {"dates": d.get("dates", []), "return": ret, "bench_return": bret},
        "ic": {"dates": ic.get("dates", []), "ic": ic.get("ic_series", []), "rank_ic": ic.get("rank_ic_series", [])},
    }


def from_quantlab_run_dir(run_dir) -> dict:
    """QuantLab 1.1 的实验目录（<QuantLab 数据目录>/runs/<编号>/，含 summary.json、nav.csv、config.json）→ 交换格式。

    在 QuantLab 自己的「导出实验结果」功能（1.2.0）出来之前，可以用这个直接读它的实验目录；字段对应关系：
    perf.cagr → annual_return，perf.excess_cagr → excess_annual_return，ic.ic.mean / ic.rank_ic.mean → ic_mean / rank_ic_mean，
    ic.*.ir → icir / rank_icir，trade_stats.turnover_annual ÷ 244 → turnover（日均），nav.csv 的 nav / bench 列 → 日收益。"""
    d = Path(run_dir)
    if not (d / "summary.json").is_file() or not (d / "nav.csv").is_file():
        raise FormatError(f"{d} 不是 QuantLab 的实验目录（应有 summary.json 和 nav.csv）")
    s = json.loads((d / "summary.json").read_text(encoding="utf-8"))
    cfg = json.loads((d / "config.json").read_text(encoding="utf-8")) if (d / "config.json").is_file() else {}
    nav = pd.read_csv(d / "nav.csv", index_col=0, parse_dates=True)
    ret = nav["nav"].pct_change().fillna(0.0)
    bret = nav["bench"].pct_change().fillna(0.0) if "bench" in nav else None
    perf, ic, ts = s.get("perf") or {}, s.get("ic") or {}, s.get("trade_stats") or {}
    bt, lab, w = cfg.get("backtest") or {}, cfg.get("label") or {}, cfg.get("walk") or {}
    ics = s.get("ic_series") or {}
    turn = ts.get("turnover_annual")
    return validate({
        "format": FORMAT, "format_version": VERSION, "app": "QuantLab", "app_version": s.get("version"),
        "engine": "QuantLab（由 Qlib 量化研究台从实验目录转换）", "run_id": s.get("id"), "name": s.get("name"),
        "created": s.get("created"),
        "dataset": {"title": s.get("dataset_title"), "source": "demo" if s.get("demo_data") else None,
                    "n_stocks": s.get("n_stocks")},
        "period": {"start": perf.get("start"), "end": perf.get("end")},
        "settings": {"factors": f"QuantLab 因子 {s.get('n_features')} 个", "n_features": s.get("n_features"),
                     "models": s.get("models"), "horizon": s.get("horizon"), "top_k": s.get("top_k"),
                     "rebalance": f"每 {bt.get('rebalance_days')} 个交易日，持仓缓冲 {bt.get('buffer')} 倍" if bt else None,
                     "deal_price": "T+1 开盘价", "benchmark": s.get("benchmark"),
                     "costs": (f"佣金 {bt.get('commission')}（最低 {bt.get('min_commission')} 元）、印花税 {bt.get('stamp_tax')}、"
                               f"滑点 {bt.get('slippage')}") if bt else None,
                     "rolling": {"train_days": w.get("train_days"), "retrain_days": w.get("retrain_days")} if w else None,
                     "label": lab or None},
        "metrics": {"total_return": perf.get("total_return"), "annual_return": perf.get("cagr"),
                    "volatility": perf.get("volatility"), "sharpe": perf.get("sharpe"), "max_drawdown": perf.get("max_drawdown"),
                    "excess_annual_return": perf.get("excess_cagr"), "info_ratio": perf.get("info_ratio"),
                    "ic_mean": (ic.get("ic") or {}).get("mean"), "rank_ic_mean": (ic.get("rank_ic") or {}).get("mean"),
                    "icir": (ic.get("ic") or {}).get("ir"), "rank_icir": (ic.get("rank_ic") or {}).get("ir"),
                    "turnover": turn / 244 if isinstance(turn, (int, float)) else None,
                    "win_rate_daily": perf.get("win_rate_daily")},
        "daily": {"dates": [x.strftime("%Y-%m-%d") for x in nav.index], "return": [round(float(v), 8) for v in ret],
                  "bench_return": [round(float(v), 8) for v in bret] if bret is not None else []},
        "ic": {"dates": ics.get("dates") or [], "ic": ics.get("ic") or [], "rank_ic": ics.get("rank_ic") or []},
    })


def validate(obj) -> dict:
    """检查交换格式，返回规范化后的对象；不合格抛 FormatError（中文原因）。"""
    if not isinstance(obj, dict):
        raise FormatError("文件内容不是 JSON 对象")
    if obj.get("format") != FORMAT:
        hint = ""
        if "perf" in obj and "ic" in obj:
            hint = "（这像是 QuantLab 内部的 summary.json；请在 QuantLab 1.2.0 以上用「导出实验结果」生成交换格式文件）"
        raise FormatError(f"format 应为 \"{FORMAT}\"，实际是 {obj.get('format')!r}{hint}")
    if int(obj.get("format_version") or 0) > VERSION:
        raise FormatError(f"格式版本 {obj.get('format_version')} 比本应用支持的 {VERSION} 新，请升级 Qlib 量化研究台")
    daily = obj.get("daily") or {}
    dates, ret = daily.get("dates") or [], daily.get("return") or []
    if not dates or len(dates) != len(ret):
        raise FormatError("daily.dates 和 daily.return 必须都有、且长度相同")
    try:
        pd.to_datetime(dates)
    except (TypeError, ValueError):
        raise FormatError("daily.dates 里有无法识别的日期（应为 YYYY-MM-DD）") from None
    bret = daily.get("bench_return") or []
    if bret and len(bret) != len(dates):
        raise FormatError("daily.bench_return 长度与 daily.dates 不同")
    ic = obj.get("ic") or {}
    if ic.get("dates") and len(ic.get("rank_ic") or ic.get("ic") or []) != len(ic["dates"]):
        raise FormatError("ic.dates 与 ic.ic / ic.rank_ic 长度不同")
    out = dict(obj)
    out["metrics"] = {k: _f(v) for k, v in (obj.get("metrics") or {}).items()}
    out.setdefault("period", {"start": dates[0], "end": dates[-1]})
    out["period"] = {"start": (out["period"] or {}).get("start") or dates[0], "end": (out["period"] or {}).get("end") or dates[-1]}
    return out


def load(path_or_bytes) -> dict:
    if isinstance(path_or_bytes, (bytes, bytearray)):
        text = bytes(path_or_bytes).decode("utf-8-sig")
    else:
        text = Path(path_or_bytes).read_text(encoding="utf-8-sig")
    try:
        obj = json.loads(text)
    except ValueError as exc:
        raise FormatError(f"不是有效的 JSON：{exc}") from None
    return validate(obj)


def _metrics_from_daily(dates, ret, bret) -> dict:
    r = pd.Series([x if x is not None else 0.0 for x in ret], index=pd.to_datetime(dates), dtype=float)
    out = {"days": len(r)}
    if len(r) < 2:
        return out
    nav = (1 + r).cumprod()
    years = len(r) / 244
    out["total_return"] = float(nav.iloc[-1] - 1)
    out["annual_return"] = float(nav.iloc[-1] ** (1 / years) - 1) if nav.iloc[-1] > 0 else None
    out["volatility"] = float(r.std() * np.sqrt(244))
    out["sharpe"] = float(r.mean() / r.std() * np.sqrt(244)) if r.std() > 0 else None
    out["max_drawdown"] = float((nav / nav.cummax() - 1).min())
    if bret:
        b = pd.Series([x if x is not None else 0.0 for x in bret], index=r.index, dtype=float)
        bn = (1 + b).cumprod()
        ex = r - b
        out["excess_annual_return"] = (out["annual_return"] or 0) - float(bn.iloc[-1] ** (1 / years) - 1)
        out["info_ratio"] = float(ex.mean() / ex.std() * np.sqrt(244)) if ex.std() > 0 else None
    return out


def side_by_side(items: list[dict], common_only: bool = True) -> dict:
    """多份结果并排：重叠区间、各自在重叠区间上重算的收益指标、净值曲线（重叠区间起点归一）、IC 序列。"""
    if not items:
        return {"items": []}
    starts = [pd.Timestamp(x["daily"]["dates"][0]) for x in items]
    ends = [pd.Timestamp(x["daily"]["dates"][-1]) for x in items]
    lo, hi = max(starts), min(ends)
    overlap = lo <= hi
    warnings = []
    if not overlap:
        warnings.append("几份结果的回测区间没有重叠，下面按各自完整区间显示，不能直接比较")
    elif any(s != lo for s in starts) or any(e != hi for e in ends):
        warnings.append(f"回测区间不同，收益指标已按共同区间 {lo.date()} ~ {hi.date()} 重新计算；IC 和表里「原始」列仍是各自全区间")
    titles = {(x.get("dataset") or {}).get("title") for x in items}
    if len(titles) > 1:
        warnings.append("几份结果用的数据集名称不同，可能不是同一份数据：" + "、".join(str(t) for t in titles))
    out = []
    for x in items:
        d = x["daily"]
        idx = pd.to_datetime(d["dates"])
        r = pd.Series([v if v is not None else 0.0 for v in d["return"]], index=idx, dtype=float)
        b = pd.Series([v if v is not None else 0.0 for v in (d.get("bench_return") or [0.0] * len(idx))], index=idx, dtype=float)
        if overlap and common_only:
            m = (idx >= lo) & (idx <= hi)
            r, b = r[m], b[m]
        nav = (1 + r).cumprod()
        bnav = (1 + b).cumprod()
        recalc = _metrics_from_daily([t.strftime("%Y-%m-%d") for t in r.index], r.tolist(),
                                     b.tolist() if d.get("bench_return") else [])
        ic = x.get("ic") or {}
        out.append({
            "app": x.get("app"), "app_version": x.get("app_version"), "name": x.get("name"), "run_id": x.get("run_id"),
            "dataset": x.get("dataset"), "settings": x.get("settings"), "period": x.get("period"),
            "metrics_original": x.get("metrics"), "metrics_common": recalc,
            "curve": {"dates": [t.strftime("%Y-%m-%d") for t in nav.index], "nav": [round(v, 6) for v in nav],
                      "bench_nav": [round(v, 6) for v in bnav], "excess": [round(v, 6) for v in (nav / bnav)]},
            "ic": {"dates": ic.get("dates") or [], "ic": ic.get("ic") or [], "rank_ic": ic.get("rank_ic") or []},
        })
    return {"items": out, "common": {"start": str(lo.date()), "end": str(hi.date())} if overlap else None,
            "warnings": warnings, "labels": METRIC_LABELS}
