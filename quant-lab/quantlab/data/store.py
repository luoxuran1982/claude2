"""数据集管理：导入 → 清洗 → 复权 → 存成一个 parquet；按需加载成宽表 Panel。

目录：<数据目录>/datasets/<id>/bars.parquet + meta.json
"""
from __future__ import annotations

import json
import logging
import re
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from ..market import BOARDS, classify
from . import adjust, readers

log = logging.getLogger("quantlab")
Progress = Callable[[float, str], None]
COLUMNS = ["symbol", "date", "open", "high", "low", "close", "volume", "amount", "factor"]


def _noop(_p: float, _m: str) -> None:
    pass


@dataclass
class Panel:
    """宽表形式的行情：行 = 交易日，列 = 证券代码。价格为不复权原始价，adj() 给复权价。"""
    dates: pd.DatetimeIndex
    symbols: list[str]
    names: dict[str, str]
    raw: dict[str, pd.DataFrame]      # open high low close volume amount
    factor: pd.DataFrame

    def adj(self, field: str) -> pd.DataFrame:
        return self.raw[field] * self.factor

    @property
    def traded(self) -> pd.DataFrame:
        """当天有成交（未停牌）。"""
        return self.raw["close"].notna() & (self.raw["volume"] > 0)

    def board_of(self) -> dict[str, str | None]:
        return {s: classify(s)[1] for s in self.symbols}

    def subset(self, symbols: list[str]) -> "Panel":
        symbols = [s for s in symbols if s in self.factor.columns]
        return Panel(self.dates, symbols, {s: self.names.get(s, "") for s in symbols},
                     {k: v[symbols] for k, v in self.raw.items()}, self.factor[symbols])


