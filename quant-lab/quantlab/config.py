"""实验配置：默认值、合并与校验。界面表单、命令行 JSON 都走这里。"""
from __future__ import annotations

import copy

from . import features
from .market import BOARDS

DEFAULTS = {
    "name": "",
    "dataset": "",
    "universe": {
        "boards": ["sh_main", "sz_main", "chinext", "star"],
        "min_listed_days": 60,      # 上市满 N 个交易日
        "min_amount": 1e7,          # 20 日平均成交额（元）
        "min_price": 0.0,
        "exclude_st": True,
    },
    "period": {"start": "", "end": ""},  # 留空 = 全部数据
    "features": {"groups": features.DEFAULT_GROUPS, "names": [], "normalize": "rank"},
    "label": {"horizon": 5, "price": "open", "transform": "rank", "excess": True},
    "model": {"types": ["lgbm"], "params": {}},
    "walk": {
        "train_days": 750,          # 训练窗口（交易日），0 = 扩展窗口（用全部历史）
        "retrain_days": 63,         # 每隔多少交易日重新训练一次
        "valid_ratio": 0.15,        # 训练窗口末尾留作验证集（早停）的比例
        "min_train_days": 250,
        "train_tradable_only": True,  # 训练时去掉次日开盘买不进的样本
    },
    "backtest": {
        "top_k": 20,
        "rebalance_days": 5,
        "capital": 1_000_000,
        "commission": 0.00025,      # 佣金（双向）
        "min_commission": 5.0,
        "stamp_tax": 0.0005,        # 印花税（卖出）
        "slippage": 0.001,          # 滑点（单边）
        "buffer": 1.5,              # 持仓排名还在前 top_k×buffer 内就不卖，降低换手
        "stop_loss": 0.0,           # 收盘跌破成本价该比例后次日开盘卖出，0 = 关闭
        "benchmark": "auto",        # auto = 股票池等权；也可以填指数代码如 sh000300
        "lot": 100,
    },
}

MODEL_TYPES = {
    "lgbm": "LightGBM 回归（推荐）",
    "lgbm_rank": "LightGBM 排序（LambdaRank）",
    "lgbm_cls": "LightGBM 分类（是否跑赢前 30%）",
    "hgb": "sklearn 直方图梯度提升",
    "rf": "随机森林",
    "ridge": "岭回归（线性基准）",
    "mlp": "神经网络 MLP",
}


def merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict) and k != "params":
            out[k] = merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def normalize(cfg: dict) -> dict:
    cfg = merge(DEFAULTS, cfg or {})
    errors = []
    if not cfg["dataset"]:
        errors.append("请选择数据集")
    u, lab, w, bt = cfg["universe"], cfg["label"], cfg["walk"], cfg["backtest"]
    u["boards"] = [b for b in u["boards"] if b in BOARDS]
    if not u["boards"]:
        errors.append("至少选择一个板块")
    types = [t for t in cfg["model"]["types"] if t in MODEL_TYPES]
    if not types:
        errors.append("至少选择一个模型")
    cfg["model"]["types"] = types
    feats = features.select(cfg["features"]["groups"], cfg["features"]["names"])
    if len(feats) < 2:
        errors.append("至少选择 2 个因子")
    cfg["features"]["resolved"] = feats
    if cfg["features"]["normalize"] not in ("rank", "zscore", "none"):
        errors.append("因子标准化方式无效")
    lab["horizon"] = int(lab["horizon"])
    if not 1 <= lab["horizon"] <= 60:
        errors.append("预测周期须在 1~60 天")
    if lab["price"] not in ("open", "close") or lab["transform"] not in ("rank", "zscore", "raw"):
        errors.append("标签设置无效")
    for key, lo, hi in (("top_k", 1, 500), ("rebalance_days", 1, 120), ("lot", 1, 10000)):
        bt[key] = int(bt[key])
        if not lo <= bt[key] <= hi:
            errors.append(f"回测参数 {key} 超出范围 {lo}~{hi}")
    for key in ("capital", "commission", "min_commission", "stamp_tax", "slippage", "buffer", "stop_loss"):
        bt[key] = float(bt[key])
    if bt["capital"] < 10000:
        errors.append("初始资金至少 1 万")
    if bt["buffer"] < 1:
        bt["buffer"] = 1.0
    for key in ("train_days", "retrain_days", "min_train_days"):
        w[key] = int(w[key])
    w["valid_ratio"] = float(w["valid_ratio"])
    if w["retrain_days"] < 1:
        errors.append("重训间隔至少 1 天")
    if not 0 <= w["valid_ratio"] < 0.5:
        errors.append("验证集比例须在 0~0.5")
    w["step"] = bt["rebalance_days"]  # 每个调仓日是一个决策日：训练样本、预测、调仓都在这些日子上
    if errors:
        raise ValueError("；".join(errors))
    return cfg
