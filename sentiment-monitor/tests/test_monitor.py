"""离线测试：不访问外网。数据源用接口样例数据验证解析逻辑。

    python -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("MONITOR_DATA_DIR", tempfile.mkdtemp(prefix="monitor-test-"))

from monitor import netutil, quotes, sentiment, sources  # noqa: E402
from monitor.db import Store  # noqa: E402
from monitor.events import EventBus  # noqa: E402
from monitor.lifecycle import BrowserWindow, Watchdog  # noqa: E402
from monitor.matcher import Matcher  # noqa: E402
from monitor.server import App  # noqa: E402
from monitor.sources.base import parse_time  # noqa: E402
from monitor.sources.rss import parse_feed, yahoo_symbol  # noqa: E402

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>
<item><title>贵州茅台净利润同比增长15% - 证券时报</title><link>https://news.example.com/a?utm_source=x</link>
<pubDate>Mon, 06 Oct 2026 08:00:00 GMT</pubDate><description><![CDATA[<p>茅台<b>业绩</b>超预期</p>]]></description>
<source url="https://stcn.com">证券时报</source></item>
<item><title>Nvidia shares plunge on probe</title><link>https://news.example.com/b</link><pubDate>Mon, 06 Oct 2026 09:00:00 +0000</pubDate></item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>宁德时代发布新电池</title><link href="https://example.com/catl"/><updated>2026-10-06T10:00:00+08:00</updated>
<summary>量产在即</summary><author><name>36氪</name></author></entry></feed>"""

SINA = {"result": {"status": {"code": 0}, "data": [
    {"title": "宁德时代大涨5%", "url": "https://finance.sina.com.cn/x.shtml", "ctime": "1791100000", "intro": "资金流入", "media_name": "新浪财经"},
    {"title": "", "url": "https://finance.sina.com.cn/empty.shtml", "ctime": "1791100000"},
]}}

EM724 = {"code": "1", "data": {"fastNewsList": [
    {"code": "202610063200000001", "title": "腾讯控股回购股份", "summary": "腾讯控股今日回购100万股", "showTime": "2026-10-06 10:00:00"},
    {"code": "202610063200000002", "title": "", "summary": "央行宣布降准0.5个百分点，释放长期资金", "showTime": "2026-10-06 10:01:00"},
]}}

CLS = {"error": 0, "data": {"roll_data": [
    {"id": 1830001, "title": "", "content": "【某公司收到立案告知书】财联社10月6日电，某公司公告……", "ctime": 1791100000, "shareurl": "https://api3.cls.cn/share/article/1830001"},
    {"id": 1830002, "title": "英伟达盘前涨3%", "content": "英伟达盘前涨3%", "ctime": 1791100100},
]}}

EMSEARCH = 'jQuery({"code":0,"result":{"cmsArticleWebOld":[{"date":"2026-10-06 09:30:00","title":"<em>贵州茅台</em>获北向资金净买入","content":"北向资金...","mediaName":"东方财富研究中心","url":"http://finance.eastmoney.com/a/1.html"}]}})'

TENCENT = 'v_sh600519="1~贵州茅台~600519~1500.00~1480.00~1482.00~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~1~~20261006150000~20.00~1.35";\nv_usNVDA="200~英伟达~NVDA.OQ~120.5~125~0";'


