"""因子库：全部基于 t 日收盘及以前的数据计算（没有未来函数，见 tests 里的截断一致性测试）。

每个因子是一个函数 ctx -> 宽表（行=日期，列=股票）。按组注册，界面上可勾选。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from .data.store import Panel


class Ctx:
    """因子计算上下文：缓存常用中间量。价格都是复权价并在停牌日前向填充，成交量停牌日为 0。"""

    def __init__(self, panel: Panel, universe: pd.DataFrame | None = None):
        self.p = panel
        traded = panel.traded
        alive = traded.cumsum() > 0
        # 退市后不再填充
        last_valid = traded[::-1].cumsum()[::-1] > 0
        self.alive = alive & last_valid
        self.traded = traded
        self.C = panel.adj("close").ffill().where(self.alive)
        self.O = panel.adj("open").ffill().where(self.alive)
        self.H = panel.adj("high").ffill().where(self.alive)
        self.L = panel.adj("low").ffill().where(self.alive)
        self.V = (panel.raw["volume"] / panel.factor).where(traded, 0.0).where(self.alive)
        self.A = panel.raw["amount"].where(traded, 0.0).where(self.alive)
        self.universe = universe if universe is not None else traded
        self._cache: dict = {}

    def get(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    @property
    def ret(self) -> pd.DataFrame:
        return self.get("ret", lambda: self.C / self.C.shift(1) - 1)

    @property
    def mkt(self) -> pd.Series:
        """市场收益：当天可交易股票的等权平均收益。"""
        return self.get("mkt", lambda: self.ret.where(self.traded).mean(axis=1).fillna(0.0))

    def ma(self, df_key: str, n: int) -> pd.DataFrame:
        return self.get((df_key, "ma", n), lambda: getattr(self, df_key).rolling(n, min_periods=max(2, int(n * 0.8))).mean())


def rmean(df, n):
    return df.rolling(n, min_periods=max(2, int(n * 0.8))).mean()


def rstd(df, n):
    return df.rolling(n, min_periods=max(2, int(n * 0.8))).std()


def rmax(df, n):
    return df.rolling(n, min_periods=max(1, int(n * 0.8))).max()


def rmin(df, n):
    return df.rolling(n, min_periods=max(1, int(n * 0.8))).min()


def rcorr(x: pd.DataFrame, y, n: int) -> pd.DataFrame:
    """滚动相关；y 可以是同形宽表或一列（市场）。用矩估计，向量化很快。"""
    if isinstance(y, pd.Series):
        y = pd.DataFrame(np.repeat(y.to_numpy()[:, None], x.shape[1], axis=1), index=x.index, columns=x.columns)
    y = y.where(x.notna())
    x = x.where(y.notna())
    mx, my = rmean(x, n), rmean(y, n)
    cov = rmean(x * y, n) - mx * my
    vx = rmean(x * x, n) - mx * mx
    vy = rmean(y * y, n) - my * my
    out = cov / np.sqrt((vx * vy).where((vx > 1e-12) & (vy > 1e-12)))
    return out.clip(-1, 1)


def ema(df, n):
    return df.ewm(span=n, adjust=False, min_periods=n).mean()


def wilder(df, n):
    return df.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def safe_div(a, b):
    return a / b.where(b.abs() > 1e-12)


@dataclass
class Feature:
    name: str
    group: str
    desc: str
    fn: Callable[[Ctx], pd.DataFrame]


REGISTRY: dict[str, Feature] = {}
GROUPS = {
    "momentum": "动量/收益",
    "trend": "均线/趋势",
    "volatility": "波动",
    "position": "价格位置",
    "technical": "经典指标",
    "volume": "量能/流动性",
    "kline": "K线形态",
    "stats": "分布/涨跌停",
    "market": "市场/相对",
}


def feature(name: str, group: str, desc: str):
    def deco(fn):
        REGISTRY[name] = Feature(name, group, desc, fn)
        return fn
    return deco


def _register_simple():
    for n in (1, 5, 10, 20, 60, 120):
        feature(f"ret_{n}", "momentum", f"{n}日收益率")(lambda c, n=n: c.C / c.C.shift(n) - 1)
    feature("mom_60_5", "momentum", "60日动量（剔除最近5日）")(lambda c: c.C.shift(5) / c.C.shift(60) - 1)
    feature("mom_250_20", "momentum", "250日动量（剔除最近20日）")(lambda c: c.C.shift(20) / c.C.shift(250) - 1)
    for n in (5, 10, 20, 60, 120):
        feature(f"ma_gap_{n}", "trend", f"收盘价相对{n}日均线偏离（乖离率）")(lambda c, n=n: c.C / c.ma("C", n) - 1)
    feature("ma5_ma20", "trend", "5日均线/20日均线")(lambda c: c.ma("C", 5) / c.ma("C", 20) - 1)
    feature("ma20_ma60", "trend", "20日均线/60日均线")(lambda c: c.ma("C", 20) / c.ma("C", 60) - 1)
    feature("ma60_slope", "trend", "60日均线 20日斜率")(lambda c: c.ma("C", 60) / c.ma("C", 60).shift(20) - 1)
    for n in (5, 20, 60):
        feature(f"vol_{n}", "volatility", f"{n}日收益波动率")(lambda c, n=n: rstd(c.ret, n))
    feature("down_vol_20", "volatility", "20日下行波动率")(lambda c: np.sqrt(rmean(c.ret.clip(upper=0) ** 2, 20)))
    feature("parkinson_20", "volatility", "20日 Parkinson 振幅波动率")(
        lambda c: np.sqrt(rmean(np.log(c.H / c.L) ** 2, 20) / (4 * np.log(2))))
    feature("vol_ratio_5_60", "volatility", "短期/长期波动率之比")(lambda c: safe_div(rstd(c.ret, 5), rstd(c.ret, 60)))
    for n in (20, 60, 250):
        feature(f"pos_{n}", "position", f"{n}日价格区间位置（0=最低 1=最高）")(
            lambda c, n=n: safe_div(c.C - rmin(c.L, n), rmax(c.H, n) - rmin(c.L, n)))
    feature("dist_high_250", "position", "距250日最高价")(lambda c: c.C / rmax(c.H, 250) - 1)
    feature("dist_low_250", "position", "距250日最低价")(lambda c: c.C / rmin(c.L, 250) - 1)
    for n in (5, 20):
        feature(f"vr_{n}", "volume", f"成交量/{n}日均量（量比）")(lambda c, n=n: np.log(safe_div(c.V, c.ma("V", n)).clip(lower=1e-3)))
    feature("vma5_vma60", "volume", "5日均量/60日均量")(lambda c: np.log(safe_div(c.ma("V", 5), c.ma("V", 60)).clip(lower=1e-3)))
    feature("log_amt_20", "volume", "20日平均成交额（对数，规模/流动性）")(lambda c: np.log1p(c.ma("A", 20)))
    feature("amt_cv_20", "volume", "20日成交额变异系数")(lambda c: safe_div(rstd(c.A, 20), c.ma("A", 20)))
    feature("corr_cv_20", "volume", "20日价量相关")(lambda c: rcorr(c.C, c.V, 20))
    feature("corr_rv_10", "volume", "10日收益与量变相关")(lambda c: rcorr(c.ret, np.log(c.V.clip(lower=1)).diff(), 10))
    feature("amihud_20", "volume", "20日 Amihud 非流动性")(lambda c: np.log1p(rmean(safe_div(c.ret.abs(), c.A) * 1e9, 20)))
    feature("body", "kline", "实体 (收-开)/开")(lambda c: c.C / c.O - 1)
    feature("upper_shadow", "kline", "上影线/收盘")(lambda c: (c.H - np.maximum(c.O, c.C)) / c.C)
    feature("lower_shadow", "kline", "下影线/收盘")(lambda c: (np.minimum(c.O, c.C) - c.L) / c.C)
    feature("clv", "kline", "收盘在当日振幅中的位置")(lambda c: safe_div((c.C - c.L) - (c.H - c.C), c.H - c.L))
    feature("gap", "kline", "跳空 开/昨收")(lambda c: c.O / c.C.shift(1) - 1)
    feature("upper_shadow_5", "kline", "5日平均上影线")(lambda c: rmean((c.H - np.maximum(c.O, c.C)) / c.C, 5))
    feature("lower_shadow_5", "kline", "5日平均下影线")(lambda c: rmean((np.minimum(c.O, c.C) - c.L) / c.C, 5))
    feature("intraday_20", "kline", "20日日内收益累计（开→收）")(lambda c: rmean(c.C / c.O - 1, 20))
    feature("overnight_20", "kline", "20日隔夜收益累计（昨收→开）")(lambda c: rmean(c.O / c.C.shift(1) - 1, 20))
    feature("skew_20", "stats", "20日收益偏度")(lambda c: c.ret.rolling(20, min_periods=16).skew())
    feature("kurt_20", "stats", "20日收益峰度")(lambda c: c.ret.rolling(20, min_periods=16).kurt())
    feature("max_ret_20", "stats", "20日最大单日涨幅")(lambda c: rmax(c.ret, 20))
    feature("min_ret_20", "stats", "20日最大单日跌幅")(lambda c: rmin(c.ret, 20))
    feature("up_ratio_20", "stats", "20日上涨天数占比")(lambda c: rmean((c.ret > 0).astype(float).where(c.ret.notna()), 20))
    feature("limit_up_20", "stats", "20日涨停次数（近似）")(lambda c: rmean((c.ret >= c.get("lim", lambda: _lim(c)) - 0.002).astype(float), 20) * 20)
    feature("limit_down_20", "stats", "20日跌停次数（近似）")(lambda c: rmean((c.ret <= -c.get("lim", lambda: _lim(c)) + 0.002).astype(float), 20) * 20)
    feature("mkt_ret_5", "market", "市场5日收益")(lambda c: _bcast(c, c.mkt.rolling(5).sum()))
    feature("mkt_ret_20", "market", "市场20日收益")(lambda c: _bcast(c, c.mkt.rolling(20).sum()))
    feature("mkt_vol_20", "market", "市场20日波动")(lambda c: _bcast(c, c.mkt.rolling(20).std()))
    feature("mkt_breadth_5", "market", "市场5日上涨家数占比")(
        lambda c: _bcast(c, (c.ret.where(c.traded) > 0).sum(axis=1).div(c.traded.sum(axis=1).clip(lower=1)).rolling(5).mean()))
    feature("rs_20", "market", "20日相对市场强弱")(lambda c: (c.C / c.C.shift(20) - 1).sub(c.mkt.rolling(20).sum(), axis=0))
    feature("beta_60", "market", "60日 Beta")(lambda c: c.get("beta60", lambda: _beta(c, 60)))
    feature("idio_vol_60", "market", "60日特质波动率")(
        lambda c: rstd(c.ret - c.get("beta60", lambda: _beta(c, 60)).mul(c.mkt, axis=0), 60))
    feature("corr_mkt_60", "market", "60日与市场相关")(lambda c: rcorr(c.ret, c.mkt, 60))


def _bcast(c: Ctx, s: pd.Series) -> pd.DataFrame:
    return pd.DataFrame(np.repeat(s.to_numpy()[:, None], c.C.shape[1], axis=1), index=c.C.index, columns=c.C.columns).where(c.alive)


def _lim(c: Ctx) -> pd.DataFrame:
    from .market import limit_rate_frame
    return limit_rate_frame(c.C.index, list(c.C.columns), c.p.names).astype("float64")


def _beta(c: Ctx, n: int) -> pd.DataFrame:
    m = _bcast(c, c.mkt).where(c.ret.notna())
    r = c.ret.where(m.notna())
    cov = rmean(r * m, n) - rmean(r, n) * rmean(m, n)
    var = rmean(m * m, n) - rmean(m, n) ** 2
    return safe_div(cov, var)


@feature("rsi_6", "technical", "RSI(6)")
def _rsi6(c):
    return _rsi(c, 6)


@feature("rsi_14", "technical", "RSI(14)")
def _rsi14(c):
    return _rsi(c, 14)


def _rsi(c: Ctx, n: int) -> pd.DataFrame:
    d = c.C.diff()
    up, dn = wilder(d.clip(lower=0), n), wilder((-d).clip(lower=0), n)
    return safe_div(up, up + dn) * 100


def _macd(c: Ctx):
    def calc():
        dif = ema(c.C, 12) - ema(c.C, 26)
        dea = ema(dif, 9)
        return dif / c.C, dea / c.C
    return c.get("macd", calc)


@feature("macd_dif", "technical", "MACD DIF/收盘")
def _macd_dif(c):
    return _macd(c)[0]


@feature("macd_dea", "technical", "MACD DEA/收盘")
def _macd_dea(c):
    return _macd(c)[1]


@feature("macd_hist", "technical", "MACD 柱/收盘")
def _macd_hist(c):
    dif, dea = _macd(c)
    return 2 * (dif - dea)


def _kdj(c: Ctx):
    def calc():
        llv, hhv = rmin(c.L, 9), rmax(c.H, 9)
        rsv = safe_div(c.C - llv, hhv - llv) * 100
        k = rsv.ewm(alpha=1 / 3, adjust=False).mean()
        d = k.ewm(alpha=1 / 3, adjust=False).mean()
        return k, d, 3 * k - 2 * d
    return c.get("kdj", calc)


@feature("kdj_k", "technical", "KDJ K")
def _kdj_k(c):
    return _kdj(c)[0]


@feature("kdj_d", "technical", "KDJ D")
def _kdj_d(c):
    return _kdj(c)[1]


@feature("kdj_j", "technical", "KDJ J")
def _kdj_j(c):
    return _kdj(c)[2]


@feature("boll_pos", "technical", "布林带位置 (收-中轨)/(2倍标准差)")
def _boll(c):
    return safe_div(c.C - c.ma("C", 20), 2 * rstd(c.C, 20))


@feature("boll_width", "technical", "布林带宽度")
def _bollw(c):
    return safe_div(4 * rstd(c.C, 20), c.ma("C", 20))


@feature("cci_14", "technical", "CCI(14)（标准差版）")
def _cci(c):
    tp = (c.H + c.L + c.C) / 3
    return safe_div(tp - rmean(tp, 14), rstd(tp, 14))


@feature("wr_14", "technical", "威廉指标 WR(14)")
def _wr(c):
    hhv, llv = rmax(c.H, 14), rmin(c.L, 14)
    return safe_div(hhv - c.C, hhv - llv)


@feature("atr_14", "volatility", "ATR(14)/收盘")
def _atr(c):
    prev = c.C.shift(1)
    tr = np.maximum(c.H - c.L, np.maximum((c.H - prev).abs(), (c.L - prev).abs()))
    return wilder(tr, 14) / c.C


@feature("obv_slope_20", "volume", "OBV 20日变化/20日成交量")
def _obv(c):
    signed = np.sign(c.ret.fillna(0)) * c.V
    obv = signed.cumsum()
    return safe_div(obv - obv.shift(20), c.V.rolling(20, min_periods=16).sum())


_register_simple()

DEFAULT_GROUPS = list(GROUPS)


def catalog() -> list[dict]:
    return [{"name": f.name, "group": f.group, "group_name": GROUPS[f.group], "desc": f.desc} for f in REGISTRY.values()]


def select(groups=None, names=None) -> list[str]:
    if names:
        return [n for n in names if n in REGISTRY]
    groups = set(groups or DEFAULT_GROUPS)
    return [f.name for f in REGISTRY.values() if f.group in groups]


def compute(ctx: Ctx, name: str) -> pd.DataFrame:
    out = REGISTRY[name].fn(ctx)
    out = out.replace([np.inf, -np.inf], np.nan)
    return out.where(ctx.alive)
