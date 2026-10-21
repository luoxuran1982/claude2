"""v0.3 新增的数据源类型：RSSHub 路由、newsnow 热榜、巨潮资讯公告、港交所披露易公告。

- RSSHub：本机运行的开源 RSS 生成器（AGPL-3.0）。本应用只通过 HTTP 调用它，不复制它的代码。
- newsnow：开源热榜聚合（ourongxing/newsnow）的公开实例，热点雷达也在用。只调用接口。
- 巨潮资讯、港交所披露易：网站页面自己在用的公开 JSON 接口，非官方 API，网站改版后可能失效。
"""

from __future__ import annotations

import json
import time
import urllib.parse

from .. import netutil, rsshub
from .base import Source, parse_time, strip_html


class RsshubSource(Source):
    type = "rsshub"
    label = "RSSHub 路由"
    desc = "调用本机 RSSHub（设置 → RSSHub 地址）。路由形如 /cls/telegraph，可在「订阅目录 → RSSHub 全目录」里查找。"
    params_spec = [{"key": "path", "label": "路由", "placeholder": "/cls/telegraph"}]

    def fetch(self, params, entities):
        from .rss import parse_feed

        path = (params.get("path") or "").strip()
        if not path:
            raise ValueError("没有填写 RSSHub 路由")
        try:
            text = netutil.fetch(rsshub.url_for(path), timeout=rsshub.FETCH_TIMEOUT)
        except netutil.FetchError as e:
            raise netutil.FetchError(rsshub.explain_error(str(e))) from e
        return parse_feed(text)


class NewsnowSource(Source):
    type = "newsnow"
    label = "热榜（newsnow）"
    desc = "微博、抖音、知乎、百度等平台的实时热榜，来自开源项目 newsnow 的公开实例（非官方接口）。"
    params_spec = [{"key": "id", "label": "平台 ID", "placeholder": "weibo / douyin / zhihu / xueqiu-hotstock"}]

    def fetch(self, params, entities):
        pid = (params.get("id") or "").strip()
        if not pid:
            raise ValueError("没有填写平台 ID")
        base = (netutil.options.get("newsnow_url") or "https://newsnow.busiyi.world").rstrip("/")
        data = netutil.fetch_json(f"{base}/api/s?id={netutil.q(pid)}", {"Referer": base + "/"})
        if data.get("status") not in ("success", "cache"):
            raise netutil.FetchError(f"newsnow 没有返回数据（status={data.get('status')}），这个平台可能已下线")
        now = int(time.time())
        out = []
        for rank, d in enumerate(data.get("items") or [], 1):
            title = strip_html(str(d.get("title") or ""))
            if not title:
                continue
            extra = d.get("extra") or {}
            hover = extra.get("hover") or extra.get("info") or ""
            out.append({
                "title": title,
                "summary": f"热榜第 {rank} 名" + (f" · {strip_html(str(hover))}" if hover else ""),
                "url": d.get("url") or d.get("mobileUrl") or "",
                "media": f"{pid} 热榜",
                # 热榜条目大多没有发布时间：用抓到的时间，不伪造
                "published_at": parse_time(extra.get("date") or d.get("pubDate")) if (extra.get("date") or d.get("pubDate")) else now,
            })
        return out


# 巨潮资讯的 plate 参数：sz 深市、sh 沪市、bj 北交所；空 = 全部 A 股
CNINFO_PLATES = {"": "全部 A 股", "sz": "深市", "sh": "沪市", "bj": "北交所"}


class CninfoSource(Source):
    type = "cninfo"
    label = "巨潮资讯·上市公司公告"
    desc = "证监会指定信息披露网站巨潮资讯的最新公告（全市场）。plate：sz 深市 / sh 沪市 / bj 北交所 / 空为全部。"
    params_spec = [
        {"key": "plate", "label": "板块", "default": "", "placeholder": "sz / sh / bj / 空"},
        {"key": "category", "label": "公告类别", "default": "", "placeholder": "如 category_ndbg_szsh（年报），空为全部"},
        {"key": "size", "label": "每次条数", "default": "50"},
    ]

    def fetch(self, params, entities):
        form = {
            "pageNum": "1", "pageSize": str(params.get("size") or 50), "column": "szse", "tabName": "fulltext",
            "plate": params.get("plate") or "", "stock": "", "searchkey": "", "secid": "",
            "category": params.get("category") or "", "trade": "", "seDate": "", "sortName": "", "sortType": "",
            "isHLtitle": "true",
        }
        text = netutil.fetch(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Referer": "http://www.cninfo.com.cn/"},
            data=urllib.parse.urlencode(form).encode(),
        )
        try:
            data = json.loads(text)
        except json.JSONDecodeError as e:
            raise netutil.FetchError("巨潮资讯返回的不是 JSON（可能改版或限流）") from e
        out = []
        for d in data.get("announcements") or []:
            name = strip_html(d.get("secName") or "")
            title = strip_html(d.get("announcementTitle") or "")
            if not title:
                continue
            out.append({
                "title": f"{name}：{title}" if name and name not in title else title,
                "summary": f"{d.get('secCode') or ''} {name} 公告（巨潮资讯）".strip(),
                "url": f"http://static.cninfo.com.cn/{d['adjunctUrl']}" if d.get("adjunctUrl") else "",
                "media": "巨潮资讯",
                "published_at": parse_time(d.get("announcementTime")),
            })
        return out


class HkexNewsSource(Source):
    type = "hkex_news"
    label = "港交所披露易·最新公告"
    desc = "港交所披露易「最新上市公司公告」。market：sehk 主板 / gem 创业板（创业板只有英文 e）；lang：c 中文 / e 英文。"
    params_spec = [
        {"key": "market", "label": "市场", "default": "sehk", "placeholder": "sehk / gem"},
        {"key": "lang", "label": "语言", "default": "c", "placeholder": "c / e"},
    ]

    def fetch(self, params, entities):
        market = "gem" if (params.get("market") or "").lower() == "gem" else "sehk"
        lang = "e" if (params.get("lang") or "").lower() == "e" else "c"
        url = f"https://www1.hkexnews.hk/ncms/json/eds/lci{market}1relsd{lang}_1.json"
        data = netutil.fetch_json(url, {"Referer": "https://www1.hkexnews.hk/"})
        out = []
        for d in data.get("newsInfoLst") or []:
            stocks = d.get("stock") or []
            who = "、".join(f"{s.get('sn', '')}({s.get('sc', '')})" for s in stocks[:3])
            title = strip_html(d.get("title") or "")
            if not title:
                continue
            rel = d.get("relTime") or ""  # "21/10/2026 22:38"，香港时间 = 北京时间
            ts = parse_time(f"{rel[6:10]}-{rel[3:5]}-{rel[0:2]} {rel[11:16]}") if len(rel) >= 16 else int(time.time())
            out.append({
                "title": f"{who}：{title}" if who else title,
                "summary": strip_html(d.get("lTxt") or ""),
                "url": "https://www1.hkexnews.hk" + d["webPath"] if d.get("webPath") else "",
                "media": "港交所披露易",
                "published_at": ts,
            })
        return out
