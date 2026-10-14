"""把读到的日线统一转成 Qlib 格式（调用上游 scripts/dump_bin.py 的 DumpDataAll）。

Qlib 的价格口径（和上游自带的 A 股数据一致）：
    $open/$high/$low/$close/$vwap = 不复权价 × 复权因子（复权价）
    $volume = 不复权成交量（股）÷ 复权因子        $factor = 复权因子
    回测按「复权数量 × 因子 = 真实股数」凑整到 100 股一手（Exchange.round_amount_by_trade_unit）。
复权因子从哪来（按优先级）：
    1. 文件自带复权因子列（如 Tushare adj_factor、富途后复权价 ÷ 不复权价）→ 直接用；
    2. 文件本身是复权价（通达信导出「前复权」等）→ 因子记为 1，一手 100 股按复权价近似；
    3. 不复权数据 → 用 QuantLab 同一套规则（adjust.estimate_factor）：开盘相对前收的跳空超出涨跌停幅度视为除权。
另外写入两个字段给回测用：
    $amount = 成交额（元）      $change = 复权收盘涨跌幅（上游回测必需字段）
    $limit_rate = 当天涨跌停幅度（主板 10%、ST 5%、创业板注册制后/科创板 20%、北交所 30%）
"""
from __future__ import annotations

import logging
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from . import adjust, datasets, quality
from .market import classify, is_st

log = logging.getLogger("qlibstudio")
FIELDS = ["open", "high", "low", "close", "volume", "amount", "vwap", "factor", "change", "limit_rate"]
BOARD_POOL = {"sh_main": "main", "sz_main": "main", "chinext": "chinext", "star": "star", "bse": "bse"}
INDEX_PREF = ["sh000300", "sh000905", "sh000852", "sz399300", "sh000001", "sz399001"]


def _dump_bin_module():
    """上游 scripts/dump_bin.py。它用 ProcessPoolExecutor 并行写文件；在打包后的程序里多进程要重新启动整个应用，
    所以在本进程里把它换成线程池（运行时替换，不改上游文件，见 UPSTREAM.md）。"""
    from .. import paths
    paths.ensure_upstream_importable()
    import dump_bin  # noqa: PLC0415  upstream/scripts/dump_bin.py
    dump_bin.ProcessPoolExecutor = ThreadPoolExecutor
    return dump_bin


def prepare(symbol: str, df: pd.DataFrame, adjusted: bool, name: str | None = None) -> tuple[pd.DataFrame, dict]:
    """一只证券的不复权日线 → Qlib 字段。返回 (表, 这只的处理说明)。"""
    df = df.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    info = {"symbol": symbol, "rows": len(df)}
    if "factor" in df and df["factor"].notna().any() and (df["factor"] > 0).all():
        factor = df["factor"].astype("float64").to_numpy()
        info["adjust"] = "文件自带复权因子"
    elif adjusted:
        factor = np.ones(len(df))
        info["adjust"] = "已复权价格（因子记为 1）"
    else:
        kind, _ = classify(symbol)
        if kind == "stock":
            factor, events = adjust.estimate_factor(symbol, df)
            info["adjust"] = f"不复权，估算除权 {len(events)} 次" if events else "不复权，未发现除权"
        else:
            factor = np.ones(len(df))
            info["adjust"] = "指数/基金，不复权"
    out = pd.DataFrame({"date": df["date"].dt.strftime("%Y-%m-%d"), "symbol": symbol})
    for c in ("open", "high", "low", "close"):
        out[c] = df[c].to_numpy() * factor
    vol = df["volume"].astype("float64").to_numpy()
    amt = df["amount"].astype("float64").to_numpy()
    out["volume"] = vol / factor
    out["amount"] = amt
    with np.errstate(divide="ignore", invalid="ignore"):
        vwap = np.where(vol > 0, amt / vol * factor, out["close"].to_numpy())
    # 成交额单位不对（比如是千元）时 vwap 会离谱：落在当天最高最低价之外就用收盘价代替
    bad = (vwap > out["high"].to_numpy() * 1.01) | (vwap < out["low"].to_numpy() * 0.99) | ~np.isfinite(vwap)
    vwap[bad] = out["close"].to_numpy()[bad]
    out["vwap"] = vwap
    out["factor"] = factor
    rate = adjust.limit_rate_series(symbol, df["date"])
    if is_st(name) and classify(symbol)[1] in ("sh_main", "sz_main"):
        rate = np.full(len(df), 0.05)
    out["limit_rate"] = rate
    # 停牌：成交量为 0 的行去掉（Qlib 里缺失的一天 = 停牌，回测时不能买卖）
    out = out[vol > 0].reset_index(drop=True)
    # $change：相对上一个有成交日的复权收盘涨跌幅（Qlib 回测按它判断涨跌停的默认口径）
    out["change"] = out["close"] / out["close"].shift(1) - 1
    info["dropped_zero_volume"] = int((vol <= 0).sum())
    return out, info


