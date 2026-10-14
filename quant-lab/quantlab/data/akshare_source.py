"""AKShare 在线数据源：免费下载 A 股日线（东方财富接口为主，新浪接口备用）。

akshare 本身只是一组网页接口的封装，不需要账号；数据网站改版或限流时会失败，所以这里：
- 每次请求之间限速（默认 0.5 秒），网络错误、限流按 2/4/8 秒退避重试 3 次；
- 东方财富接口连续失败时改用新浪接口再试一次；
- 所有失败都翻译成中文原因（没装 akshare、网络不通、被限流、接口改版、区间内没有数据）。

测试里把 module 换成假的 akshare，不访问网络。
"""
from __future__ import annotations

import logging
import time

import pandas as pd

from ..market import classify, normalize_symbol
from .online import RateLimiter, SourceError, factor_from, standardize, with_retry

log = logging.getLogger("quantlab")

INDEXES = {   # 指数成分：代码 → 名称（中证指数公司发布的成分表）
    "000300": "沪深300", "000905": "中证500", "000852": "中证1000", "000016": "上证50",
    "000688": "科创50", "399006": "创业板指", "000510": "中证A500",
}
BENCHMARKS = {"sh000300": "沪深300", "sh000905": "中证500", "sh000852": "中证1000", "sh000016": "上证50",
              "sz399006": "创业板指", "sh000001": "上证指数"}
LABEL = "AKShare"


def explain(exc: Exception) -> SourceError:
    """把 akshare / requests 抛出的各种异常翻译成用户看得懂的中文原因。"""
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, ImportError):
        return SourceError("没有安装 akshare：源码运行时请先执行 pip install akshare（安装包版已自带）", fatal=True)
    name = type(exc).__name__
    text = str(exc)
    low = text.lower()
    if "proxy" in low or name == "ProxyError":
        return SourceError("网络代理拒绝了连接：请检查系统代理/VPN 设置，或换个网络再试", retry=True)
    if any(k in name for k in ("ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout", "SSLError")) or \
            any(k in low for k in ("max retries", "connection", "timed out", "name or service", "getaddrinfo",
                                   "remote end closed", "temporarily unavailable")):
        return SourceError("网络连接失败：访问不到数据网站（东方财富/新浪）。请检查网络、代理或防火墙；"
                           "公司或学校网络可能拦截这些网站", retry=True)
    if any(k in text for k in ("429", "403", "Too Many")) or "频繁" in text:
        return SourceError("数据网站拒绝访问或请求太频繁（HTTP 403/429）：已自动放慢重试；请调大「请求间隔」或稍后再试",
                           retry=True)
    if name in ("KeyError", "JSONDecodeError", "IndexError", "TypeError", "AttributeError") or "Expecting value" in text:
        return SourceError(f"数据接口返回的格式和预期不同（{name}: {text[:80]}）：多半是数据网站改版，"
                           "请升级 akshare 或稍后再试")
    return SourceError(f"下载失败：{name}: {text[:160]}")


