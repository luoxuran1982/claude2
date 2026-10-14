"""富途 / moomoo OpenD 下载 A 股日 K（futu-api，连本机 127.0.0.1:11111）。

口径：
- 每只股票请求两次：不复权（AuType.NONE）和后复权（AuType.HFQ），复权因子 = 后复权收盘 ÷ 不复权收盘。
  后复权以上市首日为基准，历史值不会因为后来的分红送转而改变，所以可以增量补数据。
- 历史 K 线额度（OpenD 规则）：最近 30 天内请求过的「不同股票」数量有上限（随账户资产、交易情况变化，
  常见 100/300/1000/2000 只）；30 天内重复请求同一只不额外占用。界面显示 OpenD 返回的已用/剩余额度。
- 本地缓存：<数据目录>/futu-cache/<代码>.csv（不复权价 + 因子），index.json 记录每只已覆盖的日期区间。
  再次下载只请求缓存没覆盖的日期，覆盖了就完全不请求（不占时间，也不会触发新的额度占用）。
- A 股行情权限、能否请求 A 股历史 K 线取决于账户和 OpenD 的行情权限，以 OpenD 返回为准。
"""
from __future__ import annotations

import json
import socket
import time
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from . import datasets
from .market import normalize_symbol

HOST, PORT = "127.0.0.1", 11111
COLS = ["date", "open", "high", "low", "close", "volume", "amount", "factor"]


class FutuError(RuntimeError):
    """给用户看的中文原因。"""


def to_futu(code: str) -> str:
    """'600000' / 'sh600000' / '600000.SH' → 'SH.600000'。"""
    sym = normalize_symbol(code)
    if not sym:
        raise FutuError(f"无法识别的代码：{code}")
    if sym.startswith("bj"):
        raise FutuError(f"{code}：北交所股票富途 OpenD 不提供 A 股行情，请换用通达信或 CSV 导入")
    return sym[:2].upper() + "." + sym[2:]


def from_futu(code: str) -> str:
    return code.replace(".", "").lower()


