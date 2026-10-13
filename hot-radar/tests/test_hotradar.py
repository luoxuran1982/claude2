"""热点雷达外壳的单元测试与集成测试（全部离线，不访问 newsnow）。

    python -m unittest discover -s tests -v
需要已安装 requirements.txt 里的依赖（集成测试会真正运行上游 TrendRadar 的离线演示和 MCP 服务）。
"""
from __future__ import annotations

import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hotradar import __version__, configio, guide, keywords, paths, procinfo, reports  # noqa: E402
from hotradar.runner import INTERVALS, Progress, Runner, worker_command  # noqa: E402
from hotradar.server import serve  # noqa: E402


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TempDir(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hotradar-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)


class PathsTest(unittest.TestCase):
    def test_data_dir_per_platform(self):
        home = Path("/home/u")
        self.assertEqual(paths.data_dir("darwin", {}, home), home / "Library" / "Application Support" / "HotRadar")
        self.assertEqual(paths.data_dir("win32", {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, home),
                         Path(r"C:\Users\u\AppData\Local") / "HotRadar")
        self.assertEqual(paths.data_dir("linux", {}, home), home / ".local" / "share" / "HotRadar")
        self.assertEqual(paths.data_dir("win32", {"HOTRADAR_DATA": "/x"}, home), Path("/x"))

    def test_workspaces_are_separate(self):
        r = Path("/d")
        self.assertEqual(paths.workspace_dir("main", r), r)
        self.assertEqual(paths.workspace_dir("demo", r), r / "demo")
        with self.assertRaises(ValueError):
            paths.workspace_dir("../x", r)


class ConfigTest(TempDir):
    def setUp(self):
        super().setUp()
        configio.ensure_workspace(self.tmp)

    def test_ensure_workspace_copies_once_and_keeps_user_edits(self):
        kw = self.tmp / "config" / "frequency_words.txt"
        kw.write_text("我的关键词\n", encoding="utf-8")
        self.assertEqual(configio.ensure_workspace(self.tmp), [])
        self.assertEqual(kw.read_text(encoding="utf-8"), "我的关键词\n")
        self.assertTrue((self.tmp / "version").is_file())
        self.assertEqual((self.tmp / configio.MARKER).read_text(encoding="utf-8"), __version__)

    def test_prompts_refresh_on_version_change_with_backup(self):
        p = self.tmp / "config" / "ai_analysis_prompt.txt"
        p.write_text("旧提示词", encoding="utf-8")
        (self.tmp / configio.MARKER).write_text("0.0.1", encoding="utf-8")
        configio.ensure_workspace(self.tmp)
        self.assertNotEqual(p.read_text(encoding="utf-8"), "旧提示词")
        self.assertEqual(p.with_name(p.name + ".bak").read_text(encoding="utf-8"), "旧提示词")

    def test_update_keeps_comments_and_section_order(self):
        v = configio.view(self.tmp)["values"]
        plats = v["platforms.sources"] + [{"id": "36kr", "name": "36氪", "expected_domain": "36kr.com", "enabled": True}]
        plats[0]["enabled"] = False
        configio.update(self.tmp, {"report.mode": "daily", "platforms.sources": plats, "ai.model": "openai/gpt-4o",
                                   "display.standalone.platforms": ["zhihu"]})
        text = configio.config_path(self.tmp).read_text(encoding="utf-8")
        self.assertIn('mode: "daily"                       # 报告模式', text)
        self.assertIn('model: "openai/gpt-4o"', text)
        self.assertIn('platforms: ["zhihu"]', text)
        # 新增的平台排在「3. 数据源 - RSS 订阅」注释之前，不会跑到下一节里
        self.assertLess(text.index('id: "36kr"'), text.index("# 3. 数据源 - RSS 订阅"))
        self.assertTrue((self.tmp / "config" / "config.yaml.bak").is_file())
        v2 = configio.view(self.tmp)["values"]
        self.assertFalse(v2["platforms.sources"][0]["enabled"])
        self.assertEqual(v2["platforms.sources"][-1]["id"], "36kr")
        # 上游自己的加载器能读
        import yaml
        data = yaml.safe_load(text)
        self.assertEqual(data["report"]["mode"], "daily")

    def test_rss_feed_comment_kept_when_last_removed(self):
        feeds = configio.view(self.tmp)["values"]["rss.feeds"][:2]
        configio.update(self.tmp, {"rss.feeds": feeds})
        text = configio.config_path(self.tmp).read_text(encoding="utf-8")
        self.assertIn("enabled: false                  # 禁用", text)
        self.assertIn("# 自定义源示例", text)
        self.assertNotIn("yahoo-finance", text)

    def test_validation_rejects_bad_values_and_writes_nothing(self):
        before = configio.config_path(self.tmp).read_bytes()
        for changes in ({"report.mode": "weekly"}, {"rss.freshness_filter.max_age_days": -1},
                        {"platforms.api_url": "ftp://x"}, {"ai.api_key": "sk-123"}, {"app.timezone": "Mars/Base"},
                        {"platforms.sources": [{"id": "a b", "name": "x"}]},
                        {"platforms.sources": [{"id": "a", "name": "x"}, {"id": "a", "name": "y"}]},
                        {"rss.feeds": [{"id": "f", "name": "f", "url": "javascript:alert(1)"}]}):
            with self.assertRaises(configio.ConfigError, msg=str(changes)):
                configio.update(self.tmp, changes)
        self.assertEqual(configio.config_path(self.tmp).read_bytes(), before)

    def test_secrets_stay_out_of_config_yaml(self):
        configio.save_secrets(self.tmp, {"ai_api_key": "sk-secret-1234567890", "feishu_webhook_url": "https://open.feishu.cn/hook/abc"})
        self.assertNotIn("sk-secret", configio.config_path(self.tmp).read_text(encoding="utf-8"))
        env = configio.secrets_env(self.tmp)
        self.assertEqual(env["AI_API_KEY"], "sk-secret-1234567890")
        self.assertEqual(env["FEISHU_WEBHOOK_URL"], "https://open.feishu.cn/hook/abc")
        view = json.dumps(configio.secrets_view(self.tmp, self.tmp), ensure_ascii=False)
        self.assertNotIn("sk-secret-1234567890", view)
        self.assertIn("…7890", view)
        configio.save_secrets(self.tmp, {"ai_api_key": None})
        self.assertNotIn("AI_API_KEY", configio.secrets_env(self.tmp))
        with self.assertRaises(configio.ConfigError):
            configio.save_secrets(self.tmp, {"unknown": "x"})

    def test_demo_workspace_turns_off_network_features(self):
        demo = self.tmp / "demo"
        configio.prepare_demo_workspace(self.tmp, demo)
        v = configio.view(demo)["values"]
        self.assertFalse(v["rss.enabled"])
        self.assertFalse(v["notification.enabled"])
        self.assertFalse(v["ai_analysis.enabled"])
        self.assertEqual(v["filter.method"], "keyword")

    def test_timeline_presets(self):
        ids = [p["id"] for p in configio.timeline_presets(self.tmp)]
        self.assertIn("morning_evening", ids)


class KeywordsTest(unittest.TestCase):
    TEXT = "[GLOBAL_FILTER]\n震惊\n\n[WORD_GROUPS]\n\n[华为]\n华为\n鸿蒙\n\n+苹果\n+发布会\n\n/\\bAI\\b/ => AI\n"

    def test_parse_uses_upstream_rules(self):
        s = keywords.parse(self.TEXT)
        s.pop("_raw")
        names = [g["name"] for g in s["groups"]]
        self.assertEqual(names, ["华为", "苹果 / 发布会", "AI"])
        self.assertEqual([w["word"] for w in s["global_filters"]], ["震惊"])

    def test_try_match(self):
        r = keywords.try_match(self.TEXT, ["华为发布鸿蒙新版", "震惊！华为", "苹果发布会定档", "苹果降价", "OpenAI 的 AI 新品", "MAIL 系统"])
        got = [(x["matched"], x["groups"]) for x in r["results"]]
        self.assertEqual(got, [(True, ["华为"]), (False, []), (True, ["苹果 / 发布会"]), (False, []), (True, ["AI"]), (False, [])])
        self.assertIn("全局过滤词", r["results"][1]["reason"])

    def test_invalid_regex_warning_is_chinese(self):
        s = keywords.parse("/[坏/\n")
        self.assertTrue(any(w.startswith("警告：正则表达式无效") for w in s["warnings"]), s["warnings"])


class ReportsTest(TempDir):
    def test_resolve_whitelist(self):
        d = self.tmp / "output" / "html" / "2026-10-13"
        d.mkdir(parents=True)
        (d / "09-30.html").write_text("<p>x", encoding="utf-8")
        (d / "rss_09-30.html").write_text("<p>r", encoding="utf-8")
        (self.tmp / "secrets.json").write_text("{}", encoding="utf-8")
        self.assertIsNotNone(reports.resolve("main", "2026-10-13/09-30.html", self.tmp))
        for bad in ("../secrets.json", "2026-10-13/../../secrets.json", "/etc/passwd", "2026-10-13/09-30.htm",
                    "latest/../../secrets.json", "2026-10-13\\09-30.html"):
            self.assertIsNone(reports.resolve("main", bad, self.tmp), bad)
        self.assertIsNone(reports.resolve("other", "2026-10-13/09-30.html", self.tmp))
        lst = reports.list_reports("main", self.tmp)
        self.assertEqual([x["kind"] for x in lst], ["rss", "hotlist"])
        self.assertEqual(lst[1]["time"], "09:30")


class ProgressTest(unittest.TestCase):
    def test_parse_upstream_output(self):
        p = Progress("crawl")
        for line in ["监控平台数量: 3", "开始爬取数据，请求间隔 2000 毫秒", "获取 baidu 成功（最新数据）",
                     "请求 weibo 失败: 403. 3.20秒后重试...", "请求 weibo 失败: HTTPSConnectionPool(host='newsnow.busiyi.world', port=443): Max retries exceeded (Caused by ProxyError('Unable to connect to proxy', OSError('Tunnel connection failed: 403 Forbidden')))", "获取 zhihu 成功（缓存数据）",
                     "成功: ['baidu', 'zhihu'], 失败: ['weibo']", "[RSS] 开始抓取 2 个 RSS 源...", "[RSS] Hacker News: 获取 30 条",
                     "[RSS] 雅虎财经: 请求超时 (15s)", "[RSS] 抓取完成: 1 个源成功, 1 个失败, 共 30 条",
                     "HTML报告已生成: output/html/2026-10-13/09-30.html"]:
            p.feed(line)
        v = p.view(False)
        self.assertEqual(v["platforms_ok"], ["baidu", "zhihu"])
        self.assertEqual(list(v["platforms_failed"]), ["weibo"])
        self.assertTrue(v["platforms_failed"]["weibo"].startswith("网络或代理拒绝连接（403）"), v["platforms_failed"])
        self.assertEqual((v["rss_done"], v["rss_total"]), (2, 2))
        self.assertEqual(v["report"], "output/html/2026-10-13/09-30.html")
        self.assertGreaterEqual(v["percent"], 90)
        self.assertEqual(p.view(True)["percent"], 100)


class GuideTest(unittest.TestCase):
    def test_docs_and_sources_whitelist(self):
        idx = guide.index(ROOT)
        docs = {d["path"] for d in idx["docs"]}
        for n in ("README.md", "UPSTREAM.md", "docs/00 项目阅读入口.md", "docs/01 使用指南.md", "docs/02 软件框架与系统架构.md",
                  "docs/03 源码阅读与重建指南.md", "docs/04 与舆情监控系统的对比.md"):
            self.assertIn(n, docs)
        src = {s["path"]: s["group"] for s in idx["sources"]}
        self.assertEqual(src["hotradar/app.py"], "shell")
        self.assertEqual(src["upstream/trendradar/__main__.py"], "upstream")
        self.assertEqual(src["upstream/LICENSE"], "upstream")
        self.assertNotIn("upstream/trendradar/__pycache__", "".join(src))
        self.assertIsNone(guide.read_source("../CLAUDE.md", ROOT))
        self.assertIsNone(guide.read_doc("docs/../../CLAUDE.md", ROOT))
        self.assertIsNone(guide.read_asset("secrets.json", ROOT))
        self.assertIn("def main", guide.read_source("hotradar/app.py", ROOT))

    def test_source_zip_is_complete(self):
        z = zipfile.ZipFile(io.BytesIO(guide.source_zip(ROOT)))
        names = set(z.namelist())
        for n in ("hot-radar/hotradar/app.py", "hot-radar/upstream/LICENSE", "hot-radar/upstream/trendradar/__main__.py",
                  "hot-radar/LICENSE", "hot-radar/UPSTREAM.md", "hot-radar/docs/01 使用指南.md",
                  "hot-radar/samples/newsnow-sample-2025-12-27.json", "hot-radar/requirements.txt"):
            self.assertTrue(n in names, "zip 里缺少 " + n)
        self.assertFalse(any("__pycache__" in n for n in names))


class ProcInfoTest(unittest.TestCase):
    def test_main_process_listed_with_current_pid(self):
        s = procinfo.snapshot()
        self.assertEqual(s["processes"][0]["pid"], os.getpid())
        self.assertIsNotNone(s["processes"][0]["mem_mib"])
        self.assertEqual(s["unit"], "MiB")


def _request(port, method, path, body=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    data = json.dumps(body).encode() if body is not None else None
    h = {"Host": f"127.0.0.1:{port}", **(headers or {})}
    if data is not None:
        h["Content-Type"] = "application/json"
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    payload = r.read()
    c.close()
    return r.status, payload, dict(r.getheaders())


class ServerTest(TempDir):
    def setUp(self):
        super().setUp()
        configio.ensure_workspace(self.tmp)
        self.app, self.httpd = serve(self.tmp, 0)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.tok = {"X-HotRadar-Token": self.app.token}

    def tearDown(self):
        self.app.runner.shutdown()
        self.httpd.shutdown()
        self.httpd.server_close()
        super().tearDown()

    def test_pages_and_security(self):
        st, body, h = _request(self.port, "GET", "/")
        self.assertEqual(st, 200)
        self.assertIn("热点雷达", body.decode())
        self.assertIn("frame-ancestors 'none'", h["Content-Security-Policy"])
        st, _, _ = _request(self.port, "GET", "/", headers={"Host": "evil.example:80"})
        self.assertEqual(st, 403)
        st, _, _ = _request(self.port, "POST", "/api/quit", {})
        self.assertEqual(st, 403)
        st, _, _ = _request(self.port, "POST", "/api/quit", {}, {"X-HotRadar-Token": "wrong-token"})
        self.assertEqual(st, 403)
        st, _, _ = _request(self.port, "POST", "/api/quit", {}, {**self.tok, "Origin": "http://evil.example"})
        self.assertEqual(st, 403)
        st, _, _ = _request(self.port, "GET", "/report/main/../../secrets.json")
        self.assertEqual(st, 404)
        st, body, _ = _request(self.port, "GET", "/api/status")
        s = json.loads(body)
        self.assertEqual(s["version"], __version__)
        self.assertIn("newsnow", s["network_note"])

    def test_config_roundtrip_via_api(self):
        st, body, _ = _request(self.port, "POST", "/api/config", {"changes": {"report.mode": "incremental"}}, self.tok)
        self.assertEqual(st, 200, body)
        self.assertEqual(json.loads(body)["values"]["report.mode"], "incremental")
        st, body, _ = _request(self.port, "POST", "/api/config", {"changes": {"report.mode": "x"}}, self.tok)
        self.assertEqual(st, 400)
        self.assertIn("只能是", json.loads(body)["error"])
        st, body, _ = _request(self.port, "POST", "/api/secrets", {"changes": {"wework_webhook_url": "https://qyapi.weixin.qq.com/x"}}, self.tok)
        self.assertEqual(st, 200)
        self.assertTrue(json.loads(body)["secrets"]["wework_webhook_url"]["set"])
        self.assertNotIn("qyapi", body.decode())
        st, body, _ = _request(self.port, "POST", "/api/keywords/check", {"text": "华为", "titles": ["华为新机"]}, self.tok)
        self.assertTrue(json.loads(body)["results"][0]["matched"])

    def test_schedule_and_quit(self):
        st, body, _ = _request(self.port, "POST", "/api/schedule", {"enabled": True, "interval_min": 7}, self.tok)
        self.assertEqual(st, 400)
        st, body, _ = _request(self.port, "POST", "/api/schedule", {"enabled": True, "interval_min": 30}, self.tok)
        s = json.loads(body)
        self.assertTrue(s["enabled"])
        self.assertIsNotNone(s["next_at"])
        self.assertEqual(json.loads((self.tmp / "settings.json").read_text(encoding="utf-8"))["auto_interval_min"], 30)
        self.assertIn(30, INTERVALS)
        st, _, _ = _request(self.port, "POST", "/api/quit", {}, self.tok)
        self.assertEqual(st, 200)
        self.assertTrue(self.app.quit_event.is_set())


class WorkerIntegrationTest(TempDir):
    """真正启动工作进程运行上游 TrendRadar（离线演示，不联网）。"""

    def test_demo_run_end_to_end(self):
        r = Runner(self.tmp)
        configio.ensure_workspace(self.tmp)
        res = r.start("demo")
        self.assertTrue(res["started"], res)
        pid = res["job"]["pid"]
        snap = procinfo.snapshot(r.worker_pids(), False, True)
        self.assertIn(pid, [p["pid"] for p in snap["processes"]])
        self.assertFalse(r.start("crawl")["started"])  # 同一时间只跑一个
        deadline = time.time() + 180
        while r.busy() and time.time() < deadline:
            time.sleep(0.5)
        time.sleep(1)
        runs = r.runs()
        self.assertEqual(runs[0]["status"], "ok", (self.tmp / "logs" / runs[0]["log"]).read_text(encoding="utf-8")[-3000:])
        self.assertEqual(len(runs[0]["platforms_ok"]), 11)
        latest = reports.latest("demo", self.tmp)
        self.assertIsNotNone(latest)
        html = reports.resolve("demo", latest["path"], self.tmp).read_text(encoding="utf-8")
        self.assertIn("热点新闻分析", html)
        self.assertIsNone(reports.latest("main", self.tmp))  # 演示数据不进正式数据
        snap = procinfo.snapshot(r.worker_pids(), False, False)
        self.assertNotIn(pid, [p["pid"] for p in snap["processes"]])  # 结束后不再列出
        r.shutdown()

    def test_cancel_stops_worker(self):
        r = Runner(self.tmp)
        configio.ensure_workspace(self.tmp)
        res = r.start("demo")
        self.assertTrue(r.cancel())
        deadline = time.time() + 15
        while r.busy() and time.time() < deadline:
            time.sleep(0.2)
        self.assertFalse(r.busy())
        time.sleep(0.8)
        self.assertEqual(r.runs()[0]["status"], "cancelled")
        import psutil
        self.assertFalse(psutil.pid_exists(res["job"]["pid"]) and psutil.Process(res["job"]["pid"]).status() != psutil.STATUS_ZOMBIE)
        r.shutdown()

    def test_worker_exits_when_parent_dies(self):
        """异常收尾：主进程被强制结束后，工作进程（这里用常驻的 MCP 服务）在几秒内自行退出。"""
        configio.ensure_workspace(self.tmp)
        parent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        port = free_port()
        cmd = worker_command("mcp", self.tmp, self.tmp / "mcp.log", port)
        cmd[cmd.index("--parent") + 1] = str(parent.pid)
        worker = subprocess.Popen(cmd, cwd=self.tmp)
        try:
            ok = False
            for _ in range(120):  # MCP 服务能起来（端口可连）
                time.sleep(0.5)
                with socket.socket() as s:
                    if s.connect_ex(("127.0.0.1", port)) == 0:
                        ok = True
                        break
                if worker.poll() is not None:
                    break
            log = (self.tmp / "mcp.log").read_text(encoding="utf-8", errors="replace")
            self.assertTrue(ok, log[-3000:])
            parent.kill()
            parent.wait()
            self.assertEqual(worker.wait(15), 3, log[-2000:])
        finally:
            for p in (parent, worker):
                if p.poll() is None:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
