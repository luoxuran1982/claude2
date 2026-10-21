"""v0.3 订阅目录、新数据源、失败退避、只存命中、旧库升级的离线测试（不联网，网络全部用 mock 替换）。

    python -m unittest discover -s tests -v
"""

import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from monitor import catalog, netutil, rsshub, sources  # noqa: E402
from monitor.collector import _due  # noqa: E402
from monitor.db import Store  # noqa: E402
from monitor.server import App  # noqa: E402

# RSSHub /api/namespace 的精简样例：一个免参数路由、一个必填参数路由、一个需要 Cookie 的路由
NAMESPACE = {
    "cls": {"name": "财联社", "url": "cls.cn", "lang": "zh-CN", "categories": ["finance"], "routes": {
        "/telegraph/:category?": {"path": "/telegraph/:category?", "name": "电报", "example": "/cls/telegraph",
                                  "categories": ["finance"], "features": {}, "parameters": {"category": "分类"}},
        "/subject/:id": {"path": "/subject/:id", "name": "话题", "example": "/cls/subject/1103",
                         "categories": ["finance"], "features": {}, "parameters": {"id": "话题 id"}},
    }},
    "xiaohongshu": {"name": "小红书", "url": "xiaohongshu.com", "lang": "zh-CN", "routes": {
        "/user/:user_id/:category": {"path": "/user/:user_id/:category", "name": "用户笔记",
                                     "categories": ["social-media"],
                                     "features": {"requireConfig": [{"name": "XIAOHONGSHU_COOKIE"}], "requirePuppeteer": True}},
    }},
}

HKEX = {"newsInfoLst": [{"title": "董事名單", "lTxt": "公告及通告 - [董事名單]", "webPath": "/listedco/x.pdf",
                         "stock": [{"sc": "00700", "sn": "TENCENT"}], "relTime": "21/10/2026 22:38"}]}
CNINFO = json.dumps({"announcements": [{"secCode": "600519", "secName": "贵州茅台", "announcementTitle": "关于回购股份的公告",
                                        "adjunctUrl": "finalpage/2026-10-22/1.pdf", "announcementTime": 1792598400000}]})
NEWSNOW = {"status": "success", "items": [{"title": "茅台冲上热搜", "url": "https://s.weibo.com/x"}, {"title": ""}]}


class NewSourcesTest(unittest.TestCase):
    def test_registry(self):
        self.assertTrue({"rsshub", "newsnow", "cninfo", "hkex_news"} <= set(sources.REGISTRY))

    def test_hkex(self):
        with mock.patch.object(netutil, "fetch_json", return_value=HKEX):
            items = sources.get("hkex_news").fetch({}, [])
        self.assertEqual(items[0]["title"], "TENCENT(00700)：董事名單")
        self.assertTrue(items[0]["url"].startswith("https://www1.hkexnews.hk/listedco/"))
        # 21/10/2026 22:38 香港时间 = 2026-10-21 14:38 UTC
        self.assertEqual(time.strftime("%Y-%m-%d %H:%M", time.gmtime(items[0]["published_at"])), "2026-10-21 14:38")

    def test_cninfo(self):
        with mock.patch.object(netutil, "fetch", return_value=CNINFO) as f:
            items = sources.get("cninfo").fetch({"plate": "sh"}, [])
        self.assertIn(b"plate=sh", f.call_args.kwargs["data"])  # POST 表单带上了板块
        self.assertEqual(items[0]["title"], "贵州茅台：关于回购股份的公告")
        self.assertEqual(items[0]["url"], "http://static.cninfo.com.cn/finalpage/2026-10-22/1.pdf")

    def test_newsnow(self):
        with mock.patch.object(netutil, "fetch_json", return_value=NEWSNOW):
            items = sources.get("newsnow").fetch({"id": "weibo"}, [])
        self.assertEqual(len(items), 1)  # 空标题被跳过
        self.assertIn("热榜第 1 名", items[0]["summary"])
        with mock.patch.object(netutil, "fetch_json", return_value={"status": "error"}):
            with self.assertRaises(netutil.FetchError):
                sources.get("newsnow").fetch({"id": "gone"}, [])

    def test_rsshub_error_explained(self):
        with mock.patch.object(netutil, "fetch", side_effect=netutil.FetchError("网络连接失败：refused")):
            with self.assertRaises(netutil.FetchError) as cm:
                sources.get("rsshub").fetch({"path": "/cls/telegraph"}, [])
        self.assertIn("RSSHub 没有运行", str(cm.exception))

    def test_localhost_skips_proxy(self):
        netutil.options["proxy"] = "http://127.0.0.1:7890"
        try:
            local = netutil._opener("http://127.0.0.1:1200/x")
            remote = netutil._opener("https://example.com/")
            # 空的 ProxyHandler({}) 不会出现在 handlers 里，但它阻止了 urllib 自动加上系统代理
            proxies = lambda op: [h.proxies for h in op.handlers if hasattr(h, "proxies")]  # noqa: E731
            self.assertEqual(proxies(local), [])
            self.assertEqual(proxies(remote)[0].get("https"), "http://127.0.0.1:7890")
        finally:
            netutil.options["proxy"] = ""


