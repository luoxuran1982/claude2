"""把各种来源读成 readers.Series 列表，交给 convert.build。"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import demo, readers
from .readers import Series


def from_demo(n_stocks: int = 120, years: float = 4.0, end: str = "2026-09-30", seed: int = 7) -> list[Series]:
    """模拟 A 股（QuantLab 同一个生成器）：带弱规律、涨跌停、停牌、退市、送转除权和一只模拟指数。不是真实行情。"""
    bars, names = demo.generate(n_stocks=n_stocks, years=years, end=end, seed=seed)
    out = []
    for sym, df in bars.groupby("symbol"):
        out.append(Series(sym, df.drop(columns="symbol").reset_index(drop=True), name=names.get(sym),
                          adjusted=False, source="demo"))
    return out


def from_files(paths: list[str], progress=lambda p, m: None) -> tuple[list[Series], list[str]]:
    """通达信 vipdoc 目录（.day）、通达信导出 txt、CSV/TSV/Excel/Parquet；可以是文件或文件夹。返回 (数据, 读取问题)。"""
    files: list[Path] = []
    problems: list[str] = []
    for p in paths:
        p = Path(p).expanduser()
        if not p.exists():
            problems.append(f"找不到：{p}")
            continue
        found = readers.scan(p)
        # 通达信目录：只要日线（lday），跳过 minline/fzline 下的分钟线
        found = [f for f in found if not any(x.lower() in ("minline", "fzline") for x in f.parts)]
        if not found:
            problems.append(f"{p} 里没有可识别的数据文件（支持 .day .txt .csv .tsv .xlsx .xls .parquet）")
        files += found
    series: list[Series] = []
    for k, f in enumerate(files):
        try:
            got = readers.read_any(f)
        except Exception as exc:  # noqa: BLE001  单个文件坏了不影响其他文件
            problems.append(f"{f.name}：{exc}")
            continue
        series += [s for s in got if s.df is not None and len(s.df)]
        if k % 100 == 0:
            progress(0.05 * k / max(len(files), 1), f"读取 {k}/{len(files)}：{f.name}")
    # 名称表（只有代码 + 名称、没有行情的文件）合并到行情里
    names = {s.symbol: s.name for s in series if s.name}
    for s in series:
        s.name = s.name or names.get(s.symbol)
    return [s for s in series if len(s.df) and "close" in s.df], problems


def from_frame(symbol: str, df: pd.DataFrame, name: str | None = None) -> Series:
    """富途缓存等：已经是标准列（date, open, high, low, close, volume, amount[, factor]）的不复权日线。"""
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"])
    clean, notes = readers.clean(df)
    return Series(symbol, clean, name=name, adjusted=False, source="futu", notes=notes)
