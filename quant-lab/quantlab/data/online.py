"""在线数据源的公共部分：增量计划、限速、重试、复权口径、写入数据集。

AKShare（akshare_source.py）和富途 OpenD（futu_source.py）各自只负责「拿一只证券一段日期的日线」，
其余流程都在这里：

    计划（哪些代码要下、从哪天开始） → 逐只下载（限速 + 失败重试） → 重叠日校验（复权口径变了就整段重下）
    → 写进本地数据集（和通达信导入的数据存在同一个地方、同一种格式）

日线统一成 date, open, high, low, close, volume(股), amount(元)[, factor]。
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

import numpy as np
import pandas as pd

from ..market import classify
from . import readers
from .store import DataStore

log = logging.getLogger("quantlab")
Progress = Callable[[float, str], None]

ADJUST_MODES = {
    "factor": "不复权价格 + 复权因子（推荐）",
    "qfq": "前复权",
    "hfq": "后复权",
    "none": "不复权（导入后按跳空估算除权）",
}


class SourceError(Exception):
    """给用户看的中文原因（网络、接口、权限、额度……）。fatal=True 表示后面的代码也不用再试了。"""

    def __init__(self, message: str, fatal: bool = False, retry: bool = False):
        super().__init__(message)
        self.fatal = fatal
        self.retry = retry


class Fetcher(Protocol):
    label: str

    def daily(self, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame: ...


class RateLimiter:
    """两次请求之间至少间隔 interval 秒。"""

    def __init__(self, interval: float, sleep=time.sleep, clock=time.monotonic):
        self.interval = max(0.0, float(interval))
        self.sleep, self.clock = sleep, clock
        self._last = None

    def wait(self) -> None:
        if self._last is not None and self.interval > 0:
            gap = self.interval - (self.clock() - self._last)
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()


def with_retry(fn: Callable[[], pd.DataFrame], explain: Callable[[Exception], SourceError], *,
               tries: int = 3, base: float = 2.0, sleep=time.sleep, limiter: RateLimiter | None = None) -> pd.DataFrame:
    """调用 fn，可重试的错误（网络、限流）按 2/4/8 秒退避重试；最终失败抛出中文的 SourceError。"""
    last: SourceError | None = None
    for attempt in range(max(1, tries)):
        if limiter:
            limiter.wait()
        try:
            return fn()
        except SourceError as exc:
            last = exc
        except Exception as exc:  # noqa: BLE001
            last = explain(exc)
        if not last.retry or attempt == tries - 1:
            break
        sleep(base * (2 ** attempt))
    raise last  # type: ignore[misc]


def last_weekday(day) -> pd.Timestamp:
    d = pd.Timestamp(day).normalize()
    while d.weekday() >= 5:
        d -= pd.Timedelta(days=1)
    return d


@dataclass
class Task:
    symbol: str
    start: str           # YYYY-MM-DD
    incremental: bool    # True = 从本地最后一天接着下（含重叠的那一天用来校验）


@dataclass
class Plan:
    tasks: list[Task] = field(default_factory=list)
    up_to_date: list[str] = field(default_factory=list)


def plan(existing: dict[str, dict], symbols: list[str], start: str, end: str) -> Plan:
    """已有数据到 end（按工作日算）的跳过；有一部分的从最后一天接着下；没有的整段下载。"""
    out = Plan()
    want_end = last_weekday(end)
    for sym in symbols:
        info = existing.get(sym)
        if info is None:
            out.tasks.append(Task(sym, start, False))
            continue
        last, first = pd.Timestamp(info["date"]), pd.Timestamp(info["start"])
        if first > pd.Timestamp(start) + pd.Timedelta(days=7):   # 用户把开始日期往前挪了：整段重下
            out.tasks.append(Task(sym, start, False))
        elif last >= want_end:
            out.up_to_date.append(sym)
        else:
            out.tasks.append(Task(sym, last.strftime("%Y-%m-%d"), True))
    return out


def standardize(df: pd.DataFrame, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    """数值化、去重、排序、按日期截取；去掉价格无效的行。"""
    if df is None or df.empty:
        return pd.DataFrame(columns=["date", "open", "high", "low", "close", "volume", "amount"])
    df = df.copy()
    df["date"] = pd.to_datetime(df["date"]).dt.normalize()
    cols = ["open", "high", "low", "close", "volume", "amount"] + (["factor"] if "factor" in df else [])
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df else np.nan
    df = df[(df["close"] > 0) & df["close"].notna()]
    if start:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end:
        df = df[df["date"] <= pd.Timestamp(end)]
    df["volume"] = df["volume"].fillna(0.0)
    df["amount"] = df["amount"].fillna(0.0)
    return df.drop_duplicates("date", keep="last").sort_values("date")[["date"] + cols].reset_index(drop=True)


def factor_from(raw: pd.DataFrame, hfq: pd.DataFrame) -> pd.DataFrame:
    """不复权价 + 后复权收盘价 → 复权因子 = 后复权收盘 / 不复权收盘（只随除权变化，历史不会变）。"""
    raw = standardize(raw)
    if raw.empty:
        return raw
    h = standardize(hfq)[["date", "close"]].rename(columns={"close": "hclose"})
    m = raw.merge(h, on="date", how="left")
    m["factor"] = (m["hclose"] / m["close"]).replace([np.inf, -np.inf], np.nan).ffill().bfill().fillna(1.0)
    return m.drop(columns="hclose")


def _same(a: float, b: float, tol: float = 2e-3) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-9)


@dataclass
class Result:
    meta: dict | None
    downloaded: int
    up_to_date: int
    failed: list[str]
    skipped_quota: list[str]
    message: str


def download(store: DataStore, fetcher: Fetcher, symbols: list[str], *, start: str, end: str, adjust: str,
             ds_id: str | None, title: str, names: dict[str, str] | None = None, progress: Progress = lambda p, m: None,
             budget: Callable[[list[Task]], tuple[list[Task], list[str]]] | None = None,
             max_consecutive_fail: int = 5) -> Result:
    """把 symbols 在 [start, end] 的日线下载进数据集 ds_id（不存在就新建）。

    budget：可选的额度检查（富途 OpenD），输入待下载任务，返回（能下的任务，因额度不够跳过的代码）。
    """
    if adjust not in ADJUST_MODES:
        raise ValueError("复权方式无效")
    names = names or {}
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        raise ValueError("股票范围是空的：请选择范围或填写代码")
    start = pd.Timestamp(start).strftime("%Y-%m-%d")
    end = pd.Timestamp(end).strftime("%Y-%m-%d")
    if start > end:
        raise ValueError("开始日期晚于结束日期")
    existing = store.last_rows(ds_id) if ds_id else {}
    if existing and ds_id:
        old_adjust = {m["symbol"]: m.get("adjust") for m in store.meta(ds_id).get("symbols", [])}
    else:
        old_adjust = {}
    want_label = _adjust_label(adjust)
    # 复权口径和已有数据不同：这些代码整段重下，不能和旧数据拼在一起
    for sym in symbols:
        if sym in existing and old_adjust.get(sym) not in (None, want_label) and classify(sym)[0] == "stock":
            existing.pop(sym)
    pl = plan(existing, symbols, start, end)
    skipped_quota: list[str] = []
    tasks = pl.tasks
    if budget is not None:
        tasks, skipped_quota = budget(tasks)
    progress(0.02, f"计划：下载 {len(tasks)} 只，已是最新 {len(pl.up_to_date)} 只"
             + (f"，额度不够跳过 {len(skipped_quota)} 只" if skipped_quota else ""))

    series: dict[str, readers.Series] = {}
    replace: set[str] = set()
    failed: list[str] = []
    issues: list[str] = []
    consecutive = 0
    cancelled = None
    for i, t in enumerate(tasks):
        try:
            progress(0.03 + 0.62 * i / max(len(tasks), 1), f"{fetcher.label}：{t.symbol} {names.get(t.symbol, '')}（{i + 1}/{len(tasks)}）")
        except Exception as exc:  # noqa: BLE001  任务被取消：已下载的先保存
            cancelled = exc
            break
        try:
            df = fetcher.daily(t.symbol, t.start, end, adjust)
            if t.incremental and not df.empty:
                prev = existing[t.symbol]
                row = df[df["date"] == prev["date"]]
                changed = (not row.empty and (not _same(float(row["close"].iloc[0]), prev["close"])
                           or ("factor" in df and not _same(float(row["factor"].iloc[0]), prev["factor"]))))
                if changed:   # 复权价整体变了（前复权遇到新的分红送转）：整段重下
                    df = fetcher.daily(t.symbol, pd.Timestamp(prev["start"]).strftime("%Y-%m-%d"), end, adjust)
                    replace.add(t.symbol)
                    issues.append(f"{t.symbol}: 复权价格与本地不一致（期间有除权），已整段重新下载")
            consecutive = 0
        except SourceError as exc:
            failed.append(f"{t.symbol} {names.get(t.symbol, '')}：{exc}".strip())
            consecutive += 1
            if exc.fatal or consecutive >= max_consecutive_fail:
                if not series:
                    raise SourceError(f"{exc}" + (f"（连续 {consecutive} 只失败，已停止）" if consecutive > 1 else ""),
                                      fatal=True) from None
                issues.append(f"连续失败，提前停止：{exc}")
                break
            continue
        if df.empty:
            if not t.incremental:
                failed.append(f"{t.symbol} {names.get(t.symbol, '')}：所选区间没有数据（未上市、长期停牌或代码不对）")
            continue
        if t.incremental and t.symbol not in replace and len(df) <= 1:
            continue    # 只有重叠的那一天：没有新数据
        series[t.symbol] = readers.Series(t.symbol, df, name=names.get(t.symbol) or None,
                                          adjusted=adjust in ("qfq", "hfq"), source=fetcher.label,
                                          source_label=fetcher.label, adjust_label=want_label)

    for f in failed[:300]:
        issues.append("下载失败 " + f)
    for s in skipped_quota[:300]:
        issues.append(f"{s}: 历史 K 线额度不够，本次没有下载")
    meta = None
    if series:
        merge = bool(ds_id) and store.exists(ds_id)
        meta = store.ingest(series, title=title, ds_id=ds_id, merge=merge, auto_adjust=adjust == "none",
                            source=fetcher.label, issues=issues, progress=(lambda p, m: None) if cancelled else
                            (lambda p, m: progress(0.65 + 0.35 * p, m)), replace=replace)
        store.touch(meta["id"], pl.up_to_date)
    elif ds_id and store.exists(ds_id):
        store.touch(ds_id, [s for s in pl.up_to_date])
        meta = {k: v for k, v in store.meta(ds_id).items() if k not in ("symbols", "issues")}
    msg = (f"新下载/更新 {len(series)} 只，已是最新 {len(pl.up_to_date)} 只"
           + (f"，失败 {len(failed)} 只" if failed else "") + (f"，额度不够跳过 {len(skipped_quota)} 只" if skipped_quota else ""))
    if cancelled is not None:
        raise cancelled
    if meta is None:
        reason = failed[0] if failed else "没有可下载的数据"
        raise SourceError(f"没有下载到任何数据：{reason}")
    log.info("%s 下载完成：%s", fetcher.label, msg)
    return Result(meta, len(series), len(pl.up_to_date), failed, skipped_quota, msg)


def _adjust_label(adjust: str) -> str:
    return {"factor": "复权因子列", "qfq": "前复权", "hfq": "后复权", "none": "跳空估算"}[adjust]


def parse_codes(text: str) -> tuple[list[str], list[str]]:
    """「600519, 000001 sz300750；830799.BJ」→ 统一代码；返回（能识别的，不能识别的）。"""
    from ..market import normalize_symbol
    import re
    good, bad = [], []
    for tok in re.split(r"[\s,，;；、]+", text or ""):
        if not tok:
            continue
        sym = normalize_symbol(tok)
        (good if sym else bad).append(sym or tok)
    return list(dict.fromkeys(good)), bad
