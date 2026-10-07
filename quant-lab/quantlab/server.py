"""本机 HTTP 服务：只监听 127.0.0.1，随机端口；POST 需要启动时生成的随机令牌，防止别的网页调用。"""
from __future__ import annotations

import json
import logging
import os
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import numpy as np
import pandas as pd

from . import APP_NAME, __version__, config, dataset, experiment
from . import features as F
from .data import demo
from .data.store import DataStore
from .jobs import Jobs
from .market import BOARDS
from .paths import resource_dir

log = logging.getLogger("quantlab")
MAX_BODY = 5 * 1024 * 1024
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
    "/vendor/echarts.min.js": ("vendor/echarts.min.js", "text/javascript; charset=utf-8"),
}
CSP = ("default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
       "script-src 'self' 'unsafe-eval'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")


def reveal(path: Path) -> None:
    path = Path(path)
    if sys.platform == "win32":
        subprocess.Popen(["explorer", os.path.normpath(path)])
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _f(v, n=4):
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, n) if np.isfinite(v) else None


class App:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.store = DataStore(self.root)
        self.runs = experiment.Runs(self.root)
        self.jobs = Jobs()
        self.token = secrets.token_urlsafe(18)
        self.desktop = None          # 原生窗口桥：文件夹/保存对话框
        self.last_seen = time.time()
        self.quit_event = threading.Event()
        self._static = resource_dir() / "web"

    # ------------------------------------------------------------ 读
    def status(self) -> dict:
        return {"app": APP_NAME, "version": __version__, "data_dir": str(self.root),
                "datasets": self.store.list(), "n_runs": len(self.runs.list()),
                "job": self.jobs.snapshot(), "desktop": self.desktop is not None}

    def catalog(self) -> dict:
        return {"defaults": config.DEFAULTS, "features": F.catalog(), "groups": F.GROUPS,
                "models": config.MODEL_TYPES, "boards": BOARDS}

    def symbols(self, ds: str, q: str) -> list[dict]:
        meta = self.store.meta(ds)
        q = (q or "").strip().lower()
        out = []
        for m in meta.get("symbols", []):
            if not q or q in m["symbol"] or q in (m.get("name") or "").lower():
                out.append({k: m.get(k) for k in ("symbol", "name", "kind", "board", "start", "end", "rows", "adjust", "events")})
                if len(out) >= 80:
                    break
        return out

    def bars(self, ds: str, symbol: str, run_id: str | None) -> dict:
        df = self.store.symbol_bars(ds, symbol)
        if df.empty:
            raise LookupError("没有这只证券的数据")
        k = df["factor"] / df["factor"].iloc[-1]   # 前复权：最新价格与真实一致
        out = {"symbol": symbol, "dates": df["date"].dt.strftime("%Y-%m-%d").tolist(),
               "ohlc": np.round(np.c_[df["open"] * k, df["close"] * k, df["low"] * k, df["high"] * k], 3).tolist(),
               "volume": df["volume"].round().tolist(), "amount": df["amount"].round().tolist(),
               "events": df.loc[df["factor"].diff().abs() > 1e-9, "date"].dt.strftime("%Y-%m-%d").tolist()}
        if run_id:
            try:
                pr = self.runs.predictions(run_id)
                pr = pr[pr["symbol"] == symbol]
                out["scores"] = {"dates": pd.to_datetime(pr["date"]).dt.strftime("%Y-%m-%d").tolist(),
                                 "score": [_f(v) for v in pr["score"] + 0.5]}
                tr = self.runs.trades(run_id)
                if not tr.empty:
                    tr = tr[tr["symbol"] == symbol]
                    out["trades"] = tr[["date", "side", "price", "shares", "reason"]].assign(
                        price=tr["price"].round(3)).to_dict("records")
            except (LookupError, OSError):
                pass
        return out

    def run_detail(self, run_id: str) -> dict:
        s = self.runs.summary(run_id)
        nav = self.runs.nav(run_id)
        dd = nav["nav"] / nav["nav"].cummax() - 1
        s["nav"] = {"dates": nav.index.strftime("%Y-%m-%d").tolist(), "nav": [_f(v) for v in nav["nav"]],
                    "bench": [_f(v) for v in nav["bench"]], "drawdown": [_f(v) for v in dd],
                    "excess": [_f(v) for v in nav["nav"] / nav["bench"]],
                    "n_pos": nav["n_pos"].astype(int).tolist(), "cash": [_f(v, 0) for v in nav["cash"]]}
        s["config"] = self.runs.config(run_id)
        return s

    def run_trades(self, run_id: str, symbol: str = "", limit: int = 3000) -> dict:
        tr = self.runs.trades(run_id)
        total = len(tr)
        if tr.empty:
            return {"rows": [], "total": 0}
        if symbol:
            tr = tr[tr["symbol"].str.contains(symbol) | tr["name"].astype(str).str.contains(symbol)]
        tr = tr.iloc[::-1].head(limit)
        tr = tr.replace({np.nan: None})
        return {"rows": tr.to_dict("records"), "total": total}

    def compare(self, ids: list[str]) -> dict:
        out = []
        for rid in ids[:8]:
            s = self.runs.summary(rid)
            nav = self.runs.nav(rid)
            out.append({"id": rid, "name": s["name"], "perf": s["perf"], "ic": s["ic"], "trade_stats": s["trade_stats"],
                        "dates": nav.index.strftime("%Y-%m-%d").tolist(), "nav": [_f(v) for v in nav["nav"]],
                        "bench": [_f(v) for v in nav["bench"]]})
        return {"runs": out}

    def export_bytes(self, run_id: str, kind: str) -> tuple[str, bytes]:
        p = self.runs.path(run_id)
        name = self.runs.summary(run_id)["name"]
        safe = "".join(ch for ch in name if ch not in '\\/:*?"<>|')[:40]
        if kind == "trades":
            df = self.runs.trades(run_id)
        elif kind == "nav":
            df = self.runs.nav(run_id).reset_index(names="date")
        elif kind == "picks":
            df = pd.DataFrame(self.runs.picks(run_id)["picks"])
        elif kind == "predictions":
            df = self.runs.predictions(run_id)
        elif kind == "config":
            return f"{safe}-配置.json", (p / "config.json").read_bytes()
        else:
            raise ValueError("未知导出类型")
        label = {"trades": "交易记录", "nav": "净值", "picks": "最新选股", "predictions": "样本外预测"}[kind]
        return f"{safe}-{label}.csv", "﻿".encode() + df.to_csv(index=False).encode("utf-8")

    # ------------------------------------------------------------ 写
    def start_import(self, body: dict) -> dict:
        path = str(body.get("path") or "").strip().strip('"')
        if not path:
            raise ValueError("请填写或选择 K 线文件夹")
        if not Path(path).expanduser().exists():
            raise FileNotFoundError(f"路径不存在：{path}")
        kinds = tuple(body.get("kinds") or ("stock", "index"))
        title = str(body.get("title") or "").strip()
        merge_into = body.get("merge_into") or None

        def work(progress):
            meta = self.store.import_path(path, title, merge_into, kinds=kinds,
                                          auto_adjust=bool(body.get("auto_adjust", True)),
                                          merge=bool(merge_into), progress=progress)
            dataset.clear_cache()
            return {"dataset": meta["id"]}
        return self.jobs.start("import", f"导入 {Path(path).name}", work)

    def start_demo(self, body: dict) -> dict:
        n = int(body.get("n_stocks") or 300)
        years = float(body.get("years") or 8)
        if not (20 <= n <= 3000 and 2 <= years <= 20):
            raise ValueError("股票数 20~3000，年数 2~20")

        def work(progress):
            progress(0.1, f"生成 {n} 只模拟股票、{years:g} 年日线")
            bars, names = demo.generate(n, years, seed=int(body.get("seed") or 7))
            progress(0.8, "保存")
            meta = self.store.save_frame(bars, f"演示数据（模拟 {n} 只）", names, "demo", demo=True)
            dataset.clear_cache()
            return {"dataset": meta["id"]}
        return self.jobs.start("demo", "生成演示数据", work)

    def start_run(self, body: dict) -> dict:
        cfg = config.normalize(body.get("config") or {})   # 先校验，参数错误立刻返回
        return self.jobs.start("run", cfg["name"] or "实验", lambda p: {"run": experiment.run(self.store, self.runs, cfg, p)})

    def start_factors(self, body: dict) -> dict:
        cfg = config.normalize(body.get("config") or {})
        return self.jobs.start("factors", "单因子检验", lambda p: experiment.factor_study(self.store, cfg, p))


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

        def json(self, value, code=200):
            self.send(code, json.dumps(value, ensure_ascii=False, allow_nan=False, default=str).encode(),
                      "application/json; charset=utf-8")

        def do_GET(self):
            if self.headers.get("Host") not in hosts:  # 防 DNS 重绑定
                return self.json({"error": "forbidden"}, 403)
            url = urlparse(self.path)
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            path = url.path
            app.last_seen = time.time()
            if path in STATIC:
                name, ctype = STATIC[path]
                return self.send(200, (app._static / name).read_bytes(), ctype)
            try:
                if path == "/api/status":
                    return self.json(app.status())
                if path == "/api/catalog":
                    return self.json(app.catalog())
                if path == "/api/job":
                    return self.json({"job": app.jobs.snapshot()})
                if path == "/api/dataset":
                    return self.json(app.store.meta(q.get("id", "")))
                if path == "/api/symbols":
                    return self.json({"symbols": app.symbols(q.get("id", ""), q.get("q", ""))})
                if path == "/api/bars":
                    return self.json(app.bars(q.get("id", ""), q.get("symbol", ""), q.get("run") or None))
                if path == "/api/runs":
                    return self.json({"runs": app.runs.list()})
                if path == "/api/run":
                    return self.json(app.run_detail(q.get("id", "")))
                if path == "/api/run/trades":
                    return self.json(app.run_trades(q.get("id", ""), q.get("q", "")))
                if path == "/api/run/picks":
                    return self.json(app.runs.picks(q.get("id", "")))
                if path == "/api/compare":
                    return self.json(app.compare([x for x in q.get("ids", "").split(",") if x]))
                if path == "/api/export":
                    name, payload = app.export_bytes(q.get("id", ""), q.get("kind", ""))
                    ctype = "application/json" if name.endswith(".json") else "text/csv; charset=utf-8"
                    return self.send(200, payload, ctype,
                                     {"Content-Disposition": f"attachment; filename=\"export\"; filename*=UTF-8''{quote(name)}"})
            except LookupError as exc:
                return self.json({"error": str(exc)}, 404)
            except Exception as exc:  # noqa: BLE001
                log.exception("请求失败 %s", path)
                return self.json({"error": str(exc)}, 500)
            self.json({"error": "not found"}, 404)

        do_HEAD = do_GET

        def do_POST(self):
            if (self.headers.get("Host") not in hosts
                    or self.headers.get("Origin") not in origins | {None}
                    or not secrets.compare_digest(self.headers.get("X-QuantLab-Token", ""), app.token)):
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
                if path == "/api/import":
                    return self.json({"job": app.start_import(body)}, 202)
                if path == "/api/demo":
                    return self.json({"job": app.start_demo(body)}, 202)
                if path == "/api/run/start":
                    return self.json({"job": app.start_run(body)}, 202)
                if path == "/api/factors":
                    return self.json({"job": app.start_factors(body)}, 202)
                if path == "/api/job/cancel":
                    return self.json({"cancelled": app.jobs.cancel()})
                if path == "/api/dataset/delete":
                    if app.jobs.busy():
                        raise RuntimeError("有任务在运行，稍后再删")
                    app.store.delete(str(body.get("id", "")))
                    dataset.clear_cache()
                    return self.json({"ok": True})
                if path == "/api/run/delete":
                    app.runs.delete(str(body.get("id", "")))
                    return self.json({"ok": True})
                if path == "/api/run/rename":
                    app.runs.rename(str(body.get("id", "")), str(body.get("name", "")))
                    return self.json({"ok": True})
                if path == "/api/pick-folder":
                    if not app.desktop:
                        return self.json({"mode": "browser"})
                    return self.json({"path": app.desktop.folder_dialog()})
                if path == "/api/save":
                    name, payload = app.export_bytes(str(body.get("id", "")), str(body.get("kind", "")))
                    if not app.desktop:
                        return self.json({"mode": "browser"})
                    target = app.desktop.save_dialog(name)
                    if not target:
                        return self.json({"cancelled": True})
                    Path(target).write_bytes(payload)
                    return self.json({"saved": str(target)})
                if path == "/api/open-dir":
                    which = body.get("which")
                    target = app.runs.path(str(body.get("id"))) if which == "run" else app.root
                    reveal(target)
                    return self.json({"ok": True})
                if path == "/api/heartbeat":
                    return self.json({"ok": True})
                if path == "/api/quit":
                    app.quit_event.set()
                    return self.json({"ok": True})
            except (ValueError, RuntimeError, FileNotFoundError) as exc:
                return self.json({"error": str(exc)}, 400)
            except LookupError as exc:
                return self.json({"error": str(exc)}, 404)
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
