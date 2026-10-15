"""外壳（svf/）单元测试：数据目录、首启配置、缩放、使用指南白名单、运行状态、进程启停与看门狗、反向代理。

    python -m unittest discover -s tests -v

需要 psutil、tornado（requirements.txt）。不需要上游依赖，也不访问网络。
"""
from __future__ import annotations

import asyncio
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tomllib
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psutil  # noqa: E402

from svf import DEFAULT_FONT, DEFAULT_VOICE, guide, paths, procinfo, supervisor, userdata  # noqa: E402

POSIX = os.name != "nt"


def alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def wait_until(pred, timeout=15.0, step=0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(step)
    return pred()


class PathsTest(unittest.TestCase):
    def test_data_dir_per_platform(self):
        home = Path("/home/u")
        self.assertEqual(paths.data_dir("darwin", {}, home), home / "Library/Application Support/ShortVideoFactory")
        self.assertEqual(paths.data_dir("win32", {"LOCALAPPDATA": r"C:\Users\u\AppData\Local"}, home),
                         Path(r"C:\Users\u\AppData\Local") / "ShortVideoFactory")
        self.assertEqual(paths.data_dir("linux", {}, home), home / ".local/share/ShortVideoFactory")
        self.assertEqual(paths.data_dir("darwin", {"SVF_DATA_DIR": "/tmp/x"}, home), Path("/tmp/x"))

    def test_payload_dir_frozen(self):
        mac = paths.payload_dir("/Applications/AI 短视频工厂.app/Contents/MacOS/ShortVideoFactory", "darwin", True)
        self.assertEqual(mac.as_posix(), "/Applications/AI 短视频工厂.app/Contents/Resources/payload")
        win = paths.payload_dir("/opt/ShortVideoFactory/ShortVideoFactory.exe", "win32", True)
        self.assertEqual(win.as_posix(), "/opt/ShortVideoFactory/payload")
        self.assertEqual(paths.payload_dir(frozen=False), ROOT)

    def test_runtime_python_env_override(self):
        self.assertEqual(paths.runtime_python(environ={"SVF_PYTHON": "/x/python"}), Path("/x/python"))


class UserDataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svf-test-"))

    def test_initial_config_from_real_upstream_example(self):
        example = (ROOT / "upstream" / "config.example.toml").read_text(encoding="utf-8")
        text = userdata.initial_config_text(example)
        cfg = tomllib.loads(text)
        self.assertEqual(cfg["ui"]["language"], "zh")
        self.assertEqual(cfg["ui"]["font_name"], DEFAULT_FONT)
        # 其他内容不变（例如素材来源默认 pexels、Key 为空）
        self.assertEqual(cfg["app"]["video_source"], "pexels")
        self.assertEqual(cfg["app"]["pexels_api_keys"], [])

    def test_prepare_creates_dirs_config_fonts_and_keeps_user_files(self):
        data = self.tmp / "data"
        res = userdata.prepare(data, ROOT / "upstream", ROOT / "fonts")
        self.assertTrue(res["config_created"])
        for sub in ("storage", "fonts", "songs", "logs"):
            self.assertTrue((data / sub).is_dir(), sub)
        self.assertTrue((data / "fonts" / DEFAULT_FONT).exists())
        self.assertTrue((data / "fonts" / "OFL-NotoSansSC.txt").exists())
        self.assertTrue((data / "fonts" / "BeVietnamPro-Bold.ttf").exists())
        self.assertTrue((data / "songs" / "背景音乐说明.txt").exists())
        # 用户改过的配置和自己放的字体不被覆盖
        (data / "config.toml").write_text('[app]\npexels_api_keys = ["k"]\n', encoding="utf-8")
        (data / "fonts" / "my.ttf").write_bytes(b"x")
        res2 = userdata.prepare(data, ROOT / "upstream", ROOT / "fonts")
        self.assertFalse(res2["config_created"])
        self.assertEqual(res2["fonts_copied"], [])
        self.assertIn('"k"', (data / "config.toml").read_text(encoding="utf-8"))
        self.assertTrue((data / "fonts" / "my.ttf").exists())
        # 旧配置里没有 voice_name：补上默认中文声音，其余不动；再启动不重复改
        self.assertTrue(res2["default_voice_added"])
        cfg = tomllib.loads((data / "config.toml").read_text(encoding="utf-8"))
        self.assertEqual(cfg["ui"]["voice_name"], DEFAULT_VOICE)
        self.assertEqual(cfg["app"]["pexels_api_keys"], ["k"])
        self.assertFalse(userdata.prepare(data, ROOT / "upstream", ROOT / "fonts")["default_voice_added"])

    def test_initial_config_default_voice_is_chinese(self):
        example = (ROOT / "upstream" / "config.example.toml").read_text(encoding="utf-8")
        cfg = tomllib.loads(userdata.initial_config_text(example))
        self.assertEqual(cfg["ui"]["voice_name"], "zh-CN-XiaoxiaoNeural-Female")
        # 必须是上游声音列表里真实存在的 Edge 声音（azure-tts-v1 列表里的值）
        voices = json.loads((ROOT / "upstream" / "app" / "services" / "data" / "azure_voices.json").read_text(encoding="utf-8"))
        self.assertIn(DEFAULT_VOICE, {f"{v['name']}-{v['gender']}" for v in voices})

    def test_with_default_voice_keeps_existing_choice(self):
        self.assertIsNone(userdata.with_default_voice('[ui]\nvoice_name = "zu-ZA-ThandoNeural-Female"\n'))
        self.assertIsNone(userdata.with_default_voice('[ui]\nvoice_name = ""\n'))   # 用户选过「无配音」等
        self.assertIsNone(userdata.with_default_voice('not = toml = ['))
        # [ui] 不在最后、带注释、CRLF 换行，都插在表头下一行
        for text in ('[ui]\nlanguage = "zh"\n\n[app]\nx = 1\n', '[ui]  # 界面\r\nlanguage = "zh"\r\n[app]\r\nx = 1\r\n', "[app]\nx = 1\n"):
            new = userdata.with_default_voice(text)
            cfg = tomllib.loads(new)
            self.assertEqual(cfg["ui"]["voice_name"], DEFAULT_VOICE, text)
            self.assertEqual(cfg["app"]["x"], 1)

    def test_no_commercial_fonts_or_songs_shipped(self):
        names = [p.name for p in (ROOT / "upstream" / "resource").rglob("*") if p.is_file()]
        self.assertFalse([n for n in names if "YaHei" in n or "STHeiti" in n or "Kabel" in n], names)
        self.assertFalse([n for n in names if n.endswith(".mp3")], names)

    def test_zoom_clamp_and_settings_persist(self):
        self.assertEqual(userdata.ZOOM_STEPS, (70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120))
        self.assertEqual(userdata.clamp_zoom(50), 70)
        self.assertEqual(userdata.clamp_zoom(200), 120)
        self.assertEqual(userdata.clamp_zoom("103"), 105)
        self.assertEqual(userdata.clamp_zoom("abc"), 100)
        s = userdata.ShellSettings(self.tmp)
        self.assertEqual(s.values["zoom"], 100)
        s.update({"zoom": 115, "tab": "guide", "bogus": 1})
        s2 = userdata.ShellSettings(self.tmp)
        self.assertEqual(s2.values["zoom"], 115)
        self.assertEqual(s2.values["tab"], "guide")
        self.assertNotIn("bogus", s2.values)


class GuideTest(unittest.TestCase):
    def test_docs_and_sources_listed(self):
        idx = guide.index(ROOT)
        docs = {d["path"] for d in idx["docs"]}
        for need in ("README.md", "UPSTREAM.md", "docs/00 项目阅读入口.md", "docs/01 使用指南.md",
                     "docs/02 软件框架与系统架构.md", "docs/03 源码阅读与重建指南.md", "docs/04 与AI知识视频工坊的对比.md"):
            self.assertIn(need, docs)
        groups = {s["path"]: s["group"] for s in idx["sources"]}
        self.assertEqual(groups.get("svf/app.py"), "shell")
        self.assertEqual(groups.get("upstream/webui/Main.py"), "upstream")
        self.assertEqual(groups.get("upstream/app/services/task.py"), "upstream")
        self.assertFalse([p for p in groups if p.endswith((".ttf", ".ttc", ".mp3", ".pyc"))])
        self.assertTrue(any(a["path"] == "docs/assets/architecture.svg" for a in idx["assets"]))

    def test_docs_have_required_sections(self):
        g = guide.read_doc("docs/01 使用指南.md", ROOT)
        for heading in ("怎样开始和结束", "怎样完成主要任务", "数据从哪来", "结果怎么理解", "出了问题怎么办",
                        "软件框架、系统架构与源码", "当前进程与内存", "怎样缩放界面", "Pexels"):
            self.assertIn(heading, g)
        entry = guide.read_doc("docs/00 项目阅读入口.md", ROOT)
        self.assertIn("project: AI 短视频工厂", entry)
        self.assertIn("author: Claude", entry)

    def test_path_traversal_rejected(self):
        for bad in ("../README.md", "docs/../../CLAUDE.md", "/etc/passwd", "svf/../../studio/server.py", "upstream/config.toml"):
            self.assertIsNone(guide.read_doc(bad, ROOT))
            self.assertIsNone(guide.read_source(bad, ROOT))
            self.assertIsNone(guide.read_asset(bad, ROOT))

    def test_source_zip(self):
        z = zipfile.ZipFile(io.BytesIO(guide.source_zip(ROOT)))
        names = set(z.namelist())
        self.assertIn("shortvideo-factory/svf/server.py", names)
        self.assertIn("shortvideo-factory/docs/04 与AI知识视频工坊的对比.md", names)
        self.assertIn("shortvideo-factory/upstream/LICENSE", names)


class ProcInfoTest(unittest.TestCase):
    def test_snapshot_lists_self_and_children(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            wait_until(lambda: alive(child.pid))
            snap = procinfo.snapshot(webui_pid=child.pid, webui_state="ready")
            pids = {p["pid"]: p for p in snap["processes"]}
            self.assertIn(os.getpid(), pids)
            self.assertIn(child.pid, pids)
            self.assertIn("启动器", pids[os.getpid()]["role"])
            self.assertIn("生成界面后台", pids[child.pid]["role"])
            self.assertIsNotNone(pids[child.pid]["mem_mib"])
            self.assertEqual(snap["unit"], "MiB")
            self.assertIn(f"PID {child.pid}", snap["backend_note"])
        finally:
            child.kill()
            child.wait()
        # 结束后的进程不再出现
        snap = procinfo.snapshot(webui_pid=None, webui_state="stopped")
        self.assertNotIn(child.pid, {p["pid"] for p in snap["processes"]})
        self.assertIn("未运行", snap["backend_note"])

    def test_roles(self):
        self.assertIn("FFmpeg", procinfo.role_of("ffmpeg", 5, 1, 2))
        self.assertIn("WebView2", procinfo.role_of("msedgewebview2.exe", 5, 1, 2))
        self.assertEqual(procinfo.metric_name("win32"), "工作集（Working Set）")
        self.assertEqual(procinfo.metric_name("darwin"), "常驻内存 RSS")


# 假的「生成界面后台」：和 svf_child.py 接受同样的参数，提供 /app/_stcore/health，并再起一个孙进程（模拟 FFmpeg）
FAKE_CHILD = textwrap.dedent("""
    import argparse, http.server, subprocess, sys, threading
    ap = argparse.ArgumentParser()
    ap.add_argument("--parent-pid"); ap.add_argument("--port", type=int); ap.add_argument("--upstream")
    ap.add_argument("--base-path"); ap.add_argument("--extra-path")
    a = ap.parse_args()
    grand = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    print("GRANDCHILD", grand.pid, flush=True)
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.send_header("Content-Length", "2"); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *x): pass
    http.server.ThreadingHTTPServer(("127.0.0.1", a.port), H).serve_forever()
""")


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="svf-sup-"))
        self.script = self.tmp / "fake_child.py"
        self.script.write_text(FAKE_CHILD, encoding="utf-8")

    def test_start_ready_stop_kills_tree_and_frees_port(self):
        w = supervisor.WebUI(Path(sys.executable), self.script, self.tmp, self.tmp / "data", DEFAULT_FONT)
        w.start()
        self.assertTrue(wait_until(lambda: w.state == "ready", 20), w.status())
        pid, port = w.pid(), w.port
        self.assertTrue(supervisor.port_open(port))
        self.assertTrue(wait_until(lambda: len(psutil.Process(pid).children()) >= 1, 10))
        tree = [pid] + [c.pid for c in psutil.Process(pid).children(recursive=True)]
        env = w.env()
        self.assertTrue(env["MPT_CONFIG_FILE"].endswith("config.toml"))
        self.assertEqual(env["MPT_DEFAULT_FONT"], DEFAULT_FONT)
        res = w.stop()
        self.assertEqual(res["alive"], [])
        self.assertTrue(res["port_released"])
        self.assertFalse([p for p in tree if alive(p)])
        self.assertEqual(w.state, "stopped")
        self.assertTrue((self.tmp / "data" / "logs" / "webui.log").exists())

    def test_failed_start_reports_log_tail(self):
        bad = self.tmp / "bad.py"
        bad.write_text("import sys; print('启动出错了', flush=True); sys.exit(3)", encoding="utf-8")
        w = supervisor.WebUI(Path(sys.executable), bad, self.tmp, self.tmp / "data2", DEFAULT_FONT)
        w.start()
        self.assertTrue(wait_until(lambda: w.state == "failed", 15), w.status())
        self.assertIn("返回码 3", w.error)
        self.assertIn("启动出错了", w.error)
        w.stop()

    def test_cleanup_stale_kills_leftover_backend(self):
        leftover = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        wait_until(lambda: alive(leftover.pid))
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        data = self.tmp / "d"
        data.mkdir()
        supervisor.write_run_record(data, {"launcher_pid": dead.pid, "launcher_started": time.time() - 100,
                                           "webui_pid": leftover.pid,
                                           "webui_started": psutil.Process(leftover.pid).create_time()})
        msg = supervisor.cleanup_stale(data)
        self.assertIn("已结束遗留", msg)
        leftover.wait(10)
        self.assertFalse((data / "run.json").exists())
        # 运行记录里的 PID 与创建时间对不上时（PID 被复用），不能误杀
        other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            supervisor.write_run_record(data, {"launcher_pid": dead.pid, "launcher_started": 1.0,
                                               "webui_pid": other.pid, "webui_started": 12345.0})
            supervisor.cleanup_stale(data)
            time.sleep(0.5)
            self.assertTrue(alive(other.pid))
        finally:
            other.kill()
            other.wait()

    def test_cleanup_reports_running_instance(self):
        data = self.tmp / "d2"
        data.mkdir()
        me = psutil.Process(os.getpid())
        supervisor.write_run_record(data, {"launcher_pid": me.pid, "launcher_started": me.create_time()})
        self.assertEqual(supervisor.cleanup_stale(data), "running")

    @unittest.skipUnless(POSIX, "看门狗的父进程检测在 POSIX 上用 getppid；Windows 另由作业对象保证")
    def test_watchdog_kills_tree_when_launcher_dies(self):
        # 中间进程（模拟启动器）启动「后台」后被强制结束；后台里的看门狗应在几秒内结束自己和孙进程
        child_code = textwrap.dedent(f"""
            import os, subprocess, sys, threading, time
            sys.path.insert(0, {str(ROOT)!r})
            from svf import child
            child.WATCH_INTERVAL = 0.5
            # 和 svf_child.py 一样，启动器 PID 在启动时就确定（真实代码由 --parent-pid 传入），再开始看门狗
            ppid = os.getppid()
            threading.Thread(target=child.watchdog, args=(ppid,), daemon=True).start()
            g = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            print(os.getpid(), g.pid, flush=True)
            time.sleep(120)
        """)
        launcher_code = textwrap.dedent(f"""
            import subprocess, sys, time
            p = subprocess.Popen([sys.executable, "-c", {child_code!r}], start_new_session=True, stdout=subprocess.PIPE, text=True)
            print(p.stdout.readline().strip(), flush=True)
            time.sleep(120)
        """)
        launcher = subprocess.Popen([sys.executable, "-c", launcher_code], stdout=subprocess.PIPE, text=True)
        backend, grand = map(int, launcher.stdout.readline().split())
        self.assertTrue(alive(backend) and alive(grand))
        launcher.kill()   # 模拟启动器崩溃
        launcher.wait()
        self.assertTrue(wait_until(lambda: not alive(backend) and not alive(grand), 10),
                        f"后台 {backend} / 孙进程 {grand} 没有随启动器结束")


