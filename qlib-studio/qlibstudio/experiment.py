"""一次实验（在工作进程里运行）：Qlib 初始化 → 因子集 → 模型训练（固定切分或滚动）→ 预测 → IC → 回测 → 保存。

全部计算用上游 Qlib 自己的组件，不另写一套：
    因子集    qlib.contrib.data.handler.Alpha158 / Alpha360（DataHandlerLP + QlibDataLoader）
    数据集    qlib.data.dataset.DatasetH（train / valid / test 三段）
    模型      qlib.contrib.model.gbdt.LGBModel、xgboost.XGBModel、catboost_model.CatBoostModel、linear.LinearModel
    IC        qlib.contrib.eva.alpha.calc_ic
    回测      qlib.backtest.backtest + TopkDropoutStrategy + SimulatorExecutor（Exchange 负责涨跌停、费用、一手 100 股）
    风险指标  qlib.contrib.evaluate.risk_analysis
我们只做：配置校验、滚动切分、结果整理成 JSON（给界面画图）和导出对比格式。
不用 qlib.workflow 的 R / MLflow 记录器：实验结果直接写到 runs/<编号>/，界面读起来简单，也少打包一套 MLflow。
"""
from __future__ import annotations

import json
import logging
import math
import os
import time
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from . import __version__, UPSTREAM_COMMIT, UPSTREAM_VERSION, paths
from .data import datasets

log = logging.getLogger("qlibstudio")
Progress = Callable[[float, str], None]

FACTOR_SETS = {
    "Alpha158": "Alpha158：158 个量价因子（K 线形态、价格、滚动统计，窗口 5/10/20/30/60 天）",
    "Alpha360": "Alpha360：过去 60 天的开高低收、均价、成交量各除以当天收盘价/成交量（360 列原始序列）",
}
MODELS = {
    "lgbm": "LightGBM（梯度提升树，Qlib 基准默认）",
    "xgb": "XGBoost（梯度提升树）",
    "catboost": "CatBoost（梯度提升树）",
    "linear": "线性模型（最小二乘 OLS）",
    "ridge": "线性模型（岭回归 Ridge）",
}
OPTIONAL_MODELS = {
    "lstm": "LSTM / GRU / ALSTM / Transformer / TRA 等深度模型：需要 PyTorch（约 1 GB），安装包默认不带；从源码运行并 pip install torch 后可在 upstream/examples/benchmarks 里用 qrun 跑",
}
# 模型参数取自上游 examples/benchmarks/<模型>/workflow_config_*_Alpha158.yaml（线程数改成本机核数）
MODEL_PARAMS = {
    "lgbm": {"loss": "mse", "colsample_bytree": 0.8879, "learning_rate": 0.2, "subsample": 0.8789, "lambda_l1": 205.6999,
             "lambda_l2": 580.9768, "max_depth": 8, "num_leaves": 210},
    "xgb": {"eval_metric": "rmse", "colsample_bytree": 0.8879, "eta": 0.0421, "max_depth": 8, "n_estimators": 647,
            "subsample": 0.8789},
    "catboost": {"loss": "RMSE", "learning_rate": 0.0421, "subsample": 0.8789, "max_depth": 6, "num_leaves": 100,
                 "grow_policy": "Lossguide", "bootstrap_type": "Bernoulli"},
    "linear": {"estimator": "ols"},
    "ridge": {"estimator": "ridge", "alpha": 0.05},
}
DEFAULTS = {
    "name": "",
    "dataset": "",
    "factors": "Alpha158",
    "model": "lgbm",
    "pool": "stocks",
    "segments": {"train": ["", ""], "valid": ["", ""], "test": ["", ""]},  # 留空 = 按数据区间 6:2:2 自动切
    "rolling": {"enabled": False, "step_days": 63},
    "label_horizon": 1,
    "deal_price": "close",
    "strategy": {"topk": 20, "n_drop": 2},
    "costs": {"open_cost": 0.0005, "close_cost": 0.0015, "min_cost": 5.0},
    "limit": "board",
    "account": 1_000_000,
    "benchmark": "auto",
    "seed": 42,
    "quick": False,  # 冒烟测试用：缩小模型（树的轮数）让一次实验在几十秒内跑完
}


