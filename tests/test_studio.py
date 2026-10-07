"""运行：python -m unittest discover -s tests -v

用临时数据目录；用一个本机假的 OpenAI 兼容接口代替真模型；配音用静音引擎；FFmpeg 真实编码。
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
TMP = tempfile.mkdtemp(prefix="studio-test-")
os.environ["STUDIO_DATA_DIR"] = TMP

from studio import compliance, images, llm, pipeline, settings, textutil, visuals  # noqa: E402
from studio import project as proj  # noqa: E402
from studio.media import find_ffmpeg  # noqa: E402

STORYBOARD = {
    "title": "逻辑门：计算机怎样做判断",
    "cover_titles": ["计算机怎样做判断", "三个门搞懂 CPU"],
    "description": "用三种逻辑门解释计算机做判断的原理。",
    "tags": ["计算机基础", "逻辑门"],
    "scenes": [
        {"type": "title", "category": "abstract", "heading": "逻辑门", "narration": "你有没有想过，电脑是怎么做判断的？答案藏在三个小小的门里。", "data": {"subtitle": "计算机基础"}, "short": True},
        {"type": "bullets", "category": "abstract", "heading": "三种基本门", "narration": "非门把输入取反。与门要求两个输入都是一。或门只要有一个是一就输出一。", "data": {"items": ["非门：取反", "与门：全为1", "或门：有1就行"]}, "short": True},
        {"type": "table", "category": "data", "heading": "与门真值表", "narration": "我们看与门的真值表，只有一一得到一。", "data": {"headers": ["A", "B", "输出"], "rows": [["0", "0", "0"], ["1", "1", "1"]]}},
        {"type": "image", "category": "concrete", "heading": "CPU 芯片", "narration": "这些门用晶体管做成，一颗芯片里有几百亿个。", "data": {"caption": "CPU 特写"}, "image_query": "close-up of a computer CPU chip on motherboard"},
        {"type": "image", "category": "abstract", "heading": "抽象概念不该配图", "narration": "布尔代数是这一切的数学基础。", "data": {}},
        {"type": "quote", "category": "abstract", "heading": "我的看法", "narration": "我觉得理解了逻辑门，就理解了计算机的一半。", "data": {"text": "理解逻辑门，就理解了计算机的一半", "source": "作者"}},
    ],
    "claims": [{"text": "一颗芯片有几百亿个晶体管", "confidence": "low"}],
}


class FakeLLM(BaseHTTPRequestHandler):
    calls = []
    speech = b""

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.path.endswith("/audio/speech"):
            # 模拟云端 TTS 返回 MP3
            FakeLLM.calls.append({"auth": self.headers.get("Authorization"), "body": body})
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Content-Length", str(len(FakeLLM.speech)))
            self.end_headers()
            self.wfile.write(FakeLLM.speech)
            return
        FakeLLM.calls.append({"auth": self.headers.get("Authorization"), "body": body})
        content = json.dumps(STORYBOARD, ensure_ascii=False)
        resp = {
            "model": body["model"],
            "choices": [{"message": {"role": "assistant", "content": "```json\n" + content + "\n```"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1200, "completion_tokens": 800, "prompt_cache_hit_tokens": 1000},
        }
        data = json.dumps(resp).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def make_image(path, color):
    from PIL import Image

    Image.new("RGB", (800, 500), color).save(path)


class TextTests(unittest.TestCase):
    def test_split_chunks_keeps_text_and_limits_length(self):
        text = "这是第一句。第二句比较长，需要在逗号的地方切开，这样字幕不会太长，观众也看得清楚。最后一句！"
        chunks = textutil.split_chunks(text, 16)
        self.assertEqual("".join(c for c, _ in chunks), text.replace(" ", ""))
        self.assertTrue(all(textutil.spoken_length(c) <= 16 * 1.6 for c, _ in chunks))
        self.assertTrue(chunks[-1][1])

    def test_pronunciation_respects_word_boundary(self):
        out = textutil.apply_pronunciation("OpenAI 的 AI 用 GPU 训练", {"AI": "A I", "GPU": "G P U"})
        self.assertEqual(out, "OpenAI 的 A I 用 G P U 训练")

    def test_srt_time(self):
        self.assertEqual(textutil.format_srt_time(3725.5), "01:02:05,500")

    def test_extract_json_tolerates_fences_and_trailing_commas(self):
        self.assertEqual(llm.extract_json('好的：```json\n{"a": [1, 2,],}\n```'), {"a": [1, 2]})


class ProjectTests(unittest.TestCase):
    def test_normalize_rejects_image_for_abstract_and_fills_defaults(self):
        scenes = proj.normalize_scenes(STORYBOARD["scenes"] + [{"type": "bogus"}, {"type": "chart", "data": {"labels": ["a"], "values": ["x"]}}])
        self.assertEqual(scenes[4]["type"], "keyword")  # 抽象概念不配图
        self.assertEqual(scenes[3]["type"], "image")
        self.assertEqual(scenes[3]["image_query"], STORYBOARD["scenes"][3]["image_query"])
        self.assertEqual(scenes[6]["type"], "keyword")  # 未知类型
        self.assertEqual(scenes[7]["type"], "keyword")  # 数据不足的图表
        self.assertEqual(len({s["id"] for s in scenes}), len(scenes))

    def test_script_to_scenes_without_llm(self):
        text = "逻辑门入门\n今天讲计算机怎么做判断。\n\n三种门\n1. 非门\n2. 与门\n3. 或门\n\n```python\nprint(1 and 0)\n```\n这段代码输出零。\n\n全球芯片市场规模超过 5000 亿美元，每年都在增长。"
        scenes = pipeline.script_to_scenes(text)
        self.assertEqual([s["type"] for s in scenes], ["title", "bullets", "code", "stat"])
        self.assertEqual(scenes[1]["data"]["items"], ["非门", "与门", "或门"])
        self.assertIn("print", scenes[2]["data"]["code"])
        self.assertEqual(scenes[2]["narration"], "这段代码输出零。")


class VisualTests(unittest.TestCase):
    def test_every_template_renders_both_orientations(self):
        for raw in STORYBOARD["scenes"] + [
            {"type": "code", "heading": "代码", "data": {"language": "python", "code": "# 中文注释\nx = 1", "highlight": [2]}},
            {"type": "flow", "data": {"nodes": ["a", "b", "c", "d", "e", "f"]}},
            {"type": "chart", "data": {"chart": "line", "labels": ["1", "2", "3"], "values": [1, -2, 3]}},
            {"type": "compare", "data": {"left_title": "A", "left": ["x"], "right_title": "B", "right": ["y"]}},
            {"type": "stat", "data": {"value": "42%", "label": "说明"}},
        ]:
            sc = proj.normalize_scene(raw)
            for o, size in visuals.SIZES.items():
                img = visuals.render_frame(sc, {"theme": "paper", "index": 0, "total": 3}, 2, 1, "字幕测试", o)
                self.assertEqual(img.size, size)

    def test_reveal_plan_is_monotonic_and_ends_full(self):
        sc = proj.normalize_scene(STORYBOARD["scenes"][1])
        chunks = ["先看或门", "非门把输入取反", "最后总结"]
        plan = visuals.reveal_plan(sc, chunks)
        counts = [v for v, _ in plan]
        self.assertEqual(counts, sorted(counts))
        self.assertEqual(counts[-1], 3)
        self.assertEqual(plan[0][1], 2)  # 口播提到或门 → 直接强调第 3 条


class ComplianceTests(unittest.TestCase):
    def test_flags_stock_code_and_advice(self):
        p = proj.normalize_project({"id": "x1234", "brief": {"views": "我的看法"}, "scenes": [
            {"type": "keyword", "narration": "我建议现在买入 600519，目标价两千。"}]})
        cats = [(i["category"], i["severity"]) for i in compliance.check(p)]
        self.assertIn(("投资", "high"), cats)
        self.assertTrue(any(i["match"] == "600519" for i in compliance.check(p)))

    def test_missing_views_is_high(self):
        p = proj.normalize_project({"id": "x1234", "scenes": [{"type": "keyword", "narration": "讲点知识"}]})
        self.assertTrue(any(i["category"] == "原创性" and i["severity"] == "high" for i in compliance.check(p)))


class ImageTests(unittest.TestCase):
    def test_scoring_threshold_and_no_repeat(self):
        d = Path(TMP) / "imgsrc"
        d.mkdir(exist_ok=True)
        make_image(d / "cpu.jpg", (200, 30, 30))
        make_image(d / "beach.jpg", (30, 30, 200))
        cpu = images.add_image((d / "cpu.jpg").read_bytes(), "cpu.jpg", ["computer cpu chip motherboard close-up", "芯片"])
        images.add_image((d / "beach.jpg").read_bytes(), "beach.jpg", ["sunny beach ocean waves"])
        sc = proj.normalize_scene(STORYBOARD["scenes"][3])
        self.assertGreater(images.score(sc, cpu), 0.5)
        p = proj.normalize_project({"id": "img-test-1", "scenes": [STORYBOARD["scenes"][3], dict(STORYBOARD["scenes"][3], heading="海边", image_query="tropical forest waterfall")]})
        stats = images.assign(p, settings.load()["images"])
        self.assertEqual(p["scenes"][0]["image"]["id"], cpu["id"])
        self.assertEqual(p["scenes"][1]["image"]["id"], "")  # 没有及格的图 → 文字卡
        self.assertEqual(stats["fallback"], 1)
        images.record_usage("img-test-1", [cpu["id"]])
        q = proj.normalize_project({"id": "img-test-2", "scenes": [STORYBOARD["scenes"][3]]})
        images.assign(q, settings.load()["images"])
        self.assertEqual(q["scenes"][0]["image"]["id"], "")  # 最近用过的图不重复用
        images.record_usage("img-test-1", [])


@unittest.skipUnless(find_ffmpeg(), "需要 FFmpeg")
class EndToEndTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fake = ThreadingHTTPServer(("127.0.0.1", 0), FakeLLM)
        threading.Thread(target=cls.fake.serve_forever, daemon=True).start()
        cfg = settings.load()
        cfg["llm"]["base_url"] = f"http://127.0.0.1:{cls.fake.server_address[1]}/v1"
        cfg["llm"]["model"] = "fake-model"
        cfg["tts"]["engine"] = "silent"
        cfg["render"]["preset"] = "ultrafast"
        settings.save(cfg)
        settings.set_secrets({"llm_key": "sk-test"})

    @classmethod
    def tearDownClass(cls):
        cls.fake.shutdown()

    def test_generate_render_and_cache(self):
        p = proj.create({"topic": "逻辑门", "minutes": 1.5, "views": "我觉得理解了逻辑门就理解了计算机的一半。"})
        FakeLLM.calls.clear()
        ctx = pipeline.Ctx()
        pipeline.generate(p["id"], ctx)
        self.assertEqual(len(FakeLLM.calls), 1)  # 只调用一次模型
        call = FakeLLM.calls[0]
        self.assertEqual(call["auth"], "Bearer sk-test")
        self.assertEqual(call["body"]["messages"][0]["content"], __import__("studio.prompts", fromlist=["SYSTEM"]).SYSTEM)
        self.assertEqual(call["body"]["response_format"], {"type": "json_object"})
        p = proj.load(p["id"])
        self.assertEqual(p["title"], STORYBOARD["title"])
        self.assertEqual(len(p["scenes"]), 6)
        self.assertEqual(p["usage"][0]["cache_hit_tokens"], 1000)

        pipeline.generate(p["id"], pipeline.Ctx())
        self.assertEqual(len(FakeLLM.calls), 1)  # 第二次命中本地缓存

        rec = pipeline.render(p["id"], pipeline.Ctx(), "landscape", "full")
        out = proj.project_dir(p["id"]) / "exports" / rec["dir"]
        mp4 = out / rec["video"]
        self.assertTrue(mp4.exists())
        dur = probe_duration(mp4)
        self.assertAlmostEqual(dur, rec["duration"], delta=0.15)
        srt = (out / rec["srt"]).read_text(encoding="utf-8")
        self.assertIn("答案藏在三个小小的门里", srt)
        last_end = srt.strip().split("\n\n")[-1].split("\n")[1].split(" --> ")[1]
        self.assertLess(time_to_sec(last_end), rec["duration"])
        for name in ("封面-横版.jpg", "封面-竖版.jpg", "发布信息.txt", "口播稿.txt", "project-snapshot.json"):
            self.assertTrue((out / name).exists(), name)
        self.assertFalse((out / "_work").exists())

        short = pipeline.render(p["id"], pipeline.Ctx(), "portrait", "short")
        self.assertEqual(short["scenes"], 2)
        self.assertLess(short["duration"], rec["duration"])
        self.assertEqual(len(proj.load(p["id"])["exports"]), 2)

    def test_cloud_tts_is_normalized_and_cached(self):
        from studio.tts import TTS

        mp3 = Path(TMP) / "tone.mp3"
        subprocess.run([find_ffmpeg(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-ar", "44100", "-ac", "2", str(mp3)], check=True)
        FakeLLM.speech = mp3.read_bytes()
        cfg = dict(settings.load()["tts"], engine="api", api_base_url=f"http://127.0.0.1:{self.fake.server_address[1]}/v1")
        t = TTS(cfg, "tts-key", find_ffmpeg())
        FakeLLM.calls.clear()
        wav, frames = t.synthesize("云端配音测试")
        self.assertAlmostEqual(frames / 24000, 1.5, delta=0.1)
        with wave.open(str(wav)) as w:
            self.assertEqual((w.getframerate(), w.getnchannels(), w.getsampwidth()), (24000, 1, 2))
        self.assertEqual(FakeLLM.calls[0]["body"]["input"], "云端配音测试")
        self.assertEqual(FakeLLM.calls[0]["auth"], "Bearer tts-key")
        t.synthesize("云端配音测试")
        self.assertEqual(len(FakeLLM.calls), 1)  # 同一句命中配音缓存

    def test_cancel_stops_render(self):
        p = proj.create({"topic": "取消测试", "minutes": 1})
        proj.update(p["id"], lambda q: q.update(scenes=proj.normalize_scenes(STORYBOARD["scenes"])))
        ctx = pipeline.Ctx()
        ctx.cancel.set()
        from studio.media import Cancelled

        with self.assertRaises(Cancelled):
            pipeline.render(p["id"], ctx)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from studio.server import serve

        cls.app, cls.httpd = serve(0)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def call(self, method, path, body=None, token=True):
        req = urllib.request.Request(self.base + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Content-Type": "application/json", **({"X-Studio-Token": self.app.token} if token else {})})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def test_token_required_and_secrets_hidden(self):
        self.assertEqual(self.call("GET", "/api/bootstrap", token=False)[0], 401)
        settings.set_secrets({"llm_key": "sk-secret-value"})
        status, body = self.call("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertNotIn(b"sk-secret-value", body)
        self.assertTrue(json.loads(body)["secrets"]["llm_key"])
        self.assertEqual(self.call("GET", "/")[0], 200)

    def test_project_crud_and_preview(self):
        status, body = self.call("POST", "/api/projects", {"brief": {"topic": "接口测试"}})
        pid = json.loads(body)["project"]["id"]
        status, body = self.call("POST", f"/api/projects/{pid}/from_text", {"text": "标题段落。\n\n- 一\n- 二"})
        p = json.loads(body)["project"]
        self.assertEqual(len(p["scenes"]), 2)
        p["scenes"][1]["usage"] = "ignored"
        p["usage"] = [{"fake": True}]  # 页面不能改用量记录
        status, body = self.call("PUT", f"/api/projects/{pid}", {"project": p})
        self.assertEqual(json.loads(body)["project"]["usage"], [])
        sid = p["scenes"][1]["id"]
        status, body = self.call("GET", f"/api/projects/{pid}/preview/{sid}?o=portrait&w=270")
        self.assertEqual(status, 200)
        self.assertEqual(body[:2], b"\xff\xd8")
        self.assertEqual(self.call("GET", f"/api/projects/{pid}/review")[0], 200)
        self.assertEqual(self.call("DELETE", f"/api/projects/{pid}")[0], 200)
        self.assertEqual(self.call("GET", f"/api/projects/{pid}")[0], 404)

    def test_media_path_traversal_blocked(self):
        status, _ = self.call("GET", f"/media/library/..%2F..%2Fsettings.json?t={self.app.token}", token=False)
        self.assertEqual(status, 404)


def probe_duration(path):
    out = subprocess.run([find_ffmpeg(), "-i", str(path)], capture_output=True, text=True).stderr
    import re

    h, m, s = re.search(r"Duration: (\d+):(\d+):([\d.]+)", out).groups()
    return int(h) * 3600 + int(m) * 60 + float(s)


def time_to_sec(t):
    h, m, rest = t.split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def tearDownModule():
    shutil.rmtree(TMP, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