class ServerTest(unittest.TestCase):
    """用假的 Streamlit（tornado：首页、二进制 + Range、WebSocket 回声）测反向代理和外壳接口。"""

    @classmethod
    def setUpClass(cls):
        import tornado.web
        import tornado.websocket
        from tornado.httpserver import HTTPServer

        cls.blob = os.urandom(300_000)

        class Page(tornado.web.RequestHandler):
            def get(self):
                self.set_header("Content-Type", "text/html")
                self.set_cookie("_streamlit_xsrf", "abc")
                self.write("<html><head><title>st</title></head><body>streamlit</body></html>")

        blob = cls.blob

        class Blob(tornado.web.RequestHandler):
            def get(self):
                rng = self.request.headers.get("Range")
                self.set_header("Content-Type", "video/mp4")
                self.set_header("Accept-Ranges", "bytes")
                if rng:
                    a, b = rng.split("=")[1].split("-")
                    a, b = int(a), int(b or len(blob) - 1)
                    self.set_status(206)
                    self.set_header("Content-Range", f"bytes {a}-{b}/{len(blob)}")
                    self.write(blob[a:b + 1])
                else:
                    self.write(blob)

        class Upload(tornado.web.RequestHandler):
            def put(self):
                self.write({"len": len(self.request.body), "xsrf": self.request.headers.get("X-Xsrftoken", "")})

        class Echo(tornado.websocket.WebSocketHandler):
            def select_subprotocol(self, sp):
                return "streamlit" if "streamlit" in sp else None

            def check_origin(self, origin):
                return True

            def on_message(self, m):
                self.write_message(m, binary=isinstance(m, bytes))

        cls.backend_port = supervisor.free_port()
        ready = threading.Event()

        def run():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            app = tornado.web.Application([(r"/app/", Page), (r"/app/media/x.mp4", Blob),
                                           (r"/app/_stcore/upload_file/.*", Upload), (r"/app/_stcore/stream", Echo)])
            HTTPServer(app).listen(cls.backend_port, "127.0.0.1")
            cls.backend_loop = loop
            ready.set()
            loop.run_forever()

        threading.Thread(target=run, daemon=True).start()
        ready.wait(5)

        class FakeWebUI:
            state, port = "ready", cls.backend_port
            restarted = 0

            def pid(self):
                return None

            def status(self):
                return {"state": self.state, "pid": None, "port": self.port, "error": "", "started_at": 0, "restarts": 0}

            def restart(self):
                FakeWebUI.restarted += 1

        from svf.server import AppState, ServerThread
        cls.tmp = Path(tempfile.mkdtemp(prefix="svf-srv-"))
        cls.webui = FakeWebUI()
        cls.state = AppState(cls.tmp, cls.webui, userdata.ShellSettings(cls.tmp), "TOKEN123")
        cls.quit_called = threading.Event()
        cls.state.on_quit = cls.quit_called.set
        cls.server = ServerThread(cls.state, 0)
        cls.port = cls.server.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.backend_loop.call_soon_threadsafe(cls.backend_loop.stop)

    def get(self, path, headers=None, data=None, method=None):
        req = urllib.request.Request(self.base + path, headers=headers or {}, data=data, method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.headers, r.read()

    def test_index_has_boot_info(self):
        _, _, body = self.get("/")
        html = body.decode("utf-8")
        self.assertIn("AI 短视频工厂", html)
        self.assertIn('"token": "TOKEN123"', html)
        self.assertNotIn("/*__SVF_BOOT__*/null", html)

    def test_proxy_injects_script_into_streamlit_page(self):
        status, headers, body = self.get("/app/")
        self.assertEqual(status, 200)
        self.assertIn(b'<script src="/ui/inject.js"></script></head>', body)
        self.assertIn("_streamlit_xsrf", headers.get("Set-Cookie", ""))
        self.assertEqual(len(headers.get_all("Date") or []), 1)

    def test_proxy_binary_and_range(self):
        _, _, body = self.get("/app/media/x.mp4")
        self.assertEqual(body, self.blob)
        status, headers, part = self.get("/app/media/x.mp4", headers={"Range": "bytes=1000-1999"})
        self.assertEqual(status, 206)
        self.assertEqual(part, self.blob[1000:2000])
        self.assertEqual(headers["Content-Range"], f"bytes 1000-1999/{len(self.blob)}")

    def test_proxy_put_body(self):
        _, _, body = self.get("/app/_stcore/upload_file/s/f", data=b"x" * 5000, method="PUT",
                              headers={"X-Xsrftoken": "abc"})
        self.assertEqual(json.loads(body), {"len": 5000, "xsrf": "abc"})

    def test_websocket_proxy_roundtrip(self):
        import tornado.httpclient
        import tornado.websocket

        async def go():
            req = tornado.httpclient.HTTPRequest(f"ws://127.0.0.1:{self.port}/app/_stcore/stream",
                                                 headers={"Origin": self.base})
            conn = await tornado.websocket.websocket_connect(req, subprotocols=["streamlit", "xsrf-token"])
            self.assertEqual(conn.selected_subprotocol, "streamlit")
            await conn.write_message(b"\x01\x02binary", binary=True)
            got1 = await conn.read_message()
            await conn.write_message("text")
            got2 = await conn.read_message()
            conn.close()
            return got1, got2

        got1, got2 = asyncio.run(go())
        self.assertEqual(got1, b"\x01\x02binary")
        self.assertEqual(got2, "text")

    def test_websocket_rejects_foreign_origin(self):
        import tornado.httpclient
        import tornado.websocket

        async def go():
            req = tornado.httpclient.HTTPRequest(f"ws://127.0.0.1:{self.port}/app/_stcore/stream",
                                                 headers={"Origin": "http://evil.example"})
            await tornado.websocket.websocket_connect(req)

        with self.assertRaises(Exception):
            asyncio.run(go())

    def test_starting_page_when_backend_not_ready(self):
        self.webui.state = "starting"
        try:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/app/")
            self.assertEqual(cm.exception.code, 503)
            self.assertIn("正在启动", cm.exception.read().decode("utf-8"))
        finally:
            self.webui.state = "ready"

    def test_host_header_check(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/status", headers={"Host": "evil.example"})
        self.assertEqual(cm.exception.code, 403)

    def test_token_required_for_mutations(self):
        self.quit_called.clear()
        for path in ("/api/quit", "/api/settings", "/api/webui/restart", "/api/open"):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get(path, data=b"{}")
            self.assertEqual(cm.exception.code, 403, path)
        self.assertFalse(self.quit_called.is_set())

    def test_settings_roundtrip(self):
        _, _, body = self.get("/api/settings", data=json.dumps({"zoom": 87}).encode(), headers={"X-SVF-Token": "TOKEN123"})
        self.assertEqual(json.loads(body)["zoom"], 85)
        _, _, body = self.get("/api/settings")
        self.assertEqual(json.loads(body)["zoom"], 85)

    def test_open_rejects_bad_targets(self):
        for payload in ({"url": "file:///etc/passwd"}, {"what": "../../"}):
            with self.assertRaises(urllib.error.HTTPError) as cm:
                self.get("/api/open", data=json.dumps(payload).encode(), headers={"X-SVF-Token": "TOKEN123"})
            self.assertEqual(cm.exception.code, 400)

    def test_open_path_only_inside_data_dir(self):
        """子进程请外壳打开文件夹：只允许数据目录之内；文件打开它所在的文件夹；不存在、越界都给出完整路径和原因。"""
        opened = []
        self.state.opener = opened.append
        tasks = self.tmp / "storage" / "tasks" / "abc"
        tasks.mkdir(parents=True, exist_ok=True)
        (tasks / "final-1.mp4").write_bytes(b"x")
        hdr = {"X-SVF-Token": "TOKEN123", "Content-Type": "application/json"}

        def call(payload):
            try:
                _, _, body = self.get("/api/open-path", data=json.dumps(payload).encode(), headers=hdr)
                return 200, json.loads(body)
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        code, res = call({"path": str(tasks)})
        self.assertEqual((code, res["ok"], res["path"]), (200, True, str(tasks)))
        self.assertEqual(opened[-1], str(tasks))
        code, res = call({"path": str(tasks / "final-1.mp4")})
        self.assertEqual((code, opened[-1]), (200, str(tasks)))
        n = len(opened)
        code, res = call({"path": str(tasks), "check_only": True})
        self.assertEqual((code, res["ok"], len(opened)), (200, True, n))
        for bad in ("/etc", str(self.tmp / ".." ), str(self.tmp / "storage" / ".." / ".." / "x")):
            code, res = call({"path": bad})
            self.assertEqual(code, 403, bad)
            self.assertFalse(res["ok"])
            self.assertIn("数据目录", res["error"])
        code, res = call({"path": str(self.tmp / "storage" / "nope")})
        self.assertEqual(code, 404)
        self.assertEqual(res["path"], str(self.tmp / "storage" / "nope"))
        link = self.tmp / "link-out"
        try:
            os.symlink("/etc", link)
            code, _ = call({"path": str(link)})
            self.assertEqual(code, 403)   # 符号链接指到数据目录外也不行
        except (OSError, NotImplementedError):
            pass
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/open-path", data=json.dumps({"path": str(tasks)}).encode())
        self.assertEqual(cm.exception.code, 403)   # 没有令牌

        def boom(path):
            raise OSError("no association")
        self.state.opener = boom
        code, res = call({"path": str(tasks)})
        self.assertEqual(code, 500)
        self.assertIn("no association", res["error"])
        self.assertEqual(res["path"], str(tasks))

        # 子进程那一侧（svf_ext.desktop）经外壳打开
        sys.path.insert(0, str(ROOT))
        from svf_ext import desktop
        self.state.opener = opened.append
        res = desktop.open_folder(str(tasks), endpoint=(self.base, "TOKEN123"))
        self.assertEqual((res["ok"], res["via"], opened[-1]), (True, "shell", str(tasks)))
        res = desktop.open_folder("/etc", endpoint=(self.base, "TOKEN123"))
        self.assertEqual((res["ok"], res["via"], res["status"]), (False, "shell", 403))
        res = desktop.open_folder(str(tasks), endpoint=(self.base, "WRONG"))
        self.assertFalse(res["ok"])

    def test_status_processes_guide(self):
        _, _, body = self.get("/api/status")
        st = json.loads(body)
        self.assertEqual(st["webui"]["state"], "ready")
        self.assertIn("config", st["paths"])
        _, _, body = self.get("/api/processes")
        self.assertIn(os.getpid(), {p["pid"] for p in json.loads(body)["processes"]})
        _, _, body = self.get("/api/guide")
        self.assertGreaterEqual(len(json.loads(body)["docs"]), 7)
        _, _, body = self.get("/api/guide/doc?path=" + urllib.parse.quote("docs/01 使用指南.md"))
        self.assertIn("怎样缩放界面", body.decode("utf-8"))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.get("/api/guide/doc?path=" + urllib.parse.quote("../CLAUDE.md"))
        self.assertEqual(cm.exception.code, 404)
        status, headers, body = self.get("/api/guide/source.zip")
        self.assertEqual(headers["Content-Type"], "application/zip")
        self.assertIn("shortvideo-factory/svf/app.py", zipfile.ZipFile(io.BytesIO(body)).namelist())

    def test_quit_with_token(self):
        self.get("/api/quit", data=b"{}", headers={"X-SVF-Token": "TOKEN123"})
        self.assertTrue(self.quit_called.wait(3))


if __name__ == "__main__":
    unittest.main()
