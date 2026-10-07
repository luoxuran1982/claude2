"""HTTP 抓取：只用 urllib。支持 gzip、自动识别编码、可选代理。"""

from __future__ import annotations

import gzip
import json
import re
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request
import zlib

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

# 由设置页修改：{"proxy": "http://127.0.0.1:7890", "timeout": 15}
options = {"proxy": "", "timeout": 15}


class FetchError(Exception):
    pass


def _opener():
    handlers = [urllib.request.HTTPSHandler(context=ssl.create_default_context())]
    proxy = (options.get("proxy") or "").strip()
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    # 未设置时 urllib 会自动使用系统代理（Windows 注册表 / 环境变量）
    return urllib.request.build_opener(*handlers)


def _decode(raw: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w-]+)", content_type or "", re.I)
    cands = [m.group(1)] if m else []
    head = raw[:300].decode("ascii", "ignore")
    m2 = re.search(r'encoding=["\']([\w-]+)', head)
    if m2:
        cands.append(m2.group(1))
    cands += ["utf-8", "gb18030"]
    for enc in cands:
        enc = "gb18030" if enc.lower() in ("gbk", "gb2312") else enc
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def fetch(url: str, headers: dict | None = None, timeout: float | None = None) -> str:
    h = {"User-Agent": DEFAULT_UA, "Accept": "*/*", "Accept-Encoding": "gzip, deflate", "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}
    h.update(headers or {})
    req = urllib.request.Request(url, headers=h)
    try:
        with _opener().open(req, timeout=timeout or options.get("timeout") or 15) as resp:
            raw = resp.read(8 * 1024 * 1024)
            enc = (resp.headers.get("Content-Encoding") or "").lower()
            ctype = resp.headers.get("Content-Type") or ""
    except urllib.error.HTTPError as e:
        raise FetchError(f"HTTP {e.code}（{e.reason}）") from e
    except urllib.error.URLError as e:
        reason = e.reason
        if isinstance(reason, (socket.timeout, TimeoutError)):
            raise FetchError("请求超时") from e
        raise FetchError(f"网络连接失败：{reason}") from e
    except (socket.timeout, TimeoutError) as e:
        raise FetchError("请求超时") from e
    except Exception as e:  # noqa: BLE001 - 统一转成 FetchError，交给数据源状态显示
        raise FetchError(f"{type(e).__name__}: {e}") from e
    if enc == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    elif enc == "deflate":
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return _decode(raw, ctype)


def fetch_json(url: str, headers: dict | None = None):
    text = fetch(url, headers).strip()
    # 兼容 JSONP：cb({...}) / var x = {...};
    if text and text[0] not in "[{":
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        if not m:
            raise FetchError("返回内容不是 JSON")
        text = m.group(0)
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise FetchError(f"JSON 解析失败：{e}") from e


def q(s: str) -> str:
    return urllib.parse.quote(s, safe="")
