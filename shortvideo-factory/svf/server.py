"""本机服务（只监听 127.0.0.1）：外壳页面、外壳接口，以及到 Streamlit 的反向代理。

为什么要反向代理：窗口里的外壳页面（顶栏、使用指南、运行状态）和上游 Streamlit 页面必须同源，
外壳才能把缩放作用到 Streamlit 页面内部、把 Streamlit 自带的英文提示换成中文、把外部链接交给系统浏览器。
所以 Streamlit 在另一个随机端口上以 baseUrlPath=app 运行，本服务把 /app/ 下的 HTTP 和 WebSocket 原样转发过去，
只在返回 Streamlit 首页 HTML 时插入一行 <script src="/ui/inject.js">。

路由：
  /                      外壳页面（index.html，内嵌启动参数）
  /ui/*                  外壳静态文件
  /api/*                 外壳接口（见各 Handler）
  /app/_stcore/stream    WebSocket → Streamlit
  /app/*                 HTTP → Streamlit（流式转发，支持 Range，视频可拖动）
"""
from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

import tornado.httpclient
import tornado.httputil
import tornado.netutil
import tornado.web
import tornado.websocket
from tornado.httpserver import HTTPServer

from . import APP_ID, APP_NAME, UPSTREAM_COMMIT, UPSTREAM_NAME, UPSTREAM_URL, UPSTREAM_VERSION, __version__
from . import guide, procinfo
from .paths import web_dir
from .supervisor import BASE_PATH

log = logging.getLogger("svf")

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
              "transfer-encoding", "upgrade", "proxy-connection"}
INJECT_TAG = b'<link rel="stylesheet" href="/ui/inject.css"><script src="/ui/inject.js"></script>'
MAX_BODY = 1100 * 1024 * 1024   # 上传素材：Streamlit 设了 1024 MB 上限，这里略大一点

tornado.httpclient.AsyncHTTPClient.configure(None, max_body_size=1 << 36, max_buffer_size=1 << 30,
                                             max_clients=64)


class AppState:
    """服务端共享状态。window_* 回调由 app.py 在建好窗口后填上。"""

    def __init__(self, data: Path, webui, settings, token: str):
        self.data, self.webui, self.settings, self.token = data, webui, settings, token
        self.started_at = time.time()
        self.port: int | None = None
        self.quit_event = threading.Event()
        self.on_quit = lambda: self.quit_event.set()
        self.on_focus = lambda: None
        self.window_mode = "服务模式（无窗口）"
        self.last_seen = time.time()

    def paths(self) -> dict:
        d = self.data
        return {"data": str(d), "config": str(d / "config.toml"), "output": str(d / "storage" / "tasks"),
                "fonts": str(d / "fonts"), "songs": str(d / "songs"), "logs": str(d / "logs"),
                "cache": str(d / "storage")}


class Base(tornado.web.RequestHandler):
    state: AppState

    def initialize(self, state: AppState):
        self.state = state

    def prepare(self):
        # 防 DNS 重绑定：只接受用 127.0.0.1 / localhost 访问
        host = (self.request.host or "").rsplit(":", 1)[0].strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise tornado.web.HTTPError(403, "只允许本机访问")
        self.state.last_seen = time.time()

    def check_token(self):
        if self.request.headers.get("X-SVF-Token") != self.state.token:
            raise tornado.web.HTTPError(403, "缺少或错误的令牌")

    def send_json(self, obj, status: int = 200):
        self.set_status(status)
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.finish(json.dumps(obj, ensure_ascii=False))

    def json_body(self) -> dict:
        try:
            return json.loads(self.request.body or b"{}")
        except ValueError:
            raise tornado.web.HTTPError(400, "请求格式不对")

    def write_error(self, status_code, **kwargs):
        msg = self._reason
        if "exc_info" in kwargs:
            exc = kwargs["exc_info"][1]
            if isinstance(exc, tornado.web.HTTPError) and exc.log_message:
                msg = exc.log_message
        self.set_header("Content-Type", "application/json; charset=utf-8")
        self.finish(json.dumps({"error": msg}, ensure_ascii=False))


