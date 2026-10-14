"""本机 HTTP 服务：只监听 127.0.0.1，默认随机端口，每次启动一个随机令牌。

安全措施：
- 校验 Host（防 DNS 重绑定）；所有会改动东西的请求（POST）还要校验 Origin 和令牌头 X-QlibStudio-Token；
- 文件读取一律走白名单（数据集/实验编号只允许字母数字；使用指南按清单精确匹配），../ 之类的路径不会命中；
- 网页只从本服务加载脚本和样式（CSP：default-src 'self'），图表库 ECharts 随应用打包，不连外网。
"""
from __future__ import annotations

import csv
import io
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import APP_NAME, UPSTREAM_COMMIT, UPSTREAM_URL, UPSTREAM_VERSION, __version__
from . import compare, guide, paths, procinfo
from .data import community, datasets
from .data.futu import HOST as FUTU_HOST, PORT as FUTU_PORT, Cache as FutuCache
from .jobs import KINDS, Jobs

log = logging.getLogger("qlibstudio")
MAX_BODY = 32 * 1024 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/guide.js": ("guide.js", "text/javascript; charset=utf-8"),
    "/md.js": ("md.js", "text/javascript; charset=utf-8"),
    "/zoom.js": ("zoom.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/vendor/echarts.min.js": ("vendor/echarts.min.js", "text/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'")
SETTINGS_DEFAULT = {"futu_host": FUTU_HOST, "futu_port": FUTU_PORT}
RUN_FILES = {"positions": "positions.json", "trades": "trades.json", "export": "export.json", "config": "config.json"}


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
        self.root = Path(root)
        self.jobs = Jobs(self.root)
        self.token = secrets.token_urlsafe(18)
        self.desktop = None  # 原生窗口桥（文件对话框），浏览器模式下为 None
        self.last_seen = time.time()
        self.quit_event = threading.Event()
        self.runs_dir = self.root / "runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.compare_dir = self.root / "compare"
        self.compare_dir.mkdir(parents=True, exist_ok=True)
        if os.environ.get("QLIBSTUDIO_FAKE_FUTU") == "1":
            self.jobs.extra_env["QLIBSTUDIO_FAKE_FUTU_STATE"] = str(self.root / "futu-fake-quota.json")

    # ------------------------------------------------------------ 设置
    @property
    def settings_file(self) -> Path:
        return self.root / "settings.json"

    def settings(self) -> dict:
        s = datasets.read_json(self.settings_file, {}) or {}
        return {**SETTINGS_DEFAULT, **{k: v for k, v in s.items() if k in SETTINGS_DEFAULT}}

    def save_settings(self, changes: dict) -> dict:
        s = self.settings()
        if "futu_host" in changes:
            h = str(changes["futu_host"]).strip()
            if h not in ("127.0.0.1", "localhost"):
                raise ValueError("出于安全考虑只连本机 OpenD（127.0.0.1）")
            s["futu_host"] = "127.0.0.1"
        if "futu_port" in changes:
            p = int(changes["futu_port"])
            if not 1 <= p <= 65535:
                raise ValueError("端口应在 1~65535")
            s["futu_port"] = p
        datasets.write_json(self.settings_file, s)
        return s

    # ------------------------------------------------------------ 状态
    def status(self) -> dict:
        return {
            "app": APP_NAME, "version": __version__,
            "upstream": {"name": "Qlib", "version": UPSTREAM_VERSION, "commit": UPSTREAM_COMMIT, "url": UPSTREAM_URL,
                         "license": "MIT"},
            "data_dir": str(self.root), "platform": sys.platform, "frozen": paths.frozen(),
            "job": self.jobs.current_view(), "n_datasets": len(datasets.list_all(self.root)),
            "n_runs": len(self.runs()), "settings": self.settings(),
            "fake_futu": os.environ.get("QLIBSTUDIO_FAKE_FUTU") == "1", "window": self.desktop is not None,
            "community_url": community.URL,
        }

    def catalog(self) -> dict:
        from . import experiment as E
        return {"factors": E.FACTOR_SETS, "models": E.MODELS, "optional_models": E.OPTIONAL_MODELS,
                "defaults": {k: v for k, v in E.DEFAULTS.items() if k != "quick"}, "pool_names": datasets.POOL_NAMES,
                "limit_modes": {"board": "按板块（主板 10%、ST 5%、创业板/科创板 20%、北交所 30%）",
                                "qlib": "上游默认：涨跌幅 ±9.5% 不能买卖（不分板块）", "none": "不限制（只排除停牌）"},
                "deal_prices": {"close": "当日收盘价（Qlib 默认）", "open": "当日开盘价（接近 QuantLab 的 T+1 开盘成交）"},
                "sources": datasets.SOURCES, "job_kinds": KINDS}

    # ------------------------------------------------------------ 实验结果
    def run_path(self, run_id: str) -> Path:
        if not datasets.ID_RE.match(run_id or ""):
            raise ValueError("实验编号无效")
        p = self.runs_dir / run_id
        if not (p / "result.json").is_file():
            raise LookupError("实验不存在或没有完成")
        return p

    def runs(self) -> list[dict]:
        out = []
        for d in self.runs_dir.iterdir() if self.runs_dir.is_dir() else []:
            r = datasets.read_json(d / "result.json")
            if isinstance(r, dict):
                cfg = r.get("config", {})
                out.append({"id": r.get("id", d.name), "name": r.get("name"), "created": r.get("created"),
                            "dataset": (r.get("dataset") or {}).get("title"), "demo": (r.get("dataset") or {}).get("demo"),
                            "model": cfg.get("model"), "factors": cfg.get("factors"), "pool": cfg.get("pool"),
                            "test": (cfg.get("segments") or {}).get("test"), "rolling": (cfg.get("rolling") or {}).get("enabled"),
                            "metrics": {k: (r.get("metrics") or {}).get(k) for k in
                                        ("total_return", "annual_return", "max_drawdown", "sharpe", "excess_annual_return")},
                            "rank_ic": (r.get("ic") or {}).get("rank_ic_mean"), "elapsed": r.get("elapsed")})
        return sorted(out, key=lambda x: x.get("created") or "", reverse=True)

    def trades_csv(self, run_id: str) -> bytes:
        trades = datasets.read_json(self.run_path(run_id) / "trades.json", []) or []
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["日期", "代码", "名称", "方向", "股数", "成交价（不复权）", "成交额（元）", "费用（元）"])
        for t in trades:
            w.writerow([t.get("date"), t.get("symbol"), t.get("name"), t.get("side"), t.get("shares"), t.get("price"),
                        t.get("value"), t.get("cost")])
        return ("﻿" + buf.getvalue()).encode("utf-8")

    # ------------------------------------------------------------ 对比
    def compare_items(self) -> list[dict]:
        out = []
        for f in sorted(self.compare_dir.glob("*.json")):
            try:
                x = compare.load(f)
            except (compare.FormatError, OSError):
                continue
            out.append({"key": "file:" + f.stem, "app": x.get("app"), "name": x.get("name"), "period": x.get("period"),
                        "dataset": (x.get("dataset") or {}).get("title"), "imported": time.strftime(
                            "%Y-%m-%d %H:%M", time.localtime(f.stat().st_mtime)), "source": "导入的文件"})
        for r in self.runs():
            out.append({"key": "run:" + r["id"], "app": APP_NAME, "name": r["name"], "dataset": r["dataset"],
                        "period": {"start": (r.get("test") or [None, None])[0], "end": (r.get("test") or [None, None])[1]},
                        "imported": r["created"], "source": "本应用的实验"})
        return out

    def compare_load(self, key: str) -> dict:
        kind, _, ident = key.partition(":")
        if not datasets.ID_RE.match(ident or ""):
            raise ValueError("对比项编号无效")
        if kind == "file":
            return compare.load(self.compare_dir / f"{ident}.json")
        if kind == "run":
            return compare.load(self.run_path(ident) / "export.json")
        raise ValueError("对比项编号无效")

    def compare_import(self, body: dict) -> dict:
        content = body.get("content")
        if body.get("quantlab_dir"):  # QuantLab 1.1 的实验目录：转换成交换格式再存
            x = compare.from_quantlab_run_dir(Path(str(body["quantlab_dir"]).strip()).expanduser())
            content = json.dumps(x, ensure_ascii=False).encode("utf-8")
        elif body.get("path"):
            content = Path(str(body["path"])).expanduser().read_bytes()
        if isinstance(content, str):
            content = content.encode("utf-8")
        if not content:
            raise ValueError("没有收到文件内容")
        x = compare.load(content)
        cid = datasets.new_id("c")
        (self.compare_dir / f"{cid}.json").write_bytes(content)
        return {"key": "file:" + cid, "app": x.get("app"), "name": x.get("name"), "period": x.get("period")}

    # ------------------------------------------------------------ 原生对话框 / 保存
    def pick(self, kind: str) -> dict:
        if not self.desktop:
            return {"mode": "browser"}
        got = self.desktop.pick(kind)
        return {"paths": got or [], "cancelled": not got}

    def save(self, body: dict) -> dict:
        kind = body.get("kind")
        rid = str(body.get("id", ""))
        if kind == "source-zip":
            name, payload, types = f"QlibStudio-{__version__}-source.zip", guide.source_zip(), ("ZIP 压缩包 (*.zip)",)
        elif kind == "run-export":
            name, payload, types = f"QlibStudio-{rid}.json", (self.run_path(rid) / "export.json").read_bytes(), ("JSON (*.json)",)
        elif kind == "trades-csv":
            name, payload, types = f"QlibStudio-{rid}-trades.csv", self.trades_csv(rid), ("CSV (*.csv)",)
        else:
            raise ValueError("未知导出类型")
        if not self.desktop:
            return {"mode": "browser"}
        target = self.desktop.save_dialog(name, types)
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

        def send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", CSP)
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

        def attachment(self, data: bytes, ctype: str, name: str):
            from urllib.parse import quote
            self.send(200, data, ctype, {"Content-Disposition": f"attachment; filename=\"download\"; filename*=UTF-8''{quote(name)}"})

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
                if path == "/api/catalog":
                    return self.json(app.catalog())
                if path == "/api/datasets":
                    return self.json({"datasets": datasets.list_all(app.root)})
                if path == "/api/dataset":
                    ds = arg("id")
                    d = datasets.path(app.root, ds)
                    q_ = datasets.quality(app.root, ds)
                    return self.json({"meta": datasets.meta(app.root, ds), "quality": q_, "pools": datasets.pools(d),
                                      "calendar": (lambda c: {"start": c[0], "end": c[-1], "days": len(c)} if c else None)(datasets.calendar(d))})
                if path == "/api/jobs":
                    return self.json({"current": app.jobs.current_view(), "jobs": app.jobs.list()})
                if path == "/api/job":
                    v = app.jobs.view(arg("id")) if datasets.ID_RE.match(arg("id")) else None
                    return self.json(v) if v else self.json({"error": "没有这个任务"}, 404)
                if path == "/api/job-log":
                    t = app.jobs.read_log(arg("id"), int(arg("tail", "400") or 400))
                    return self.text(t) if t is not None else self.json({"error": "没有这个任务的日志"}, 404)
                if path == "/api/runs":
                    return self.json({"runs": app.runs()})
                if path == "/api/run":
                    return self.send(200, (app.run_path(arg("id")) / "result.json").read_bytes(), "application/json; charset=utf-8")
                if path.startswith("/api/run/"):
                    what = path.rsplit("/", 1)[1]
                    if what == "trades.csv":
                        return self.attachment(app.trades_csv(arg("id")), "text/csv; charset=utf-8", f"QlibStudio-{arg('id')}-trades.csv")
                    if what not in RUN_FILES:
                        return self.json({"error": "not found"}, 404)
                    data = (app.run_path(arg("id")) / RUN_FILES[what]).read_bytes()
                    if what == "export":
                        return self.attachment(data, "application/json; charset=utf-8", f"QlibStudio-{arg('id')}.json")
                    return self.send(200, data, "application/json; charset=utf-8")
                if path == "/api/compare/items":
                    return self.json({"items": app.compare_items()})
                if path == "/api/compare":
                    keys = [k for k in arg("keys").split(",") if k][:6]
                    items = [app.compare_load(k) for k in keys]
                    return self.json(compare.side_by_side(items, arg("common", "1") == "1"))
                if path == "/api/futu/cache":
                    return self.json({"cache": FutuCache(app.root).summary()})
                if path == "/api/processes":
                    return self.json(procinfo.snapshot(app.jobs.worker_pids(), app.jobs.busy()))
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
                    return self.attachment(guide.source_zip(), "application/zip", f"QlibStudio-{__version__}-source.zip")
            except LookupError as exc:
                return self.json({"error": str(exc)}, 404)
            except ValueError as exc:
                return self.json({"error": str(exc)}, 400)
            except Exception as exc:  # noqa: BLE001
                log.exception("请求失败 %s", path)
                return self.json({"error": str(exc)}, 500)
            self.json({"error": "not found"}, 404)

        do_HEAD = do_GET

        def do_POST(self):  # noqa: C901
            if (self.headers.get("Host") not in hosts
                    or self.headers.get("Origin") not in origins | {None}
                    or not secrets.compare_digest(self.headers.get("X-QlibStudio-Token", "").encode("utf-8", "replace"),
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
            try:
                if path == "/api/job/start":
                    kind = str(body.get("kind", ""))
                    params = body.get("params") or {}
                    if kind.startswith("futu"):
                        s = app.settings()
                        params.setdefault("host", s["futu_host"])
                        params.setdefault("port", s["futu_port"])
                    return self.json(app.jobs.start(kind, params), 202)
                if path == "/api/job/cancel":
                    return self.json({"cancelled": app.jobs.cancel()})
                if path == "/api/dataset/delete":
                    if app.jobs.busy():
                        return self.json({"error": "有任务在运行，等它结束后再删除"}, 409)
                    datasets.delete(app.root, str(body.get("id", "")))
                    return self.json({"ok": True})
                if path == "/api/dataset/rename":
                    d = datasets.path(app.root, str(body.get("id", "")))
                    m = datasets.read_json(d / "meta.json", {})
                    m["title"] = str(body.get("title", ""))[:80] or m.get("title")
                    datasets.write_json(d / "meta.json", m)
                    return self.json({"ok": True})
                if path == "/api/run/delete":
                    shutil.rmtree(app.run_path(str(body.get("id", ""))))
                    return self.json({"ok": True})
                if path == "/api/compare/import":
                    return self.json(app.compare_import(body))
                if path == "/api/compare/delete":
                    key = str(body.get("key", ""))
                    ident = key.partition(":")[2]
                    if not key.startswith("file:") or not datasets.ID_RE.match(ident):
                        return self.json({"error": "只能删除导入的文件"}, 400)
                    (app.compare_dir / f"{ident}.json").unlink(missing_ok=True)
                    return self.json({"ok": True})
                if path == "/api/settings":
                    return self.json(app.save_settings(body.get("changes") or {}))
                if path == "/api/pick":
                    return self.json(app.pick(str(body.get("kind", "folder"))))
                if path == "/api/save":
                    return self.json(app.save(body))
                if path == "/api/open":
                    what = body.get("what")
                    if what == "data-dir":
                        open_path(str(app.root), reveal=False)
                    elif what == "url":
                        u = str(body.get("url", ""))
                        if not re.match(r"^https?://", u):
                            return self.json({"error": "只能打开网址"}, 400)
                        webbrowser.open(u)
                    else:
                        return self.json({"error": "未知操作"}, 400)
                    return self.json({"ok": True})
                if path == "/api/heartbeat":
                    return self.json({"ok": True})
                if path == "/api/quit":
                    log.info("界面上点了「退出」")
                    app.quit_event.set()
                    return self.json({"ok": True})
            except RuntimeError as exc:
                return self.json({"error": str(exc)}, 409)
            except LookupError as exc:
                return self.json({"error": str(exc)}, 404)
            except (ValueError, compare.FormatError, OSError) as exc:
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
