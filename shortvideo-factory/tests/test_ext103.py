"""1.0.3 新增外壳扩展的单元测试：日志解读（explain）、目标时长（duration）、大模型调用记录去钥匙（llmlog）、
知识卡片快速通道（fastcards：编码器选择、音频滤镜、字幕画进画面）。不需要网络。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svf_ext import duration, explain, fastcards, llmlog  # noqa: E402


def rec(t, level, msg, where='"./app/services/task.py:1": f'):
    return f"2026-10-15 {t} | {level} | {where} - {msg}"


class ExplainTest(unittest.TestCase):
    LOG = [
        rec("08:00:00", "INFO", "start task: abc, stop_at: video"),
        rec("08:00:01", "INFO", "\n\n## generating video script"),
        rec("08:00:09", "SUCCESS", "completed: \n逻辑门是……\n第二段"),
        rec("08:00:09", "INFO", "\n\n## generating video terms"),
        rec("08:00:12", "INFO", "\n\n## generating audio"),
        rec("08:00:12", "INFO", "start, voice name: zh-CN-XiaoxiaoNeural, try: 1"),
        rec("08:00:20", "INFO", "目标时长 60 秒（请大模型写约 270 字）；实际配音 58.2 秒，文案 262 字，比目标短 2 秒"),
        rec("08:00:20", "INFO", "\n\n## generating subtitle"),
        rec("08:00:21", "INFO", "\n\n## downloading videos from pixabay"),
        rec("08:00:22", "INFO", "searching videos on pixabay: term='logic gate'"),
        rec("08:00:40", "SUCCESS", "downloaded 6 videos"),
        rec("08:00:40", "INFO", "\n\n## combining video: 1 => x"),
        rec("08:00:44", "INFO", "processed clip 1: 5.0 of 58.3s covered"),
        rec("08:00:50", "INFO", "concatenating 6 clips with ffmpeg"),
        rec("08:00:55", "INFO", "video combining completed"),
        rec("08:00:55", "INFO", "\n\n## generating video: 1 => final-1.mp4"),
        rec("08:01:25", "INFO", "final video render still running: elapsed=30s"),
        rec("08:01:55", "INFO", "final video render still running: elapsed=60s"),
    ]

    def test_steps_and_heartbeat(self):
        r = explain.explain(self.LOG)
        st = {s.key: s for s in r["steps"]}
        self.assertEqual(r["state"], "running")
        self.assertEqual(r["current"], "render")
        self.assertEqual([st[k].status for k in ("script", "terms", "audio", "subtitle", "materials", "clips", "concat")],
                         ["done"] * 7)
        self.assertEqual(st["render"].status, "running")
        texts = [n[2] for n in st["render"].notes]
        self.assertIn("仍在渲染，已用 30 秒", texts)
        self.assertIn("仍在渲染，已用 60 秒", texts)
        self.assertIn("最终渲染", r["summary"])
        self.assertIn("已用 60 秒", r["summary"])
        self.assertTrue(any("目标时长 60 秒" in n[2] for n in st["audio"].notes))

    def test_line_numbers_match_joined_raw_log(self):
        """记录里带换行时，行号按「记录用换行连起来」后的行算，和界面上的原始日志一致。"""
        r = explain.explain(self.LOG)
        raw = "\n".join(self.LOG).split("\n")
        for s in r["steps"]:
            if s.start_line:
                self.assertIn(s.start_time, raw[s.start_line - 1], s.title)
        render = next(s for s in r["steps"] if s.key == "render")
        line = next(n[0] for n in render.notes if "30 秒" in n[2])
        self.assertIn("elapsed=30s", raw[line - 1])

    def test_failure_and_skips(self):
        log = [rec("09:00:00", "INFO", "\n\n## generating video script"),
               rec("09:00:01", "INFO", "\n\n## generating audio"),
               rec("09:00:02", "INFO", "\n\n## generating subtitle"),
               rec("09:00:03", "INFO", "知识卡片：9 页，时间依据：字幕"),
               rec("09:00:04", "SUCCESS", "知识卡片快速通道：讲解页已画好（9 页，47.6 秒）"),
               rec("09:00:04", "INFO", "\n\n## 知识卡片快速通道：一次合成画面、配音、背景音乐和字幕 => x"),
               rec("09:00:14", "INFO", "知识卡片快速通道 still running: elapsed=10s（已编码 300/1428 帧）"),
               rec("09:00:20", "ERROR", "task failed, task_id: abc, stage: video, error: RuntimeError: FFmpeg 合成失败")]
        r = explain.explain(log, {"user_script": True})
        st = {s.key: s for s in r["steps"]}
        self.assertEqual(r["state"], "failed")
        self.assertEqual(st["terms"].status, "skipped")       # 知识卡片不提取关键词
        self.assertEqual(st["clips"].status, "skipped")       # 快速通道没有处理片段、拼接
        self.assertEqual(st["concat"].status, "skipped")
        self.assertEqual(st["render"].status, "failed")
        self.assertIn("仍在渲染，已用 10 秒", [n[2] for n in st["render"].notes])
        self.assertIn("FFmpeg 合成失败", r["summary"])
        self.assertTrue(any("自己写的文案" in n[2] for n in st["script"].notes))

    def test_done_and_duration_line(self):
        r = explain.explain(self.LOG + [rec("08:02:10", "SUCCESS", "task abc finished, generated 1 videos.")])
        self.assertEqual(r["state"], "done")
        self.assertTrue(all(s.status in ("done", "skipped") for s in r["steps"]))
        line = explain.duration_line({"target_seconds": 60, "actual_seconds": 58.2, "script_chars": 262,
                                      "chars_per_second": 4.5, "target_chars": 270})
        self.assertIn("目标时长 60 秒", line)
        self.assertIn("实际配音 58.2 秒", line)
        self.assertIn("比目标短 2 秒", line)


class DurationTest(unittest.TestCase):
    def test_plan_and_prompt(self):
        p = duration.plan(60, "zh", 1.0, stats={})
        self.assertEqual((p["target_chars"], p["chars_per_second"]), (270, 4.5))
        self.assertEqual(duration.plan(60, "zh", 1.2, stats={})["target_chars"], 320)   # 语速快 → 多写
        self.assertIsNone(duration.plan(0, "zh"))
        self.assertEqual(duration.plan(5, "zh", stats={})["target_seconds"], duration.MIN_SECONDS)
        add = duration.prompt_addition(p)
        self.assertIn("about 270 Chinese characters", add)
        self.assertIn("目标时长约 60 秒", add)
        self.assertEqual(duration.plan(60, "en", stats={})["unit"], "个英文单词")

    def test_counting_and_language(self):
        self.assertEqual(duration.count_units("逻辑门，是CPU的基础。123", "zh"), 9)   # 7 个汉字 + CPU + 123
        self.assertEqual(duration.count_units("Logic gates are tiny switches.", "en"), 5)
        self.assertEqual(duration.language_of("逻辑门"), "zh")
        self.assertEqual(duration.language_of("logic gates"), "en")
        self.assertEqual(duration.language_of("logic", "zh-CN"), "zh")

    def test_learns_from_actual_runs(self):
        with tempfile.TemporaryDirectory() as d:
            stats = Path(d) / "rate.json"
            for secs in (50, 52):
                info = duration.record_actual(Path(d) / "t", "字" * 260, secs, "zh", 1.0, stats_path=stats)
            self.assertEqual(info["script_chars"], 260)
            data = json.loads(stats.read_text(encoding="utf-8"))
            cps, why = duration.chars_per_second("zh", 1.0, stats=data)
            self.assertAlmostEqual(cps, (5.2 + 5.0) / 2, places=2)
            self.assertIn("实测", why)
            duration.record_actual(Path(d) / "t", "字" * 260, 80, "zh", 1.0, stats_path=stats, learn=False)  # 无配音不学
            self.assertEqual(len(json.loads(stats.read_text(encoding="utf-8"))["zh"]), 2)
            self.assertEqual(duration.read_task(Path(d) / "t")["actual_seconds"], 80)


class ScrubTest(unittest.TestCase):
    def test_secrets_removed_everywhere(self):
        cfg = {"openai_api_key": "sk-abcdef1234567890XYZ", "pexels_api_keys": "key1abcdefg, key2hijklmn",
               "llm_provider": "openai", "nested": {"secret_token": "tok_9876543210"}}
        secrets = llmlog.secret_values([cfg])
        self.assertIn("key2hijklmn", secrets)
        self.assertNotIn("openai", secrets)
        rec = {"messages": [{"role": "user", "content": "我的钥匙 sk-abcdef1234567890XYZ 和 tok_9876543210"}],
               "raw_response": [{"headers": {"Authorization": "Bearer abc.def.ghi.jkl"}, "api_key": "anything"}],
               "error": "401 invalid key2hijklmn; Bearer qwertyuiop123"}
        out = json.dumps(llmlog.scrub(rec, secrets), ensure_ascii=False)
        for s in ("sk-abcdef1234567890XYZ", "tok_9876543210", "key2hijklmn", "abc.def.ghi.jkl", "qwertyuiop123", "anything"):
            self.assertNotIn(s, out)
        self.assertIn("我的钥匙", out)

    def test_pattern_only(self):
        self.assertEqual(llmlog.scrub("api_key=abcdefgh12345678", set()), "api_key=***")
        self.assertNotIn("sk-zzzzzzzzzzzzzzzz", llmlog.scrub("x sk-zzzzzzzzzzzzzzzz", set()))


class FastCardsTest(unittest.TestCase):
    def test_encoder_order(self):
        listed = {"libx264", "h264_nvenc", "h264_qsv", "h264_amf"}
        self.assertEqual(fastcards.choose_encoders("ff", "auto", "", "win32", listed, probe=lambda c: c == "h264_qsv"),
                         ["h264_qsv", "libx264"])
        self.assertEqual(fastcards.choose_encoders("ff", "auto", "", "win32", listed, probe=lambda c: False), ["libx264"])
        self.assertEqual(fastcards.choose_encoders("ff", "off", "", "win32", listed, probe=lambda c: True), ["libx264"])
        self.assertEqual(fastcards.choose_encoders("ff", "auto", "h264_amf", "win32", listed, probe=lambda c: True),
                         ["h264_amf", "libx264"])   # 用户在上游设置里指定的硬件编码器优先
        self.assertEqual(fastcards.choose_encoders("ff", "auto", "", "darwin", {"h264_videotoolbox"}, probe=lambda c: True),
                         ["h264_videotoolbox", "libx264"])
        self.assertEqual(fastcards.choose_encoders("ff", "auto", "", "linux", {"libx264"}, probe=lambda c: True), ["libx264"])

    def test_audio_filter(self):
        inputs, flt = fastcards.audio_args("a.mp3", "", 1.0, 0.2, 47.6)
        self.assertEqual(inputs, ["-i", "a.mp3"])
        self.assertIn("[1:a]volume=1.000,apad[a]", flt)
        inputs, flt = fastcards.audio_args("a.mp3", "m.mp3", 0.8, 0.2, 47.6)
        self.assertEqual(inputs, ["-i", "a.mp3", "-stream_loop", "-1", "-i", "m.mp3"])
        f = flt[1]
        self.assertIn("volume=0.200", f)
        self.assertIn("afade=t=out:st=44.600:d=3", f)          # 结尾 3 秒淡出
        self.assertIn("amix=inputs=2:duration=longest", f)     # 背景音乐铺满整条画面
        self.assertIn("atrim=0:47.600", f)

    def test_subtitle_drawn_with_bundled_font(self):
        params = NS(subtitle_enabled=True, font_name="", font_size=60, text_fore_color="#FFFFFF", stroke_color="#000000",
                    stroke_width=1.5, text_background_color=False, rounded_subtitle_background=False,
                    subtitle_position="bottom", custom_position=70.0)
        style = fastcards.SubtitleStyle(params, 1080, 1920)
        img, x, y = style.render("逻辑门是数字电路的基本单元，与门只有两个输入都为1时才输出1")
        self.assertEqual(img.mode, "RGBA")
        self.assertLessEqual(img.width, int(1080 * 0.9))
        self.assertGreater(img.height, 120)                    # 长句换成两行
        self.assertAlmostEqual(y + img.height, 1920 * 0.95, delta=2)
        self.assertGreater(img.getchannel("A").getextrema()[1], 0)
        subs = fastcards.Subtitles([(0.0, 1.0, "第一句"), (1.0, 2.5, "第二句")], style)
        self.assertIsNotNone(subs.at(0.5))
        self.assertIsNot(subs.at(0.5), subs.at(1.2))
        self.assertIsNone(subs.at(3.0))
        params.subtitle_enabled = False
        self.assertIsNone(fastcards.Subtitles([(0.0, 1.0, "x")], fastcards.SubtitleStyle(params, 1080, 1920)).at(0.5))

    def test_render_final_small(self):
        """真的合成一条 3 秒横屏小样：有画面、有声音、尺寸对、字幕画进去了。"""
        import subprocess
        from svf_ext.render import _ffmpeg
        from svf_ext.storyboard import build_pages
        ff = _ffmpeg()
        with tempfile.TemporaryDirectory() as d:
            audio = str(Path(d) / "a.mp3")
            subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", "sine=f=440:d=3", audio], check=True)
            pages, kws = build_pages("逻辑门是数字电路的基本单元。", "逻辑门", [(0.2, 2.8, "逻辑门是数字电路的基本单元")], 3.0)
            params = NS(subtitle_enabled=True, font_name="", font_size=40, text_fore_color="#FFFFFF", stroke_color="#000000",
                        stroke_width=2, text_background_color=True, rounded_subtitle_background=True,
                        subtitle_position="bottom", custom_position=70.0, voice_volume=1.0, bgm_volume=0.0)
            out = str(Path(d) / "final.mp4")
            res = fastcards.render_final(pages, [(0.2, 2.8, "逻辑门是数字电路的基本单元")], params, audio, out, 640, 360,
                                         "classroom", kws, "bottom", motion=False, hw_mode="off", still_dir=str(Path(d) / "s"))
            self.assertEqual(res["codec"], "libx264")
            info = subprocess.run([ff, "-hide_banner", "-i", out], capture_output=True, text=True).stderr
            self.assertIn("640x360", info)
            self.assertIn("Audio: aac", info)
            self.assertTrue(list((Path(d) / "s").glob("page-*.png")))


class PopenTrackerTest(unittest.TestCase):
    def test_finds_and_kills_task_processes_without_psutil(self):
        """取消任务时按命令行里的任务 ID 找到 FFmpeg 等子进程并结束（随包 Python 没有 psutil，靠 Popen 登记）。"""
        import subprocess
        from svf_ext import tasks
        tasks._install_popen_tracker()
        tid = "task-0000-abcd"
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", f"/tmp/{tid}/out.mp4"])
        try:
            found = tasks.task_processes(tid)
            self.assertIn(p.pid, [x.pid for x in found])
            self.assertEqual(tasks.task_processes("other-task"), [])
            self.assertIn(p.pid, tasks.kill_task_processes(tid))
            p.wait(10)
            self.assertIsNotNone(p.returncode)
            self.assertEqual(tasks.task_processes(tid), [])
        finally:
            if p.poll() is None:
                p.kill()


if __name__ == "__main__":
    unittest.main()