def merge(base: dict, override: dict) -> dict:
    out = json.loads(json.dumps(base))
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = merge(out[k], v)
        else:
            out[k] = v
    return out


def auto_segments(cal: list[str]) -> dict:
    n = len(cal)
    if n < 300:
        raise ValueError(f"数据只有 {n} 个交易日，至少要 300 个（Alpha158 用 60 日窗口，训练、验证、测试各要一段）")
    a, b = int(n * 0.6), int(n * 0.8)
    return {"train": [cal[60], cal[a - 1]], "valid": [cal[a], cal[b - 1]], "test": [cal[b], cal[-2]]}


def normalize(cfg_in: dict, data_root: Path) -> dict:
    cfg = merge(DEFAULTS, cfg_in or {})
    errors = []
    if not cfg["dataset"]:
        raise ValueError("请先选择数据集")
    ds_dir = datasets.path(data_root, cfg["dataset"])
    cal = datasets.calendar(ds_dir)
    if cfg["factors"] not in FACTOR_SETS:
        errors.append("因子集只能是 Alpha158 或 Alpha360")
    if cfg["model"] not in MODELS:
        errors.append("未知模型" + ("（深度模型需要另装 PyTorch，见使用指南）" if cfg["model"] in OPTIONAL_MODELS else ""))
    pools = {p["id"] for p in datasets.pools(ds_dir)}
    if cfg["pool"] not in pools:
        errors.append(f"数据集里没有股票池 {cfg['pool']}")
    seg = cfg["segments"]
    if not all(seg.get(k) and all(seg[k]) for k in ("train", "valid", "test")):
        seg = cfg["segments"] = auto_segments(cal)
    try:
        ts = {k: [pd.Timestamp(x) for x in seg[k]] for k in ("train", "valid", "test")}
    except (TypeError, ValueError):
        raise ValueError("区间日期格式应为 YYYY-MM-DD") from None
    for k, (a, b) in ts.items():
        if a > b:
            errors.append(f"{ {'train': '训练', 'valid': '验证', 'test': '测试'}[k]}区间开始晚于结束")
    if not (ts["train"][1] < ts["valid"][0] and ts["valid"][1] < ts["test"][0]):
        errors.append("三个区间要按 训练 → 验证 → 测试 的顺序排列，不能重叠")
    if cal and (ts["train"][0] < pd.Timestamp(cal[0]) or ts["test"][1] > pd.Timestamp(cal[-1])):
        errors.append(f"区间超出数据范围 {cal[0]} ~ {cal[-1]}")
    cfg["label_horizon"] = int(cfg["label_horizon"])
    if not 1 <= cfg["label_horizon"] <= 20:
        errors.append("预测周期须在 1~20 天")
    if cfg["deal_price"] not in ("close", "open"):
        errors.append("成交价只能是开盘价或收盘价")
    st = cfg["strategy"]
    st["topk"], st["n_drop"] = int(st["topk"]), int(st["n_drop"])
    if not 1 <= st["topk"] <= 500 or not 0 <= st["n_drop"] <= st["topk"]:
        errors.append("持仓数 1~500，每天换出数 0~持仓数")
    for k in ("open_cost", "close_cost", "min_cost"):
        cfg["costs"][k] = float(cfg["costs"][k])
    if cfg["limit"] not in ("board", "qlib", "none"):
        errors.append("涨跌停规则无效")
    cfg["account"] = float(cfg["account"])
    if cfg["account"] < 10000:
        errors.append("初始资金至少 1 万元")
    cfg["rolling"]["enabled"] = bool(cfg["rolling"]["enabled"])
    cfg["rolling"]["step_days"] = int(cfg["rolling"]["step_days"])
    if cfg["rolling"]["enabled"] and not 20 <= cfg["rolling"]["step_days"] <= 252:
        errors.append("滚动步长须在 20~252 个交易日")
    if errors:
        raise ValueError("；".join(errors))
    cfg["dataset_dir"] = str(ds_dir)
    return cfg


# ---------------------------------------------------------------- Qlib 组件

