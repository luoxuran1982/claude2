"""读取本地 K 线文件，统一成日线 DataFrame：date, open, high, low, close, volume, amount[, factor]。

支持：
- 通达信二进制日线 vipdoc/{sh,sz,bj}/lday/*.day（不复权原始价格）
- 通达信“数据导出”的 .txt/.csv（GBK，首行“600000 浦发银行 日线 前复权”，末行“数据来源:通达信”）
- 通用 CSV/TXT/TSV/Excel/Parquet：中英文列名自动识别；单文件可以含多只股票（有代码列时）
- Tushare 日线（ts_code, trade_date, vol(手), amount(千元)）自动换算单位；带 adj_factor 列时直接用作复权因子
- 分钟线文件（同一天多行）自动聚合为日线
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from ..market import normalize_symbol, price_divisor

FIELDS = ["open", "high", "low", "close", "volume", "amount"]
TEXT_EXT = {".txt", ".csv", ".tsv"}
EXCEL_EXT = {".xlsx", ".xls"}
SUPPORTED_EXT = {".day", ".parquet"} | TEXT_EXT | EXCEL_EXT

ALIASES = {
    "date": ["日期", "交易日期", "交易日", "date", "trade_date", "tradedate", "datetime", "day", "时间"],
    "time": ["时间", "time", "交易时间"],
    "open": ["开盘", "开盘价", "今开", "open", "open_price"],
    "high": ["最高", "最高价", "high", "high_price"],
    "low": ["最低", "最低价", "low", "low_price"],
    "close": ["收盘", "收盘价", "close", "close_price", "最新价"],
    "volume": ["成交量", "成交量(股)", "成交量(手)", "成交量（股）", "成交量（手）", "vol", "volume"],
    "amount": ["成交额", "成交金额", "成交额(元)", "成交额（元）", "成交金额(元)", "amount", "money", "turnover_value"],
    "code": ["代码", "股票代码", "证券代码", "code", "ts_code", "symbol", "sec_code", "stock_code", "order_book_id", "thscode"],
    "name": ["名称", "股票名称", "证券名称", "简称", "股票简称", "name", "sec_name", "stock_name"],
    "factor": ["adj_factor", "复权因子", "factor", "adjfactor"],
}


@dataclass
class Series:
    """一只证券的日线。adjusted=True 表示价格已经复权（前/后复权），不再做除权估算。"""
    symbol: str
    df: pd.DataFrame
    name: str | None = None
    adjusted: bool = False
    source: str = ""
    notes: list[str] = field(default_factory=list)


def _norm(col) -> str:
    return re.sub(r"\s+", "", str(col)).strip().lower()


def match_columns(columns) -> dict[str, str]:
    """把原始列名映射到标准字段。返回 {标准字段: 原始列名}。"""
    normed = {_norm(c): c for c in columns}
    found: dict[str, str] = {}
    for key, names in ALIASES.items():
        for alias in names:
            col = normed.get(_norm(alias))
            if col is not None and col not in found.values():
                found[key] = col
                break
    # “时间”同时可能是日期列：有“日期”时它是分钟时间，否则当日期用
    if "time" in found and found.get("date") == found["time"]:
        del found["time"]
    return found


def parse_dates(s: pd.Series) -> pd.Series:
    text = s.astype(str).str.strip()
    text = text.str.replace(r"\.0$", "", regex=True)
    if len(text) and text.str.fullmatch(r"\d{8}").all():
        return pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    return pd.to_datetime(text.str.replace("/", "-", regex=False), errors="coerce", format="mixed")


def _fix_units(df: pd.DataFrame, tushare: bool, header_hint: str) -> list[str]:
    """成交量统一成“股”、成交额统一成“元”。"""
    notes = []
    if tushare:
        df["volume"] *= 100
        df["amount"] *= 1000
        return ["Tushare 单位：成交量 手→股，成交额 千元→元"]
    if "手" in header_hint and "volume" in df:
        df["volume"] *= 100
        notes.append("成交量单位为手，已换算成股")
        return notes
    if "volume" in df and "amount" in df:
        ok = (df["volume"] > 0) & (df["amount"] > 0) & (df["close"] > 0)
        if ok.sum() >= 5:
            r = float(np.median(df.loc[ok, "amount"] / (df.loc[ok, "close"] * df.loc[ok, "volume"])))
            if 50 < r < 200:
                df["volume"] *= 100
                notes.append("推断成交量单位为手，已换算成股")
            elif 0.0005 < r < 0.002:
                df["amount"] *= 1000
                notes.append("推断成交额单位为千元，已换算成元")
            elif 0.05 < r < 0.2:
                df["volume"] *= 100
                df["amount"] *= 1000
                notes.append("推断成交量为手、成交额为千元，已换算")
    return notes


def clean(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """排序、去重、去掉无效行、修正高低价。返回 (结果, 说明)。"""
    notes = []
    n0 = len(df)
    for col in FIELDS:
        if col not in df:
            df[col] = np.nan if col in ("open", "high", "low", "close") else 0.0
    df = df.dropna(subset=["date", "close"])
    df = df[df["close"] > 0]
    for col in ("open", "high", "low"):
        df.loc[~(df[col] > 0), col] = df["close"]
    df = df.sort_values("date").drop_duplicates("date", keep="last")
    hi = df[["open", "high", "low", "close"]].max(axis=1)
    lo = df[["open", "high", "low", "close"]].min(axis=1)
    bad = int(((df["high"] < hi - 1e-9) | (df["low"] > lo + 1e-9)).sum())
    if bad:
        notes.append(f"{bad} 行最高/最低价不一致，已修正")
    df["high"], df["low"] = hi, lo
    df["volume"] = df["volume"].fillna(0).clip(lower=0)
    df["amount"] = df["amount"].fillna(0).clip(lower=0)
    if n0 - len(df):
        notes.append(f"丢弃 {n0 - len(df)} 行无效/重复数据")
    cols = ["date"] + FIELDS + (["factor"] if "factor" in df else [])
    return df[cols].reset_index(drop=True), notes


def to_daily(df: pd.DataFrame) -> pd.DataFrame:
    """分钟线 → 日线。"""
    df = df.sort_values("date")
    day = df["date"].dt.normalize()
    agg = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum", "amount": "sum"}
    if "factor" in df:
        agg["factor"] = "last"
    out = df.groupby(day).agg(agg)
    out.index.name = "date"
    return out.reset_index()


# ---------------------------------------------------------------- 通达信二进制

TDX_DTYPE = np.dtype([("date", "<u4"), ("open", "<u4"), ("high", "<u4"), ("low", "<u4"), ("close", "<u4"),
                      ("amount", "<f4"), ("volume", "<u4"), ("reserved", "<u4")])


def read_tdx_day(path: Path) -> Series | None:
    stem = path.stem.lower()
    hint = stem[:2] if stem[:2] in ("sh", "sz", "bj") else None
    if not hint:
        parent = path.parent.parent.name.lower()
        hint = parent if parent in ("sh", "sz", "bj") else None
    symbol = normalize_symbol(stem, hint)
    if not symbol:
        return None
    raw = path.read_bytes()
    n = len(raw) // TDX_DTYPE.itemsize
    if n == 0:
        return None
    arr = np.frombuffer(raw[: n * TDX_DTYPE.itemsize], dtype=TDX_DTYPE)
    div = float(price_divisor(symbol))
    df = pd.DataFrame({
        "date": pd.to_datetime(arr["date"].astype(str), format="%Y%m%d", errors="coerce"),
        "open": arr["open"] / div, "high": arr["high"] / div, "low": arr["low"] / div, "close": arr["close"] / div,
        "volume": arr["volume"].astype("float64"), "amount": arr["amount"].astype("float64"),
    })
    df, notes = clean(df)
    return Series(symbol, df, adjusted=False, source=str(path), notes=notes)


def write_tdx_day(path: Path, df: pd.DataFrame, symbol: str) -> None:
    """测试/演示用：把日线写成通达信 .day 格式。"""
    div = price_divisor(symbol)
    arr = np.zeros(len(df), dtype=TDX_DTYPE)
    arr["date"] = df["date"].dt.strftime("%Y%m%d").astype(int)
    for col in ("open", "high", "low", "close"):
        arr[col] = np.round(df[col].to_numpy() * div).astype("u4")
    arr["amount"] = df["amount"].to_numpy(dtype="float32")
    arr["volume"] = np.clip(np.nan_to_num(df["volume"].to_numpy()), 0, 2**32 - 1).astype("u4")
    path.write_bytes(arr.tobytes())


# ---------------------------------------------------------------- 文本 / 表格

def decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


_TDX_TITLE = re.compile(r"^\s*(\d{6})\s+(\S+)\s+.*(日线|周线|月线|分钟|分钟线|线)")


def _sep(line: str) -> str:
    if "\t" in line:
        return "\t"
    if "," in line:
        return ","
    if ";" in line:
        return ";"
    return r"\s+"


def read_text(path: Path, market_hint: str | None = None) -> list[Series]:
    text = decode(path.read_bytes())
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return []
    title_code = title_name = None
    adjusted = False
    head = 0
    m = _TDX_TITLE.match(lines[0])
    if m:  # 通达信导出
        title_code, title_name = m.group(1), m.group(2)
        adjusted = "复权" in lines[0] and "不复权" not in lines[0]
        head = 1
    # 找表头：前几行里能识别出日期列的那一行
    header_idx = None
    for i in range(head, min(head + 5, len(lines))):
        cols = re.split(_sep(lines[i]), lines[i].strip())
        mc = match_columns(cols)
        if ("date" in mc or ("code" in mc and "name" in mc)) and not re.match(r"^\d", cols[0]):
            header_idx = i
            break
    body = lines[header_idx + 1 if header_idx is not None else head:]
    if header_idx is not None:
        body = [ln for ln in body if "数据来源" not in ln]
    else:
        body = [ln for ln in body if re.match(r"^\s*[\d\"']", ln)]
    if not body:
        return []
    sep = _sep(body[0])
    if header_idx is not None:
        header = re.split(_sep(lines[header_idx]), lines[header_idx].strip())
        csv_text = "\n".join([sep.join(header) if sep != r"\s+" else " ".join(header)] + body)
        df = pd.read_csv(io.StringIO(csv_text), sep=sep, dtype=str)
    else:  # 无表头：默认通达信列顺序 日期 开 高 低 收 量 额
        df = pd.read_csv(io.StringIO("\n".join(body)), sep=sep, header=None, dtype=str)
        df = df.iloc[:, :7]
        df.columns = ["日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额"][: df.shape[1]]
    header_hint = lines[header_idx] if header_idx is not None else ""
    adjusted = adjusted or "复权" in header_hint and "不复权" not in header_hint
    return frame_to_series(df, path, market_hint, title_code, title_name, adjusted, header_hint)


def frame_to_series(df: pd.DataFrame, path: Path, market_hint=None, code=None, name=None,
                    adjusted=False, header_hint="") -> list[Series]:
    cols = match_columns(df.columns)
    if "date" not in cols and "code" in cols and "name" in cols:  # 名称表：只有 代码 + 名称
        out = []
        for c, n in zip(df[cols["code"]].astype(str), df[cols["name"]].astype(str)):
            sym = normalize_symbol(c, market_hint)
            if sym and n and n != "nan":
                out.append(Series(sym, pd.DataFrame(columns=["date"] + FIELDS), name=n.strip(), source=str(path)))
        return out
    missing = [k for k in ("date", "close") if k not in cols]
    if missing:
        raise ValueError(f"找不到列：{'、'.join(missing)}（现有列：{', '.join(map(str, df.columns[:12]))}）")
    tushare = "ts_code" in [_norm(c) for c in df.columns] and _norm(cols.get("volume", "")) == "vol"
    out = pd.DataFrame({"date": parse_dates(df[cols["date"]])})
    if "time" in cols:  # 日期 + 时间 两列（分钟线）
        hhmm = df[cols["time"]].astype(str).str.replace(r"\D", "", regex=True).str[:4].str.zfill(4)
        out["date"] = out["date"] + pd.to_timedelta(hhmm.str[:2].astype(int) * 60 + hhmm.str[2:].astype(int), unit="min")
    for key in FIELDS + ["factor"]:
        if key in cols:
            out[key] = pd.to_numeric(df[cols[key]].astype(str).str.replace(",", "", regex=False), errors="coerce")
    adjusted = adjusted or bool(re.search(r"(前|后)复权|qfq|hfq", path.stem, re.I))
    groups: list[tuple[str | None, str | None, pd.DataFrame]] = []
    if "code" in cols:
        codes = df[cols["code"]].astype(str)
        names = df[cols["name"]].astype(str) if "name" in cols else None
        for raw_code, idx in codes.groupby(codes).groups.items():
            nm = names.loc[idx].iloc[-1] if names is not None else None
            groups.append((raw_code, nm, out.loc[idx].copy()))
    else:
        nm = name or (df[cols["name"]].astype(str).iloc[-1] if "name" in cols and len(df) else None)
        groups.append((code or path.stem, nm, out))
    result = []
    for raw_code, nm, part in groups:
        symbol = normalize_symbol(raw_code, market_hint)
        if not symbol:
            continue
        part = part.dropna(subset=["date"])
        notes = []
        if part["date"].dt.normalize().duplicated().any():
            part = to_daily(part)
            notes.append("检测到分钟数据，已聚合为日线")
        else:
            part["date"] = part["date"].dt.normalize()
        notes += _fix_units(part, tushare, header_hint)
        part, more = clean(part)
        if "factor" in part:
            part["factor"] = part["factor"].ffill().bfill().fillna(1.0)
        result.append(Series(symbol, part, name=nm if nm and nm != "nan" else None,
                             adjusted=adjusted, source=str(path), notes=notes + more))
    return result


def read_any(path: Path) -> list[Series]:
    ext = path.suffix.lower()
    hint = None
    for part in reversed(path.parts[:-1]):
        if part.lower() in ("sh", "sz", "bj"):
            hint = part.lower()
            break
    if ext == ".day":
        s = read_tdx_day(path)
        return [s] if s else []
    if ext in TEXT_EXT:
        return read_text(path, hint)
    if ext in EXCEL_EXT:
        return frame_to_series(pd.read_excel(path, dtype=str), path, hint)
    if ext == ".parquet":
        df = pd.read_parquet(path)
        return frame_to_series(df.astype({c: str for c in df.columns}), path, hint)
    return []


def scan(folder: Path) -> list[Path]:
    folder = Path(folder)
    if folder.is_file():
        return [folder]
    files = [p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXT]
    # 通达信目录里只要日线（lday），跳过分钟线等目录下同名扩展的文件
    return sorted(files)