class PerEntitySourcesTest(unittest.TestCase):
    """v0.3.1：按监控对象查公告。"""
    ENTS = [{"id": 1, "name": "贵州茅台", "code": "600519", "market": "A"},
            {"id": 2, "name": "腾讯控股", "code": "700", "market": "HK"},
            {"id": 3, "name": "英伟达", "code": "NVDA", "market": "US"},
            {"id": 4, "name": "人工智能", "code": "", "market": ""}]

    def setUp(self):
        from monitor.sources import hub
        self.hub = hub
        hub._cninfo_orgs.value, hub._cninfo_orgs.at = {("A", "600519"): "gssh0600519", ("HK", "00700"): "gshk0000700"}, time.time()
        hub._sec_ciks.value, hub._sec_ciks.at = {"NVDA": 1045810}, time.time()

    def test_cninfo_stock_by_code(self):
        resp = json.dumps({"announcements": [{"secName": "贵州茅台", "announcementTitle": "2026年半年度报告",
                                              "adjunctUrl": "a.pdf", "announcementTime": 1792598400000}]})
        with mock.patch.object(netutil, "fetch", return_value=resp) as f:
            items = sources.get("cninfo_stock").fetch({}, self.ENTS)
        forms = [c.kwargs["data"].decode() for c in f.call_args_list]
        self.assertEqual(len(forms), 2)  # 只查 A 股和港股，美股和主题词跳过
        self.assertIn("stock=600519%2Cgssh0600519", forms[0])
        self.assertIn("column=hke", forms[1])  # 港股代码补成 5 位 00700
        self.assertEqual(items[0]["entity_ids"], [1])
        self.assertEqual(items[0]["title"], "贵州茅台：2026年半年度报告")

    def test_sec_filings_default_forms(self):
        today = time.strftime("%Y-%m-%d")
        sub = {"name": "NVIDIA CORP", "filings": {"recent": {
            "form": ["4", "8-K", "10-Q/A", "10-Q"], "filingDate": [today, today, today, "2000-01-01"],
            "acceptanceDateTime": [f"{today}T01:00:00.000Z"] * 4, "accessionNumber": ["0001-26-1", "0001-26-2", "0001-26-3", "0001-00-4"],
            "primaryDocument": ["f4.xml", "8k.htm", "q.htm", "old.htm"], "primaryDocDescription": ["FORM 4", "8-K", "", ""]}}}
        netutil.options["sec_contact"] = "test@example.com"
        try:
            with mock.patch.object(netutil, "fetch_json", return_value=sub) as f:
                items = sources.get("sec_filings").fetch({}, self.ENTS)
        finally:
            netutil.options["sec_contact"] = ""
        self.assertEqual(f.call_count, 1)  # 只查美股
        titles = [i["title"] for i in items]
        self.assertEqual(titles, ["英伟达：8-K（重大事项）", "英伟达：10-Q/A（季报）"])  # 默认不含 Form 4；太旧的截断
        self.assertEqual(items[0]["url"], "https://www.sec.gov/Archives/edgar/data/1045810/0001262/8k.htm")
        self.assertIn("修订版", items[1]["summary"])

    def test_sec_requires_contact(self):
        with self.assertRaises(netutil.FetchError):
            netutil._sec_headers("https://data.sec.gov/submissions/CIK0001045810.json")
        self.assertEqual(netutil._sec_headers("https://www.sec.gov/news/pressreleases.rss"), {})


