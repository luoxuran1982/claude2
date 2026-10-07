"""实验：配置 → 样本 → 滚动训练 → 评估 → 回测 → 保存。

每次实验保存在 <数据目录>/runs/<id>/：
  config.json  summary.json  nav.csv  trades.csv  predictions.parquet  picks.json  models/*.pkl
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import time
import warnings
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import __version__, backtest, config, dataset, evaluate, metrics, trainer
from .data.store import DataStore

log = logging.getLogger("quantlab")
Progress = Callable[[float, str], None]


def _sub(progress: Progress, lo: float, hi: float) -> Progress:
    return lambda p, m: progress(lo + (hi - lo) * min(max(p, 0), 1), m)


def _clean(obj):
    """把 NaN/inf 变成 None，numpy 标量变成 Python 数，便于 JSON。"""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.floating, float)):
        return float(obj) if np.isfinite(obj) else None
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


class Runs:
    def __init__(self, root: Path):
        self.root = Path(root) / "runs"
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", run_id or ""):
            raise ValueError("实验编号无效")
        p = self.root / run_id
        if not p.exists():
            raise LookupError("实验不存在")
        return p

    def list(self) -> list[dict]:
        out = []
        for d in self.root.iterdir():
            f = d / "summary.json"
            if f.exists():
                try:
                    s = json.loads(f.read_text(encoding="utf-8"))
                    out.append({k: s.get(k) for k in ("id", "name", "created", "dataset_title", "models",
                                                      "perf", "ic", "elapsed", "horizon", "top_k", "n_features")})
                except (OSError, ValueError):
                    continue
        return sorted(out, key=lambda r: r.get("created") or "", reverse=True)

    def summary(self, run_id: str) -> dict:
        return json.loads((self.path(run_id) / "summary.json").read_text(encoding="utf-8"))

    def config(self, run_id: str) -> dict:
        return json.loads((self.path(run_id) / "config.json").read_text(encoding="utf-8"))

    def nav(self, run_id: str) -> pd.DataFrame:
        return pd.read_csv(self.path(run_id) / "nav.csv", index_col=0, parse_dates=True)

    def trades(self, run_id: str) -> pd.DataFrame:
        f = self.path(run_id) / "trades.csv"
        return pd.read_csv(f, dtype={"symbol": str}) if f.exists() and f.stat().st_size > 5 else pd.DataFrame()

    def predictions(self, run_id: str) -> pd.DataFrame:
        return pd.read_parquet(self.path(run_id) / "predictions.parquet")

    def picks(self, run_id: str) -> dict:
        return json.loads((self.path(run_id) / "picks.json").read_text(encoding="utf-8"))

    def delete(self, run_id: str) -> None:
        shutil.rmtree(self.path(run_id))

    def rename(self, run_id: str, name: str) -> None:
        p = self.path(run_id) / "summary.json"
        s = json.loads(p.read_text(encoding="utf-8"))
        s["name"] = str(name)[:80]
        p.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")


def run(store: DataStore, runs: Runs, cfg_in: dict, progress: Progress = lambda p, m: None) -> str:
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return _run(store, runs, cfg_in, progress)


def _run(store: DataStore, runs: Runs, cfg_in: dict, progress: Progress) -> str:
    t_start = time.time()
    cfg = config.normalize(cfg_in)
    meta = store.meta(cfg["dataset"])
    progress(0.0, "准备样本")
    s = dataset.build(store, cfg, _sub(progress, 0.0, 0.3))
    log.info("样本 %s × %s", s.X.shape[0], s.X.shape[1])

    wf = trainer.walk_forward(s, cfg, progress)
    pred = wf["pred"]
    oos = np.isfinite(pred)
    if not oos.any():
        raise ValueError("没有产生样本外预测")
    lab, bt = cfg["label"], cfg["backtest"]

    progress(0.83, "评估模型")
    dates = s.decision_dates
    ic = evaluate.ic_series(pred[oos], s.y_raw[oos], s.date_idx[oos], dates)
    ic_sum = evaluate.ic_summary(ic, lab["horizon"], cfg["walk"]["step"])
    groups = evaluate.group_returns(pred[oos], s.y_raw[oos], s.date_idx[oos], dates, 5, lab["horizon"], cfg["walk"]["step"])
    per_model_ic = {}
    if len(cfg["model"]["types"]) > 1:
        for t, p in wf["per_model"].items():
            m = np.isfinite(p)
            if m.any():
                per_model_ic[t] = evaluate.ic_summary(
                    evaluate.ic_series(p[m], s.y_raw[m], s.date_idx[m], dates), lab["horizon"], cfg["walk"]["step"])
    progress(0.86, "单因子检验")
    factors = evaluate.factor_report(s, np.nonzero(oos)[0])

    progress(0.88, "组合回测")
    res = backtest.run(s, pred, bt, _sub(progress, 0.88, 0.96))
    nav = res["nav"]
    perf = metrics.perf(nav["nav"], nav["bench"])
    tstats = metrics.trade_stats(res["trades"], nav, res["fees"])
    monthly = metrics.monthly_table(nav["nav"])

    progress(0.97, "保存结果")
    run_id = time.strftime("r%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"
    out = runs.root / run_id
    (out / "models").mkdir(parents=True)
    (out / "config.json").write_text(json.dumps(_clean(cfg), ensure_ascii=False, indent=1), encoding="utf-8")
    nav.to_csv(out / "nav.csv", float_format="%.6f")
    if not res["trades"].empty:
        res["trades"].to_csv(out / "trades.csv", index=False, float_format="%.4f")
    pdf = pd.DataFrame({"date": s.dates, "symbol": np.asarray(s.panel.symbols)[s.sym_idx],
                        "score": pred.astype("float32"), "fwd_ret": s.y_raw})
    pdf[oos].to_parquet(out / "predictions.parquet", index=False)
    for t, model in wf["final_models"].items():
        model.save(out / "models" / f"{t}.pkl")

    latest_date = dates[-1]
    rows = wf["latest_rows"]
    picks = []
    if len(rows):
        order = np.argsort(-np.nan_to_num(wf["latest_score"], nan=-9))
        close = s.panel.raw["close"].iloc[-1]
        for rank, k in enumerate(order[:100]):
            r = rows[k]
            sym = s.panel.symbols[s.sym_idx[r]]
            picks.append({"rank": rank + 1, "symbol": sym, "name": s.panel.names.get(sym, ""),
                          "score": _clean(round(float(wf["latest_score"][k]) + 0.5, 4)),
                          "close": _clean(close.get(sym))})
    (out / "picks.json").write_text(json.dumps({"date": latest_date.strftime("%Y-%m-%d"), "picks": picks,
                                                "n_universe": int(len(rows))}, ensure_ascii=False), encoding="utf-8")

    imp = {t: sorted([{"name": n, "value": round(float(v), 5)} for n, v in zip(s.feature_names, vals)],
                     key=lambda r: -r["value"]) for t, vals in wf["importance"].items()}
    ic_plot = ic.copy()
    summary = {
        "id": run_id, "name": cfg["name"] or f"{'+'.join(cfg['model']['types'])} · {lab['horizon']}日 · 前{bt['top_k']}",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "version": __version__,
        "dataset": cfg["dataset"], "dataset_title": meta.get("title"), "demo_data": bool(meta.get("demo")),
        "models": cfg["model"]["types"], "horizon": lab["horizon"], "top_k": bt["top_k"],
        "n_features": len(s.feature_names), "n_samples": int(len(s.y)), "n_oos": int(oos.sum()),
        "n_stocks": len(s.panel.symbols), "benchmark": s.benchmark or "股票池等权",
        "elapsed": round(time.time() - t_start, 1),
        "perf": perf, "trade_stats": tstats, "monthly": monthly,
        "ic": ic_sum, "per_model_ic": per_model_ic,
        "ic_series": {"dates": [d.strftime("%Y-%m-%d") for d in ic_plot.index],
                      "ic": [_clean(v) for v in ic_plot["ic"]], "rank_ic": [_clean(v) for v in ic_plot["rank_ic"]]},
        "groups": groups, "importance": imp, "factors": factors, "folds": wf["folds"],
        "final_positions": res["final_positions"], "latest_date": latest_date.strftime("%Y-%m-%d"),
    }
    (out / "summary.json").write_text(json.dumps(_clean(summary), ensure_ascii=False), encoding="utf-8")
    progress(1.0, f"完成：年化 {(perf.get('cagr') or 0) * 100:.1f}%，夏普 {perf.get('sharpe')}")
    return run_id


def factor_study(store: DataStore, cfg_in: dict, progress: Progress = lambda p, m: None) -> dict:
    """只做单因子检验，不训练模型（因子研究页）。"""
    with warnings.catch_warnings(), np.errstate(all="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return _factor_study(store, cfg_in, progress)


def _factor_study(store: DataStore, cfg_in: dict, progress: Progress) -> dict:
    cfg = config.normalize(cfg_in)
    s = dataset.build(store, cfg, _sub(progress, 0.0, 0.7))
    progress(0.75, "计算单因子 IC")
    rows = np.nonzero(np.isfinite(s.y_raw))[0]
    rep = evaluate.factor_report(s, rows)
    from . import features as F
    desc = {f.name: (f.desc, F.GROUPS[f.group]) for f in F.REGISTRY.values()}
    for r in rep:
        r["desc"], r["group"] = desc.get(r["name"], ("", ""))
    progress(1.0, f"完成：{len(rep)} 个因子")
    return _clean({"factors": rep, "horizon": cfg["label"]["horizon"], "n_samples": int(len(rows)),
                   "start": str(s.decision_dates[0].date()), "end": str(s.decision_dates[-1].date())})
