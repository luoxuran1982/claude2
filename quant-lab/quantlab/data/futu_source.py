"""富途 / moomoo OpenD 数据源：通过本机 OpenD（默认 127.0.0.1:11111）下载 A 股日 K。

OpenD 是富途/moomoo 提供的本地网关程序，需要用户自己打开并登录牛牛号/moomoo 账号；本应用只连接它，不负责启动或关闭它。

历史 K 线额度：富途按账户限制「最近 30 天内下载过历史 K 线的不同股票数」（额度多少取决于账户资产和交易情况），
30 天内重复下载同一只股票不再占额度。所以这里：
- 下载前查询额度（已用 / 剩余 / 最近 30 天下载过的代码），界面上显示；
- 本地已是最新的不再请求；最近 30 天下载过的（不占额度）优先；剩余额度不够时只下能下的，其余列出来。

测试里把 module 换成假的 futu 模块，不需要真的 OpenD。
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import pandas as pd

from .online import RateLimiter, SourceError, Task, factor_from, standardize, with_retry

log = logging.getLogger("quantlab")
LABEL = "富途 OpenD"
DEFAULT_HOST, DEFAULT_PORT = "127.0.0.1", 11111


def to_futu(symbol: str) -> str:
    """sh600000 → SH.600000（富途代码格式）。北交所富途暂不提供 A 股行情。"""
    return symbol[:2].upper() + "." + symbol[2:]


def from_futu(code: str) -> str | None:
    mk, _, c = str(code).partition(".")
    mk = mk.upper()
    return (mk.lower() + c) if mk in ("SH", "SZ") and len(c) == 6 else None


def explain_text(msg) -> SourceError:
    """OpenD 返回的错误文字 → 中文原因。"""
    text = str(msg)
    low = text.lower()
    if any(k in low for k in ("quota", "额度")):
        return SourceError(f"历史 K 线额度不足：{text}。额度按最近 30 天下载过的不同股票数计算，"
                           "30 天内重复下载同一只不占额度；请减少股票数或等额度恢复", fatal=True)
    if any(k in low for k in ("permission", "no right", "权限", "authority")):
        return SourceError(f"没有该市场的行情权限：{text}。请在富途牛牛/moomoo 里开通 A 股行情（沪深 LV1）后重试",
                           fatal=True)
    if any(k in low for k in ("login", "登录", "not logged", "未登")):
        return SourceError(f"OpenD 没有登录：{text}。请在 OpenD 界面用牛牛号/moomoo 账号登录", fatal=True)
    if any(k in low for k in ("frequen", "频率", "too many", "limit")):
        return SourceError(f"请求太频繁：{text}。已自动放慢重试；可以调大「请求间隔」", retry=True)
    if any(k in low for k in ("disconnect", "timeout", "超时", "断开", "connect")):
        return SourceError(f"与 OpenD 的连接中断或超时：{text}。请确认 OpenD 还在运行", retry=True)
    if any(k in low for k in ("unknown stock", "未知股票", "不存在", "invalid")):
        return SourceError(f"富途不认识这个代码：{text}")
    return SourceError(f"OpenD 返回错误：{text}")


def explain(exc: Exception) -> SourceError:
    if isinstance(exc, SourceError):
        return exc
    if isinstance(exc, ImportError):
        return SourceError("没有安装 futu-api：源码运行时请先执行 pip install futu-api（安装包版已自带）", fatal=True)
    return explain_text(f"{type(exc).__name__}: {exc}")


class FutuFetcher:
    label = LABEL

    def __init__(self, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, module=None, interval: float = 0.6,
                 tries: int = 3, sleep=time.sleep, connect_timeout: float = 3.0, open_timeout: float = 15.0):
        self.host = (host or DEFAULT_HOST).strip()
        try:
            self.port = int(port or DEFAULT_PORT)
        except (TypeError, ValueError):
            raise ValueError("OpenD 端口必须是数字，例如 11111") from None
        if not 0 < self.port < 65536:
            raise ValueError("OpenD 端口须在 1~65535")
        self._module = module
        self.limiter = RateLimiter(interval, sleep=sleep)   # 富途限制：每 30 秒最多 60 次历史 K 线请求
        self.tries, self.sleep = tries, sleep
        self.connect_timeout, self.open_timeout = connect_timeout, open_timeout
        self.ctx = None

    @property
    def ft(self):
        if self._module is None:
            try:
                import futu  # noqa: PLC0415
            except Exception as exc:  # noqa: BLE001
                raise explain(ImportError(str(exc))) from None
            self._module = futu
        return self._module

    # ------------------------------------------------------------ 连接
    def check_port(self) -> None:
        try:
            with socket.create_connection((self.host, self.port), timeout=self.connect_timeout):
                pass
        except ConnectionRefusedError:
            raise SourceError(f"连不上 OpenD（{self.host}:{self.port}）：OpenD 没有运行，或端口不是 {self.port}。"
                              "请先打开富途 OpenD / moomoo OpenD 并登录，再点「查询额度」", fatal=True) from None
        except socket.timeout:
            raise SourceError(f"连接 OpenD（{self.host}:{self.port}）超时：地址不对或被防火墙拦截", fatal=True) from None
        except OSError as exc:
            raise SourceError(f"连不上 OpenD（{self.host}:{self.port}）：{exc}", fatal=True) from None

    def open(self):
        if self.ctx is not None:
            return self.ctx
        self.check_port()
        ft = self.ft
        try:
            ft.SysConfig.set_all_thread_daemon(True)   # 退出应用时 futu 的后台线程不挡住退出
        except Exception:  # noqa: BLE001
            pass
        box: dict = {}

        def make():   # futu 的同步构造函数连不上时会一直重试，所以放到线程里限时等待
            try:
                box["ctx"] = ft.OpenQuoteContext(host=self.host, port=self.port)
            except Exception as exc:  # noqa: BLE001
                box["err"] = exc
        th = threading.Thread(target=make, name="futu-connect", daemon=True)
        th.start()
        th.join(self.open_timeout)
        if "err" in box:
            raise explain(box["err"])
        if "ctx" not in box:
            raise SourceError(f"端口 {self.port} 能连上，但 {self.open_timeout:.0f} 秒内没有完成 OpenD 握手："
                              "确认这个端口是 OpenD 的（OpenD 设置里的「API 端口」），并且 OpenD 版本不太旧", fatal=True)
        self.ctx = box["ctx"]
        try:
            ret, state = self.ctx.get_global_state()
            if ret == ft.RET_OK and isinstance(state, dict) and state.get("qot_logined") is False:
                self.close()
                raise SourceError("OpenD 已启动但行情没有登录：请在 OpenD 里登录牛牛号/moomoo 账号后重试", fatal=True)
        except SourceError:
            raise
        except Exception:  # noqa: BLE001  老版本 OpenD 没有这个字段，不影响下载
            pass
        return self.ctx

    def close(self) -> None:
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:  # noqa: BLE001
                pass
            self.ctx = None

    # ------------------------------------------------------------ 额度
    def quota(self) -> dict:
        ctx = self.open()
        ret, data = ctx.get_history_kl_quota(get_detail=True)
        if ret != self.ft.RET_OK:
            raise explain_text(data)
        used, remain, detail = data
        recent = []
        for item in detail or []:
            code = item.get("code") if isinstance(item, dict) else item[0]
            when = item.get("request_time", "") if isinstance(item, dict) else (item[-1] if len(item) > 1 else "")
            recent.append({"code": code, "symbol": from_futu(code), "request_time": str(when)})
        return {"used": int(used), "remain": int(remain), "recent": recent, "host": self.host, "port": self.port}

    def budget(self, tasks: list[Task]) -> tuple[list[Task], list[str]]:
        """额度检查：最近 30 天下载过的不占额度，先下；其余按剩余额度截断。"""
        q = self.quota()
        seen = {r["symbol"] for r in q["recent"] if r["symbol"]}
        free = [t for t in tasks if t.symbol in seen]
        new = [t for t in tasks if t.symbol not in seen]
        allowed = new[:max(q["remain"], 0)]
        return free + allowed, [t.symbol for t in new[len(allowed):]]

    # ------------------------------------------------------------ 日 K
    def daily(self, symbol: str, start: str, end: str, adjust: str) -> pd.DataFrame:
        if symbol.startswith("bj"):
            raise SourceError("富途 OpenD 不提供北交所行情，这只请用 AKShare 下载")
        ft = self.ft
        if adjust == "factor":
            raw = self._kline(symbol, start, end, ft.AuType.NONE)
            if raw.empty:
                return raw
            return standardize(factor_from(raw, self._kline(symbol, start, end, ft.AuType.HFQ)))
        au = {"qfq": ft.AuType.QFQ, "hfq": ft.AuType.HFQ, "none": ft.AuType.NONE}[adjust]
        return self._kline(symbol, start, end, au)

    def _kline(self, symbol: str, start: str, end: str, autype) -> pd.DataFrame:
        ctx, ft = self.open(), self.ft
        code = to_futu(symbol)
        parts, key = [], None
        for _ in range(200):   # 每页最多 1000 根，200 页足够 80 年
            def req(k=key):
                ret, data, nxt = ctx.request_history_kline(code, start=start, end=end, ktype=ft.KLType.K_DAY,
                                                           autype=autype, max_count=1000, page_req_key=k)
                if ret != ft.RET_OK:
                    raise explain_text(data)
                return data, nxt
            data, key = with_retry(req, explain, tries=self.tries, sleep=self.sleep, limiter=self.limiter)
            if data is not None and len(data):
                parts.append(data)
            if key is None:
                break
        if not parts:
            return standardize(None)
        df = pd.concat(parts, ignore_index=True)
        out = pd.DataFrame({"date": pd.to_datetime(df["time_key"]), "open": df["open"], "high": df["high"],
                            "low": df["low"], "close": df["close"], "volume": df["volume"],   # 股
                            "amount": df["turnover"] if "turnover" in df else 0.0})
        return standardize(out, start, end)