class RsshubUpdateTest(unittest.TestCase):
    def test_refuses_when_dirty(self):
        """本机 RSSHub 有未提交修改时不自动更新，避免覆盖。"""
        calls = []

        def fake_run(args, cwd, timeout=900):
            calls.append(args)
            if args[:2] == ["git", "rev-parse"]:
                return "abc123\n"
            if args[:2] == ["git", "status"]:
                return " M lib/routes/x.ts\n"
            raise AssertionError("不应继续执行：" + " ".join(args))

        with mock.patch.object(rsshub, "_run", side_effect=fake_run):
            rsshub._update["running"] = True
            rsshub._do_update(Path("/tmp/none"))
        st = rsshub.update_status()
        self.assertFalse(st["ok"])
        self.assertIn("未提交的修改", st["error"])
        self.assertFalse(any(a[:2] == ["git", "pull"] for a in calls))


class CatalogTest(unittest.TestCase):
    def setUp(self):
        rsshub._cache.update(at=0, routes=None, error="")
        p = mock.patch.object(netutil, "fetch", return_value=json.dumps(NAMESPACE))
        p.start()
        self.addCleanup(p.stop)

    def test_flatten_and_default_feed(self):
        items, err = catalog.all_items()
        self.assertEqual(err, "")
        by = {it["key"]: it for it in items if not it.get("direct")}
        tele = by["/cls/telegraph/:category?"]
        self.assertEqual(tele["required"], [])
        self.assertEqual(catalog.default_feed(tele), {"type": "rsshub", "params": {"path": "/cls/telegraph"}})
        self.assertIsNone(catalog.default_feed(by["/cls/subject/:id"]))  # 必填参数：需要用户填
        xhs = by["/xiaohongshu/user/:user_id/:category"]
        self.assertEqual(set(xhs["needs"]), {"config", "browser"})
        self.assertEqual(xhs["categories"], ["social-media"])

    def test_categories_and_search(self):
        items, _ = catalog.all_items()
        cats = {c["id"]: c for c in catalog.categories(items)}
        self.assertEqual(cats["finance"]["name"], "金融财经")
        total, page = catalog.search(items, cat="social-media", only_ready=True)
        self.assertTrue(all(it.get("direct") for it in page))  # 小红书需要 Cookie，被「只看免配置」排除
        total, page = catalog.search(items, q="巨潮")
        self.assertGreaterEqual(total, 4)
        self.assertEqual(page[0]["site"], "巨潮资讯")

    def test_direct_entries_valid(self):
        keys = set()
        for it in catalog.DIRECT:
            self.assertIn(it["type"], sources.REGISTRY, it["name"])
            self.assertIn(it["categories"][0], rsshub.CATEGORY_NAMES, it["name"])
            self.assertNotIn(it["key"], keys, "目录里有重复的直连源：" + it["name"])
            keys.add(it["key"])

    def test_catalog_fallback_when_rsshub_down(self):
        catalog.all_items()  # 先成功一次
        rsshub._cache["at"] = 0
        with mock.patch.object(netutil, "fetch", side_effect=netutil.FetchError("网络连接失败：refused")):
            items, err = catalog.all_items()
        self.assertIn("RSSHub 没有运行", err)
        self.assertGreater(len(items), len(catalog.DIRECT))  # 仍返回上次成功的目录


class SchedulingTest(unittest.TestCase):
    def test_backoff(self):
        now = 10_000
        s = {"last_run": now - 600, "interval_min": 10, "fail_count": 0}
        self.assertTrue(_due(s, now))
        self.assertFalse(_due({**s, "fail_count": 1}, now))       # 失败 1 次：间隔 ×2
        self.assertTrue(_due({**s, "fail_count": 5, "last_run": now - 4800}, now))  # 最多 ×8

    def test_only_matched_and_fail_count(self):
        tmp = tempfile.mkdtemp()
        app = App(Path(tmp) / "t.db")
        try:
            now = int(time.time())
            items = [{"title": "贵州茅台发布回购公告", "summary": "", "url": "http://a/1", "published_at": now},
                     {"title": "某不相关公司公告", "summary": "", "url": "http://a/2", "published_at": now}]
            new = app.collector.ingest(items, {"id": None, "name": "公告", "params": {"only_matched": "1"}})
            self.assertEqual([a["title"] for a in new], ["贵州茅台发布回购公告"])
            sid = app.store.save_source({"type": "rss", "name": "x", "params": {"url": "http://x"}})["id"]
            app.store.source_status(sid, False, "坏了")
            app.store.source_status(sid, False, "坏了")
            self.assertEqual(app.store.source(sid)["fail_count"], 2)
            app.store.source_status(sid, True, count=1)
            self.assertEqual(app.store.source(sid)["fail_count"], 0)
        finally:
            app.collector.stop()
            app.store.close()


