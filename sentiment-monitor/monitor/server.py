"""本机 HTTP 服务（只监听 127.0.0.1）：静态页面 + JSON 接口 + SSE 实时推送。"""

from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import queue
import re
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import APP_ID, APP_NAME, __version__, catalog, guide, paths, procinfo, rsshub, sources
from .collector import Collector
from .db import Store
from .events import EventBus
from .quotes import QuoteCache

log = logging.getLogger("monitor.server")

MAX_BODY = 2 * 1024 * 1024
KINDS = [
    {"id": "stock", "name": "股票"}, {"id": "topic", "name": "行业/主题"},
    {"id": "company", "name": "公司/品牌"}, {"id": "person", "name": "人物"},
]
MARKETS = [{"id": "A", "name": "A股"}, {"id": "HK", "name": "港股"}, {"id": "US", "name": "美股"}, {"id": "", "name": "无"}]


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self, db_path=None):
        self.store = Store(db_path or paths.db_path())
        self.bus = EventBus()
        self.collector = Collector(self.store, self.bus)
        self.quotes = QuoteCache()
        self.routes = []
        self.httpd = None
        self.port = 0
        self.on_shutdown = []
        self.browser = None       # lifecycle.BrowserWindow（run.py 设置）
        self.browser_mode = ""   # edge/chrome/default/none
        self.native_window = False  # v0.4：True = 用 pywebview 原生窗口（窗口在本进程里）
        self._stopping = threading.Event()
        self._health_lock = threading.Lock()
        self._final_lock = threading.Lock()
        self._finalized = False
        self._register()

    # ------------------------------------------------------------ 关闭
    def shutdown(self, reason="用户退出"):
        if self._stopping.is_set():
            return
        self._stopping.set()
        log.info("正在退出：%s", reason)
        self.bus.publish("shutdown", {"reason": reason})
        self.collector.stop()

        def later():
            time.sleep(0.6)  # 让 SSE 把 shutdown 事件发出去
            for cb in self.on_shutdown:
                try:
                    cb()
                except Exception:  # noqa: BLE001
                    log.exception("shutdown callback")
            if self.httpd:
                self.httpd.shutdown()

        threading.Thread(target=later, daemon=True).start()

    def stop_now(self, reason="用户退出"):
        """同步退出：走完 shutdown() 的步骤后，当场停 HTTP 服务、关数据库再返回。
        给原生窗口用：macOS 上 ⌘Q / 程序坞「退出」时，系统在窗口的 closing 事件返回后就直接结束进程，
        不会再回到 run.py 的收尾代码，所以收尾必须在这里同步做完。可以重复调用。"""
        with self._final_lock:
            if self._finalized:
                return
            self._finalized = True
        self.shutdown(reason)
        if self.httpd:
            self.httpd.shutdown()  # 等 serve_forever 的循环退出（最多约 0.5 秒）
        self.store.close()
        log.info("已退出（%s）：采集已停、服务已停、数据库已关闭", reason)

    @property
    def stopping(self):
        return self._stopping.is_set()

    # ------------------------------------------------------------ 路由
    def route(self, method, pattern):
        def deco(fn):
            self.routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn

        return deco

    def _register(self):
        r = self.route
        ID = r"(?P<id>\d+)"

        @r("GET", "/api/ping")
        def ping(req):
            return {"app": APP_ID, "version": __version__}

        @r("GET", "/api/bootstrap")
        def bootstrap(req):
            return {
                "app": APP_NAME, "version": __version__, "settings": self.store.settings(),
                "source_types": sources.catalog(), "kinds": KINDS, "markets": MARKETS,
                "category_names": rsshub.CATEGORY_NAMES, "native_window": self.native_window,
                "data_dir": str(paths.data_dir()), "db": self.store.path,
            }

        @r("GET", "/api/overview")
        def overview(req):
            tz = req.qi("tz", 0)
            days = min(max(req.qi("days", 14), 3), 90)
            now = int(time.time())
            return {
                "overview": self.store.overview(now),
                "trend": self.store.trend(days, tz_offset_min=tz, now=now),
                "sources": self.store.source_dist(now - 86400),
                "entities": self.store.entity_board(now),
                "alerts": self.store.alerts(limit=8),
            }

        @r("GET", "/api/articles")
        def articles(req):
            days = req.qi("days", 0)
            return self.store.articles(
                entity_id=req.qi("entity", 0) or None, label=req.qs("label"), source_id=req.qi("source", 0) or None,
                q=req.qs("q").strip() or None, since=int(time.time()) - days * 86400 if days else None,
                risk_only=req.qs("risk") == "1", matched_only=req.qs("matched") == "1",
                limit=min(req.qi("limit", 50), 500), offset=req.qi("offset", 0),
            )

        @r("GET", f"/api/articles/{ID}")
        def article(req, id):
            a = self.store.article(int(id))
            if not a:
                raise ApiError(404, "资讯不存在")
            return a

        @r("GET", "/api/export")
        def export(req):
            days = req.qi("days", 7)
            res = self.store.articles(
                entity_id=req.qi("entity", 0) or None, label=req.qs("label"), source_id=req.qi("source", 0) or None,
                q=req.qs("q").strip() or None, since=int(time.time()) - days * 86400 if days else None,
                risk_only=req.qs("risk") == "1", matched_only=req.qs("matched") == "1", limit=20000,
            )
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["发布时间", "标题", "情感", "得分", "风险标签", "监控对象", "来源", "媒体", "链接", "摘要"])
            lab = {"pos": "利好", "neg": "利空", "neu": "中性"}
            for a in res["items"]:
                w.writerow([
                    time.strftime("%Y-%m-%d %H:%M", time.localtime(a["published_at"])), a["title"], lab.get(a["label"], ""),
                    a["score"], a["risk"], "、".join(e["name"] for e in a["entities"]), a["source_name"], a["media"], a["url"], a["summary"][:300],
                ])
            data = ("﻿" + buf.getvalue()).encode("utf-8")  # 带 BOM，Excel 直接打开不乱码
            fname = urllib.parse.quote(f"舆情导出_{time.strftime('%Y%m%d_%H%M')}.csv")
            return Raw(data, "text/csv; charset=utf-8", {"Content-Disposition": f"attachment; filename*=UTF-8''{fname}"})

        # ---- 监控对象
        @r("GET", "/api/entities")
        def entities(req):
            return self.store.entity_board()

        @r("POST", "/api/entities")
        def save_entity(req):
            try:
                return self.store.save_entity(req.json())
            except ValueError as e:
                raise ApiError(400, str(e)) from e

        @r("DELETE", f"/api/entities/{ID}")
        def del_entity(req, id):
            self.store.delete_entity(int(id))
            return {"ok": True}

        @r("GET", f"/api/entities/{ID}/trend")
        def entity_trend(req, id):
            return self.store.trend(min(max(req.qi("days", 14), 3), 90), entity_id=int(id), tz_offset_min=req.qi("tz", 0))

        @r("GET", "/api/quotes")
        def quotes(req):
            cfg = self.store.settings()
            q = self.quotes.get(self.store.entities(enabled_only=True), max_age=cfg["quote_refresh_sec"])
            return {"quotes": {str(k): v for k, v in q.items()}, "error": self.quotes.error}

        # ---- 数据源
        @r("GET", "/api/sources")
        def list_sources(req):
            rows = self.store.sources()
            for s in rows:
                s["running"] = s["id"] in self.collector.running
            return rows

        @r("POST", "/api/sources")
        def save_source(req):
            body = req.json()
            if body.get("type") not in sources.REGISTRY:
                raise ApiError(400, "未知数据源类型")
            try:
                return self.store.save_source(body)
            except (ValueError, json.JSONDecodeError) as e:
                raise ApiError(400, str(e)) from e

        @r("DELETE", f"/api/sources/{ID}")
        def del_source(req, id):
            self.store.delete_source(int(id))
            return {"ok": True}

        @r("POST", f"/api/sources/{ID}/run")
        def run_source(req, id):
            if not self.store.source(int(id)):
                raise ApiError(404, "数据源不存在")
            self.collector.run_now(int(id))
            return {"ok": True}

        @r("POST", "/api/sources/test")
        def test_source(req):
            body = req.json()
            try:
                src = sources.get(body.get("type", ""))
            except KeyError as e:
                raise ApiError(400, str(e)) from e
            # 按监控对象的源用前 5 个对象测：有的源只管某个市场（如 SEC 只查美股），只拿第 1 个可能测出 0 条
            ents = self.store.entities(enabled_only=True)[:5] if src.per_entity else []
            if src.per_entity and not ents:
                raise ApiError(400, "这个数据源按监控对象搜索，请先添加一个监控对象")
            t0 = time.time()
            try:
                items = src.fetch(body.get("params") or {}, ents)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e) or type(e).__name__, "ms": int((time.time() - t0) * 1000)}
            return {"ok": True, "count": len(items), "ms": int((time.time() - t0) * 1000),
                    "items": [{k: it.get(k) for k in ("title", "url", "media", "published_at")} for it in items[:8]],
                    "tested_with": "、".join(e["name"] for e in ents)}

        # ---- 订阅目录（v0.3）
        @r("GET", "/api/rsshub/status")
        def rsshub_status(req):
            return rsshub.status()

        @r("POST", "/api/rsshub/start")
        def rsshub_start(req):
            return rsshub.start()

        @r("GET", "/api/rsshub/update")
        def rsshub_update_status(req):
            return rsshub.update_status()

        @r("POST", "/api/rsshub/check")
        def rsshub_check(req):
            return rsshub.check_behind()

        @r("POST", "/api/rsshub/update")
        def rsshub_update(req):
            return rsshub.start_update()

        @r("GET", "/api/catalog/categories")
        def catalog_categories(req):
            items, err = catalog.all_items(force=req.qs("reload") == "1")
            return {"categories": catalog.categories(items), "total": len(items), "error": err,
                    "direct": len(catalog.DIRECT), "rsshub": len(items) - len(catalog.DIRECT)}

        @r("GET", "/api/catalog")
        def catalog_list(req):
            items, err = catalog.all_items()
            total, page = catalog.search(
                items, cat=req.qs("cat"), q=req.qs("q"), lang=req.qs("lang"), only_ready=req.qs("ready") == "1",
                direct_only=req.qs("direct") == "1", only_tested=req.qs("tested") == "1", limit=min(500, req.qi("limit", 200)), offset=req.qi("offset", 0))
            subscribed = {}
            for s in self.store.sources():
                subscribed[s["catalog_key"] or catalog.feed_key(s["type"], s["params"])] = s["id"]
            tested = catalog.tested()
            health = self._catalog_health()
            out = []
            for it in page:
                feed = catalog.default_feed(it)
                fk = catalog.feed_key(feed["type"], feed["params"]) if feed else ""
                out.append({**it, "feed": feed, "feed_key": fk, "subscribed": subscribed.get(fk) if fk else None,
                            "tested": tested.get(fk), "health": health.get(fk)})
            return {"total": total, "items": out, "error": err}

        @r("POST", "/api/catalog/subscribe")
        def catalog_subscribe(req):
            """批量订阅：[{type, params, name, category, interval, only_matched, enabled}]，已订阅的跳过。"""
            have = {s["catalog_key"] or catalog.feed_key(s["type"], s["params"]) for s in self.store.sources()}
            added = []
            for it in (req.json().get("items") or [])[:500]:
                if it.get("type") not in sources.REGISTRY:
                    continue
                params = dict(it.get("params") or {})
                fk = catalog.feed_key(it["type"], params)
                if fk in have:
                    continue
                if it.get("only_matched"):
                    params["only_matched"] = "1"
                row = self.store.save_source({
                    "type": it["type"], "name": it.get("name") or fk, "params": params,
                    "enabled": it.get("enabled", True), "interval_min": it.get("interval") or 30,
                    "category": it.get("category") or "", "catalog_key": fk,
                })
                have.add(fk)
                added.append(row["id"])
            for sid in added:
                self.collector.run_now(sid)
            return {"ok": True, "added": len(added)}

        @r("POST", "/api/catalog/health")
        def catalog_health_save(req):
            """记录用户在目录里点「测试」的结果，下次打开目录仍能看到。"""
            body = req.json()
            fk = body.get("feed_key") or ""
            if fk:
                # 「测试本页」会并发发来多个请求：读-改-写要加锁，否则后写的会覆盖先写的
                with self._health_lock:
                    h = self._catalog_health()
                    h[fk] = {"ok": bool(body.get("ok")), "n": int(body.get("count") or 0), "at": int(time.time()),
                             "error": str(body.get("error") or "")[:200]}
                    self.store.kv_set("catalog_health", json.dumps(h, ensure_ascii=False))
            return {"ok": True}

        @r("POST", "/api/notify/test")
        def notify_test(req):
            """原生窗口模式：发一条测试系统通知（macOS 第一次会请求通知权限）。"""
            if not self.collector.notifier:
                return {"ok": False, "error": "当前不是原生窗口模式，请在浏览器里允许通知"}
            self.collector.notifier("舆情监控", "这是一条测试通知：预警会这样提醒你")
            return {"ok": True}

        @r("POST", "/api/refresh")
        def refresh(req):
            self.collector.run_now()
            return {"ok": True}

        # ---- 预警
        @r("GET", "/api/alerts")
        def alerts(req):
            return self.store.alerts(limit=min(req.qi("limit", 200), 1000), unread_only=req.qs("unread") == "1")

        @r("POST", "/api/alerts/read")
        def alerts_read(req):
            self.store.mark_alerts_read(req.json().get("ids"))
            return {"unread": self.store.overview()["unread_alerts"]}

        # ---- 设置 / 维护
        @r("GET", "/api/settings")
        def get_settings(req):
            return self.store.settings()

        @r("POST", "/api/settings")
        def save_settings(req):
            cfg = self.store.save_settings(req.json())
            self.collector.apply_settings()
            return cfg

        @r("POST", "/api/demo")
        def demo(req):
            from .demo import seed_demo

            n = seed_demo(self)
            return {"ok": True, "count": n}

        @r("POST", "/api/clear")
        def clear(req):
            self.store.clear_articles()
            return {"ok": True}

        # ---- 使用指南：文档、源码（白名单）、运行状态
        @r("GET", "/api/guide/docs")
        def guide_docs(req):
            return {"docs": guide.list_docs(), "assets": guide.list_doc_assets()}

        @r("GET", "/api/guide/doc")
        def guide_doc(req):
            f = guide.resolve_doc_file(req.qs("path"))
            if not f or f.suffix.lower() != ".md":
                raise ApiError(404, "文档不存在")
            return {"path": req.qs("path"), "text": f.read_text(encoding="utf-8")}

        @r("GET", "/api/guide/sources")
        def guide_sources(req):
            return {"files": guide.list_sources(), "version": __version__}

        @r("GET", "/api/guide/source")
        def guide_source(req):
            text = guide.read_source(req.qs("path"))
            if text is None:
                raise ApiError(404, "不在本应用源码白名单内")
            return {"path": req.qs("path"), "text": text}

        @r("GET", "/api/guide/source.zip")
        def guide_zip(req):
            fname = f"sentiment-monitor-{__version__}-source.zip"
            return Raw(guide.source_zip(__version__), "application/zip", {"Content-Disposition": f'attachment; filename="{fname}"'})

        @r("GET", "/api/processes")
        def processes(req):
            return procinfo.snapshot(self.browser, self.browser_mode, self.native_window)

        @r("POST", "/api/shutdown")
        def shutdown(req):
            self.shutdown("在页面点了退出")
            return {"ok": True}

    # ------------------------------------------------------------ 服务
    def _catalog_health(self) -> dict:
        try:
            return json.loads(self.store.kv_get("catalog_health", "{}"))
        except json.JSONDecodeError:
            return {}

    def make_server(self, port=0):
        app = self

        class Handler(RequestHandler):
            pass

        Handler.app = app
        self.httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        return self.httpd