def init_qlib(ds_dir: str, run_dir: Path | None = None) -> None:
    paths.ensure_upstream_importable()
    import qlib
    from qlib.constant import REG_CN
    kw = {}
    if run_dir is not None:
        # 上游 LGBModel.fit 会把每轮训练/验证损失写进 MLflow（R.log_metrics），默认写到当前目录的 mlruns/。
        # 调用方（run）已把当前目录切到本次实验目录；这里用相对路径 "mlruns"，不用 file:// 地址——
        # 上游 expm.py 对 file:// 地址做文件锁时会去掉开头的 /，在 macOS/Linux 上把锁建到当前目录下的一串子目录里。
        # 实验结束后删掉 mlruns/（损失曲线另存在 result.json）。
        kw["exp_manager"] = {"class": "MLflowExpManager", "module_path": "qlib.workflow.expm",
                             "kwargs": {"uri": "mlruns", "default_exp_name": "QlibStudio"}}
    # kernels=1：在本进程里顺序读数据，不再开多进程（打包后的程序开多进程要重新启动整个应用）
    qlib.init(provider_uri=ds_dir, region=REG_CN, kernels=1, expression_cache=None, dataset_cache=None,
              logging_level=logging.WARNING, **kw)


def label_expr(deal_price: str, horizon: int) -> str:
    p = "$open" if deal_price == "open" else "$close"
    return f"Ref({p}, -{horizon + 1})/Ref({p}, -1) - 1"


def make_handler(cfg: dict):
    from qlib.contrib.data.handler import Alpha158, Alpha360
    seg = cfg["segments"]
    kw = {"instruments": cfg["pool"], "start_time": seg["train"][0], "end_time": seg["test"][1],
          "fit_start_time": seg["train"][0], "fit_end_time": seg["train"][1],
          "label": ([label_expr(cfg["deal_price"], cfg["label_horizon"])], ["LABEL0"])}
    if cfg["model"] in ("linear", "ridge"):
        # 线性模型不能有缺失值：按上游 benchmarks/Linear 的配置做稳健标准化 + 填 0
        kw["infer_processors"] = [{"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
                                  {"class": "Fillna", "kwargs": {"fields_group": "feature"}}]
        kw["learn_processors"] = [{"class": "DropnaLabel"}, {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}}]
    elif cfg["factors"] == "Alpha360":
        kw["infer_processors"] = [{"class": "RobustZScoreNorm", "kwargs": {"fields_group": "feature", "clip_outlier": True}},
                                  {"class": "Fillna", "kwargs": {"fields_group": "feature"}}]
        kw["learn_processors"] = [{"class": "DropnaLabel"}, {"class": "CSRankNorm", "kwargs": {"fields_group": "label"}}]
    return (Alpha158 if cfg["factors"] == "Alpha158" else Alpha360)(**kw)


def make_model(cfg: dict):
    kind = cfg["model"]
    p = dict(MODEL_PARAMS[kind])
    threads = max(1, (os.cpu_count() or 2) - 1)
    if kind == "lgbm":
        from qlib.contrib.model.gbdt import LGBModel
        if cfg["quick"]:
            p.update(num_boost_round=60, early_stopping_rounds=20, num_leaves=31)
        return LGBModel(num_threads=threads, seed=cfg["seed"], **p)
    if kind == "xgb":
        from qlib.contrib.model.xgboost import XGBModel
        if cfg["quick"]:
            p["n_estimators"] = 60
        return XGBModel(nthread=threads, seed=cfg["seed"], **p)
    if kind == "catboost":
        from qlib.contrib.model.catboost_model import CatBoostModel
        if cfg["quick"]:
            p["iterations"] = 60
        return CatBoostModel(thread_count=threads, random_seed=cfg["seed"], allow_writing_files=False, **p)
    from qlib.contrib.model.linear import LinearModel
    return LinearModel(**p)


