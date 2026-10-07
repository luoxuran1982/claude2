"""滚动训练（walk-forward）：每段测试期只用它之前的数据训练，训练集与测试集之间留出标签长度的隔离带。

    |------ 训练窗口 ------|-- 验证 --|隔离|== 测试(样本外) ==|
                                          |------ 训练窗口 ------|-- 验证 --|隔离|== 测试 ==| ...

隔离带 = 预测周期 H + 1 天：训练样本的标签要用到 t+1+H 日开盘价，必须在测试期开始前就已知。
所有回测都只用拼起来的样本外预测，杜绝“用未来训练、在过去测试”。
"""
from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np
import pandas as pd

from . import models as MD
from .dataset import Samples

log = logging.getLogger("quantlab")


def cs_rank(values: np.ndarray, date_idx: np.ndarray) -> np.ndarray:
    return pd.Series(values).groupby(date_idx).rank(pct=True).to_numpy() - 0.5


def plan_folds(cal_pos: np.ndarray, horizon: int, train_days: int, retrain_days: int,
               min_train_days: int) -> list[dict]:
    """按决策日（在交易日历中的位置 cal_pos）切分训练/测试段。"""
    gap = horizon + 1
    folds = []
    first = cal_pos[0]
    k = int(np.searchsorted(cal_pos, first + min_train_days + gap))
    while k < len(cal_pos):
        t0 = cal_pos[k]
        end = int(np.searchsorted(cal_pos, t0 + retrain_days))
        train_hi = t0 - gap                              # 训练样本决策日 ≤ train_hi
        train_lo = train_hi - train_days if train_days > 0 else -1
        train = np.nonzero((cal_pos <= train_hi) & (cal_pos > train_lo))[0]
        folds.append({"train": train, "test": np.arange(k, max(end, k + 1))})
        k = max(end, k + 1)
    return folds


def _split_valid(train_dates: np.ndarray, cal_pos: np.ndarray, ratio: float, gap: int):
    if ratio <= 0 or len(train_dates) < 10:
        return train_dates, np.array([], dtype=int)
    nv = max(2, int(len(train_dates) * ratio))
    valid = train_dates[-nv:]
    v0 = cal_pos[valid[0]]
    fit = train_dates[cal_pos[train_dates] <= v0 - gap]
    return fit, valid


def _rows(s: Samples, date_set: np.ndarray, labeled_only: bool, tradable_only: bool) -> np.ndarray:
    m = np.isin(s.date_idx, date_set)
    if labeled_only:
        m &= np.isfinite(s.y)
    if tradable_only:
        m &= s.tradable
    return np.nonzero(m)[0]


def _ic(pred: np.ndarray, y: np.ndarray, d: np.ndarray) -> float:
    df = pd.DataFrame({"p": pred, "y": y, "d": d}).dropna()
    if df.empty:
        return float("nan")
    ics = df.groupby("d").apply(lambda g: g["p"].rank().corr(g["y"].rank()) if len(g) > 5 else np.nan,
                                include_groups=False)
    return float(ics.mean())


