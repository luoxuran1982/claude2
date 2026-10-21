"""订阅目录：RSSHub 全部路由（运行时从本机 RSSHub 读取）+ 本应用直连的源（下方 DIRECT）。

分类沿用 RSSHub 自己的分类（只翻译名称）。直连的源按性质归入同样的分类，并标「直连」。
每条目录项最后都变成一个普通数据源（sources 表的一行），之后和手动添加的数据源一样调度。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from . import rsshub

TESTED_FILE = Path(__file__).with_name("catalog_tested.json")


def _d(cat, site, name, type_, params, interval, only_matched=False, lang="zh-CN", note=""):
    key = f"direct:{type_}:" + json.dumps(params, ensure_ascii=False, sort_keys=True)
    return {
        "key": key, "direct": True, "site": site, "name": name, "type": type_, "params": params,
        "categories": [cat], "interval": interval, "only_matched": only_matched, "lang": lang, "desc": note,
        "needs": [], "required": [],
    }


def _rss(cat, site, name, url, interval=30, only_matched=False, lang="en", note=""):
    return _d(cat, site, name, "rss", {"url": url}, interval, only_matched, lang, note)


def _hot(cat, site, pid, interval=15, only_matched=True):
    return _d(cat, site, "热榜", "newsnow", {"id": pid}, interval, only_matched,
              note="来自 newsnow 公开实例（非官方接口）。热榜条目多数没有发布时间，按抓到的时间记录。")


SEC_NOTE = "美国证监会 EDGAR 实时申报流（全市场），默认只保存命中监控对象的条目。"
EDGAR = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={}&count=40&output=atom"

# 本应用直连的源（2026-10-21 在用户 Mac 上实测能抓到）
DIRECT = [
    # ---- 交易所 / 信息披露
    _d("finance", "巨潮资讯", "A 股全市场最新公告", "cninfo", {"plate": ""}, 10, True, note="证监会指定信息披露网站。全市场公告量大，默认只保存命中监控对象的公告。"),
    _d("finance", "巨潮资讯", "深市最新公告", "cninfo", {"plate": "sz"}, 10, True),
    _d("finance", "巨潮资讯", "沪市最新公告", "cninfo", {"plate": "sh"}, 10, True),
    _d("finance", "巨潮资讯", "北交所最新公告", "cninfo", {"plate": "bj"}, 15, True),
    _d("finance", "港交所披露易", "主板最新公告（中文）", "hkex_news", {"market": "sehk", "lang": "c"}, 10, True),
    _d("finance", "港交所披露易", "主板最新公告（英文）", "hkex_news", {"market": "sehk", "lang": "e"}, 10, True, lang="en"),
    _d("finance", "港交所披露易", "创业板最新公告（英文）", "hkex_news", {"market": "gem", "lang": "e"}, 30, True, lang="en"),
    _rss("finance", "香港交易所", "新闻稿", "https://www.hkex.com.hk/Services/RSS-Feeds/News-Releases?sc_lang=zh-HK", 60, lang="zh-HK"),
    _rss("finance", "香港交易所", "监管公告", "https://www.hkex.com.hk/Services/RSS-Feeds/Regulatory-Announcements?sc_lang=zh-HK", 60, lang="zh-HK"),
    _rss("finance", "香港交易所", "市场通讯", "https://www.hkex.com.hk/Services/RSS-Feeds/Market-Communications?sc_lang=en", 60),
    _rss("finance", "美国证监会 SEC", "新闻稿", "https://www.sec.gov/news/pressreleases.rss", 60),
    _rss("finance", "美国证监会 SEC", "声明", "https://www.sec.gov/news/statements.rss", 120),
    _rss("finance", "美国证监会 SEC", "讲话与声明", "https://www.sec.gov/news/speeches-statements.rss", 120),
    _rss("finance", "SEC EDGAR", "8-K 重大事项（实时）", EDGAR.format("8-K"), 10, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "6-K 外国发行人报告（中概股常用）", EDGAR.format("6-K"), 10, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "10-K 年报", EDGAR.format("10-K"), 30, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "10-Q 季报", EDGAR.format("10-Q"), 30, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "S-1 美国 IPO 招股书", EDGAR.format("S-1"), 30, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "F-1 外国公司 IPO 招股书", EDGAR.format("F-1"), 30, True, note=SEC_NOTE),
    _rss("finance", "SEC EDGAR", "Form 4 内部人交易", EDGAR.format("4"), 15, True, note=SEC_NOTE),
    _rss("finance", "美国商品期货委员会 CFTC", "新闻", "https://www.cftc.gov/RSS/RSSGP/rssgp.xml", 120),
    _rss("finance", "香港证监会 SFC", "新闻稿", "https://www.sfc.hk/en/RSS-Feeds/Press-releases", 60),
    # ---- 央行 / 宏观
    _rss("finance", "美联储", "新闻稿", "https://www.federalreserve.gov/feeds/press_all.xml", 60),
    _rss("finance", "美联储", "讲话", "https://www.federalreserve.gov/feeds/speeches.xml", 120),
    _rss("finance", "欧洲央行", "新闻稿", "https://www.ecb.europa.eu/rss/press.html", 60),
    _rss("finance", "英格兰银行", "新闻", "https://www.bankofengland.co.uk/rss/news", 120),
    _rss("finance", "日本央行", "最新消息", "https://www.boj.or.jp/en/rss/whatsnew.xml", 120),
    _rss("finance", "国际清算银行 BIS", "各国央行行长讲话", "https://www.bis.org/doclist/cbspeeches.rss", 120),
    _rss("finance", "香港金管局", "新闻稿", "https://www.hkma.gov.hk/eng/other-information/rss/rss_press-release.xml", 60),
    _rss("finance", "美国经济分析局 BEA", "数据发布", "https://apps.bea.gov/rss/rss.xml", 240),
    # ---- 海外财经媒体
    _rss("finance", "Bloomberg", "Markets", "https://feeds.bloomberg.com/markets/news.rss", 30),
    _rss("finance", "Financial Times", "首页", "https://www.ft.com/rss/home", 30),
    _rss("finance", "Financial Times", "Markets", "https://www.ft.com/markets?format=rss", 30),
    _rss("finance", "CNBC", "Top News", "https://www.cnbc.com/id/100003114/device/rss/rss.html", 20),
    _rss("finance", "CNBC", "Finance", "https://www.cnbc.com/id/10000664/device/rss/rss.html", 30),
    _rss("finance", "CNBC", "Markets", "https://www.cnbc.com/id/15839069/device/rss/rss.html", 30),
    _rss("finance", "CNBC", "Asia", "https://www.cnbc.com/id/19854910/device/rss/rss.html", 30),
    _rss("finance", "MarketWatch", "Top Stories", "https://feeds.content.dowjones.io/public/rss/mw_topstories", 30),
    _rss("finance", "Investing.com", "全部新闻", "https://www.investing.com/rss/news.rss", 20),
    _rss("finance", "Investing.com", "股市新闻", "https://www.investing.com/rss/news_25.rss", 30),
    _rss("finance", "Investing.com", "经济新闻", "https://www.investing.com/rss/news_14.rss", 30),
    _rss("finance", "Seeking Alpha", "最新文章", "https://seekingalpha.com/feed.xml", 30),
    _rss("finance", "Seeking Alpha", "Market Currents", "https://seekingalpha.com/market_currents.xml", 20),
    _rss("finance", "The Economist", "Finance & economics", "https://www.economist.com/finance-and-economics/rss.xml", 120),
    _rss("traditional-media", "New York Times", "Business", "https://rss.nytimes.com/services/xml/rss/nyt/Business.xml", 30),
    _rss("traditional-media", "BBC", "Business", "https://feeds.bbci.co.uk/news/business/rss.xml", 30),
    _rss("traditional-media", "The Guardian", "Business", "https://www.theguardian.com/business/rss", 30),
    _rss("traditional-media", "FT 中文网", "最新", "https://www.ftchinese.com/rss/news", 30, lang="zh-CN"),
    # ---- 国内科技/新媒体（官方 RSS）
    _rss("new-media", "IT之家", "最新", "https://www.ithome.com/rss/", 20, True, lang="zh-CN"),
    _rss("new-media", "少数派", "最新", "https://sspai.com/feed", 60, True, lang="zh-CN"),
    _rss("new-media", "钛媒体", "最新", "https://www.tmtpost.com/rss.xml", 30, True, lang="zh-CN"),
    _rss("new-media", "极客公园", "最新", "https://www.geekpark.net/rss", 60, True, lang="zh-CN"),
    _rss("new-media", "雷峰网", "最新", "https://www.leiphone.com/feed", 60, True, lang="zh-CN"),
    _rss("new-media", "爱范儿", "最新", "https://www.ifanr.com/feed", 60, True, lang="zh-CN"),
    # ---- 热榜（newsnow）：财经
    _hot("finance", "雪球", "xueqiu-hotstock", 10, False),
    _hot("finance", "财联社", "cls-hot", 10, False),
    _hot("finance", "财联社·深度", "cls-depth", 30, False),
    _hot("finance", "华尔街见闻", "wallstreetcn-hot", 15, False),
    _hot("finance", "华尔街见闻·最新", "wallstreetcn-news", 10, False),
    _hot("finance", "格隆汇", "gelonghui", 15, False),
    _hot("finance", "金十数据", "jin10", 5, False),
    _hot("finance", "法布财经", "fastbull-express", 10, False),
    _hot("finance", "MKTNews", "mktnews-flash", 10, False),
    # ---- 热榜（newsnow）：社交/综合
    _hot("social-media", "微博", "weibo"),
    _hot("social-media", "抖音", "douyin"),
    _hot("social-media", "知乎", "zhihu"),
    _hot("social-media", "百度", "baidu"),
    _hot("social-media", "今日头条", "toutiao"),
    _hot("social-media", "哔哩哔哩", "bilibili-hot-search"),
    _hot("social-media", "百度贴吧", "tieba"),
    _hot("social-media", "虎扑", "hupu", 30),
    _hot("social-media", "豆瓣", "douban", 60),
    _hot("social-media", "酷安", "coolapk", 30),
    _hot("traditional-media", "澎湃新闻", "thepaper"),
    _hot("traditional-media", "凤凰网", "ifeng"),
    _hot("traditional-media", "腾讯新闻", "tencent-hot"),
    _hot("traditional-media", "联合早报", "zaobao"),
    _hot("traditional-media", "参考消息", "cankaoxiaoxi"),
    _hot("traditional-media", "卫星通讯社", "sputniknewscn", 30),
    _hot("new-media", "IT之家", "ithome", 30),
    _hot("new-media", "少数派", "sspai", 60),
    _hot("programming", "稀土掘金", "juejin", 60),
    _hot("programming", "GitHub Trending", "github-trending-today", 120),
    _hot("programming", "Hacker News", "hackernews", 60),
    _hot("programming", "Product Hunt", "producthunt", 120),
    _hot("programming", "牛客", "nowcoder", 120),
    _hot("programming", "FreeBuf", "freebuf", 120),
    _hot("multimedia", "腾讯视频", "qqvideo-tv-hotsearch", 120),
    _hot("multimedia", "爱奇艺", "iqiyi-hot-ranklist", 120),
    _hot("game", "Steam", "steam", 240),
]


def tested() -> dict:
    """随应用附带的实测记录：{订阅地址: {"n": 条数, "at": "日期"}}。"""
    try:
        return json.loads(TESTED_FILE.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def feed_key(type_: str, params: dict) -> str:
    """一个具体订阅的唯一标识：RSSHub 用路由路径，其他用类型 + 参数。"""
    if type_ == "rsshub":
        return params.get("path", "")
    if type_ == "rss":
        return params.get("url", "")
    return f"{type_}:" + json.dumps(params, ensure_ascii=False, sort_keys=True)


def default_feed(item: dict) -> dict | None:
    """目录项不填参数就能订阅时，返回 {type, params}；否则 None（需要用户填参数）。"""
    if item.get("direct"):
        return {"type": item["type"], "params": item["params"]}
    if item["required"]:
        return None
    segs = [s for s in item["path"].strip("/").split("/") if s and not s.startswith(":") and s != "*"]
    return {"type": "rsshub", "params": {"path": "/" + "/".join(segs)}}


def all_items(force=False) -> tuple[list[dict], str]:
    routes, err = rsshub.catalog(force)
    return list(DIRECT) + (routes or []), err


def categories(items: list[dict]) -> list[dict]:
    count = {}
    for it in items:
        for c in it["categories"]:
            count[c] = count.get(c, 0) + 1
    order = {c: i for i, c in enumerate(rsshub.CATEGORY_ORDER)}
    cats = sorted(count, key=lambda c: (order.get(c, 999), c))
    return [{"id": c, "name": rsshub.CATEGORY_NAMES.get(c, c), "count": count[c]} for c in cats]


def search(items, cat="", q="", lang="", only_ready=False, direct_only=False, limit=300, offset=0):
    """按分类 / 关键词 / 语言筛选目录。返回 (总数, 本页条目)。"""
    q = (q or "").strip().lower()
    out = []
    for it in items:
        if cat and cat not in it["categories"]:
            continue
        if direct_only and not it.get("direct"):
            continue
        if lang == "zh" and not str(it.get("lang", "")).startswith("zh"):
            continue
        if lang == "other" and str(it.get("lang", "")).startswith("zh"):
            continue
        if only_ready and (it["needs"] and set(it["needs"]) & {"config", "browser"} or it["required"]):
            continue
        if q and q not in f"{it['site']} {it['name']} {it.get('key', '')} {it.get('site_url', '')}".lower():
            continue
        out.append(it)
    # 中文在前；同语言里直连、实测过的在前，其余按网站名
    tst = tested()

    def rank(it):
        feed = default_feed(it)
        ok = feed and feed_key(feed["type"], feed["params"]) in tst
        zh = str(it.get("lang", "")).startswith("zh")
        return (0 if zh else 1, 0 if it.get("direct") else 1 if ok else 2, it["site"], it["name"])

    out.sort(key=rank)
    return len(out), out[offset:offset + limit]


def stamp() -> str:
    return time.strftime("%Y-%m-%d")
