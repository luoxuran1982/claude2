"""本机 HTTP 服务：只监听 127.0.0.1，接口需要启动时生成的随机令牌。"""

import base64
import io
import json
import mimetypes
import re
import secrets
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, images, llm, paths, pipeline, tts, visuals
from . import project as proj
from . import settings as settings_mod
from .jobs import JobBusy, JobManager
from .media import Runner, ToolError, find_ffmpeg, open_in_file_manager, python_info

MAX_BODY = 60 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class App:
    def __init__(self):
        self.token = secrets.token_urlsafe(24)
        self.jobs = JobManager()
        self.routes = []
        self.last_seen = time.time()
        self.shutdown_cb = None
        self._register()

    # ------------------------------------------------------------ 路由表
    def route(self, method, pattern):
        def deco(fn):
            self.routes.append((method, re.compile("^" + pattern + "$"), fn))
            return fn

        return deco

    def _register(self):
        r = self.route
        P = r"(?P<pid>[A-Za-z0-9_-]{4,64})"
        S = r"(?P<sid>[A-Za-z0-9_-]{2,20})"

        @r("GET", "/api/bootstrap")
        def bootstrap(req):
            cfg = settings_mod.load()
            return {
                "version": __version__,
                "platform": "windows" if paths.IS_WIN else "mac" if paths.IS_MAC else "linux",
                "python": python_info(),
                "settings": cfg,
                "secrets": settings_mod.secret_status(),
                "ffmpeg": find_ffmpeg(cfg["render"].get("ffmpeg_path")),
                "fonts": visuals.font_status(),
                "tts_engines": [{"id": e, "name": tts.ENGINE_NAMES[e]} for e in tts.available_engines()],
                "tts_resolved": tts.resolve_engine(cfg["tts"]["engine"]),
                "scene_types": proj.SCENE_TYPES,
                "categories": proj.CATEGORIES,
                "themes": proj.THEMES,
                "llm_presets": llm.PRESETS,
                "data_dir": str(paths.data_dir()),
                "music": sorted(p.name for p in paths.music_dir().iterdir() if p.suffix.lower() in (".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg")),
                "job": self.jobs.status(),
            }

        @r("POST", "/api/settings")
        def save_settings(req):
            body = req.json()
            if "secrets" in body:
                settings_mod.set_secrets(body["secrets"])
            cfg = settings_mod.save(body.get("settings") or settings_mod.load())
            return {"settings": cfg, "secrets": settings_mod.secret_status()}

        @r("GET", "/api/voices")
        def voices(req):
            engine = req.query.get("engine", "auto")
            return {"engine": tts.resolve_engine(engine), "voices": tts.list_voices(engine)}

        @r("POST", "/api/tts/test")
        def tts_test(req):
            body = req.json()
            cfg = settings_mod.load()
            ffmpeg = find_ffmpeg(cfg["render"].get("ffmpeg_path"))
            if not ffmpeg:
                raise ApiError(400, "找不到 FFmpeg")
            engine_cfg = dict(cfg["tts"], **(body.get("tts") or {}))
            t = tts.TTS(engine_cfg, settings_mod.get_secret("tts_key"), ffmpeg, Runner())
            from .textutil import apply_pronunciation

            text = apply_pronunciation(body.get("text") or "你好，这是一段配音试听。", cfg["pronunciation"])
            wav, frames = t.synthesize(text)
            return {"url": f"/media/tts/{wav.name}", "seconds": round(frames / 24000, 2), "engine": t.engine}

        @r("POST", "/api/llm/test")
        def llm_test(req):
            cfg = settings_mod.load()
            text, usage = llm.chat(
                [{"role": "user", "content": '只输出 JSON：{"ok": true}'}], cfg["llm"], settings_mod.get_secret("llm_key"),
                json_mode=True, use_cache=False, max_tokens=20,
            )
            return {"reply": text[:200], "usage": usage}

        # ---------------- 项目
        @r("GET", "/api/projects")
        def list_projects(req):
            return {"projects": proj.list_projects()}

        @r("POST", "/api/projects")
        def create_project(req):
            body = req.json()
            p = proj.create(body.get("brief") or {})
            if body.get("theme") in proj.THEMES:
                p["theme"] = body["theme"]
                p = proj.save(p)
            return {"project": p}

        @r("GET", "/api/projects/" + P)
        def get_project(req, pid):
            return {"project": _load(pid), "estimate": pipeline.estimate(_load(pid))}

        @r("PUT", "/api/projects/" + P)
        def put_project(req, pid):
            body = req.json()
            cur = _load(pid)
            incoming = body.get("project") or {}
            incoming["id"] = pid
            # 用量和导出记录以服务器为准，页面不能覆盖
            incoming["usage"] = cur["usage"]
            incoming["exports"] = cur["exports"]
            incoming["created"] = cur["created"]
            p = proj.save(incoming)
            return {"project": p, "estimate": pipeline.estimate(p)}

        @r("DELETE", "/api/projects/" + P)
        def delete_project(req, pid):
            if self.jobs.busy() and self.jobs.current["project"] == pid:
                raise ApiError(409, "该项目有任务在运行")
            proj.trash(pid)
            return {"ok": True}

        @r("POST", "/api/projects/" + P + "/duplicate")
        def dup(req, pid):
            return {"project": proj.duplicate(pid)}

        @r("POST", "/api/projects/" + P + "/generate")
        def generate(req, pid):
            body = req.json()
            _load(pid)
            return {"job": self._start("generate", pid, pipeline.generate, pid, use_cache=not body.get("fresh"), label="生成文案和分镜")}

        @r("POST", "/api/projects/" + P + "/from_text")
        def from_text(req, pid):
            body = req.json()
            scenes = pipeline.script_to_scenes(body.get("text") or "")
            if not scenes:
                raise ApiError(400, "没有识别出内容")
            p = _load(pid)
            p["scenes"] = scenes
            p["title"] = p["title"] or scenes[0]["heading"]
            images.assign(p, settings_mod.load()["images"])
            return {"project": proj.save(p)}

        @r("POST", "/api/projects/" + P + "/import_json")
        def import_json(req, pid):
            body = req.json()
            data = body.get("data")
            if isinstance(data, str):
                data = llm.extract_json(data)
            p = _load(pid)
            pipeline.apply_generation(p, data)
            images.assign(p, settings_mod.load()["images"])
            return {"project": proj.save(p)}

        @r("POST", "/api/projects/" + P + "/scenes/" + S + "/rewrite")
        def rewrite(req, pid, sid):
            body = req.json()
            return {"job": self._start("rewrite", pid, pipeline.rewrite_scene, pid, sid, body.get("instruction", ""), label="改写分镜")}

        @r("POST", "/api/projects/" + P + "/factcheck")
        def factcheck(req, pid):
            return {"job": self._start("factcheck", pid, pipeline.fact_check, pid, label="事实核查")}

        @r("POST", "/api/projects/" + P + "/render")
        def render(req, pid):
            body = req.json()
            orient = body.get("orientation") if body.get("orientation") in visuals.SIZES else "landscape"
            mode = "short" if body.get("mode") == "short" else "full"
            label = ("竖版" if orient == "portrait" else "横版") + ("精简版" if mode == "short" else "完整版")
            return {"job": self._start("render", pid, pipeline.render, pid, orientation=orient, mode=mode, label="渲染" + label)}

        @r("GET", "/api/projects/" + P + "/review")
        def review(req, pid):
            p = _load(pid)
            recent = []
            for item in proj.list_projects()[:20]:
                if item["id"] != pid:
                    try:
                        recent.append(proj.load(item["id"]))
                    except FileNotFoundError:
                        pass
            return pipeline.review(p, recent)

        @r("GET", "/api/projects/" + P + "/preview/" + S)
        def preview(req, pid, sid):
            p = _load(pid)
            orient = req.query.get("o") if req.query.get("o") in visuals.SIZES else "landscape"
            img = pipeline.preview(p, sid, orient)
            w = int(req.query.get("w") or 0)
            if 64 <= w < img.size[0]:
                img = img.resize((w, int(img.size[1] * w / img.size[0])))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return RawResponse(buf.getvalue(), "image/jpeg")

        @r("GET", "/api/projects/" + P + "/cover")
        def cover(req, pid):
            p = _load(pid)
            orient = req.query.get("o") if req.query.get("o") in visuals.COVER_SIZES else "landscape"
            title = req.query.get("title") or (p["cover_titles"][p["cover_choice"]] if p["cover_titles"] else p["title"])
            img = visuals.render_cover(title, {"theme": p["theme"], "series": p["brief"].get("series", "")}, orient)
            img.thumbnail((960, 960))
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=85)
            return RawResponse(buf.getvalue(), "image/jpeg")

        @r("GET", "/api/projects/" + P + "/scenes/" + S + "/candidates")
        def cands(req, pid, sid):
            p = _load(pid)
            sc = _scene(p, sid)
            taken = [s["image"]["id"] for s in p["scenes"] if s["id"] != sid and s.get("image", {}).get("id")]
            return {"candidates": images.candidates(sc, pid, settings_mod.load()["images"], taken, limit=12),
                    "min_score": settings_mod.load()["images"]["min_score"]}

        @r("POST", "/api/projects/" + P + "/scenes/" + S + "/image")
        def set_image(req, pid, sid):
            body = req.json()
            action = body.get("action")

            def apply(p):
                sc = _scene(p, sid)
                img = sc["image"]
                if action == "choose":
                    if not images.get(body.get("image_id", "")):
                        raise ApiError(404, "图片不存在")
                    img.update({"id": body["image_id"], "locked": True, "score": float(body.get("score") or 0)})
                    images.mark_approved(body["image_id"])
                elif action == "reject":
                    if img.get("id"):
                        img["rejected"] = (img.get("rejected") or []) + [img["id"]]
                    img.update({"id": "", "locked": False})
                    # 自动换下一张及格的候选
                    taken = [s["image"]["id"] for s in p["scenes"] if s["id"] != sid and s.get("image", {}).get("id")]
                    c = images.candidates(sc, pid, settings_mod.load()["images"], taken, limit=1)
                    if c and c[0]["score"] >= settings_mod.load()["images"]["min_score"]:
                        img.update({"id": c[0]["id"], "score": c[0]["score"]})
                elif action == "clear":
                    img.update({"id": "", "locked": True})
                else:
                    raise ApiError(400, "未知操作")

            return {"project": proj.update(pid, apply)}

        @r("POST", "/api/projects/" + P + "/images/assign")
        def assign(req, pid):
            p = _load(pid)
            cfg = settings_mod.load()["images"]
            stats = images.assign(p, cfg, settings_mod.get_secret("pexels_key"))
            return {"project": proj.save(p), "stats": stats}

        @r("POST", "/api/projects/" + P + "/reveal")
        def reveal(req, pid):
            body = req.json()
            base = proj.project_dir(pid) / "exports"
            target = base / (body.get("dir") or "")
            target = target.resolve()
            if base.resolve() not in target.parents and target != base.resolve():
                raise ApiError(400, "路径不合法")
            if not target.exists():
                raise ApiError(404, "文件夹不存在")
            open_in_file_manager(target)
            return {"ok": True}

        # ---------------- 任务
        @r("GET", "/api/job")
        def job(req):
            return {"job": self.jobs.status()}

        @r("POST", "/api/job/cancel")
        def cancel(req):
            return {"cancelled": self.jobs.cancel()}

        # ---------------- 素材库
        @r("GET", "/api/library")
        def library(req):
            return {"images": list(reversed(images.load_index()["images"])), "dir": str(images.images_dir())}

        @r("POST", "/api/library")
        def library_add(req):
            body = req.json()
            added = []
            for f in body.get("files") or []:
                data = base64.b64decode(f.get("data", "").split(",")[-1])
                added.append(images.add_image(data, f.get("name") or "image.jpg", f.get("tags") or body.get("tags"), "local", body.get("license") or ""))
            return {"added": added}

        @r("POST", "/api/library/import_folder")
        def library_folder(req):
            folder = Path(req.json().get("folder") or "")
            if not folder.is_dir():
                raise ApiError(400, "文件夹不存在")
            return {"count": images.import_folder(folder)}

        @r("POST", r"/api/library/(?P<iid>[0-9a-f]{16})")
        def library_update(req, iid):
            body = req.json()
            return {"image": images.update_tags(iid, body.get("tags"), body.get("license"))}

        @r("DELETE", r"/api/library/(?P<iid>[0-9a-f]{16})")
        def library_delete(req, iid):
            images.remove(iid)
            return {"ok": True}

        @r("POST", "/api/open_folder")
        def open_folder(req):
            which = req.json().get("which")
            target = {"data": paths.data_dir(), "music": paths.music_dir(), "fonts": paths.fonts_dir(), "library": images.images_dir()}.get(which)
            if not target:
                raise ApiError(400, "未知文件夹")
            open_in_file_manager(target)
            return {"ok": True}

        @r("POST", "/api/heartbeat")
        def heartbeat(req):
            return {"ok": True, "job": self.jobs.status()}

        @r("POST", "/api/shutdown")
        def shutdown(req):
            self.jobs.cancel()
            if self.shutdown_cb:
                threading.Timer(0.3, self.shutdown_cb).start()
            return {"ok": True}

    def _start(self, kind, pid, fn, *args, label="", **kwargs):
        try:
            return self.jobs.start(kind, pid, fn, *args, label=label, **kwargs)
        except JobBusy as e:
            raise ApiError(409, str(e))