class AkshareFetcher:
    label = LABEL

    def __init__(self, module=None, interval: float = 0.5, tries: int = 3, sleep=time.sleep):
        self._module = module
        self.limiter = RateLimiter(interval, sleep=sleep)
        self.tries, self.sleep = tries, sleep

    @property
    def ak(self):
        if self._module is None:
            try:
                import akshare  # noqa: PLC0415  启动时不加载，用到才导入（约 1 秒）
            except Exception as exc:  # noqa: BLE001
                raise explain(ImportError(str(exc))) from None
            self._module = akshare
        return self._module

    def _call(self, fn):
        return with_retry(fn, explain, tries=self.tries, sleep=self.sleep, limiter=self.limiter)

    # ------------------------------------------------------------ 股票范围
    def stock_list(self) -> dict[str, str]:
        """全部 A 股：{代码: 名称}（沪深京）。"""
        df = self._call(lambda: self.ak.stock_info_a_code_name())
        if df is None or df.empty:
            raise SourceError("没有取到 A 股列表：接口返回空，请稍后再试")
        out = {}
        for code, name in zip(df["code"], df["name"]):
            sym = normalize_symbol(str(code).zfill(6))
            if sym and classify(sym)[0] == "stock":
                out[sym] = str(name).strip()
        return out

    def index_members(self, index_code: str) -> dict[str, str]:
        """指数成分股：先用中证指数公司的成分表，不行再用新浪的。"""
        code = str(index_code).strip()[-6:]
        try:
            df = self._call(lambda: self.ak.index_stock_cons_csindex(symbol=code))
            hints = [("sh" if "上海" in str(x) else "sz" if "深圳" in str(x) else "bj" if "北京" in str(x) else None)
                     for x in df.get("交易所", [None] * len(df))]
            pairs = zip(df["成分券代码"], df["成分券名称"], hints)
        except SourceError as first:
            try:
                df = self._call(lambda: self.ak.index_stock_cons(symbol=code))
            except SourceError:
                raise SourceError(f"取不到指数 {code} 的成分股：{first}") from None
            pairs = zip(df["品种代码"], df["品种名称"], [None] * len(df))
        out = {}
        for c, n, hint in pairs:
            sym = normalize_symbol(str(c).zfill(6), hint)
            if sym:
                out[sym] = str(n).strip()
        if not out:
            raise SourceError(f"指数 {code} 的成分股列表是空的")
        return out

    # ------------------------------------------------------------ 日线
    def daily(self, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
        if classify(symbol)[0] == "index":
            return self._index(symbol, start, end)
        if adjust == "factor":
            raw = self._stock(symbol, start, end, "")
            if raw.empty:
                return raw
            hfq = self._stock(symbol, start, end, "hfq")
            return standardize(factor_from(raw, hfq))
        return self._stock(symbol, start, end, {"qfq": "qfq", "hfq": "hfq", "none": ""}[adjust])

    def _stock(self, symbol: str, start: str, end: str, adj: str) -> pd.DataFrame:
        s, e = start.replace("-", ""), end.replace("-", "")
        try:
            df = self._call(lambda: self.ak.stock_zh_a_hist(symbol=symbol[2:], period="daily", start_date=s,
                                                            end_date=e, adjust=adj))
            if df is None or df.empty:
                return standardize(None)
            out = pd.DataFrame({"date": df["日期"], "open": df["开盘"], "high": df["最高"], "low": df["最低"],
                                "close": df["收盘"], "volume": pd.to_numeric(df["成交量"], errors="coerce") * 100,  # 手 → 股
                                "amount": df["成交额"]})
            return standardize(out, start, end)
        except SourceError as exc:
            if not exc.retry:
                raise
            log.info("东方财富接口失败（%s），改用新浪接口：%s", exc, symbol)
            primary = exc
        try:   # 备用：新浪（成交量单位是股）
            df = self._call(lambda: self.ak.stock_zh_a_daily(symbol=symbol, start_date=s, end_date=e, adjust=adj))
        except SourceError:
            raise primary from None
        if df is None or df.empty:
            return standardize(None)
        return standardize(df[["date", "open", "high", "low", "close", "volume", "amount"]], start, end)

    def _index(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        s, e = start.replace("-", ""), end.replace("-", "")
        try:
            df = self._call(lambda: self.ak.stock_zh_index_daily_em(symbol=symbol, start_date=s, end_date=e))
        except SourceError as exc:
            if not exc.retry:
                raise
            df = self._call(lambda: self.ak.stock_zh_index_daily(symbol=symbol))
            if "amount" not in df:
                df = df.assign(amount=0.0)
        if df is None or df.empty:
            return standardize(None)
        return standardize(df[["date", "open", "high", "low", "close", "volume", "amount"]], start, end)


def resolve_universe(fetcher: AkshareFetcher, body: dict, store_symbols: list[str] | None = None) -> tuple[list[str], dict[str, str], list[str]]:
    """界面上的「股票范围」→ (代码列表, 名称, 提示)。scope：all / index / custom / dataset。"""
    from .online import parse_codes
    scope = body.get("scope") or "custom"
    notes: list[str] = []
    if scope == "all":
        names = fetcher.stock_list()
        syms = sorted(names)
    elif scope == "index":
        code = str(body.get("index") or "000300")
        names = fetcher.index_members(code)
        syms = sorted(names)
        notes.append(f"指数 {INDEXES.get(code, code)} 当前成分 {len(syms)} 只（只有最新成分，历史上调出的股票不在内，有幸存者偏差）")
    elif scope == "dataset":
        syms = [s for s in (store_symbols or []) if classify(s)[0] in ("stock", "index")]
        names = {}
        if not syms:
            raise ValueError("所选数据集里没有股票可以更新")
    else:
        syms, bad = parse_codes(body.get("codes") or "")
        names = {}
        if bad:
            notes.append("没认出这些代码，已跳过：" + "、".join(bad[:20]))
        if not syms:
            raise ValueError("请填写股票代码，例如 600519, 000001, 300750")
    for b in body.get("benchmarks") or []:
        sym = normalize_symbol(b) if not str(b)[:2] in ("sh", "sz", "bj") else str(b)
        if sym and sym not in syms:
            syms.append(sym)
            names.setdefault(sym, BENCHMARKS.get(sym, ""))
    return syms, names, notes
