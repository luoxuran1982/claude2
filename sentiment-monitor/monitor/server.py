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

from . import APP_ID, APP_NAME, __version__, paths, sources
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
        self._stopping = threading.Event()
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
            ents = self.store.entities(enabled_only=True)[:1] if src.per_entity else []
            if src.per_entity and not ents:
                raise ApiError(400, "这个数据源按监控对象搜索，请先添加一个监控对象")
            t0 = time.time()
            try:
                items = src.fetch(body.get("params") or {}, ents)
            except Exception as e:  # noqa: BLE001
                return {"ok": False, "error": str(e) or type(e).__name__, "ms": int((time.time() - t0) * 1000)}
            return {"ok": True, "count": len(items), "ms": int((time.time() - t0) * 1000),
                    "items": [{k: it.get(k) for k in ("title", "url", "media", "published_at")} for it in items[:8]],
                    "tested_with": ents[0]["name"] if ents else ""}

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

        @r("POST", "/api/shutdown")
        def shutdown(req):
            self.shutdown("在页面点了退出")
            return {"ok": True}

    # ------------------------------------------------------------ 服务
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
