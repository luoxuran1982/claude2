"""通用 RSS / Atom 数据源，以及基于 RSS 的搜索源（Google 新闻、必应新闻、Yahoo Finance）。"""

from __future__ import annotations

import re
import urllib.parse
import xml.etree.ElementTree as ET

from .. import netutil
from .base import Source, entity_query, parse_time, strip_html


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _child_text(el, *names):
    for c in el:
        if _local(c.tag) in names:
            if _local(c.tag) == "link" and c.get("href"):
                return c.get("href")
            if (c.text or "").strip():
                return c.text.strip()
    return ""


def parse_feed(text: str) -> list[dict]:
    """解析 RSS 2.0 / RSS 1.0(RDF) / Atom。"""
    text = text.lstrip("﻿ \r\n\t")
    # 去掉 XML 声明里的 encoding，避免 ET 对已解码字符串报错；去掉非法控制字符
    text = re.sub(r"^<\?xml[^>]*\?>", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    root = ET.fromstring(text)
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        title = strip_html(_child_text(el, "title"))
        if not title:
            continue
        link = _child_text(el, "link", "guid")
        summary = strip_html(_child_text(el, "description", "summary", "content", "encoded"))
        pub = _child_text(el, "pubdate", "published", "updated", "date", "issued")
        media = ""
        for c in el:
            if _local(c.tag) in ("source", "author", "creator"):
                media = (c.text or "").strip() or _child_text(c, "name")
                if media:
                    break
        out.append({"title": title, "summary": summary[:1000], "url": link, "media": media, "published_at": parse_time(pub)})
    return out


class RssSource(Source):
    type = "rss"
    label = "RSS / Atom 订阅"
    desc = "任意 RSS/Atom 地址。配合 RSSHub 可订阅微博、雪球、公众号等。"
    params_spec = [{"key": "url", "label": "订阅地址", "placeholder": "https://example.com/feed.xml"}]

    def fetch(self, params, entities):
        url = (params.get("url") or "").strip()
        if not url:
            raise ValueError("没有填写订阅地址")
        return parse_feed(netutil.fetch(url))


class GoogleNewsSource(Source):
    type = "google_news"
    label = "Google 新闻搜索"
    desc = "按每个监控对象的名称搜索 Google 新闻 RSS。国内网络需在设置里填代理。"
    per_entity = True
    params_spec = [
        {"key": "hl", "label": "语言 hl", "default": "zh-CN"},
        {"key": "gl", "label": "地区 gl", "default": "CN"},
        {"key": "when", "label": "时间范围", "default": "7d", "placeholder": "1d / 7d / 30d"},
    ]

    def fetch(self, params, entities):
        hl = params.get("hl") or "zh-CN"
        gl = params.get("gl") or "CN"
        when = params.get("when") or "7d"
        ceid = f"{gl}:{'zh-Hans' if hl.lower() == 'zh-cn' else hl.split('-')[0]}"
        out = []
        for e in entities:
            q = f'"{entity_query(e)}" when:{when}'
            url = f"https://news.google.com/rss/search?q={netutil.q(q)}&hl={hl}&gl={gl}&ceid={netutil.q(ceid)}"
            for it in parse_feed(netutil.fetch(url)):
                it["title"] = re.sub(r"\s+-\s+[^-]{1,40}$", "", it["title"])
                it["summary"] = ""  # Google 的 description 只是标题 + 媒体名
                it["entity_ids"] = [e["id"]]
                out.append(it)
        return out


class BingNewsSource(Source):
    type = "bing_news"
    label = "必应新闻搜索"
    desc = "按每个监控对象的名称搜索必应新闻（RSS 格式），国内可直接访问。"
    per_entity = True
    params_spec = [
        {"key": "mkt", "label": "市场 mkt", "default": "zh-CN"},
        {"key": "host", "label": "域名", "default": "www.bing.com", "placeholder": "www.bing.com 或 cn.bing.com"},
    ]

    def fetch(self, params, entities):
        host = params.get("host") or "www.bing.com"
        mkt = params.get("mkt") or "zh-CN"
        out = []
        for e in entities:
            url = f"https://{host}/news/search?q={netutil.q(entity_query(e))}&format=rss&mkt={mkt}&qft=sortbydate%3d%221%22"
            for it in parse_feed(netutil.fetch(url)):
                it["url"] = _bing_real_url(it["url"])
                it["entity_ids"] = [e["id"]]
                out.append(it)
        return out


def _bing_real_url(u: str) -> str:
    try:
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(u).query)
        if "url" in qs:
            return qs["url"][0]
    except ValueError:
        pass
    return u


def yahoo_symbol(e: dict) -> str:
    code = (e.get("code") or "").strip().upper()
    market = (e.get("market") or "").upper()
    if not code:
        return ""
    if market == "US" or (not market and code.isalpha()):
        return code
    if market == "HK":
        return code.lstrip("0").zfill(4) + ".HK"
    if market == "A" and code.isdigit():
        return code + (".SS" if code.startswith(("5", "6", "9")) else ".SZ" if code.startswith(("0", "1", "2", "3")) else ".BJ")
    return ""


class YahooFinanceSource(Source):
    type = "yahoo_finance"
    label = "Yahoo Finance 个股新闻"
    desc = "按股票代码订阅 Yahoo Finance 新闻（英文）。美股最全，A 股/港股自动转换代码。"
    per_entity = True
    params_spec = [{"key": "markets", "label": "适用市场", "default": "US,HK", "placeholder": "US,HK,A"}]

    def fetch(self, params, entities):
        markets = {m.strip().upper() for m in (params.get("markets") or "US,HK").split(",") if m.strip()}
        out = []
        for e in entities:
            sym = yahoo_symbol(e)
            if not sym or (e.get("market") or "US").upper() not in markets:
                continue
            url = f"https://feeds.finance.yahoo.com/rss/2.0/headline?s={netutil.q(sym)}&region=US&lang=en-US"
            for it in parse_feed(netutil.fetch(url)):
                it["entity_ids"] = [e["id"]]
                it["media"] = it.get("media") or "Yahoo Finance"
                out.append(it)
        return out
