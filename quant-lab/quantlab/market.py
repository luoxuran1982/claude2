"""A 股代码规范化、品种/板块识别、涨跌停幅度。

统一代码格式：小写市场前缀 + 6 位数字，例如 sh600000、sz000001、bj830799。
"""
from __future__ import annotations

import re

import numpy as np
import pandas as pd

BOARDS = {
    "sh_main": "沪市主板",
    "sz_main": "深市主板",
    "chinext": "创业板",
    "star": "科创板",
    "bse": "北交所",
}
CHINEXT_REFORM = pd.Timestamp("2020-08-24")  # 创业板注册制，涨跌幅 10% → 20%

_CODE = re.compile(r"(\d{6})")
_PREFIX = re.compile(r"^(sh|sz|bj)", re.I)
_SUFFIX = re.compile(r"\.(sh|sz|bj|ss|xshg|xshe|bse)$", re.I)
_SUFFIX_MAP = {"sh": "sh", "ss": "sh", "xshg": "sh", "sz": "sz", "xshe": "sz", "bj": "bj", "bse": "bj"}


def guess_market(code: str) -> str:
    """只有 6 位数字时按股票代码规则推断市场（000001 视为平安银行而不是上证指数）。"""
    if code.startswith(("6", "9", "5")):
        return "sh"
    if code.startswith(("4", "8", "92")):
        return "bj"
    return "sz"


def normalize_symbol(raw, market_hint: str | None = None) -> str | None:
    """'600000.SH' / 'SH600000' / 'sh.600000' / '600000' / 600000 → 'sh600000'。无法识别返回 None。"""
    if raw is None:
        return None
    if isinstance(raw, (int, np.integer)):
        raw = f"{int(raw):06d}"
    text = str(raw).strip()
    if not text:
        return None
    m = _CODE.search(text)
    if not m:
        return None
    code = m.group(1)
    market = None
    pm = _PREFIX.match(text)
    if pm:
        market = pm.group(1).lower()
    else:
        sm = _SUFFIX.search(text)
        if sm:
            market = _SUFFIX_MAP[sm.group(1).lower()]
    market = market or (market_hint.lower() if market_hint else None) or guess_market(code)
    if market not in ("sh", "sz", "bj"):
        return None
    return market + code


def classify(symbol: str) -> tuple[str, str | None]:
    """返回 (品种, 板块)。品种：stock / index / fund / bond / other。"""
    market, code = symbol[:2], symbol[2:]
    if market == "sh":
        if code.startswith("60"):
            return "stock", "sh_main"
        if code.startswith(("688", "689")):
            return "stock", "star"
        if code.startswith(("000", "880", "999")):
            return "index", None
        if code.startswith("5"):
            return "fund", None
        if code.startswith(("01", "02", "10", "11", "12", "13", "14", "20", "204")):
            return "bond", None
        return "other", None
    if market == "sz":
        if code.startswith(("000", "001", "002", "003", "004")):
            return "stock", "sz_main"
        if code.startswith(("300", "301", "302")):
            return "stock", "chinext"
        if code.startswith("399"):
            return "index", None
        if code.startswith(("15", "16", "18")):
            return "fund", None
        if code.startswith(("10", "11", "12", "13")):
            return "bond", None
        return "other", None
    if market == "bj":
        if code.startswith("899"):
            return "index", None
        if code.startswith(("43", "83", "87", "88", "92")):
            return "stock", "bse"
    return "other", None


def price_divisor(symbol: str) -> int:
    """通达信 .day 文件里价格是整数：股票/指数 ×100，基金/债券 ×1000。"""
    kind, _ = classify(symbol)
    return 1000 if kind in ("fund", "bond") else 100


def is_st(name: str | None) -> bool:
    return bool(name) and "ST" in name.upper()


def limit_rate_frame(dates: pd.DatetimeIndex, symbols: list[str], names: dict[str, str] | None = None) -> pd.DataFrame:
    """每只股票每天的涨跌停幅度（宽表）。ST 按名称判断（只知道当前名称，属近似）。"""
    names = names or {}
    rates = np.full((len(dates), len(symbols)), 0.10, dtype="float32")
    reform = np.asarray(dates >= CHINEXT_REFORM)
    for j, sym in enumerate(symbols):
        _, board = classify(sym)
        if board == "star":
            rates[:, j] = 0.20
        elif board == "bse":
            rates[:, j] = 0.30
        elif board == "chinext":
            rates[:, j] = np.where(reform, 0.20, 0.10)
        if is_st(names.get(sym)) and board in ("sh_main", "sz_main"):
            rates[:, j] = 0.05
    return pd.DataFrame(rates, index=dates, columns=symbols)