class DataStore:
    def __init__(self, root: Path):
        self.root = Path(root) / "datasets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self._cache: tuple[tuple, pd.DataFrame] | None = None

    # ------------------------------------------------------------ 查询
    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.root.iterdir()) if self.root.exists() else []:
            meta = d / "meta.json"
            if meta.exists():
                try:
                    m = json.loads(meta.read_text(encoding="utf-8"))
                    m.pop("symbols", None)
                    m.pop("issues", None)
                    out.append(m)
                except (OSError, ValueError):
                    log.warning("数据集元信息损坏：%s", d)
        return sorted(out, key=lambda m: m.get("created", ""), reverse=True)

    def meta(self, ds_id: str) -> dict:
        path = self._dir(ds_id) / "meta.json"
        if not path.exists():
            raise LookupError(f"数据集不存在：{ds_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def _dir(self, ds_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_\-]+", ds_id or ""):
            raise ValueError("数据集编号无效")
        return self.root / ds_id

    def exists(self, ds_id: str) -> bool:
        try:
            return (self._dir(ds_id) / "meta.json").exists()
        except ValueError:
            return False

    def touch(self, ds_id: str, symbols: list[str]) -> None:
        """在线更新时这些代码已是最新：记下检查时间（数据本身不变）。"""
        if not symbols or not self.exists(ds_id):
            return
        meta = self.meta(ds_id)
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        want = set(symbols)
        for m in meta.get("symbols", []):
            if m["symbol"] in want:
                m["checked"] = now
        meta["checked"] = now
        (self._dir(ds_id) / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")

    def delete(self, ds_id: str) -> None:
        shutil.rmtree(self._dir(ds_id), ignore_errors=True)
        self._cache = None

    def bars(self, ds_id: str) -> pd.DataFrame:
        meta = self.meta(ds_id)
        key = (ds_id, meta.get("revision"))
        with self.lock:
            if self._cache and self._cache[0] == key:
                return self._cache[1]
            df = pd.read_parquet(self._dir(ds_id) / "bars.parquet")
            df["symbol"] = df["symbol"].astype(str)
            self._cache = (key, df)
            return df

    def symbol_bars(self, ds_id: str, symbol: str) -> pd.DataFrame:
        df = self.bars(ds_id)
        return df[df["symbol"] == symbol].sort_values("date").reset_index(drop=True)

    # ------------------------------------------------------------ 导入
    def import_path(self, path: str | Path, title: str = "", ds_id: str | None = None, *,
                    kinds=("stock", "index"), auto_adjust: bool = True, merge: bool = False,
                    progress: Progress = _noop) -> dict:
        src = Path(path).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"路径不存在：{src}")
        files = readers.scan(src)
        if not files:
            raise ValueError("没有找到可识别的 K 线文件（支持 .day / .txt / .csv / .tsv / .xlsx / .parquet）")
        progress(0.01, f"找到 {len(files)} 个文件，开始读取")
        series: dict[str, readers.Series] = {}
        name_table: dict[str, str] = {}
        issues: list[str] = []
        done = 0

        def read(p: Path):
            try:
                return p, readers.read_any(p), None
            except Exception as exc:  # noqa: BLE001
                return p, [], str(exc)

        with ThreadPoolExecutor(max_workers=4) as pool:
            for p, items, err in pool.map(read, files):
                done += 1
                if err:
                    issues.append(f"{p.name}: 读取失败：{err}")
                for s in items:
                    kind, _ = classify(s.symbol)
                    if s.df.empty:
                        if s.name:
                            name_table[s.symbol] = s.name
                        continue
                    if kind not in kinds:
                        continue
                    if s.symbol in series:  # 同一代码多个文件：合并，后读到的覆盖重叠日期
                        old = series[s.symbol]
                        s.df = pd.concat([old.df, s.df]).drop_duplicates("date", keep="last").sort_values("date")
                        s.name = s.name or old.name
                    series[s.symbol] = s
                if done % 50 == 0 or done == len(files):
                    progress(0.05 + 0.6 * done / len(files), f"已读取 {done}/{len(files)} 个文件，{len(series)} 只证券")
        for sym, nm in name_table.items():
            if sym in series and not series[sym].name:
                series[sym].name = nm
        if name_table:
            issues.insert(0, f"从名称表读到 {len(name_table)} 个证券名称")
        if not series:
            raise ValueError("文件里没有识别出所选类型的证券。" + ("；".join(issues[:3]) if issues else ""))

        if not title:
            exists = merge and ds_id and (self._dir(ds_id) / "meta.json").exists()
            title = self.meta(ds_id).get("title", "") if exists else src.name
        return self.ingest(series, title=title, ds_id=ds_id, merge=merge, auto_adjust=auto_adjust,
                           source=str(src), issues=issues, n_files=len(files), progress=progress)

    def ingest(self, series: dict[str, "readers.Series"], *, title: str, ds_id: str | None = None,
               merge: bool = False, auto_adjust: bool = True, source: str = "", issues: list[str] | None = None,
               n_files: int = 0, progress: Progress = _noop, replace: set[str] | None = None) -> dict:
        """把若干只证券的日线写进数据集（本地导入、AKShare、富途 OpenD 共用）。

        每只证券在 meta.symbols 里记录 source（数据来源）和 updated（最后更新时间）。
        merge=True 时与已有数据合并：重叠日期以新数据为准；replace 里的代码先删掉旧数据再写（复权口径变了时整段重下）。
        """
        issues = list(issues or [])
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        progress(0.68, "复权处理")
        frames, sym_meta = [], []
        n_events = 0
        for i, (sym, s) in enumerate(sorted(series.items())):
            df = s.df.reset_index(drop=True)
            events = []
            if "factor" in df:
                method = "复权因子列"
            elif s.adjusted:
                df["factor"] = 1.0
                method = s.adjust_label or "已复权数据"
            elif auto_adjust and classify(sym)[0] == "stock":
                df["factor"], events = adjust.estimate_factor(sym, df)
                method = "跳空估算"
            else:
                df["factor"] = 1.0
                method = "未复权"
            n_events += len(events)
            df.insert(0, "symbol", sym)
            frames.append(df[COLUMNS])
            kind, board = classify(sym)
            sym_meta.append({"symbol": sym, "name": s.name or "", "kind": kind, "board": board,
                             "start": df["date"].iloc[0].strftime("%Y-%m-%d"),
                             "end": df["date"].iloc[-1].strftime("%Y-%m-%d"),
                             "rows": len(df), "adjust": method, "events": [d.strftime("%Y-%m-%d") for d in events][:20],
                             "source": s.source_label or source_label_of(s.source), "updated": now})
            for note in s.notes:
                if len(issues) < 500:
                    issues.append(f"{sym}: {note}")
        bars = pd.concat(frames, ignore_index=True)

        ds_id = ds_id or time.strftime("ds%Y%m%d_%H%M%S")
        target = self._dir(ds_id)
        old_info = {}
        if merge and (target / "bars.parquet").exists():
            progress(0.8, "与已有数据合并")
            old_info = self.meta(ds_id)
            old = pd.read_parquet(target / "bars.parquet")
            old["symbol"] = old["symbol"].astype(str)
            if replace:
                old = old[~old["symbol"].isin(replace)]
            old_meta = {m["symbol"]: m for m in old_info.get("symbols", [])}
            bars = pd.concat([old, bars]).drop_duplicates(["symbol", "date"], keep="last")
            bars = _recompute_factor_after_merge(bars, series, auto_adjust)
            new_syms = {m["symbol"] for m in sym_meta}
            for m in sym_meta:
                if not m["name"] and m["symbol"] in old_meta:
                    m["name"] = old_meta[m["symbol"]].get("name", "")
            sym_meta = [m for s, m in old_meta.items() if s not in new_syms] + sym_meta
            title = title or old_info.get("title", "")
            sym_meta = _refresh_ranges(bars, sym_meta)
        progress(0.88, "写入数据集")
        bars = bars.sort_values(["symbol", "date"]).reset_index(drop=True)
        for col in ("open", "high", "low", "close", "factor", "volume", "amount"):
            bars[col] = bars[col].astype("float64")
        target.mkdir(parents=True, exist_ok=True)
        tmp = target / "bars.parquet.tmp"
        bars.to_parquet(tmp, index=False, compression="zstd")
        tmp.replace(target / "bars.parquet")
        sym_meta.sort(key=lambda m: m["symbol"])
        kinds_count = pd.Series([m["kind"] for m in sym_meta]).value_counts().to_dict()
        boards_count = pd.Series([m["board"] for m in sym_meta if m["board"]]).value_counts().to_dict()
        fallback = _fallback_source(old_info) if old_info else source_label_of(source)
        sources = pd.Series([m.get("source") or fallback for m in sym_meta]).value_counts().to_dict()
        meta = {
            "id": ds_id, "title": title or ds_id, "source": old_info.get("source") or source,
            "created": old_info.get("created") or now, "updated": now, "revision": f"{time.time():.3f}",
            "n_symbols": len(sym_meta), "n_rows": int(len(bars)),
            "start": bars["date"].min().strftime("%Y-%m-%d"), "end": bars["date"].max().strftime("%Y-%m-%d"),
            "kinds": kinds_count, "boards": {BOARDS.get(k, k): v for k, v in boards_count.items()},
            "sources": sources,
            "adjust_events": n_events,
            "n_files": n_files, "issues_count": len(issues),
            "symbols": sym_meta, "issues": issues[:500], "demo": False,
        }
        (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        self._cache = None
        progress(1.0, f"写入完成：{len(sym_meta)} 只证券，{len(bars):,} 行")
        return {k: v for k, v in meta.items() if k not in ("symbols", "issues")}

    def last_rows(self, ds_id: str) -> dict[str, dict]:
        """每只证券最后一行（增量更新用）：{代码: {date, close, factor, start}}。数据集不存在时返回空。"""
        try:
            df = self.bars(ds_id)
        except (LookupError, ValueError, OSError):
            return {}
        df = df.sort_values(["symbol", "date"])
        first = df.groupby("symbol")["date"].min()
        last = df.groupby("symbol").tail(1).set_index("symbol")
        return {sym: {"date": r["date"], "close": float(r["close"]), "factor": float(r["factor"]),
                      "start": first[sym]} for sym, r in last.iterrows()}

    def save_frame(self, bars: pd.DataFrame, title: str, names: dict[str, str], ds_id: str, demo=False) -> dict:
        """直接保存一个长表（演示数据、测试用）。"""
        target = self._dir(ds_id)
        target.mkdir(parents=True, exist_ok=True)
        bars = bars[COLUMNS].sort_values(["symbol", "date"]).reset_index(drop=True)
        bars.to_parquet(target / "bars.parquet", index=False, compression="zstd")
        sym_meta = []
        for sym, g in bars.groupby("symbol", sort=True):
            kind, board = classify(sym)
            sym_meta.append({"symbol": sym, "name": names.get(sym, ""), "kind": kind, "board": board,
                             "start": g["date"].iloc[0].strftime("%Y-%m-%d"), "end": g["date"].iloc[-1].strftime("%Y-%m-%d"),
                             "rows": len(g), "adjust": "已复权数据", "events": [],
                             "source": "演示数据" if demo else "内置生成", "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
        meta = {"id": ds_id, "title": title, "source": "内置生成", "created": time.strftime("%Y-%m-%d %H:%M:%S"),
                "revision": f"{time.time():.3f}", "n_symbols": len(sym_meta), "n_rows": int(len(bars)),
                "start": bars["date"].min().strftime("%Y-%m-%d"), "end": bars["date"].max().strftime("%Y-%m-%d"),
                "kinds": pd.Series([m["kind"] for m in sym_meta]).value_counts().to_dict(),
                "boards": {BOARDS.get(k, k): v for k, v in
                           pd.Series([m["board"] for m in sym_meta if m["board"]]).value_counts().to_dict().items()},
                "updated": time.strftime("%Y-%m-%d %H:%M:%S"), "sources": {"演示数据" if demo else "内置生成": len(sym_meta)},
                "adjust_events": 0, "n_files": 0, "issues_count": 0, "symbols": sym_meta, "issues": [], "demo": demo}
        (target / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
        self._cache = None
        return {k: v for k, v in meta.items() if k not in ("symbols", "issues")}

    # ------------------------------------------------------------ 宽表
    def panel(self, ds_id: str, start=None, end=None, symbols: list[str] | None = None,
              warmup_days: int = 400) -> Panel:
        """加载宽表。start 之前多加载 warmup_days 个自然日给指标预热。"""
        meta = self.meta(ds_id)
        names = {m["symbol"]: m.get("name", "") for m in meta.get("symbols", [])}
        df = self.bars(ds_id)
        if symbols is not None:
            df = df[df["symbol"].isin(set(symbols))]
        if start is not None:
            df = df[df["date"] >= pd.Timestamp(start) - pd.Timedelta(days=warmup_days)]
        if end is not None:
            df = df[df["date"] <= pd.Timestamp(end)]
        if df.empty:
            raise ValueError("所选范围内没有数据")
        wide = {f: df.pivot(index="date", columns="symbol", values=f) for f in
                ("open", "high", "low", "close", "volume", "amount", "factor")}
        dates = wide["close"].index
        syms = list(wide["close"].columns)
        factor = wide.pop("factor").ffill().bfill().fillna(1.0)
        return Panel(pd.DatetimeIndex(dates), syms, {s: names.get(s, "") for s in syms}, wide, factor)


def _recompute_factor_after_merge(bars: pd.DataFrame, series: dict, auto_adjust: bool) -> pd.DataFrame:
    """合并后重新估算受影响股票的复权因子（新旧数据拼接处可能也有除权）。"""
    parts = []
    for sym, g in bars.groupby("symbol", sort=False):
        g = g.sort_values("date").reset_index(drop=True)
        s = series.get(sym)
        if s is not None and auto_adjust and not s.adjusted and "factor" not in s.df and classify(sym)[0] == "stock":
            g["factor"], _ = adjust.estimate_factor(sym, g)
        parts.append(g)
    return pd.concat(parts, ignore_index=True)


def _refresh_ranges(bars: pd.DataFrame, sym_meta: list[dict]) -> list[dict]:
    agg = bars.groupby("symbol")["date"].agg(["min", "max", "count"])
    for m in sym_meta:
        if m["symbol"] in agg.index:
            r = agg.loc[m["symbol"]]
            m.update(start=r["min"].strftime("%Y-%m-%d"), end=r["max"].strftime("%Y-%m-%d"), rows=int(r["count"]))
    return sym_meta


def source_label_of(path: str) -> str:
    """本地文件的来源名称：通达信 .day 记作「通达信」，其余记作「本地文件」。"""
    text = str(path or "")
    if text.lower().endswith(".day") or "vipdoc" in text.lower():
        return "通达信"
    return "本地文件" if text else "未知"


def _fallback_source(meta: dict) -> str:
    """1.2.0 之前导入的数据没有逐只记录来源：按数据集整体推断。"""
    if meta.get("demo"):
        return "演示数据"
    return source_label_of(meta.get("source", ""))


def symbol_source(meta: dict, m: dict) -> tuple[str, str]:
    """(来源, 最后更新时间)；老数据集退回到数据集级别的信息。"""
    return (m.get("source") or _fallback_source(meta), m.get("updated") or meta.get("updated") or meta.get("created", ""))


def symbols_where(meta: dict, kinds=("stock",), boards=None) -> list[str]:
    out = []
    for m in meta.get("symbols", []):
        if m["kind"] in kinds and (boards is None or m["kind"] != "stock" or m["board"] in boards):
            out.append(m["symbol"])
    return out


def nan_to_none(values) -> list:
    return [None if (v is None or (isinstance(v, float) and not np.isfinite(v))) else v for v in values]