def walk_forward(s: Samples, cfg: dict, progress: Callable[[float, str], None]) -> dict:
    w, lab, mcfg = cfg["walk"], cfg["label"], cfg["model"]
    gap = lab["horizon"] + 1
    folds = plan_folds(s.cal_pos, lab["horizon"], w["train_days"], w["retrain_days"], w["min_train_days"])
    if not folds:
        raise ValueError("数据不够切出测试段：请延长数据区间或缩短“最少训练天数”")
    pred = np.full(len(s.y), np.nan)
    per_model = {t: np.full(len(s.y), np.nan) for t in mcfg["types"]}
    importance = {t: np.zeros(len(s.feature_names)) for t in mcfg["types"]}
    fold_info = []
    dates = s.decision_dates
    n_steps = len(folds) * len(mcfg["types"]) + len(mcfg["types"])
    step = 0
    for fi, fold in enumerate(folds):
        fit_dates, val_dates = _split_valid(fold["train"], s.cal_pos, w["valid_ratio"], gap)
        tr = _rows(s, fit_dates, True, w["train_tradable_only"])
        va = _rows(s, val_dates, True, w["train_tradable_only"])
        te = _rows(s, fold["test"], False, False)
        info = {"fold": fi + 1, "train_start": str(dates[fold["train"][0]].date()) if len(fold["train"]) else "",
                "train_end": str(dates[fold["train"][-1]].date()) if len(fold["train"]) else "",
                "test_start": str(dates[fold["test"][0]].date()), "test_end": str(dates[fold["test"][-1]].date()),
                "n_train": int(len(tr)), "n_valid": int(len(va)), "n_test": int(len(te)), "models": {}}
        if len(tr) < 500 or len(te) == 0:
            info["skipped"] = "训练样本不足"
            fold_info.append(info)
            step += len(mcfg["types"])
            continue
        ranks = []
        for t in mcfg["types"]:
            t0 = time.time()
            progress(0.32 + 0.5 * step / n_steps, f"第 {fi + 1}/{len(folds)} 段 {info['test_start']}~{info['test_end']}：训练 {t}（{len(tr):,} 样本）")
            model = MD.create(t, mcfg["params"].get(t))
            model.fit(s.X[tr], s.y[tr], s.date_idx[tr], s.X[va] if len(va) else None,
                      s.y[va] if len(va) else None, s.date_idx[va] if len(va) else None)
            p = model.predict(s.X[te])
            per_model[t][te] = p
            ranks.append(cs_rank(p, s.date_idx[te]))
            imp = model.importance(len(s.feature_names))
            if imp is not None:
                importance[t] += imp
            vic = _ic(model.predict(s.X[va]), s.y_raw[va], s.date_idx[va]) if len(va) else float("nan")
            info["models"][t] = {"seconds": round(time.time() - t0, 1), "valid_rank_ic": _round(vic), **model.info}
            step += 1
        pred[te] = np.mean(ranks, axis=0)
        fold_info.append(info)

    # 用最近一段数据训练最终模型，给最新一天打分（今日选股）
    last = len(dates) - 1
    train_hi = s.cal_pos[last] - gap
    lo = train_hi - w["train_days"] if w["train_days"] > 0 else -1
    final_dates = np.nonzero((s.cal_pos <= train_hi) & (s.cal_pos > lo))[0]
    fit_dates, val_dates = _split_valid(final_dates, s.cal_pos, w["valid_ratio"], gap)
    tr = _rows(s, fit_dates, True, w["train_tradable_only"])
    va = _rows(s, val_dates, True, w["train_tradable_only"])
    te = _rows(s, np.array([last]), False, False)
    final_models = {}
    latest = np.full(len(te), np.nan)
    if len(tr) >= 500 and len(te):
        ranks = []
        for t in mcfg["types"]:
            progress(0.32 + 0.5 * step / n_steps, f"训练最终模型 {t}（截至 {dates[final_dates[-1]].date()}）")
            model = MD.create(t, mcfg["params"].get(t))
            model.fit(s.X[tr], s.y[tr], s.date_idx[tr], s.X[va] if len(va) else None,
                      s.y[va] if len(va) else None, s.date_idx[va] if len(va) else None)
            ranks.append(cs_rank(model.predict(s.X[te]), s.date_idx[te]))
            final_models[t] = model
            step += 1
        latest = np.mean(ranks, axis=0)
    n_ok = max(1, sum(1 for f in fold_info if "skipped" not in f))
    imp = {t: (v / n_ok).tolist() for t, v in importance.items() if v.sum() > 0}
    return {"pred": pred, "per_model": per_model, "folds": fold_info, "importance": imp,
            "final_models": final_models, "latest_rows": te, "latest_score": latest}


def _round(x, n=4):
    return None if x is None or not np.isfinite(x) else round(float(x), n)