def boot_info(state: AppState) -> dict:
    return {"app": APP_NAME, "id": APP_ID, "version": __version__, "token": state.token,
            "upstream": {"name": UPSTREAM_NAME, "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT, "url": UPSTREAM_URL},
            "settings": state.settings.values, "platform": sys.platform, "base_path": "/" + BASE_PATH + "/"}


class IndexHandler(Base):
    def get(self):
        html = (web_dir() / "index.html").read_text(encoding="utf-8")
        boot = json.dumps(boot_info(self.state), ensure_ascii=False).replace("</", "<\\/")
        self.set_header("Content-Type", "text/html; charset=utf-8")
        self.set_header("Cache-Control", "no-store")
        self.finish(html.replace("/*__SVF_BOOT__*/null", boot))


class StaticHandler(tornado.web.StaticFileHandler):
    def set_extra_headers(self, path):
        self.set_header("Cache-Control", "no-cache")


class PingHandler(Base):
    def get(self):
        self.send_json({"app": APP_ID, "pid": os.getpid(), "version": __version__})


class StatusHandler(Base):
    def get(self):
        s = self.state
        self.send_json({"app": APP_NAME, "version": __version__, "pid": os.getpid(), "port": s.port,
                        "started_at": s.started_at, "window": s.window_mode, "platform": sys.platform,
                        "upstream": {"name": UPSTREAM_NAME, "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT},
                        "webui": s.webui.status(), "paths": s.paths()})


class ProcessesHandler(Base):
    def get(self):
        w = self.state.webui
        self.send_json(procinfo.snapshot(w.pid(), w.state))


class SettingsHandler(Base):
    def get(self):
        self.send_json(self.state.settings.values)

    def post(self):
        self.check_token()
        self.send_json(self.state.settings.update(self.json_body()))


class QuitHandler(Base):
    def post(self):
        self.check_token()
        self.send_json({"ok": True, "message": "正在退出：停止生成界面后台和全部子进程"})
        threading.Timer(0.2, self.state.on_quit).start()


class FocusHandler(Base):
    def post(self):
        self.check_token()
        self.state.on_focus()
        self.send_json({"ok": True})


class RestartHandler(Base):
    def post(self):
        self.check_token()
        threading.Thread(target=self.state.webui.restart, name="webui-restart", daemon=True).start()
        self.send_json({"ok": True})


def open_path(target: str) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", target])
    elif sys.platform == "win32":
        os.startfile(target)  # noqa: S606 —— 打开本机文件夹
    else:
        subprocess.Popen(["xdg-open", target])


class OpenHandler(Base):
    """打开数据文件夹（Finder / 资源管理器）或用系统浏览器打开外部网址。"""

    def post(self):
        self.check_token()
        body = self.json_body()
        url = str(body.get("url") or "")
        if url:
            if not url.startswith(("https://", "http://")):
                raise tornado.web.HTTPError(400, "只能打开 http/https 网址")
            webbrowser.open(url)
            return self.send_json({"ok": True})
        paths = self.state.paths()
        what = body.get("what")
        if what not in paths:
            raise tornado.web.HTTPError(400, "未知的文件夹")
        target = Path(paths[what])
        if what != "config":
            target.mkdir(parents=True, exist_ok=True)
        open_path(str(target if target.exists() else target.parent))
        self.send_json({"ok": True, "path": str(target)})


class GuideIndexHandler(Base):
    def get(self):
        self.send_json(guide.index())


class GuideTextHandler(Base):
    def initialize(self, state: AppState, kind: str):
        super().initialize(state)
        self.kind = kind

    def get(self):
        path = self.get_argument("path", "")
        text = guide.read_doc(path) if self.kind == "doc" else guide.read_source(path)
        if text is None:
            raise tornado.web.HTTPError(404, "没有这个文件")
        self.set_header("Content-Type", "text/plain; charset=utf-8")
        self.finish(text)


class GuideFileHandler(Base):
    def get(self):
        found = guide.read_asset(self.get_argument("path", ""))
        if not found:
            raise tornado.web.HTTPError(404, "没有这个附件")
        data, ctype = found
        self.set_header("Content-Type", ctype)
        self.finish(data)


class SourceZipHandler(Base):
    def get(self):
        self.set_header("Content-Type", "application/zip")
        self.set_header("Content-Disposition", "attachment; filename*=UTF-8''ShortVideoFactory-%s-source.zip" % __version__)
        self.finish(guide.source_zip())


# ---------------- 反向代理 ----------------

STARTING_PAGE = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta http-equiv="refresh" content="2">
<title>正在启动</title><style>body{font:15px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;color:#445;
display:grid;place-items:center;height:90vh;margin:0}</style></head><body><div>生成界面正在启动（{state}），稍候自动刷新…</div></body></html>"""


def _proxy_headers(request, keep_encoding: bool = True) -> dict:
    headers = {}
    for k, v in request.headers.get_all():
        if k.lower() in HOP_BY_HOP or k.lower().startswith("sec-websocket"):
            continue
        if k.lower() == "accept-encoding" and not keep_encoding:
            continue
        if k in headers:
            headers[k] += ", " + v if k.lower() != "cookie" else "; " + v
        else:
            headers[k] = v
    return headers


def inject(html: bytes) -> bytes:
    if INJECT_TAG in html:
        return html
    pos = html.find(b"</head>")
    return html[:pos] + INJECT_TAG + html[pos:] if pos >= 0 else INJECT_TAG + html


class ProxyHandler(Base):
    SUPPORTED_METHODS = ("GET", "HEAD", "POST", "PUT", "DELETE", "PATCH", "OPTIONS")

    async def proxy(self):
        webui = self.state.webui
        if webui.state != "ready" or not webui.port:
            self.set_status(503)
            self.set_header("Content-Type", "text/html; charset=utf-8")
            self.set_header("Cache-Control", "no-store")
            return self.finish(STARTING_PAGE.replace("{state}", webui.state))
        uri = self.request.uri
        is_page = self.request.method == "GET" and (uri.split("?")[0].rstrip("/") in ("/" + BASE_PATH, "/" + BASE_PATH + "/index.html"))
        url = f"http://127.0.0.1:{webui.port}{uri}"
        headers = _proxy_headers(self.request, keep_encoding=not is_page)
        body = self.request.body if self.request.method in ("POST", "PUT", "PATCH", "DELETE") else None
        if self.request.method in ("POST", "PUT", "PATCH") and body is None:
            body = b""
        meta = {"status": 502, "reason": None, "headers": tornado.httputil.HTTPHeaders(), "started": False, "html": None}
        buffered: list[bytes] = []

        def on_header(line: str):
            line = line.rstrip("\r\n")
            if line.startswith("HTTP/"):
                first = tornado.httputil.parse_response_start_line(line)
                if first.code == 100:
                    return
                meta["status"], meta["reason"] = first.code, first.reason
                meta["headers"] = tornado.httputil.HTTPHeaders()
            elif line:
                meta["headers"].parse_line(line)

        def begin():
            if meta["started"]:
                return
            meta["started"] = True
            h = meta["headers"]
            meta["html"] = is_page and meta["status"] == 200 and "text/html" in h.get("Content-Type", "")
            self.set_status(meta["status"], meta["reason"])
            self.clear_header("Content-Type")
            self.clear_header("Server")
            for k, v in h.get_all():
                kl = k.lower()
                if kl in HOP_BY_HOP or kl in ("date", "server") or (meta["html"] and kl in ("content-length", "content-encoding")):
                    continue
                self.add_header(k, v)

        def on_chunk(chunk: bytes):
            begin()
            if meta["html"]:
                buffered.append(chunk)
            else:
                self.write(chunk)
                self.flush()

        req = tornado.httpclient.HTTPRequest(
            url, method=self.request.method, headers=headers, body=body, follow_redirects=False,
            decompress_response=False, request_timeout=24 * 3600, connect_timeout=10,
            header_callback=on_header, streaming_callback=on_chunk, allow_nonstandard_methods=True)
        try:
            resp = await tornado.httpclient.AsyncHTTPClient().fetch(req, raise_error=False)
        except Exception as exc:  # noqa: BLE001
            if not meta["started"]:
                raise tornado.web.HTTPError(502, f"生成界面后台没有响应：{exc}")
            return
        if resp.code == 599 and not meta["started"]:
            raise tornado.web.HTTPError(502, f"生成界面后台没有响应：{resp.error}")
        begin()
        if meta["html"]:
            page = b"".join(buffered)
            if meta["headers"].get("Content-Encoding", "").lower() == "gzip":
                page = gzip.decompress(page)
            self.write(inject(page))
        self.finish()

    get = head = post = put = delete = patch = options = proxy

    def check_xsrf_cookie(self):  # Streamlit 自己校验 XSRF，这里原样转发
        pass


class WSProxyHandler(tornado.websocket.WebSocketHandler):
    """把 Streamlit 的 WebSocket（/app/_stcore/stream）双向转发到子进程。"""

    def initialize(self, state: AppState):
        self.state = state
        self.upstream = None
        self.protocols: list[str] = []

    def check_origin(self, origin):
        host = (self.request.host or "")
        return origin in (f"http://{host}",)

    def select_subprotocol(self, subprotocols):
        self.protocols = list(subprotocols)
        return subprotocols[0] if subprotocols else None

    async def open(self, *args):
        webui = self.state.webui
        if webui.state != "ready" or not webui.port:
            self.close(1013, "starting")
            return
        headers = _proxy_headers(self.request)
        headers["Host"] = self.request.host
        req = tornado.httpclient.HTTPRequest(f"ws://127.0.0.1:{webui.port}{self.request.uri}", headers=headers,
                                             connect_timeout=10, request_timeout=None)
        try:
            self.upstream = await tornado.websocket.websocket_connect(
                req, on_message_callback=self.on_upstream, subprotocols=self.protocols or None,
                max_message_size=1 << 30)
        except Exception as exc:  # noqa: BLE001
            log.warning("WebSocket 转发失败：%s", exc)
            self.close(1011, "upstream failed")

    def on_upstream(self, message):
        if message is None:
            if self.ws_connection:
                self.close()
            return
        try:
            self.write_message(message, binary=isinstance(message, bytes))
        except tornado.websocket.WebSocketClosedError:
            pass

    async def on_message(self, message):
        if self.upstream is not None:
            try:
                await self.upstream.write_message(message, binary=isinstance(message, bytes))
            except tornado.websocket.WebSocketClosedError:
                self.close()

    def on_close(self):
        if self.upstream is not None:
            self.upstream.close()
            self.upstream = None

    def get_compression_options(self):
        return None


def make_app(state: AppState) -> tornado.web.Application:
    s = {"state": state}
    base = BASE_PATH
    return tornado.web.Application([
        (r"/", IndexHandler, s),
        (r"/ui/(.*)", StaticHandler, {"path": str(web_dir())}),
        (r"/api/ping", PingHandler, s),
        (r"/api/status", StatusHandler, s),
        (r"/api/processes", ProcessesHandler, s),
        (r"/api/settings", SettingsHandler, s),
        (r"/api/quit", QuitHandler, s),
        (r"/api/focus", FocusHandler, s),
        (r"/api/webui/restart", RestartHandler, s),
        (r"/api/open", OpenHandler, s),
        (r"/api/guide", GuideIndexHandler, s),
        (r"/api/guide/doc", GuideTextHandler, {"state": state, "kind": "doc"}),
        (r"/api/guide/source", GuideTextHandler, {"state": state, "kind": "source"}),
        (r"/api/guide/file", GuideFileHandler, s),
        (r"/api/guide/source.zip", SourceZipHandler, s),
        (rf"/{base}/_stcore/stream", WSProxyHandler, s),
        (rf"/{base}", tornado.web.RedirectHandler, {"url": f"/{base}/"}),
        (rf"/{base}/.*", ProxyHandler, s),
    ], websocket_ping_interval=None, compress_response=False)


class ServerThread:
    """在后台线程里跑 tornado（asyncio 事件循环），主线程留给窗口（macOS 要求 GUI 在主线程）。"""

    def __init__(self, state: AppState, port: int = 0):
        self.state, self.want_port = state, port
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: HTTPServer | None = None
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, name="shell-server", daemon=True)

    def _run(self):
        try:
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.server = HTTPServer(make_app(self.state), max_body_size=MAX_BODY, max_buffer_size=MAX_BODY,
                                     idle_connection_timeout=3600)
            sockets = tornado.netutil.bind_sockets(self.want_port, address="127.0.0.1")
            self.server.add_sockets(sockets)
            self.state.port = sockets[0].getsockname()[1]
        except BaseException as exc:  # noqa: BLE001
            self.error = exc
            self.ready.set()
            return
        self.ready.set()
        self.loop.run_forever()
        # 收尾：关闭监听和现有连接
        self.loop.run_until_complete(self._close())
        self.loop.close()

    async def _close(self):
        self.server.stop()
        try:
            await asyncio.wait_for(self.server.close_all_connections(), 3)
        except Exception:  # noqa: BLE001
            pass

    def start(self) -> int:
        self.thread.start()
        self.ready.wait(15)
        if self.error:
            raise self.error
        return self.state.port

    def stop(self):
        if self.loop and self.loop.is_running():
            self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(6)