def build(series: list, ds_dir: Path, meta: dict, progress=lambda p, m: None) -> dict:
    """series：readers.Series 列表（同一代码多份会合并，后读到的覆盖先读到的日期）。"""
    ds_dir = Path(ds_dir)
    merged: dict[str, dict] = {}
    for s in series:
        if s.df is None or s.df.empty:
            continue
        cur = merged.get(s.symbol)
        if cur is None:
            merged[s.symbol] = {"df": s.df.copy(), "adjusted": s.adjusted, "name": s.name, "notes": list(s.notes)}
        else:
            cur["df"] = pd.concat([cur["df"], s.df]).drop_duplicates("date", keep="last")
            cur["name"] = s.name or cur["name"]
            cur["notes"] += s.notes
    if not merged:
        raise ValueError("没有读到任何有效的日线数据")
    staging = ds_dir / "_csv"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    names, infos = {}, []
    for k, (sym, item) in enumerate(sorted(merged.items())):
        frame, info = prepare(sym, item["df"], item["adjusted"], item["name"])
        if frame.empty:
            continue
        info["notes"] = item["notes"]
        infos.append(info)
        if item["name"]:
            names[sym.upper()] = item["name"]
        frame.to_csv(staging / f"{sym}.csv", index=False, float_format="%.6g")
        if k % 50 == 0:
            progress(0.05 + 0.45 * k / len(merged), f"整理 {k}/{len(merged)}：{sym}")
    progress(0.5, f"写入 Qlib 二进制格式（{len(infos)} 只）")
    dump_bin = _dump_bin_module()
    dumper = dump_bin.DumpDataAll(data_path=str(staging), qlib_dir=str(ds_dir), freq="day", max_workers=4,
                                  date_field_name="date", symbol_field_name="symbol", include_fields=",".join(FIELDS))
    dumper.dump()
    shutil.rmtree(staging, ignore_errors=True)
    progress(0.8, "生成股票池")
    write_pools(ds_dir)
    datasets.write_json(ds_dir / "names.json", names)
    progress(0.85, "数据质量检查")
    q = quality.check(ds_dir)
    q["import"] = infos[:3000]
    datasets.write_json(ds_dir / "quality.json", q)
    return finish_meta(ds_dir, meta, q)


def write_pools(ds_dir: Path) -> None:
    """按 all.txt 生成 stocks.txt 和各板块股票池（Qlib 的 instruments 文件格式：代码\\t开始\\t结束）。"""
    inst = Path(ds_dir) / "instruments"
    lines = [ln for ln in (inst / "all.txt").read_text(encoding="utf-8").splitlines() if ln.strip()]
    groups: dict[str, list[str]] = {"stocks": []}
    for ln in lines:
        sym = ln.split("\t")[0].lower()
        kind, board = classify(sym)
        if kind != "stock":
            continue
        groups["stocks"].append(ln)
        if board in BOARD_POOL:
            groups.setdefault(BOARD_POOL[board], []).append(ln)
    for name, rows in groups.items():
        if rows and not (inst / f"{name}.txt").exists():
            (inst / f"{name}.txt").write_text("\n".join(rows) + "\n", encoding="utf-8")


def finish_meta(ds_dir: Path, meta: dict, q: dict) -> dict:
    ds_dir = Path(ds_dir)
    cal = datasets.calendar(ds_dir)
    feats = ds_dir / "features"
    syms = sorted(p.name for p in feats.iterdir() if p.is_dir()) if feats.is_dir() else []
    indexes = [s for s in syms if classify(s)[0] == "index"]
    bench = [s for s in INDEX_PREF if s in indexes] + [s for s in indexes if s not in INDEX_PREF]
    m = {**meta, "id": ds_dir.name, "start": cal[0] if cal else None, "end": cal[-1] if cal else None,
         "calendar_days": len(cal), "n_symbols": len(syms), "n_stocks": sum(classify(s)[0] == "stock" for s in syms),
         "benchmarks": [s.upper() for s in bench[:8]], "pools": datasets.pools(ds_dir),
         "fields": sorted({f.name.split(".")[0] for f in (feats / syms[0]).glob("*.day.bin")}) if syms else [],
         "quality_level": q.get("summary", {}).get("level")}
    datasets.write_json(ds_dir / "meta.json", m)
    return m