class MigrationTest(unittest.TestCase):
    def test_v02_database_upgraded(self):
        """模拟 v0.2 的数据库：sources 表没有新列、带着已失效的 36氪 默认订阅。"""
        path = Path(tempfile.mkdtemp()) / "old.db"
        con = sqlite3.connect(path)
        con.executescript("""
            CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE sources (id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL, name TEXT NOT NULL,
              params TEXT NOT NULL DEFAULT '{}', enabled INTEGER NOT NULL DEFAULT 1, interval_min REAL NOT NULL DEFAULT 10,
              last_run INTEGER NOT NULL DEFAULT 0, last_ok INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '',
              last_count INTEGER NOT NULL DEFAULT 0, last_new INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0);
            INSERT INTO kv VALUES ('seeded', '1');
            INSERT INTO sources(type,name,params) VALUES ('rss','36氪','{"url": "https://36kr.com/feed"}');
            INSERT INTO sources(type,name,params) VALUES ('cls_telegraph','财联社·电报','{}');
        """)
        con.commit()
        con.close()
        st = Store(path)
        try:
            rows = {r["name"]: r for r in st.sources()}
            self.assertEqual(rows["36氪·快讯"]["type"], "rsshub")
            self.assertEqual(rows["36氪·快讯"]["params"], {"path": "/36kr/newsflashes"})
            self.assertEqual(rows["财联社·电报"]["category"], "finance")
            self.assertEqual(rows["财联社·电报"]["fail_count"], 0)
        finally:
            st.close()


class CatalogApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        rsshub._cache.update(at=time.time(), routes=rsshub._flatten(NAMESPACE), error="")
        cls.app = App(Path(tempfile.mkdtemp()) / "s.db")
        cls.httpd = cls.app.make_server(0)
        cls.base = f"http://127.0.0.1:{cls.app.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.app.collector.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, body=None):
        path = urllib.parse.quote(path, safe="/?=&")
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method="POST" if data else "GET", headers={"X-Monitor": "1"})
        with urllib.request.urlopen(r, timeout=5) as resp:
            return json.loads(resp.read())

    def test_subscribe_flow(self):
        cats = self.req("/api/catalog/categories")
        self.assertEqual(cats["rsshub"], 3)
        d = self.req("/api/catalog?cat=finance&q=电报")
        tele = next(it for it in d["items"] if it["key"] == "/cls/telegraph/:category?")
        self.assertIsNone(tele["subscribed"])
        with mock.patch.object(self.app.collector, "run_now"):
            r = self.req("/api/catalog/subscribe", {"items": [{"type": "rsshub", "params": {"path": "/cls/telegraph"},
                                                              "name": "财联社·电报", "category": "finance", "interval": 5}]})
            self.assertEqual(r["added"], 1)
            again = self.req("/api/catalog/subscribe", {"items": [{"type": "rsshub", "params": {"path": "/cls/telegraph"}}]})
            self.assertEqual(again["added"], 0)  # 同一地址不重复订阅
        d = self.req("/api/catalog?cat=finance&q=电报")
        tele = next(it for it in d["items"] if it["key"] == "/cls/telegraph/:category?")
        self.assertIsNotNone(tele["subscribed"])
        self.req("/api/catalog/health", {"feed_key": "/cls/telegraph", "ok": True, "count": 20})
        d = self.req("/api/catalog?cat=finance&q=电报")
        tele = next(it for it in d["items"] if it["key"] == "/cls/telegraph/:category?")
        self.assertEqual(tele["health"]["n"], 20)


if __name__ == "__main__":
    unittest.main()
