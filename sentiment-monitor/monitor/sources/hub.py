"""v0.3 新增的数据源类型：RSSHub 路由、newsnow 热榜、巨潮资讯公告、港交所披露易公告；
v0.3.1 加按监控对象查公告：巨潮（A 股、港股）、SEC（美股）。

- RSSHub：本机运行的开源 RSS 生成器（AGPL-3.0）。本应用只通过 HTTP 调用它，不复制它的代码。
- newsnow：开源热榜聚合（ourongxing/newsnow）的公开实例，热点雷达也在用。只调用接口。
- 巨潮资讯、港交所披露易：网站页面自己在用的公开 JSON 接口，非官方 API，网站改版后可能失效。
"""

from __future__ import annotations

import json
import threading
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


# ------------------------------------------------------------------ v0.3.1：按监控对象查公告
class _DailyCache:
    """简单的「一天一刷新」缓存：代码对照表（股票代码 → 巨潮 orgId / SEC CIK）一天取一次就够。"""

    def __init__(self, loader, ttl=24 * 3600):
        self.loader, self.ttl = loader, ttl
        self.value, self.at = None, 0.0
        self.lock = threading.Lock()

    def get(self):
        with self.lock:
            if self.value is None or time.time() - self.at > self.ttl:
                self.value = self.loader()  # 失败时抛出，下次再试；不缓存失败结果
                self.at = time.time()
            return self.value


def _load_cninfo_orgs() -> dict:
    """巨潮的代码对照表：{("A"|"HK", 代码): orgId}。A 股表含沪深，北交所和港股各一张表。"""
    out = {}
    for name, market in (("szse_stock.json", "A"), ("bj_stock.json", "A"), ("hke_stock.json", "HK")):
        data = netutil.fetch_json(f"http://www.cninfo.com.cn/new/data/{name}", {"Referer": "http://www.cninfo.com.cn/"})
        for s in data.get("stockList") or []:
            if s.get("code") and s.get("orgId"):
                out[(market, s["code"])] = s["orgId"]
    if not out:
        raise netutil.FetchError("巨潮代码对照表为空（可能改版）")
    return out


_cninfo_orgs = _DailyCache(_load_cninfo_orgs)


def _cninfo_target(e: dict):
    """监控对象 → (市场, 代码)。只处理 A 股和港股；没有代码的对象跳过。"""
    code = (e.get("code") or "").strip().upper()
    market = (e.get("market") or "").upper()
    if not code.isdigit():
        return None
    if market == "HK" or (not market and len(code) == 5):
        return "HK", code.zfill(5)
    if market in ("A", "") and len(code) == 6:
        return "A", code
    return None


class CninfoStockSource(Source):
    type = "cninfo_stock"
    label = "巨潮资讯·按监控对象查公告"
    desc = "对每个 A 股、港股监控对象，到巨潮资讯查它自己最近的公告（按代码匹配，不靠标题里的名称）。"
    per_entity = True
    params_spec = [{"key": "size", "label": "每个对象条数", "default": "20"}]

    def fetch(self, params, entities):
        out = []
        for e in entities:
            target = _cninfo_target(e)
            if not target:
                continue  # 美股、主题词等：这个数据源不管
            org = _cninfo_orgs.get().get(target)
            if not org:
                raise netutil.FetchError(f"巨潮找不到代码 {target[1]}（{e['name']}）")
            market, code = target
            form = {
                "pageNum": "1", "pageSize": str(params.get("size") or 20), "column": "hke" if market == "HK" else "szse",
                "tabName": "fulltext", "plate": "", "stock": f"{code},{org}", "searchkey": "", "secid": "",
                "category": "", "trade": "", "seDate": "", "sortName": "", "sortType": "", "isHLtitle": "true",
            }
            text = netutil.fetch(
                "http://www.cninfo.com.cn/new/hisAnnouncement/query",
                {"Content-Type": "application/x-www-form-urlencoded; charset=UTF-8", "Referer": "http://www.cninfo.com.cn/"},
                data=urllib.parse.urlencode(form).encode(),
            )
            try:
                data = json.loads(text)
            except json.JSONDecodeError as ex:
                raise netutil.FetchError("巨潮资讯返回的不是 JSON（可能改版或限流）") from ex
            for d in data.get("announcements") or []:
                title = strip_html(d.get("announcementTitle") or "")
                if not title:
                    continue
                out.append({
                    "title": title if e["name"] in title else f"{e['name']}：{title}",
                    "summary": f"{code} {d.get('secName') or ''} 公告（巨潮资讯）",
                    "url": f"http://static.cninfo.com.cn/{d['adjunctUrl']}" if d.get("adjunctUrl") else "",
                    "media": "巨潮资讯",
                    "published_at": parse_time(d.get("announcementTime")),
                    "entity_ids": [e["id"]],
                })
        return out


