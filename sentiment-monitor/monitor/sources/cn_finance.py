"""国内财经网站的公开接口（网页自身在用的 JSON 接口，非官方，可能随网站改版失效）。"""

from __future__ import annotations

import hashlib
import json
import random
import time
import urllib.parse

from .. import netutil
from .base import Source, entity_query, parse_time, strip_html


class SinaRollSource(Source):
    type = "sina_roll"
    label = "新浪财经滚动新闻"
    desc = "新浪财经滚动新闻接口。lid：2516 财经、2517 股市、2518 美股、2515 科技、2509 全部。"
    params_spec = [
        {"key": "lid", "label": "栏目 lid", "default": "2516"},
        {"key": "num", "label": "每次条数", "default": "50"},
    ]

    def fetch(self, params, entities):
        lid = params.get("lid") or "2516"
        num = params.get("num") or "50"
        url = f"https://feed.mix.sina.com.cn/api/roll/get?pageid=153&lid={lid}&k=&num={num}&page=1&r={random.random()}"
        data = netutil.fetch_json(url, {"Referer": "https://finance.sina.com.cn/"})
        res = data.get("result") or {}
        out = []
        for d in res.get("data") or []:
            title = strip_html(d.get("title") or "")
            if not title:
                continue
            out.append({
                "title": title,
                "summary": strip_html(d.get("intro") or d.get("summary") or ""),
                "url": d.get("url") or d.get("wapurl") or "",
                "media": d.get("media_name") or "新浪财经",
                "published_at": parse_time(d.get("ctime") or d.get("intime")),
            })
        return out


class EastmoneyFastSource(Source):
    type = "eastmoney_724"
    label = "东方财富 7×24 快讯"
    desc = "东方财富全球财经快讯。栏目 102 为全部快讯。"
    params_spec = [{"key": "column", "label": "栏目", "default": "102"}]

    def fetch(self, params, entities):
        col = params.get("column") or "102"
        url = (
            "https://np-weblist.eastmoney.com/comm/web/getFastNewsList?client=web&biz=web_724"
            f"&fastColumn={col}&sortEnd=&pageSize=50&req_trace={int(time.time() * 1000)}"
        )
        data = netutil.fetch_json(url, {"Referer": "https://kuaixun.eastmoney.com/"})
        out = []
        for d in ((data.get("data") or {}).get("fastNewsList") or []):
            summary = strip_html(d.get("summary") or "")
            title = strip_html(d.get("title") or "") or summary[:60]
            if not title:
                continue
            code = d.get("code") or ""
            out.append({
                "title": title,
                "summary": summary if summary != title else "",
                "url": f"https://finance.eastmoney.com/a/{code}.html" if code else "",
                "media": "东方财富",
                "published_at": parse_time(d.get("showTime")),
            })
        return out


class ClsTelegraphSource(Source):
    type = "cls_telegraph"
    label = "财联社电报"
    desc = "财联社 7×24 电报（网页接口，带签名参数）。"
    params_spec = [{"key": "rn", "label": "每次条数", "default": "50"}]

    def fetch(self, params, entities):
        q = {"app": "CailianpressWeb", "os": "web", "rn": str(params.get("rn") or "50"), "sv": "8.4.6"}
        qs = urllib.parse.urlencode(sorted(q.items()))
        sign = hashlib.md5(hashlib.sha1(qs.encode()).hexdigest().encode()).hexdigest()
        url = f"https://www.cls.cn/nodeapi/telegraphList?{qs}&sign={sign}"
        data = netutil.fetch_json(url, {"Referer": "https://www.cls.cn/telegraph"})
        rows = ((data.get("data") or {}).get("roll_data")) or []
        out = []
        for d in rows:
            content = strip_html(d.get("content") or d.get("brief") or "")
            title = strip_html(d.get("title") or "")
            if not title:
                # 电报常常没有标题：取正文【】里的内容或前 60 字
                if content.startswith("【") and "】" in content:
                    title = content[1:content.index("】")]
                else:
                    title = content[:60]
            if not title:
                continue
            out.append({
                "title": title,
                "summary": content if content != title else "",
                "url": d.get("shareurl") or (f"https://www.cls.cn/detail/{d['id']}" if d.get("id") else ""),
                "media": "财联社",
                "published_at": parse_time(d.get("ctime")),
            })
        return out


class EastmoneySearchSource(Source):
    type = "eastmoney_search"
    label = "东方财富资讯搜索"
    desc = "按每个监控对象的名称在东方财富搜索最新资讯。"
    per_entity = True
    params_spec = [{"key": "size", "label": "每个对象条数", "default": "20"}]

    def fetch(self, params, entities):
        size = int(params.get("size") or 20)
        out = []
        for e in entities:
            p = {
                "uid": "", "keyword": entity_query(e), "type": ["cmsArticleWebOld"], "client": "web",
                "clientType": "web", "clientVersion": "curr",
                "param": {"cmsArticleWebOld": {"searchScope": "default", "sort": "time", "pageIndex": 1, "pageSize": size, "preTag": "", "postTag": ""}},
            }
            url = "https://search-api-web.eastmoney.com/search/jsonp?cb=jQuery&param=" + netutil.q(json.dumps(p, ensure_ascii=False, separators=(",", ":")))
            data = netutil.fetch_json(url, {"Referer": "https://so.eastmoney.com/"})
            rows = ((data.get("result") or {}).get("cmsArticleWebOld")) or []
            for d in rows:
                title = strip_html(d.get("title") or "")
                if not title:
                    continue
                out.append({
                    "title": title,
                    "summary": strip_html(d.get("content") or ""),
                    "url": d.get("url") or "",
                    "media": d.get("mediaName") or "东方财富",
                    "published_at": parse_time(d.get("date")),
                    "entity_ids": [e["id"]],
                })
        return out