def parse_codes(text: str) -> list[str]:
    raw = [c for c in text.replace("，", ",").replace("、", ",").replace("\n", ",").replace(" ", ",").split(",") if c.strip()]
    out, seen = [], set()
    for c in raw:
        f = to_futu(c.strip())
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def probe(host: str = HOST, port: int = PORT, timeout: float = 3.0) -> None:
    """先用普通 TCP 试一下端口：futu-api 连不上时会在后台无限重连，直接用它没法给出清楚的原因。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return
    except ConnectionRefusedError:
        raise FutuError(f"连不上富途 OpenD（{host}:{port}）：端口没有程序在监听。请先打开 OpenD（或 moomoo OpenD）"
                        "并用账号登录；如果在 OpenD 里改过「API 端口」，把这里的端口改成一样的") from None
    except socket.timeout:
        raise FutuError(f"连接富途 OpenD（{host}:{port}）超时：请确认 OpenD 已启动且没有被防火墙拦截") from None
    except OSError as exc:
        raise FutuError(f"连不上富途 OpenD（{host}:{port}）：{exc}") from None


def explain(err) -> str:
    """把 OpenD 返回的错误翻成可操作的中文原因。"""
    s = str(err)
    low = s.lower()
    if "quota" in low or "额度" in s:
        return f"历史 K 线额度不足（OpenD：{s}）。额度按最近 30 天请求过的不同股票数计算，可等额度释放或减少股票数"
    if "right" in low or "权限" in s or "permission" in low:
        return f"没有这个市场的行情权限（OpenD：{s}）。请在富途/moomoo App 里确认 A 股行情权限"
    if "freq" in low or "频率" in s or "too many" in low:
        return f"请求太频繁（OpenD：{s}），稍后再试"
    if "unknown" in low or "不存在" in s or "invalid" in low or "未知" in s:
        return f"代码无效或不支持（OpenD：{s}）"
    if "login" in low or "登录" in s:
        return f"OpenD 没有登录（OpenD：{s}）。请在 OpenD 窗口里登录账号"
    return f"OpenD 返回错误：{s}"


def real_context(host: str = HOST, port: int = PORT):
    probe(host, port)
    try:
        import futu  # noqa: PLC0415
    except ImportError:
        raise FutuError("没有安装 futu-api（从源码运行时：pip install futu-api）") from None
    try:
        futu.SysConfig.set_all_thread_daemon(True)  # 退出时不被 futu-api 的后台线程拖住
    except Exception:  # noqa: BLE001
        pass
    ctx = futu.OpenQuoteContext(host=host, port=port)
    return ctx, futu


class Cache:
    def __init__(self, root: Path):
        self.dir = Path(root) / "futu-cache"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.index_file = self.dir / "index.json"
        self.index = datasets.read_json(self.index_file, {}) or {}

    def frame(self, code: str) -> pd.DataFrame | None:
        f = self.dir / f"{code}.csv"
        if not f.is_file():
            return None
        return pd.read_csv(f, parse_dates=["date"])

    def covered(self, code: str) -> tuple[str, str] | None:
        r = (self.index.get(code) or {}).get("covered")
        return tuple(r) if r else None

    def save(self, code: str, df: pd.DataFrame, covered: tuple[str, str], name: str | None) -> None:
        old = self.frame(code)
        if old is not None and len(old):
            df = pd.concat([old, df]).drop_duplicates("date", keep="last")
        df = df.sort_values("date")[COLS]
        df.to_csv(self.dir / f"{code}.csv", index=False, date_format="%Y-%m-%d", float_format="%.6g")
        prev = self.covered(code)
        if prev:
            covered = (min(prev[0], covered[0]), max(prev[1], covered[1]))
        self.index[code] = {"covered": list(covered), "name": name or (self.index.get(code) or {}).get("name"),
                            "rows": int(len(df)), "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
        datasets.write_json(self.index_file, self.index)

    def summary(self) -> list[dict]:
        return [{"code": k, **v} for k, v in sorted(self.index.items())]


def missing_ranges(covered: tuple[str, str] | None, start: str, end: str) -> list[tuple[str, str]]:
    if not covered:
        return [(start, end)]
    c0, c1 = covered
    out = []
    if start < c0:
        out.append((start, (date.fromisoformat(c0) - timedelta(days=1)).isoformat()))
    if end > c1:
        out.append(((date.fromisoformat(c1) + timedelta(days=1)).isoformat(), end))
    return [(a, b) for a, b in out if a <= b]


def _kline(ctx, futu, code: str, start: str, end: str, autype) -> pd.DataFrame:
    frames, key = [], None
    while True:
        ret, data, key = ctx.request_history_kline(code, start=start, end=end, ktype=futu.KLType.K_DAY, autype=autype,
                                                   max_count=1000, page_req_key=key)
        if ret != futu.RET_OK:
            raise FutuError(f"{code}：" + explain(data))
        if data is not None and len(data):
            frames.append(data)
        if not key:
            break
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def quota(ctx, futu) -> dict:
    ret, data = ctx.get_history_kl_quota(get_detail=True)
    if ret != futu.RET_OK:
        raise FutuError("查询历史 K 线额度失败：" + explain(data))
    used, remain, detail = data
    return {"used": int(used), "remain": int(remain), "total": int(used) + int(remain),
            "detail": [{"code": d.get("code"), "name": d.get("name"), "time": d.get("request_time")} for d in (detail or [])][:500]}


def check(root: Path, host: str = HOST, port: int = PORT, factory=None) -> dict:
    """连接测试 + 额度。"""
    ctx, futu = (factory or real_context)(host, port)
    try:
        q = quota(ctx, futu)
    finally:
        ctx.close()
    q["cache"] = Cache(root).summary()
    return q


def download(root: Path, codes: list[str], start: str, end: str, host: str = HOST, port: int = PORT,
             factory=None, progress=lambda p, m: None) -> dict:
    """把 codes 的日 K 补进缓存。返回 {downloaded, skipped, failed, quota_before, quota_after}。"""
    cache = Cache(root)
    todo = {c: missing_ranges(cache.covered(c), start, end) for c in codes}
    need = [c for c, r in todo.items() if r]
    result = {"requested": codes, "downloaded": [], "skipped": [c for c in codes if not todo[c]], "failed": [],
              "start": start, "end": end}
    if not need:
        progress(1.0, f"{len(codes)} 只全部已在本地缓存里，没有请求 OpenD")
        return result
    ctx, futu = (factory or real_context)(host, port)
    try:
        q0 = quota(ctx, futu)
        result["quota_before"] = {k: q0[k] for k in ("used", "remain", "total")}
        already = {d["code"] for d in q0["detail"]}
        new_codes = [c for c in need if c not in already]
        if len(new_codes) > q0["remain"]:
            raise FutuError(f"这次要新请求 {len(new_codes)} 只股票，但历史 K 线额度只剩 {q0['remain']} 只"
                            f"（已用 {q0['used']} / 共 {q0['total']}，按最近 30 天计）。请减少股票数，或等额度释放")
        for k, code in enumerate(need):
            progress(k / len(need), f"下载 {code}（{k + 1}/{len(need)}）")
            try:
                parts, name = [], None
                for a, b in todo[code]:
                    raw = _kline(ctx, futu, code, a, b, futu.AuType.NONE)
                    if raw.empty:
                        continue
                    hfq = _kline(ctx, futu, code, a, b, futu.AuType.HFQ)
                    name = str(raw["name"].iloc[-1]) if "name" in raw else name
                    df = pd.DataFrame({"date": pd.to_datetime(raw["time_key"]).dt.normalize(),
                                       "open": raw["open"], "high": raw["high"], "low": raw["low"], "close": raw["close"],
                                       "volume": raw["volume"], "amount": raw["turnover"]})
                    if not hfq.empty:
                        h = pd.Series(hfq["close"].to_numpy(), index=pd.to_datetime(hfq["time_key"]).dt.normalize())
                        df["factor"] = (h.reindex(df["date"]).to_numpy() / df["close"].to_numpy())
                    else:
                        df["factor"] = 1.0
                    df["factor"] = df["factor"].ffill().bfill().fillna(1.0)
                    parts.append(df)
                frame = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=COLS)
                cov = cache.covered(code)
                cache.save(code, frame, (min(start, cov[0]) if cov else start, max(end, cov[1]) if cov else end), name)
                result["downloaded"].append({"code": code, "rows": int(len(frame)), "name": name})
            except FutuError as exc:
                result["failed"].append({"code": code, "reason": str(exc)})
                if "额度" in str(exc):
                    break
            time.sleep(0.05)
        try:
            q1 = quota(ctx, futu)
            result["quota_after"] = {k: q1[k] for k in ("used", "remain", "total")}
        except FutuError:
            pass
    finally:
        ctx.close()
    progress(1.0, f"下载 {len(result['downloaded'])} 只，缓存命中 {len(result['skipped'])} 只，失败 {len(result['failed'])} 只")
    return result


def series_from_cache(root: Path, codes: list[str], start: str | None = None, end: str | None = None) -> list:
    from .sources import from_frame
    cache = Cache(root)
    out = []
    for c in codes:
        df = cache.frame(c)
        if df is None or df.empty:
            continue
        if start:
            df = df[df["date"] >= pd.Timestamp(start)]
        if end:
            df = df[df["date"] <= pd.Timestamp(end)]
        if len(df):
            out.append(from_frame(from_futu(c), df, (cache.index.get(c) or {}).get("name")))
    return out


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)