def fit_kwargs(cfg: dict) -> dict:
    if cfg["model"] == "lgbm":
        return {"num_boost_round": 60 if cfg["quick"] else 1000, "early_stopping_rounds": 20 if cfg["quick"] else 50,
                "verbose_eval": 0}
    if cfg["model"] == "xgb":
        return {"num_boost_round": 60 if cfg["quick"] else 1000, "early_stopping_rounds": 20 if cfg["quick"] else 50,
                "verbose_eval": False}
    if cfg["model"] == "catboost":
        return {"num_boost_round": 60 if cfg["quick"] else 1000, "early_stopping_rounds": 20 if cfg["quick"] else 50,
                "verbose_eval": False}
    return {}


def plan_rolling(cal: list[str], seg: dict, step: int) -> list[dict]:
    """按上游 RollingGen 的思路切分：测试期每 step 个交易日一段；训练、验证窗口长度不变，跟着往后滑。"""
    pos = {d: i for i, d in enumerate(cal)}

    def idx(d: str, side: str) -> int:
        ts = pd.Timestamp(d).strftime("%Y-%m-%d")
        arr = np.array(cal)
        return int(np.searchsorted(arr, ts, side="left" if side == "start" else "right")) - (0 if side == "start" else 1)

    tr0, tr1 = idx(seg["train"][0], "start"), idx(seg["train"][1], "end")
    va0, va1 = idx(seg["valid"][0], "start"), idx(seg["valid"][1], "end")
    te0, te1 = idx(seg["test"][0], "start"), idx(seg["test"][1], "end")
    n_train, n_valid, gap_tv, gap_vt = tr1 - tr0 + 1, va1 - va0 + 1, va0 - tr1, te0 - va1
    folds = []
    k = te0
    while k <= te1:
        end = min(k + step - 1, te1)
        v1 = k - gap_vt
        v0 = v1 - n_valid + 1
        t1 = v0 - gap_tv
        t0 = max(t1 - n_train + 1, tr0)
        folds.append({"train": [cal[t0], cal[t1]], "valid": [cal[v0], cal[v1]], "test": [cal[k], cal[end]]})
        k = end + 1
    del pos
    return folds


def equal_weight_bench(pool: str, start: str, end: str) -> pd.Series:
    from qlib.data import D
    df = D.features(D.instruments(pool), ["$close/Ref($close, 1) - 1"], start_time=start, end_time=end)
    s = df.iloc[:, 0].replace([np.inf, -np.inf], np.nan).clip(-0.35, 0.35).groupby(level="datetime").mean().fillna(0.0)
    s.name = "bench"
    return s


def limit_threshold(cfg: dict):
    if cfg["limit"] == "none":
        return None
    if cfg["limit"] == "qlib":
        return 0.095  # 上游 A 股示例的默认口径：涨跌幅 ≥ 9.5% 不能买 / ≤ -9.5% 不能卖（不分板块）
    p = "$open" if cfg["deal_price"] == "open" else "$close"
    # 按板块的涨跌停幅度（数据里的 $limit_rate），留 0.2% 容差（价格四舍五入到分）
    return (f"{p}/Ref($close, 1) - 1 >= $limit_rate - 0.002", f"{p}/Ref($close, 1) - 1 <= 0.002 - $limit_rate")


# ---------------------------------------------------------------- 主流程

def _r(x, n=6):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, n) if math.isfinite(x) else None


def _series(s: pd.Series, n=6) -> list:
    return [_r(v, n) for v in s.to_numpy()]


def run(cfg_in: dict, data_root: Path, out_dir: Path, progress: Progress = lambda p, m: None) -> dict:
    """当前目录临时切到实验目录：上游 MLflow 记录、CatBoost 等写到当前目录的东西都落在这里，结束后切回。"""
    import warnings
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    old = os.getcwd()
    os.chdir(out_dir)
    try:
        with warnings.catch_warnings(), np.errstate(all="ignore"):
            warnings.simplefilter("ignore")
            return _run(cfg_in, Path(data_root).resolve(), out_dir, progress)
    finally:
        os.chdir(old)


