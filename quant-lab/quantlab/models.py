"""模型注册表：统一 fit / predict / importance / save / load 接口。

想加新模型（XGBoost、CatBoost、PyTorch 时序网络……）：写一个同接口的类放进 REGISTRY 即可。
"""
from __future__ import annotations

import pickle
import warnings
from pathlib import Path

import numpy as np

DEFAULT_PARAMS = {
    "lgbm": {"num_leaves": 63, "learning_rate": 0.05, "n_estimators": 600, "min_child_samples": 200,
             "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping": 50},
    "lgbm_rank": {"num_leaves": 63, "learning_rate": 0.05, "n_estimators": 600, "min_child_samples": 200,
                  "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping": 50},
    "lgbm_cls": {"num_leaves": 63, "learning_rate": 0.05, "n_estimators": 600, "min_child_samples": 200,
                 "subsample": 0.8, "subsample_freq": 1, "colsample_bytree": 0.8, "reg_lambda": 1.0, "early_stopping": 50},
    "hgb": {"max_iter": 300, "learning_rate": 0.05, "max_leaf_nodes": 63, "min_samples_leaf": 200, "l2_regularization": 1.0},
    "rf": {"n_estimators": 200, "max_depth": 8, "min_samples_leaf": 200, "max_samples": 0.3, "max_features": 0.5},
    "ridge": {"alpha": 10.0},
    "mlp": {"hidden": [64, 32], "alpha": 1e-3, "max_iter": 60, "learning_rate_init": 1e-3},
}


def _groups(dates: np.ndarray) -> np.ndarray:
    """按日期连续分组的组大小（样本已按日期排序）。"""
    _, counts = np.unique(dates, return_counts=True)
    return counts


class Base:
    kind = ""
    fill_nan = False

    def __init__(self, params: dict | None = None, seed: int = 42):
        self.params = {**DEFAULT_PARAMS.get(self.kind, {}), **(params or {})}
        self.seed = seed
        self.model = None
        self.info: dict = {}

    def _x(self, X):
        return np.nan_to_num(X, nan=0.0) if self.fill_nan else X

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        raise NotImplementedError

    def predict(self, X) -> np.ndarray:
        return np.asarray(self.model.predict(self._x(X)), dtype="float64")

    def importance(self, n_features: int) -> np.ndarray | None:
        return None

    def save(self, path: Path) -> None:
        path.write_bytes(pickle.dumps(self))

    @staticmethod
    def load(path: Path) -> "Base":
        return pickle.loads(path.read_bytes())


class LGBM(Base):
    kind = "lgbm"
    objective = "regression"

    def _label(self, y, dates):
        return y

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        import lightgbm as lgb
        p = dict(self.params)
        stop = int(p.pop("early_stopping", 50))
        model_cls = lgb.LGBMRanker if self.objective == "lambdarank" else (
            lgb.LGBMClassifier if self.objective == "binary" else lgb.LGBMRegressor)
        self.model = model_cls(objective=self.objective, random_state=self.seed, n_jobs=-1, verbose=-1, **p)
        kw = {}
        if self.objective == "lambdarank":
            kw["group"] = _groups(dates)
        has_val = Xv is not None and len(Xv) > 0
        if has_val:
            kw["eval_set"] = [(Xv, self._label(yv, dates_v))]
            if self.objective == "lambdarank":
                kw["eval_group"] = [_groups(dates_v)]
                kw["eval_at"] = [50]
            kw["callbacks"] = [lgb.early_stopping(stop, verbose=False)]
        with warnings.catch_warnings():  # 不同 LightGBM 版本的 eval_set 弃用提示
            warnings.simplefilter("ignore")
            self.model.fit(X, self._label(y, dates), **kw)
        self.info = {"best_iteration": int(getattr(self.model, "best_iteration_", 0) or p.get("n_estimators", 0))}
        return self

    def predict(self, X):
        if self.objective == "binary":
            return self.model.predict_proba(X)[:, 1]
        return np.asarray(self.model.predict(X), dtype="float64")

    def importance(self, n_features):
        imp = self.model.booster_.feature_importance(importance_type="gain").astype("float64")
        return imp / imp.sum() if imp.sum() > 0 else imp


class LGBMRank(LGBM):
    kind = "lgbm_rank"
    objective = "lambdarank"

    def _label(self, y, dates):
        # 每天把标签分成 0~9 十档（相关度越高越好）
        out = np.zeros(len(y), dtype="int32")
        for d in np.unique(dates):
            m = dates == d
            r = y[m].argsort().argsort() / max(m.sum() - 1, 1)
            out[m] = np.minimum((r * 10).astype(int), 9)
        return out


class LGBMCls(LGBM):
    kind = "lgbm_cls"
    objective = "binary"

    def _label(self, y, dates):
        out = np.zeros(len(y), dtype="int32")
        for d in np.unique(dates):
            m = dates == d
            out[m] = (y[m] >= np.quantile(y[m], 0.7)).astype("int32")
        return out


class HGB(Base):
    kind = "hgb"

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        from sklearn.ensemble import HistGradientBoostingRegressor
        self.model = HistGradientBoostingRegressor(random_state=self.seed, early_stopping=True,
                                                   validation_fraction=0.15, **self.params)
        self.model.fit(X, y)
        return self


class RF(Base):
    kind = "rf"
    fill_nan = True

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        from sklearn.ensemble import RandomForestRegressor
        self.model = RandomForestRegressor(random_state=self.seed, n_jobs=-1, bootstrap=True, **self.params)
        self.model.fit(self._x(X), y)
        return self

    def importance(self, n_features):
        return self.model.feature_importances_


class Ridge(Base):
    kind = "ridge"
    fill_nan = True

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        from sklearn.linear_model import Ridge as R
        self.model = R(**self.params)
        self.model.fit(self._x(X), y)
        return self

    def importance(self, n_features):
        c = np.abs(self.model.coef_)
        return c / c.sum() if c.sum() > 0 else c


class MLP(Base):
    kind = "mlp"
    fill_nan = True

    def fit(self, X, y, dates, Xv=None, yv=None, dates_v=None):
        from sklearn.neural_network import MLPRegressor
        p = dict(self.params)
        hidden = tuple(p.pop("hidden", [64, 32]))
        self.model = MLPRegressor(hidden_layer_sizes=hidden, random_state=self.seed, early_stopping=True,
                                  validation_fraction=0.15, n_iter_no_change=5, batch_size=1024, **p)
        self.model.fit(self._x(X), y)
        return self


REGISTRY = {c.kind: c for c in (LGBM, LGBMRank, LGBMCls, HGB, RF, Ridge, MLP)}


def create(kind: str, params: dict | None = None, seed: int = 42) -> Base:
    return REGISTRY[kind](params, seed)