class SentimentTest(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(sentiment.analyze("贵州茅台三季度净利润大幅增长，业绩超预期")["label"], "pos")
        self.assertEqual(sentiment.analyze("某公司收到证监会立案调查通知书")["label"], "neg")
        self.assertEqual(sentiment.analyze("公司召开年度股东大会")["label"], "neu")
        self.assertEqual(sentiment.analyze("Nvidia stock surges after earnings beat")["label"], "pos")
        self.assertEqual(sentiment.analyze("Tesla plunges on recall probe")["label"], "neg")

    def test_negation(self):
        r = sentiment.analyze("控股股东承诺不减持")
        self.assertGreater(r["score"], 0)
        self.assertEqual(r["risk"], "")
        self.assertEqual(sentiment.analyze("公司否认存在财务造假")["risk"], "")

    def test_risk_tags(self):
        self.assertIn("监管处罚", sentiment.analyze("某公司收到立案调查通知")["risk"])
        self.assertIn("股东减持", sentiment.analyze("大股东拟减持3%股份")["risk"])
        self.assertIn("退市风险", sentiment.analyze("*ST海润 股价异动")["risk"])
        self.assertIn("监管处罚", sentiment.analyze("SEC probe into accounting")["risk"])

    def test_degree(self):
        a = sentiment.analyze("股价上涨")["score"]
        b = sentiment.analyze("股价小幅上涨")["score"]
        self.assertGreater(a, b)
        self.assertGreater(b, 0)


class MatcherTest(unittest.TestCase):
    def setUp(self):
        self.m = Matcher([
            {"id": 1, "name": "贵州茅台", "code": "600519", "market": "A", "aliases": "茅台", "exclude": "茅台镇旅游", "enabled": 1},
            {"id": 2, "name": "英伟达", "code": "NVDA", "market": "US", "aliases": "NVIDIA", "enabled": 1},
            {"id": 3, "name": "停用的", "code": "", "aliases": "", "enabled": 0},
        ])

    def test_match(self):
        self.assertEqual(self.m.match("茅台批价回落"), [1])
        self.assertEqual(self.m.match("600519 放量"), [1])
        self.assertEqual(self.m.match("1600519"), [])
        self.assertEqual(self.m.match("nvidia earnings"), [2])
        self.assertEqual(self.m.match("NVDA up"), [2])
        self.assertEqual(self.m.match("nvda"), [])  # 短代码区分大小写
        self.assertEqual(self.m.match("茅台镇旅游火爆"), [])
        self.assertEqual(self.m.match("停用的"), [])


class ParseTest(unittest.TestCase):
    def test_rss(self):
        items = parse_feed(RSS)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0]["media"], "证券时报")
        self.assertEqual(items[0]["summary"], "茅台业绩超预期")
        self.assertEqual(items[0]["published_at"], 1791273600)

    def test_atom(self):
        items = parse_feed(ATOM)
        self.assertEqual(items[0]["url"], "https://example.com/catl")
        self.assertEqual(items[0]["media"], "36氪")
        self.assertEqual(items[0]["published_at"], 1791252000)

    def test_times(self):
        self.assertEqual(parse_time("2026-10-06 10:00:00"), 1791252000)  # 北京时间
        self.assertEqual(parse_time("2026-10-06T02:00:00Z"), 1791252000)
        self.assertEqual(parse_time(1791252000000), 1791252000)
        self.assertEqual(parse_time("2026年10月06日 10:00"), 1791252000)
        self.assertAlmostEqual(parse_time("垃圾"), time.time(), delta=5)

    def test_yahoo_symbol(self):
        self.assertEqual(yahoo_symbol({"code": "600519", "market": "A"}), "600519.SS")
        self.assertEqual(yahoo_symbol({"code": "300750", "market": "A"}), "300750.SZ")
        self.assertEqual(yahoo_symbol({"code": "00700", "market": "HK"}), "0700.HK")
        self.assertEqual(yahoo_symbol({"code": "NVDA", "market": "US"}), "NVDA")

    def test_quotes(self):
        q = quotes.parse(TENCENT)
        self.assertEqual(q["sh600519"]["price"], 1500.0)
        self.assertEqual(q["sh600519"]["pct"], 1.35)
        self.assertEqual(q["usNVDA"]["pct"], -3.6)
        self.assertEqual(quotes.tencent_code({"code": "600519", "market": "A"}), "sh600519")
        self.assertEqual(quotes.tencent_code({"code": "300750", "market": "A"}), "sz300750")
        self.assertEqual(quotes.tencent_code({"code": "700", "market": "HK"}), "hk00700")

    def test_fetch_json_jsonp(self):
        with mock.patch.object(netutil, "fetch", return_value=EMSEARCH):
            self.assertEqual(netutil.fetch_json("x")["code"], 0)


class SourcesTest(unittest.TestCase):
    ENT = [{"id": 7, "name": "贵州茅台", "code": "600519", "market": "A"}]

    def _with(self, text=None, js=None):
        p1 = mock.patch.object(netutil, "fetch", return_value=text)
        p2 = mock.patch.object(netutil, "fetch_json", return_value=js)
        p1.start(), p2.start()
        self.addCleanup(mock.patch.stopall)

    def test_sina(self):
        self._with(js=SINA)
        items = sources.get("sina_roll").fetch({}, [])
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["published_at"], 1791100000)

    def test_eastmoney_724(self):
        self._with(js=EM724)
        items = sources.get("eastmoney_724").fetch({}, [])
        self.assertEqual(items[0]["url"], "https://finance.eastmoney.com/a/202610063200000001.html")
        self.assertTrue(items[1]["title"].startswith("央行宣布降准"))

    def test_cls(self):
        self._with(js=CLS)
        items = sources.get("cls_telegraph").fetch({}, [])
        self.assertEqual(items[0]["title"], "某公司收到立案告知书")
        self.assertEqual(items[1]["url"], "https://www.cls.cn/detail/1830002")

    def test_em_search(self):
        with mock.patch.object(netutil, "fetch", return_value=EMSEARCH):
            items = sources.get("eastmoney_search").fetch({}, self.ENT)
        self.assertEqual(items[0]["title"], "贵州茅台获北向资金净买入")
        self.assertEqual(items[0]["entity_ids"], [7])

    def test_google_bing_yahoo(self):
        self._with(text=RSS)
        g = sources.get("google_news").fetch({}, self.ENT)
        self.assertEqual(g[0]["title"], "贵州茅台净利润同比增长15%")
        b = sources.get("bing_news").fetch({}, self.ENT)
        self.assertEqual(b[0]["entity_ids"], [7])
        y = sources.get("yahoo_finance").fetch({"markets": "A"}, self.ENT)
        self.assertEqual(len(y), 2)

    def test_catalog(self):
        types = {c["type"] for c in sources.catalog()}
        self.assertTrue({"rss", "sina_roll", "eastmoney_724", "cls_telegraph", "bing_news"} <= types)


class StoreCollectorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.app = App(Path(self.tmp) / "t.db")
        self.store = self.app.store

    def tearDown(self):
        self.app.collector.stop()
        self.store.close()

    def test_seed(self):
        self.assertGreaterEqual(len(self.store.entities()), 3)
        self.assertGreaterEqual(len(self.store.sources()), 5)

    def test_ingest_dedupe_alerts(self):
        now = int(time.time())
        src = {"id": None, "name": "测试"}
        items = [
            {"title": "贵州茅台收到立案调查通知书", "url": "https://a.com/1", "published_at": now - 60},
            {"title": "贵州茅台收到立案调查通知书 - 某报", "url": "https://b.com/2", "published_at": now - 50},  # 同标题
            {"title": "宁德时代股价暴跌，市场担忧需求疲软", "url": "https://a.com/3", "published_at": now - 40},
            {"title": "市场今日平稳", "url": "https://a.com/4", "published_at": now - 30},
        ]
        new = self.app.collector.ingest(items, src)
        self.assertEqual(len(new), 3)
        kinds = sorted(a["kind"] for a in self.store.alerts())
        self.assertEqual(kinds, ["negative", "risk"])
        self.assertEqual(self.store.articles(matched_only=True)["total"], 2)
        self.assertEqual(self.store.articles(label="neg")["total"], 2)
        self.assertEqual(self.store.articles(q="平稳")["total"], 1)
        ov = self.store.overview()
        self.assertEqual(ov["count_24h"], 3)
        self.assertEqual(ov["unread_alerts"], 2)
        # 再次抓到同一条：不重复入库、不重复预警
        self.assertEqual(self.app.collector.ingest(items[:1], src), [])
        self.assertEqual(len(self.store.alerts()), 2)

    def test_settings(self):
        cfg = self.store.save_settings({"exit_grace_sec": "5", "auto_exit": False, "unknown": 1, "spike_ratio": "2.5"})
        self.assertEqual(cfg["exit_grace_sec"], 5)
        self.assertIs(cfg["auto_exit"], False)
        self.assertEqual(cfg["spike_ratio"], 2.5)
        self.assertNotIn("unknown", cfg)

    def test_trend_and_cleanup(self):
        now = int(time.time())
        self.app.collector.ingest([{"title": f"测试资讯{i}上涨", "published_at": now - i * 86400} for i in range(5)], {"name": "t"})
        tr = self.store.trend(7, tz_offset_min=480, now=now)
        self.assertEqual(len(tr), 7)
        self.assertEqual(sum(d["pos"] for d in tr), 5)
        self.store._x("UPDATE articles SET fetched_at=? WHERE title LIKE '测试资讯4%'", (now - 40 * 86400,))
        self.assertEqual(self.store.cleanup(30), 1)

    def test_run_source_failure_recorded(self):
        sid = self.store.save_source({"type": "rss", "name": "坏源", "params": {"url": "http://x"}})["id"]
        with mock.patch.object(netutil, "fetch", side_effect=netutil.FetchError("请求超时")):
            r = self.app.collector.run_source(sid)
        self.assertFalse(r["ok"])
        self.assertEqual(self.store.source(sid)["last_error"], "请求超时")

    def test_per_entity_partial_failure(self):
        sid = self.store.save_source({"type": "bing_news", "name": "b"})["id"]
        calls = {"n": 0}

        def fake(url, headers=None, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise netutil.FetchError("HTTP 429")
            return RSS

        with mock.patch.object(netutil, "fetch", side_effect=fake):
            r = self.app.collector.run_source(sid)
        self.assertTrue(r["ok"])
        self.assertIn("HTTP 429", r["warning"])

    def test_demo(self):
        from monitor.demo import seed_demo

        self.assertGreater(seed_demo(self.app, seed=1), 50)


class ServerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.app = App(Path(cls.tmp) / "s.db")
        cls.httpd = cls.app.make_server(0)
        cls.base = f"http://127.0.0.1:{cls.app.port}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.app.collector.stop()
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def req(self, path, method="GET", body=None, headers=None):
        h = {"X-Monitor": "1", **(headers or {})}
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_static_and_api(self):
        st, body = self.req("/")
        self.assertEqual(st, 200)
        self.assertIn("舆情监控".encode(), body)
        st, body = self.req("/api/bootstrap")
        self.assertEqual(json.loads(body)["version"], __import__("monitor").__version__)
        st, _ = self.req("/../../etc/passwd")
        self.assertEqual(st, 404)

    def test_csrf_and_host(self):
        st, _ = self.req("/api/refresh", "POST", {}, headers={"X-Monitor": ""})
        self.assertEqual(st, 403)
        st, _ = self.req("/api/ping", headers={"Host": "evil.com"})
        self.assertEqual(st, 403)

    def test_entity_crud_and_export(self):
        st, body = self.req("/api/entities", "POST", {"name": "比亚迪", "code": "002594", "market": "A", "aliases": "BYD，比亚迪汽车"})
        e = json.loads(body)
        self.assertEqual(e["aliases"], "BYD,比亚迪汽车")
        st, _ = self.req("/api/entities", "POST", {"name": " "})
        self.assertEqual(st, 400)
        self.app.collector.ingest([{"title": "比亚迪销量大涨", "url": "https://x.com/byd"}], {"name": "t"})
        st, body = self.req(f"/api/articles?entity={e['id']}")
        self.assertEqual(json.loads(body)["total"], 1)
        st, body = self.req("/api/export?days=0")
        self.assertTrue(body.startswith("﻿".encode()))
        self.assertIn("比亚迪销量大涨".encode(), body)
        st, _ = self.req(f"/api/entities/{e['id']}", "DELETE")
        self.assertEqual(st, 200)

    def test_sse_counts_clients(self):
        import http.client

        c = http.client.HTTPConnection("127.0.0.1", self.app.port, timeout=5)
        c.request("GET", "/api/events")
        r = c.getresponse()
        line = r.fp.readline()
        self.assertEqual(line.strip(), b"event: hello")
        self.assertEqual(self.app.bus.clients(), 1)
        c.close()
        r.close()
        deadline = time.time() + 8
        while self.app.bus.clients() and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual(self.app.bus.clients(), 0)


class WatchdogTest(unittest.TestCase):
    def test_rules(self):
        bus = EventBus()
        br = BrowserWindow()
        cfg = {"auto_exit": True, "exit_grace_sec": 5}
        wd = Watchdog(bus, br, lambda: cfg, lambda r: None)
        self.assertEqual(wd.check(), "")  # 还没有页面连上：不退出
        q = bus.subscribe()
        self.assertEqual(wd.check(), "")
        bus.unsubscribe(q)
        self.assertEqual(wd.check(time.time() + 1), "")  # 宽限期内（可能是刷新）
        self.assertIn("页面已全部关闭", wd.check(time.time() + 6))
        cfg["auto_exit"] = False
        self.assertEqual(wd.check(time.time() + 60), "")


class LifecycleEndToEndTest(unittest.TestCase):
    """真实启动 run.py：页面连上后断开，后台应在宽限期后自动退出。"""

    def test_exit_when_page_closed(self):
        data = tempfile.mkdtemp()
        s = Store(Path(data) / "monitor.db")
        s.save_settings({"exit_grace_sec": 3})
        for src in s.sources():  # 测试环境不联网
            s._x("UPDATE sources SET enabled=0 WHERE id=?", (src["id"],))
        s.close()
        env = {**os.environ, "MONITOR_DATA_DIR": data, "PYTHONIOENCODING": "utf-8"}
        p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--no-browser", "--port", "0"], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        try:
            port = None
            for _ in range(50):
                line = p.stdout.readline()
                if "页面地址" in line:
                    port = int(line.strip().rstrip("/").rsplit(":", 1)[1])
                    break
            self.assertIsNotNone(port, "没有拿到端口")
            import http.client

            c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            c.request("GET", "/api/events")
            r = c.getresponse()
            self.assertEqual(r.fp.readline().strip(), b"event: hello")
            time.sleep(1)
            self.assertIsNone(p.poll(), "页面连着时不应退出")
            c.close()
            r.close()
            p.wait(timeout=20)
            self.assertEqual(p.returncode, 0)
        finally:
            if p.poll() is None:
                p.kill()
            p.stdout.close()


if __name__ == "__main__":
    unittest.main()