def _run(cfg_in: dict, data_root: Path, out: Path, progress: Progress) -> dict:
    t_start = time.time()
    cfg = normalize(cfg_in, data_root)
    out.mkdir(parents=True, exist_ok=True)
    (out / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=1), encoding="utf-8")
    ds_dir = cfg["dataset_dir"]
    ds_meta = datasets.read_json(Path(ds_dir) / "meta.json", {})
    names = datasets.read_json(Path(ds_dir) / "names.json", {}) or {}
    progress(0.02, "初始化 Qlib")
    init_qlib(ds_dir, out)
    from qlib.data import D
    from qlib.data.dataset import DatasetH
    seg = cfg["segments"]
    progress(0.05, f"计算因子 {cfg['factors']}（股票池 {cfg['pool']}，{seg['train'][0]} ~ {seg['test'][1]}）")
    handler = make_handler(cfg)
    progress(0.40, "因子计算完成，开始训练")
    cal = [d.strftime("%Y-%m-%d") for d in D.calendar(start_time=seg["train"][0], end_time=seg["test"][1])]
    folds = plan_rolling(datasets.calendar(Path(ds_dir)), seg, cfg["rolling"]["step_days"]) if cfg["rolling"]["enabled"] \
        else [{"train": seg["train"], "valid": seg["valid"], "test": seg["test"]}]
    preds, fold_info, importance = [], [], None
    for i, f in enumerate(folds):
        progress(0.40 + 0.35 * i / len(folds), f"第 {i + 1}/{len(folds)} 段：训练 {f['train'][0]}~{f['train'][1]}，"
                                                 f"验证 {f['valid'][0]}~{f['valid'][1]}，预测 {f['test'][0]}~{f['test'][1]}")
        ds = DatasetH(handler, segments={k: tuple(v) for k, v in f.items()})
        model = make_model(cfg)
        t0 = time.time()
        evals: dict = {}
        info: dict = {}
        kw = fit_kwargs(cfg)
        if cfg["model"] in ("lgbm", "xgb"):
            kw["evals_result"] = evals
        model.fit(ds, **kw)
        if evals:
            info["loss"] = {}
            for part, d in evals.items():  # LightGBM：{部分: {指标: [...]}}；XGBoost 上游已整理成 {部分: [...]}
                for metric, vals in (d.items() if isinstance(d, dict) else [("loss", d)]):
                    vals = list(vals)
                    info["loss"][f"{part}_{metric}"] = [_r(v, 6) for v in vals][::max(1, len(vals) // 200)]
        p = model.predict(ds, segment="test")
        if isinstance(p, pd.DataFrame):
            p = p.iloc[:, 0]
        preds.append(p)
        info.update({"fold": i + 1, **{k: v for k, v in f.items()}, "seconds": round(time.time() - t0, 1), "n_pred": int(len(p))})
        try:
            if cfg["model"] == "lgbm":
                imp = model.get_feature_importance(importance_type="gain")
                importance = imp if importance is None else importance.add(imp, fill_value=0)
                info["best_iteration"] = int(model.model.best_iteration or 0)
        except Exception:  # noqa: BLE001
            pass
        fold_info.append(info)
    pred = pd.concat(preds).sort_index()
    pred = pred[~pred.index.duplicated(keep="last")]
    pred.name = "score"

    progress(0.76, "计算 IC / RankIC")
    from qlib.contrib.eva.alpha import calc_ic
    from qlib.data.dataset.handler import DataHandlerLP
    ds_all = DatasetH(handler, segments={"test": tuple(seg["test"])})
    label = ds_all.prepare("test", col_set="label", data_key=DataHandlerLP.DK_R).iloc[:, 0]
    label = label.reindex(pred.index)
    ic, ric = calc_ic(pred, label)
    ic, ric = ic.dropna(), ric.dropna()
    ic_sum = {
        "ic_mean": _r(ic.mean()), "ic_std": _r(ic.std()), "icir": _r(ic.mean() / ic.std() if ic.std() > 0 else np.nan),
        "rank_ic_mean": _r(ric.mean()), "rank_ic_std": _r(ric.std()),
        "rank_icir": _r(ric.mean() / ric.std() if ric.std() > 0 else np.nan),
        "ic_positive": _r((ic > 0).mean()), "rank_ic_positive": _r((ric > 0).mean()), "n_days": int(len(ic)),
    }
    groups = group_returns(pred, label, cfg["label_horizon"])

    progress(0.80, "回测（TopkDropout 策略）")
    bench_name, bench = resolve_benchmark(cfg, ds_meta)
    report, positions, trades = backtest(cfg, pred, bench, names, progress)
    progress(0.95, "整理结果")
    res = summarize(cfg, report, bench_name)
    res.update({
        "id": out.name, "name": cfg["name"] or f"{MODELS[cfg['model']].split('（')[0]} · {cfg['factors']} · 前{cfg['strategy']['topk']}",
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "app_version": __version__,
        "qlib": {"version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT},
        "dataset": {"id": cfg["dataset"], "title": ds_meta.get("title"), "source": ds_meta.get("source"),
                    "demo": ds_meta.get("source") == "demo", "start": ds_meta.get("start"), "end": ds_meta.get("end")},
        "config": {k: v for k, v in cfg.items() if k != "dataset_dir"},
        "ic": {**ic_sum, "dates": [d.strftime("%Y-%m-%d") for d in ic.index], "ic_series": _series(ic, 4),
               "rank_ic_series": _series(ric.reindex(ic.index), 4)},
        "groups": groups, "folds": fold_info, "n_pred": int(len(pred)), "elapsed": round(time.time() - t_start, 1),
        "importance": [{"name": str(k), "value": _r(v, 2)} for k, v in
                       (importance.sort_values(ascending=False).head(30).items() if importance is not None else [])],
    })
    (out / "result.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    (out / "positions.json").write_text(json.dumps(positions, ensure_ascii=False), encoding="utf-8")
    (out / "trades.json").write_text(json.dumps(trades, ensure_ascii=False), encoding="utf-8")
    pred.reset_index().to_csv(out / "pred.csv", index=False, float_format="%.6f")
    _end_mlflow(out)
    from . import compare
    (out / "export.json").write_text(json.dumps(compare.export_from_result(res), ensure_ascii=False), encoding="utf-8")
    m = res["metrics"]
    progress(1.0, f"完成：年化收益 {(m.get('annual_return') or 0) * 100:.1f}%，最大回撤 {(m.get('max_drawdown') or 0) * 100:.1f}%，"
                  f"RankIC {ic_sum['rank_ic_mean']}")
    return res


def _end_mlflow(out: Path) -> None:
    """结束上游 LGBModel 自动开启的 MLflow 记录，再删掉 mlruns/（没开启时什么也不做）。"""
    import shutil
    try:
        from qlib.workflow import R
        R.end_exp(recorder_status="FINISHED")
    except Exception:  # noqa: BLE001
        pass
    shutil.rmtree(out / "mlruns", ignore_errors=True)


def resolve_benchmark(cfg: dict, ds_meta: dict):
    seg = cfg["segments"]
    b = cfg["benchmark"]
    cands = ds_meta.get("benchmarks") or []
    if b == "auto":
        b = cands[0] if cands else "equal"
    if b == "equal":
        return f"股票池 {cfg['pool']} 等权", equal_weight_bench(cfg["pool"], seg["test"][0], seg["test"][1])
    return b, b


def group_returns(pred: pd.Series, label: pd.Series, horizon: int, n: int = 5) -> dict:
    """每天按预测分 n 组，每组下一期平均收益；累计曲线只取不重叠的持有期（每 horizon 天取一次）。"""
    df = pd.DataFrame({"s": pred, "y": label}).dropna()
    if df.empty:
        return {}
    df["g"] = df.groupby(level="datetime")["s"].transform(
        lambda x: np.minimum((x.rank(pct=True) * n).clip(upper=n - 1e-9).astype(int), n - 1))
    tab = df.groupby([df.index.get_level_values("datetime"), "g"])["y"].mean().unstack()
    nonover = tab.iloc[::max(1, horizon)]
    curves = (1 + nonover.fillna(0)).cumprod()
    ls = (nonover[n - 1] - nonover[0]).fillna(0) if n - 1 in nonover and 0 in nonover else pd.Series(dtype=float)
    mean = tab.mean()
    return {"groups": [f"第{i + 1}组" for i in range(n)], "note": "第 1 组预测最低，第 5 组预测最高",
            "mean": [_r(mean.get(i), 6) for i in range(n)],
            "dates": [d.strftime("%Y-%m-%d") for d in curves.index],
            "curves": [_series(curves[i], 4) if i in curves else [] for i in range(n)],
            "long_short": _series((1 + ls).cumprod(), 4), "long_short_mean": _r(ls.mean(), 6)}


def backtest(cfg: dict, pred: pd.Series, bench, names: dict, progress: Progress):
    from qlib.backtest import backtest as qbacktest
    from qlib.backtest import executor as qexec
    from qlib.contrib.strategy import TopkDropoutStrategy
    seg = cfg["segments"]
    strategy = TopkDropoutStrategy(signal=pred, topk=cfg["strategy"]["topk"], n_drop=cfg["strategy"]["n_drop"])
    executor = qexec.SimulatorExecutor(time_per_step="day", generate_portfolio_metrics=True, verbose=False)
    ex = {"freq": "day", "limit_threshold": limit_threshold(cfg), "deal_price": cfg["deal_price"],
          "open_cost": cfg["costs"]["open_cost"], "close_cost": cfg["costs"]["close_cost"],
          "min_cost": cfg["costs"]["min_cost"], "trade_unit": 100}
    portfolio, indicator = qbacktest(start_time=seg["test"][0], end_time=seg["test"][1], strategy=strategy,
                                     executor=executor, benchmark=bench, account=cfg["account"], exchange_kwargs=ex)
    report, pos_dict = portfolio["1day"]
    positions = []
    for d, p in sorted(pos_dict.items()):
        hold = []
        pos = p.position if hasattr(p, "position") else p
        value = float(pos.get("now_account_value", 0) or 0)
        for code, v in pos.items():
            if not isinstance(v, dict):
                continue
            amt, price = float(v.get("amount", 0)), float(v.get("price", 0))
            hold.append({"symbol": code, "name": names.get(code, ""), "weight": _r(v.get("weight"), 4),
                         "value": _r(amt * price, 2), "days": int(v.get("count_day", 0) or 0)})
        hold.sort(key=lambda h: -(h["value"] or 0))
        positions.append({"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "cash": _r(pos.get("cash"), 2),
                          "value": _r(value, 2), "holdings": hold})
    trades = extract_trades(indicator, names)
    add_raw_shares(trades, seg["test"][0], seg["test"][1])
    return report, positions, trades


def extract_trades(indicator: dict, names: dict) -> list[dict]:
    """从上游 Indicator 的逐日订单记录（order_indicator_his）里取成交明细。

    上游 NumpyOrderIndicator.to_series() 在 pandas 2.x 下会报错（把自家 Index 对象直接交给 pd.Series），
    这里改用每个字段的 to_dict()，不改上游。成交数量是复权股数，× 复权因子 = 真实股数；价格 ÷ 因子 = 不复权价。"""
    out = []
    try:
        his = indicator["1day"][1].order_indicator_his
    except Exception:  # noqa: BLE001
        return out

    def field(oi, name):
        try:
            return oi.data[name].to_dict()
        except Exception:  # noqa: BLE001
            return {}
    for d, oi in his.items():
        deal = field(oi, "deal_amount")
        if not deal:
            continue
        price, value, cost, direction = (field(oi, k) for k in ("trade_price", "trade_value", "trade_cost", "trade_dir"))
        for code, a in deal.items():
            a = float(a)
            if not a or not math.isfinite(a):
                continue
            out.append({"date": pd.Timestamp(d).strftime("%Y-%m-%d"), "symbol": code, "name": names.get(code, ""),
                        "side": "买入" if int(direction.get(code, 1)) == 1 else "卖出", "amount_adj": _r(abs(a), 2),
                        "price_adj": _r(price.get(code), 4), "value": _r(abs(float(value.get(code, 0) or 0)), 2),
                        "cost": _r(cost.get(code), 2)})
    return out


def add_raw_shares(trades: list[dict], pool_start: str, pool_end: str) -> None:
    """用 $factor 把复权数量/价格换回真实股数/不复权价（Qlib 数据口径：复权价 = 不复权价 × 因子）。"""
    if not trades:
        return
    from qlib.data import D
    codes = sorted({t["symbol"] for t in trades})
    try:
        fac = D.features(codes, ["$factor"], start_time=pool_start, end_time=pool_end).iloc[:, 0]
    except Exception:  # noqa: BLE001
        fac = None
    for t in trades:
        f = 1.0
        if fac is not None:
            try:
                f = float(fac.loc[(t["symbol"], pd.Timestamp(t["date"]))])
            except (KeyError, TypeError, ValueError):
                f = 1.0
        if not math.isfinite(f) or f <= 0:
            f = 1.0
        t["shares"] = int(round((t.get("amount_adj") or 0) * f))
        t["price"] = _r((t.get("price_adj") or 0) / f, 3)


def summarize(cfg: dict, report: pd.DataFrame, bench_name: str) -> dict:
    from qlib.contrib.evaluate import risk_analysis
    gross = report["return"].fillna(0)        # 上游 return 是扣费用之前的日收益（rtn），费用单列在 cost
    cost = report["cost"].fillna(0)
    r = gross - cost                            # 扣费后的日收益，和账户净值一致
    b = report["bench"].fillna(0)
    nav = (report["account"] / cfg["account"]).astype(float)
    bench_nav = (1 + b).cumprod()
    dd = nav / nav.cummax() - 1
    days = len(r)
    years = days / 244
    total = float(nav.iloc[-1] - 1) if days else None
    cagr = float(nav.iloc[-1] ** (1 / years) - 1) if days and years > 0 and nav.iloc[-1] > 0 else None
    vol = float(r.std() * np.sqrt(244)) if days > 1 else None
    ex = r - b
    # 上游口径（与 PortAnaRecord 相同）：超额 = return − bench（不含费用）或再减 cost（含费用），
    # 按日相加，年化 = 日均 × 238，见 qlib.contrib.evaluate.risk_analysis
    ra_ex = risk_analysis(gross - b, freq="day")["risk"]
    ra_ex_cost = risk_analysis(gross - b - cost, freq="day")["risk"]
    metrics = {
        "total_return": _r(total), "annual_return": _r(cagr), "volatility": _r(vol),
        "sharpe": _r(r.mean() / r.std() * np.sqrt(244) if r.std() > 0 else np.nan, 3),
        "max_drawdown": _r(dd.min()), "calmar": _r(cagr / abs(dd.min()) if cagr is not None and dd.min() < 0 else np.nan, 3),
        "bench_total_return": _r(bench_nav.iloc[-1] - 1 if days else None),
        "bench_annual_return": _r(bench_nav.iloc[-1] ** (1 / years) - 1 if days and years > 0 else None),
        "excess_annual_return": _r(cagr - (bench_nav.iloc[-1] ** (1 / years) - 1) if cagr is not None and years > 0 else None),
        "qlib_excess_annualized": _r(ra_ex.get("annualized_return")), "qlib_excess_ir": _r(ra_ex.get("information_ratio"), 3),
        "qlib_excess_mdd": _r(ra_ex.get("max_drawdown")),
        "qlib_excess_with_cost_annualized": _r(ra_ex_cost.get("annualized_return")),
        "qlib_excess_with_cost_ir": _r(ra_ex_cost.get("information_ratio"), 3),
        "qlib_excess_with_cost_mdd": _r(ra_ex_cost.get("max_drawdown")),
        "turnover_daily": _r(report["turnover"].mean()), "cost_total": _r(float((report["cost"] * report["account"]).sum()), 2),
        "win_rate_daily": _r((r > 0).mean()), "excess_win_rate": _r((ex > 0).mean()), "days": days,
        "start": report.index[0].strftime("%Y-%m-%d") if days else None,
        "end": report.index[-1].strftime("%Y-%m-%d") if days else None,
    }
    daily = {"dates": [d.strftime("%Y-%m-%d") for d in report.index], "return": _series(r), "bench_return": _series(b),
             "cost": _series(cost, 7), "nav": _series(nav), "bench_nav": _series(bench_nav),
             "excess_cum": _series(ex.cumsum()), "drawdown": _series(dd), "turnover": _series(report["turnover"], 4),
             "value": _series(report["value"], 2), "cash": _series(report["cash"], 2)}
    return {"metrics": metrics, "daily": daily, "benchmark": bench_name}