class Raw:
    def __init__(self, data: bytes, ctype: str, headers=None):
        self.data, self.ctype, self.headers = data, ctype, headers or {}


class RequestHandler(BaseHTTPRequestHandler):
    app: App = None
    protocol_version = "HTTP/1.1"
    server_version = "SentimentMonitor"

    def log_message(self, fmt, *args):  # 安静
        pass

    # ---- 请求辅助
    def qs(self, key, default=""):
        return self._query.get(key, [default])[0]

    def qi(self, key, default=0):
        try:
            return int(self.qs(key, str(default)))
        except ValueError:
            return default

    def json(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_BODY:
            raise ApiError(413, "请求太大")
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError as e:
            raise ApiError(400, "JSON 格式错误") from e

    def _send(self, status, body: bytes, ctype, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status, obj):
        self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _host_ok(self):
        # 防 DNS 重绑定：只接受 127.0.0.1 / localhost
        host = (self.headers.get("Host") or "").split(":")[0]
        return host in ("127.0.0.1", "localhost")

    # ---- 分发
    def do_GET(self):
        self._dispatch("GET")

    def do_HEAD(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method):
        u = urllib.parse.urlparse(self.path)
        self._query = urllib.parse.parse_qs(u.query)
        path = u.path
        if not self._host_ok():
            return self._json(403, {"error": "forbidden host"})
        if method != "GET" and self.headers.get("X-Monitor") != "1":
            # 自定义请求头：别的网站无法跨域伪造（会触发 CORS 预检被拒）
            return self._json(403, {"error": "missing header"})
        if path == "/api/events":
            return self._sse()
        if not path.startswith("/api/"):
            return self._static(path)
        for m, rx, fn in self.app.routes:
            if m != method:
                continue
            mt = rx.match(path)
            if mt:
                try:
                    res = fn(self, **mt.groupdict())
                except ApiError as e:
                    return self._json(e.status, {"error": str(e)})
                except Exception as e:  # noqa: BLE001
                    log.exception("接口出错 %s", path)
                    return self._json(500, {"error": f"{type(e).__name__}: {e}"})
                if isinstance(res, Raw):
                    return self._send(200, res.data, res.ctype, res.headers)
                return self._json(200, res)
        self._json(404, {"error": "not found"})

    def _static(self, path):
        if path.startswith("/guide-file/"):
            f = guide.resolve_doc_file(urllib.parse.unquote(path[len("/guide-file/"):]))
            if not f:
                return self._json(404, {"error": "not found"})
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            if ctype.startswith("text/"):
                ctype += "; charset=utf-8"
            return self._send(200, f.read_bytes(), ctype)
        if path in ("", "/"):
            path = "/index.html"
        root = paths.web_dir().resolve()
        f = (root / path.lstrip("/")).resolve()
        if root not in f.parents or not f.is_file():
            return self._json(404, {"error": "not found"})
        ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self._send(200, f.read_bytes(), ctype)

    def _sse(self):
        bus = self.app.bus
        q = bus.subscribe()
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.end_headers()
            self._emit("hello", {"version": __version__, "clients": bus.clients()})
            while not self.app.stopping:
                try:
                    kind, data = q.get(timeout=3)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")  # 写失败即说明页面已关闭
                    self.wfile.flush()
                    continue
                self._emit(kind, data)
            # 退出前把剩余事件（包含 shutdown）发出去
            while True:
                try:
                    kind, data = q.get_nowait()
                except queue.Empty:
                    break
                self._emit(kind, data)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            bus.unsubscribe(q)

    def _emit(self, kind, data):
        payload = json.dumps(data, ensure_ascii=False)
        self.wfile.write(f"event: {kind}\ndata: {payload}\n\n".encode("utf-8"))
        self.wfile.flush()
