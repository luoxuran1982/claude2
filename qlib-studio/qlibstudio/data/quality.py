"""数据质量检查：直接读 Qlib 二进制文件（features/<代码>/<字段>.day.bin），不依赖 Qlib 初始化。

Qlib 的 .bin 文件格式：第 1 个 float32 是这只股票第一天在交易日历里的位置，后面每个 float32 是一天的值；
导入时已对齐到交易日历，所以「缺失」= 这只股票在上市区间内某个交易日没有数据（多半是停牌，也可能是数据源漏了）。

检查项与口径：
- 缺失天数：上市区间（第一天到最后一天）内收盘价为空的交易日数。停牌和漏数据在这里分不开，统一叫「缺失/停牌」。
- 零成交天数：有价格但成交量为 0 的天数（部分数据源把停牌记成成交量 0 的一行）。
- 长时间停牌：连续缺失超过 20 个交易日的片段。
- 复权提示：复权因子从不变化、但出现超出涨跌停幅度的价格跳空 → 很可能是不复权数据里的除权日，
  收益和因子会被这一天的假跳空污染；有复权因子（factor 字段在变化）时视为已处理。
- 数据中断：最后一天早于整个数据集最后一天 20 个交易日以上（退市、改代码或数据没更新）。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from . import datasets
from .market import classify

LONG_SUSPEND = 20
STALE_DAYS = 20


def read_bin(f: Path, n_cal: int) -> np.ndarray | None:
    """返回对齐到完整日历的数组（不在区间内的位置为 NaN）。"""
    try:
        raw = np.fromfile(f, dtype="<f4")
    except OSError:
        return None
    if raw.size < 1:
        return None
    start = int(raw[0])
    vals = raw[1:].astype("float64")
    out = np.full(n_cal, np.nan)
    end = min(n_cal, start + len(vals))
    if start < n_cal:
        out[start:end] = vals[: end - start]
    return out


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """连续 True 片段 [(起, 止)]。"""
    if not mask.any():
        return []
    d = np.diff(np.r_[0, mask.astype(int), 0])
    return list(zip(np.nonzero(d == 1)[0], np.nonzero(d == -1)[0] - 1))


def _limit(symbol: str) -> float:
    _, board = classify(symbol.lower())
    return {"star": 0.20, "chinext": 0.20, "bse": 0.30}.get(board, 0.10)


def check(ds_dir: Path, max_symbols: int | None = None, progress=lambda p, m: None) -> dict:
    ds_dir = Path(ds_dir)
    cal = datasets.calendar(ds_dir)
    n = len(cal)
    feats = ds_dir / "features"
    dirs = sorted(p for p in feats.iterdir() if p.is_dir()) if feats.is_dir() else []
    sampled = False
    if max_symbols and len(dirs) > max_symbols:
        idx = np.linspace(0, len(dirs) - 1, max_symbols).astype(int)
        dirs = [dirs[i] for i in idx]
        sampled = True
    rows, issues = [], []
    totals = {"missing": 0, "zero_volume": 0, "long_suspend": 0, "suspect_exright": 0, "stale": 0, "symbols": 0,
              "with_factor": 0, "bad": 0}
    has_factor_field = False
    for k, d in enumerate(dirs):
        sym = d.name.upper()
        close = read_bin(d / "close.day.bin", n)
        if close is None:
            totals["bad"] += 1
            issues.append({"symbol": sym, "level": "错误", "text": "没有收盘价文件"})
            continue
        vol = read_bin(d / "volume.day.bin", n)
        fac = read_bin(d / "factor.day.bin", n) if (d / "factor.day.bin").is_file() else None
        has_factor_field |= fac is not None
        valid = np.isfinite(close)
        if not valid.any():
            totals["bad"] += 1
            issues.append({"symbol": sym, "level": "错误", "text": "收盘价全部为空"})
            continue
        first, last = int(np.argmax(valid)), int(n - 1 - np.argmax(valid[::-1]))
        span = slice(first, last + 1)
        miss = ~valid[span]
        n_miss = int(miss.sum())
        zero = int(((vol[span] == 0) & valid[span]).sum()) if vol is not None else 0
        long_runs = [(a, b) for a, b in _runs(miss) if b - a + 1 > LONG_SUSPEND]
        kind = classify(sym.lower())[0]
        factor_varies = bool(fac is not None and np.nanmax(fac[span]) - np.nanmin(fac[span]) > 1e-6)
        suspect = 0
        if kind == "stock" and not factor_varies:
            c = close[span]
            c = c[np.isfinite(c)]
            if len(c) > 2:
                ret = c[1:] / c[:-1] - 1
                suspect = int((np.abs(ret[5:]) > _limit(sym) + 0.03).sum())
        stale = (n - 1 - last) > STALE_DAYS
        totals["symbols"] += 1
        totals["missing"] += n_miss
        totals["zero_volume"] += zero
        totals["long_suspend"] += int(bool(long_runs))
        totals["suspect_exright"] += int(suspect > 0)
        totals["stale"] += int(stale)
        totals["with_factor"] += int(factor_varies)
        row = {"symbol": sym, "kind": kind, "start": cal[first], "end": cal[last], "days": last - first + 1,
               "missing": n_miss, "zero_volume": zero, "long_suspend": len(long_runs),
               "suspect_exright": suspect, "factor": "有（在变化）" if factor_varies else ("有（恒定）" if fac is not None else "无"),
               "stale": stale}
        rows.append(row)
        if long_runs:
            a, b = long_runs[0]
            issues.append({"symbol": sym, "level": "提示",
                           "text": f"长时间停牌/缺数据：{cal[first + a]} ~ {cal[first + b]}（{b - a + 1} 个交易日）"
                                   + (f" 等 {len(long_runs)} 段" if len(long_runs) > 1 else "")})
        if suspect:
            issues.append({"symbol": sym, "level": "警告",
                           "text": f"复权提示：复权因子不变，但有 {suspect} 天收盘价跳空超过涨跌停幅度，可能是不复权数据的除权日"})
        if stale:
            issues.append({"symbol": sym, "level": "提示", "text": f"数据在 {cal[last]} 之后中断（退市、改代码或没更新）"})
        if k % 200 == 0:
            progress(k / max(len(dirs), 1), f"质量检查 {k}/{len(dirs)}")
    level = "良好"
    notes = []
    if totals["bad"]:
        level = "有错误"
    elif totals["suspect_exright"]:
        level = "需注意"
    if totals["suspect_exright"]:
        notes.append(f"{totals['suspect_exright']} 只股票疑似含未复权的除权跳空（见明细「复权提示」）。"
                     "建议改用带复权因子的数据，或导入时选「不复权（自动估算除权）」")
    if not has_factor_field:
        notes.append("数据里没有复权因子字段（$factor），回测时一手 100 股按复权价近似计算")
    if totals["long_suspend"]:
        notes.append(f"{totals['long_suspend']} 只股票有超过 {LONG_SUSPEND} 个交易日的停牌/缺数据")
    if sampled:
        notes.append(f"股票较多，只抽查了 {len(dirs)} 只")
    summary = {"level": level, "calendar_days": n, "start": cal[0] if cal else None, "end": cal[-1] if cal else None,
               "checked": len(dirs), "sampled": sampled, **totals, "notes": notes}
    rows.sort(key=lambda r: (-r["suspect_exright"], -r["missing"]))
    level_order = {"错误": 0, "警告": 1, "提示": 2}
    issues.sort(key=lambda r: level_order.get(r["level"], 9))
    return {"summary": summary, "rows": rows[:2000], "issues": issues[:500]}