def _load_sec_ciks() -> dict:
    """SEC 的代码对照表：{股票代码: CIK}。CIK 是 SEC 给每家公司的固定编号。"""
    data = netutil.fetch_json("https://www.sec.gov/files/company_tickers.json")
    return {str(v["ticker"]).upper(): int(v["cik_str"]) for v in data.values()}


_sec_ciks = _DailyCache(_load_sec_ciks)

# 常见申报类型的中文名（/A 表示修订版）
SEC_FORMS = {
    "8-K": "重大事项", "10-K": "年报", "10-Q": "季报", "6-K": "外国发行人报告", "20-F": "外国发行人年报",
    "S-1": "招股书", "F-1": "外国公司招股书", "424B4": "最终招股书", "4": "内部人交易", "3": "内部人首次持股",
    "144": "拟出售限制性股票", "SC 13D": "大股东持股（主动）", "SC 13G": "大股东持股（被动）",
    "DEF 14A": "股东大会委托书", "11-K": "员工持股计划年报",
}


# 默认关注的申报类型：不含 4 / 3 / 144（内部人交易，大公司每月几十份，量大）
SEC_DEFAULT_FORMS = "8-K,10-K,10-Q,6-K,20-F,S-1,F-1,424B4,SC 13D,SC 13G,DEF 14A"


class SecFilingsSource(Source):
    type = "sec_filings"
    label = "SEC·按监控对象查申报"
    desc = "对每个美股监控对象，用 SEC 官方接口查它自己最近的申报（8-K、10-Q、6-K 等）。需先填「设置 → SEC 联系邮箱」。"
    per_entity = True
    params_spec = [
        {"key": "forms", "label": "申报类型", "default": SEC_DEFAULT_FORMS,
         "placeholder": "逗号分隔；加上 4 可看内部人交易（量大）"},
        {"key": "days", "label": "最近几天", "default": "90"},
    ]

    def fetch(self, params, entities):
        forms = {f.strip().upper() for f in (params.get("forms") or SEC_DEFAULT_FORMS).split(",") if f.strip()}
        since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - float(params.get("days") or 90) * 86400))
        out = []
        for e in entities:
            code = (e.get("code") or "").strip().upper()
            market = (e.get("market") or "").upper()
            if not code or market not in ("US", "") or not code.replace(".", "").replace("-", "").isalpha():
                continue  # 只处理美股代码
            cik = _sec_ciks.get().get(code.replace(".", "-"))
            if not cik:
                raise netutil.FetchError(f"SEC 找不到股票代码 {code}（{e['name']}）")
            sub = netutil.fetch_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
            r = (sub.get("filings") or {}).get("recent") or {}
            for i, form in enumerate(r.get("form") or []):
                date = r["filingDate"][i]
                if date < since:
                    break  # 按时间倒序排列，再往后都更旧
                if form.upper().removesuffix("/A") not in forms:
                    continue
                acc = r["accessionNumber"][i]
                doc = r["primaryDocument"][i]
                desc = r.get("primaryDocDescription", [""] * (i + 1))[i] or ""
                cn = SEC_FORMS.get(form.upper().removesuffix("/A"), "")
                out.append({
                    "title": f"{e['name']}：{form}{'（' + cn + '）' if cn else ''}" + (f" {desc}" if desc and form.upper() not in desc.upper() else ""),
                    "summary": f"{sub.get('name') or code} 向 SEC 提交 {form}，申报日 {date}" + ("（修订版）" if form.upper().endswith("/A") else ""),
                    "url": f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}/{doc}",
                    "media": "SEC EDGAR",
                    "published_at": parse_time(r["acceptanceDateTime"][i] or date),
                    "entity_ids": [e["id"]],
                })
        return out
