"""本机 HTTP 服务：只监听 127.0.0.1，默认随机端口，每次启动一个随机令牌。

安全措施：
- 校验 Host（防 DNS 重绑定）；所有会改动东西的请求（POST）还要校验 Origin 和令牌头 X-HotRadar-Token；
- 上游报告放在无 allow-same-origin 的沙箱 iframe 里，报告里的脚本拿不到令牌、调不了本服务的写接口；
- 文件读取一律走白名单（报告路径正则 + 目录约束；使用指南按清单精确匹配）。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

from . import APP_NAME, UPSTREAM_COMMIT, UPSTREAM_URL, UPSTREAM_VERSION, __version__
from . import configio, guide, keywords, paths, procinfo, reports
from .runner import Runner

log = logging.getLogger("hotradar")
MAX_BODY = 4 * 1024 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/guide.js": ("guide.js", "text/javascript; charset=utf-8"),
    "/md.js": ("md.js", "text/javascript; charset=utf-8"),
    "/zoom.js": ("zoom.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
# 上游报告自带内联脚本（宽屏/深色切换、复制、保存图片），保存图片要从 cdnjs 加载 html2canvas（离线时这个按钮不可用）
REPORT_CSP = ("default-src 'none'; script-src 'unsafe-inline' https://cdnjs.cloudflare.com; style-src 'unsafe-inline'; "
              "img-src data: blob: https: http:; font-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; "
              "frame-ancestors 'self'")
NETWORK_NOTE = ("热榜数据来自第三方公开接口 newsnow（默认 https://newsnow.busiyi.world/api/s，开源项目 ourongxing/newsnow），"
                "不是各平台官方接口，也不归热点雷达或 TrendRadar 所有。它可能限流、改版或停止服务；"
                "连不上时对应平台记为「失败」，报告里只有成功的平台，已保存的历史数据不受影响。")


def open_path(target: str, reveal: bool = False) -> None:
    if sys.platform == "darwin":
        subprocess.Popen(["open", "-R", target] if reveal else ["open", target])
    elif sys.platform == "win32":
        if reveal:
            subprocess.Popen(["explorer", "/select,", os.path.normpath(target)])
        else:
            os.startfile(target)  # type: ignore[attr-defined]  # noqa: S606
    else:
        subprocess.Popen(["xdg-open", str(Path(target).parent if reveal else target)])


class App:
    def __init__(self, root: Path):
        self.root = root
        self.runner = Runner(root)
        self.token = secrets.token_urlsafe(18)
        self.desktop = None  # 原生窗口桥（保存对话框），浏览器模式下为 None
        self.last_seen = time.time()
        self.quit_event = threading.Event()
        self.started_at = time.time()

    @property
    def main_ws(self) -> Path:
        return paths.workspace_dir("main", self.root)

    def status(self) -> dict:
        r = self.runner
        runs = r.runs()
        last = next((x for x in runs if x.get("task") in ("crawl", "demo")), None)
        return {
            "app": APP_NAME, "version": __version__,
            "upstream": {"name": "TrendRadar", "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT, "url": UPSTREAM_URL,
                         "license": "GPL-3.0"},
            "data_dir": str(self.root), "platform": sys.platform,
            "job": r.job_view(), "schedule": r.schedule_view(), "mcp": r.mcp_view(),
            "last_run": last, "network_note": NETWORK_NOTE,
            "latest": {ws: reports.latest(ws, self.root) for ws in paths.WORKSPACES},
            "stats": reports.stats("main", self.root),
            "api_url": self._api_url(),
        }

    def _api_url(self) -> str:
        try:
            v = configio.view(self.main_ws)["values"].get("platforms.api_url") or ""
        except Exception:  # noqa: BLE001
            v = ""
        return v or "https://newsnow.busiyi.world/api/s（默认）"

    def config_payload(self) -> dict:
        ws = self.main_ws
        cfg = configio.view(ws)
        return {**cfg, "secrets": configio.secrets_view(self.root, ws), "presets": configio.timeline_presets(ws),
                "keywords": configio.read_text(ws, "keywords"), "interests": configio.read_text(ws, "interests"),
                "files": {"config": str(configio.config_path(ws)), "keywords": str(ws / "config" / "frequency_words.txt"),
                          "interests": str(ws / "config" / "ai_interests.txt"), "secrets": str(configio.secrets_path(self.root))}}

    def save(self, body: dict) -> dict:
        kind = body.get("kind")
        if kind != "source-zip":
            raise ValueError("未知导出类型")
        name, payload = f"HotRadar-{__version__}-source.zip", guide.source_zip()
        if not self.desktop:
            return {"mode": "browser"}
        target = self.desktop.save_dialog(name, ("ZIP 压缩包 (*.zip)",))
        if not target:
            return {"cancelled": True}
        Path(target).write_bytes(payload)
        return {"saved": str(target)}


def make_handler(app: App, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            log.debug(fmt, *args)

        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None, csp: str = CSP):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", csp)
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                try:
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        def json(self, value, code=200, extra=None):
            self.send(code, json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode(),
                      "application/json; charset=utf-8", extra)

        def text(self, s: str, code=200):
            self.send(code, s.encode("utf-8"), "text/plain; charset=utf-8")

        def do_GET(self):  # noqa: C901
            if self.headers.get("Host") not in hosts:  # 防 DNS 重绑定
                return self.json({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            path, q = url.path, parse_qs(url.query)
            arg = lambda k, d="": q.get(k, [d])[0]  # noqa: E731
            app.last_seen = time.time()
            try:
                if path in STATIC:
                    name, ctype = STATIC[path]
                    return self.send(200, (paths.web_dir() / name).read_bytes(), ctype)
                if path == "/api/status":
                    return self.json(app.status())
                if path == "/api/runs":
                    return self.json({"runs": app.runner.runs()})
                if path == "/api/run-log":
                    t = app.runner.read_log(arg("id"), tail=int(arg("tail", "2000") or 2000))
                    return self.text(t) if t is not None else self.json({"error": "没有这条日志"}, 404)
                if path == "/api/mcp-log":
                    return self.text(app.runner.mcp_log())
                if path == "/api/reports":
                    ws = arg("ws", "main")
                    if ws not in paths.WORKSPACES:
                        return self.json({"error": "未知工作区"}, 400)
                    return self.json({"ws": ws, "latest": reports.latest(ws, app.root),
                                      "reports": reports.list_reports(ws, app.root)})
                if path.startswith("/report/"):
                    _, _, ws, rel = path.split("/", 3)
                    f = reports.resolve(ws, unquote(rel), app.root)
                    if f is None:
                        return self.send(404, "<!doctype html><meta charset=utf-8><p style='font:14px sans-serif;color:#666'>"
                                              "还没有这份报告。先在「首页」点「立即抓取」或「离线演示」。</p>".encode(),
                                         "text/html; charset=utf-8", csp=REPORT_CSP)
                    return self.send(200, f.read_bytes(), "text/html; charset=utf-8", csp=REPORT_CSP)
                if path == "/api/config":
                    return self.json(app.config_payload())
                if path == "/api/processes":
                    r = app.runner
                    return self.json(procinfo.snapshot(r.worker_pids(), r.mcp_view()["running"], r.busy()))
                if path == "/api/guide":
                    return self.json(guide.index())
                if path in ("/api/guide/doc", "/api/guide/source", "/api/guide/file"):
                    rel = arg("path")
                    if path == "/api/guide/file":
                        found = guide.read_asset(rel)
                        if found is None:
                            return self.json({"error": "不在附件清单里"}, 404)
                        return self.send(200, found[0], found[1])
                    t = (guide.read_doc if path == "/api/guide/doc" else guide.read_source)(rel)
                    return self.text(t) if t is not None else self.json({"error": "不在清单里"}, 404)
                if path == "/api/guide/source.zip":
                    return self.send(200, guide.source_zip(), "application/zip",
                                     {"Content-Disposition": f'attachment; filename="HotRadar-{__version__}-source.zip"'})
            except Exception as exc:  # noqa: BLE001
                log.exception("请求失败 %s", path)
                return self.json({"error": str(exc)}, 500)
            self.json({"error": "not found"}, 404)

        do_HEAD = do_GET

        def do_POST(self):  # noqa: C901
            if (self.headers.get("Host") not in hosts
                    or self.headers.get("Origin") not in origins | {None}
                    or not secrets.compare_digest(self.headers.get("X-HotRadar-Token", "").encode("utf-8", "replace"),
                                                  app.token.encode())):
                return self.json({"error": "请求来源不允许"}, 403)
            try:
                size = int(self.headers.get("Content-Length", 0))
                if not 0 <= size <= MAX_BODY:
                    raise ValueError
                body = json.loads(self.rfile.read(size) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError
            except ValueError:
                return self.json({"error": "请求内容无效"}, 400)
            path = urlparse(self.path).path
            app.last_seen = time.time()
            r = app.runner
            try:
                if path == "/api/run":
                    res = r.start(str(body.get("task", "")))
                    return self.json({**res, "status": app.status()}, 202 if res["started"] else 409)
                if path == "/api/cancel":
                    return self.json({"cancelled": r.cancel()})
                if path == "/api/schedule":
                    return self.json(r.set_schedule(bool(body.get("enabled")), int(body.get("interval_min", 60))))
                if path == "/api/mcp":
                    port = body.get("port")
                    return self.json(r.set_mcp(bool(body.get("enabled")), int(port) if port else None))
                if path == "/api/config":
                    if r.busy():
                        return self.json({"error": "正在运行任务，等它结束后再保存配置"}, 409)
                    configio.update(app.main_ws, body.get("changes"))
                    return self.json(app.config_payload())
                if path == "/api/secrets":
                    configio.save_secrets(app.root, body.get("changes"))
                    return self.json(app.config_payload())
                if path in ("/api/keywords", "/api/interests"):
                    which = path.rsplit("/", 1)[1]
                    configio.write_text(app.main_ws, which, str(body.get("text", "")))
                    return self.json(app.config_payload())
                if path == "/api/keywords/check":
                    titles = body.get("titles") or []
                    if not isinstance(titles, list):
                        raise configio.ConfigError("titles 应为列表")
                    res = keywords.try_match(str(body.get("text", "")), [str(t) for t in titles])
                    res["summary"].pop("_raw", None)
                    return self.json(res)
                if path == "/api/open":
                    what = body.get("what")
                    if what == "data-dir":
                        open_path(str(app.root / "config"), reveal=True)
                    elif what == "report":
                        f = reports.resolve(str(body.get("ws", "main")), str(body.get("path", "")), app.root)
                        if f is None:
                            return self.json({"error": "没有这份报告"}, 404)
                        webbrowser.open(f.as_uri())
                    elif what == "url":
                        u = str(body.get("url", ""))
                        if not u.startswith(("https://", "http://")):
                            return self.json({"error": "只能打开网址"}, 400)
                        webbrowser.open(u)
                    else:
                        return self.json({"error": "未知操作"}, 400)
                    return self.json({"ok": True})
                if path == "/api/save":
                    return self.json(app.save(body))
                if path == "/api/heartbeat":
                    return self.json({"ok": True})
                if path == "/api/quit":
                    log.info("界面上点了「退出」")
                    app.quit_event.set()
                    return self.json({"ok": True})
            except (configio.ConfigError, ValueError) as exc:
                return self.json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                log.exception("请求失败 %s", path)
                return self.json({"error": str(exc)}, 500)
            self.json({"error": "not found"}, 404)

    return Handler


def serve(root: Path, port: int = 0) -> tuple[App, ThreadingHTTPServer]:
    app = App(root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    httpd.RequestHandlerClass = make_handler(app, httpd.server_address[1])
    httpd.daemon_threads = True
    return app, httpd