class RawResponse:
    def __init__(self, data, ctype):
        self.data, self.ctype = data, ctype


def _load(pid):
    try:
        return proj.load(pid)
    except (FileNotFoundError, ValueError):
        raise ApiError(404, "项目不存在")


def _scene(p, sid):
    for s in p["scenes"]:
        if s["id"] == sid:
            return s
    raise ApiError(404, "分镜不存在")


class Request:
    def __init__(self, handler, query, body):
        self.handler, self.query, self.body = handler, query, body

    def json(self):
        if not self.body:
            return {}
        try:
            data = json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "请求不是合法 JSON")
        return data if isinstance(data, dict) else {}


def make_handler(app):
    web = paths.web_dir()

    class Handler(BaseHTTPRequestHandler):
        server_version = "AIVideoStudio"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):
            pass

        # 防 DNS 重绑定：只接受本机 Host
        def _host_ok(self):
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("127.0.0.1", "localhost")

        def _send(self, status, body, ctype="application/json; charset=utf-8", extra=None):
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
            self._send(status, json.dumps(obj, ensure_ascii=False).encode("utf-8"))

        def _authed(self, query):
            tok = self.headers.get("X-Studio-Token") or query.get("t", "")
            return secrets.compare_digest(tok, app.token)

        def do_GET(self):
            self._dispatch("GET")

        def do_HEAD(self):
            self._dispatch("GET")

        def do_POST(self):
            self._dispatch("POST")

        def do_PUT(self):
            self._dispatch("PUT")

        def do_DELETE(self):
            self._dispatch("DELETE")

        def _dispatch(self, method):
            if not self._host_ok():
                return self._send(403, b"forbidden", "text/plain")
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            app.last_seen = time.time()
            try:
                if path.startswith("/api/"):
                    if not self._authed(query):
                        raise ApiError(401, "令牌无效，请从启动程序打开的链接进入")
                    length = int(self.headers.get("Content-Length") or 0)
                    if length > MAX_BODY:
                        raise ApiError(413, "请求太大")
                    body = self.rfile.read(length) if length else b""
                    for m, rx, fn in app.routes:
                        if m != method:
                            continue
                        mt = rx.match(path)
                        if mt:
                            res = fn(Request(self, query, body), **mt.groupdict())
                            if isinstance(res, RawResponse):
                                return self._send(200, res.data, res.ctype)
                            return self._json(200, res)
                    raise ApiError(404, "接口不存在")
                if path.startswith("/media/"):
                    if not self._authed(query):
                        raise ApiError(401, "令牌无效")
                    return self._media(path)
                return self._static(path)
            except ApiError as e:
                self._json(e.status, {"error": str(e)})
            except (ToolError, llm.LLMError, ValueError) as e:
                self._json(400, {"error": str(e)})
            except FileNotFoundError as e:
                self._json(404, {"error": f"文件不存在：{e}"})
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                import traceback

                traceback.print_exc()
                self._json(500, {"error": f"{type(e).__name__}: {e}"})

        def _static(self, path):
            if path in ("", "/"):
                path = "/index.html"
            target = (web / path.lstrip("/")).resolve()
            if web.resolve() not in target.parents or not target.is_file():
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            self._send(200, target.read_bytes(), ctype)

        def _media(self, path):
            parts = path.split("/")[2:]
            if len(parts) >= 2 and parts[0] == "tts":
                f = (paths.cache_dir("tts") / parts[1]).resolve()
                root = paths.cache_dir("tts").resolve()
            elif len(parts) >= 2 and parts[0] == "library":
                f = (images.images_dir() / parts[1]).resolve()
                root = images.images_dir().resolve()
                if self.headers and "thumb" in (urllib.parse.urlsplit(self.path).query or ""):
                    return self._thumb(f, root)
            elif len(parts) >= 3 and parts[0] == "projects":
                root = (proj.project_dir(parts[1]) / "exports").resolve()
                f = root.joinpath(*parts[2:]).resolve()
            else:
                raise ApiError(404, "not found")
            if root not in f.parents or not f.is_file():
                raise ApiError(404, "文件不存在")
            self._file(f)

        def _thumb(self, f, root):
            if root not in f.parents or not f.is_file():
                raise ApiError(404, "文件不存在")
            from PIL import Image

            with Image.open(f) as im:
                im = im.convert("RGB")
                im.thumbnail((360, 360))
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=80)
            self._send(200, buf.getvalue(), "image/jpeg")

        def _file(self, f):
            """支持 Range，Safari 播放视频需要。"""
            size = f.stat().st_size
            ctype = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            rng = self.headers.get("Range")
            start, end = 0, size - 1
            status = 200
            if rng:
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m:
                    if m.group(1):
                        start = int(m.group(1))
                        if m.group(2):
                            end = min(size - 1, int(m.group(2)))
                    elif m.group(2):
                        start = max(0, size - int(m.group(2)))
                    if start > end or start >= size:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{size}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    status = 206
            length = end - start + 1
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            if status == 206:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if self.command == "HEAD":
                return
            with open(f, "rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)

    return Handler


def serve(port=0):
    app = App()
    httpd = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    httpd.daemon_threads = True
    app.shutdown_cb = httpd.shutdown
    return app, httpd
