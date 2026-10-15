"""外壳扩展（svf_ext/）单元测试：知识卡片的分页与版式选择、画图、缓动编码；钥匙格式识别；配音/素材失败原因；
打开目录的平台分支；声音与文案语言不一致检测；素材下载代理；以及离线跑通一个知识卡片任务（上游完整流水线，产出 mp4）。

    python -m unittest discover -s tests -v

需要上游依赖（requirements-runtime.txt：Pillow、MoviePy、imageio-ffmpeg、loguru、edge-tts）。不访问网络。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svf_ext import CARD_SOURCES, HYBRID_STOCK, KNOWLEDGE_CARDS, desktop, hints, storyboard as sb  # noqa: E402
from svf_ext import webui as ext_webui  # noqa: E402

LOGIC = ("逻辑门是数字电路的基本单元。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。非门则把输入取反。"
         "学习逻辑门有三个要点：第一，记住真值表。第二，会画电路符号。第三，能把门组合成加法器。"
         "计算机处理数据，先把信号转成二进制，再交给逻辑门运算，最后输出结果。"
         "晶体管的开关速度可以达到每秒10亿次。CPU里有数十亿个晶体管，而早期的计算机只有几千个电子管。"
         "常见的门电路包括与门、或门、非门和异或门。把这些门组合起来，就能做出整个CPU。")


def kinds(pages):
    return [p.kind for p in pages]


class StoryboardTest(unittest.TestCase):
    def test_split_sentences_keeps_punctuation_and_splits_colon_before_ordinal(self):
        out = sb.split_sentences("有三个要点：第一，记住真值表。第二，会画符号！\n换行也算一句")
        self.assertEqual(out, ["有三个要点：", "第一，记住真值表。", "第二，会画符号！", "换行也算一句"])
        long = "，".join(["这是一个相当长的分句用来测试切分"] * 6) + "。"
        self.assertGreater(len(sb.split_sentences(long)), 1)

    def test_layout_choice_by_sentence_features(self):
        pages, kws = sb.build_pages(LOGIC, "逻辑门", None, 40)
        self.assertEqual(kinds(pages), ["title", "bullets", "bullets", "flow", "stat", "compare", "bullets", "keyword"])
        self.assertEqual(pages[0].heading, "逻辑门")
        self.assertEqual(pages[1].heading, "与门、或门、非门")            # 主语同类的并列句 → 编号要点
        self.assertTrue(pages[1].data["items"][0].startswith("与门："))
        self.assertEqual(pages[2].heading, "学习逻辑门有三个要点")          # 「第一…第二…第三」，引子句做标题
        self.assertEqual(pages[2].data["items"], ["记住真值表", "会画电路符号", "能把门组合成加法器"])
        self.assertEqual(len(pages[3].data["nodes"]), 4)                    # 「先…再…最后」→ 流程箭头
        self.assertEqual(pages[4].data["value"], "10亿次")                  # 带单位的数字 → 大数字卡
        self.assertEqual(pages[5].data["right_title"], "早期的计算机")       # 「…，而…」→ 对比两栏
        self.assertEqual(pages[6].data["items"], ["与门", "或门", "非门", "异或门"])   # 「包括 A、B、C 和 D」，不把「与门」拆开
        self.assertEqual(pages[7].heading, "CPU")
        for w in ("逻辑门", "CPU", "与门", "输入"):
            self.assertIn(w, kws)
        self.assertNotIn("门只", kws)

    def test_two_parallel_topics_become_compare(self):
        pages, _ = sb.build_pages("今天聊存储器。内存是临时存储，断电后数据会丢失。硬盘是长期存储，断电后数据还在。", "", None, 12)
        self.assertEqual(kinds(pages), ["title", "compare"])
        self.assertEqual((pages[1].data["left_title"], pages[1].data["right_title"]), ("内存", "硬盘"))

    def test_short_plain_sentences_merge_and_pages_cover_audio(self):
        script = "我们来看看。这很简单。光是一种电磁波。它的速度非常快。"
        pages, _ = sb.build_pages(script, "光", None, 9)
        self.assertLessEqual(len(pages), 3)
        self.assertEqual(pages[0].start, 0.0)
        for a, b in zip(pages, pages[1:]):
            self.assertAlmostEqual(a.end, b.start)
        self.assertGreaterEqual(pages[-1].end, 9)

    def test_timing_follows_subtitles_sentence_and_word_level(self):
        srt = ("1\n00:00:00,100 --> 00:00:02,000\n逻辑门是基本单元\n\n2\n00:00:05,000 --> 00:00:07,000\n与门只有都为1才输出1\n\n"
               "3\n00:00:07,500 --> 00:00:09,000\n非门把输入取反\n")
        cues = sb.parse_srt(srt)
        self.assertEqual(len(cues), 3)
        times = sb.sentence_times(["逻辑门是基本单元。", "与门只有都为1才输出1。", "非门把输入取反。"], cues, 9.5)
        self.assertAlmostEqual(times[1][0], 5.0, places=2)
        self.assertAlmostEqual(times[2][0], 7.5, places=2)
        # 逐词字幕（每个字一条）也能对齐
        words = []
        t = 0.0
        for ch in "甲乙丙丁戊己庚辛":
            words.append((t, t + 0.3, ch))
            t += 0.5
        times = sb.sentence_times(["甲乙丙丁。", "戊己庚辛。"], words, 4.0)
        self.assertAlmostEqual(times[1][0], 2.0, places=2)
        # 没有字幕：按朗读长度分配
        times = sb.sentence_times(["一二三四", "一二三四五六七八"], [], 12)
        self.assertAlmostEqual(times[0][1], 4.0, places=2)

    def test_reveals_follow_sentences(self):
        srt = "\n\n".join(f"{i + 1}\n00:00:{2 * i:02d},000 --> 00:00:{2 * i + 1:02d},500\n{t}" for i, t in
                          enumerate(["今天讲三点", "第一，记住真值表", "第二，会画符号", "第三，会组合"]))
        pages, _ = sb.build_pages("今天讲三点。第一，记住真值表。第二，会画符号。第三，会组合。", "要点", sb.parse_srt(srt), 8)
        bullets = pages[-1]
        self.assertEqual(bullets.kind, "bullets")
        self.assertEqual(len(bullets.reveals), 3)
        self.assertEqual(bullets.visible_at(bullets.reveals[1] - 0.05), 1)
        self.assertEqual(bullets.visible_at(bullets.reveals[1] + 0.01), 2)
        self.assertEqual(bullets.visible_at(bullets.end), 3)

    def test_highlight_spans_longest_first(self):
        spans = sb.highlight_spans("与门和异或门不同", ["或门", "异或门", "与门"])
        self.assertEqual(spans, [("与门", True), ("和", False), ("异或门", True), ("不同", False)])

    def test_english_and_empty_scripts(self):
        pages, _ = sb.build_pages("", "Logic gates", None, 3)
        self.assertEqual(kinds(pages), ["title"])
        pages, _ = sb.build_pages("Logic gates are tiny switches. They decide outputs from inputs.", "Logic", None, 6)
        self.assertGreaterEqual(len(pages), 1)


class CardsRenderTest(unittest.TestCase):
    def test_every_layout_in_every_theme_and_aspect(self):
        from svf_ext import cards
        self.assertTrue(cards.font_path().endswith("NotoSansSC-Bold.ttf"), cards.font_path())
        pages, kws = sb.build_pages(LOGIC, "逻辑门", None, 40)
        pages.append(sb.Page("text", "文字卡", 40, 42, data={"body": "这是一段没有明显关键词的普通说明文字"}))
        for theme in cards.THEMES:
            for W, H in ((1080, 1920), (1920, 1080), (1080, 1080)):
                for i, p in enumerate(pages):
                    for vis in (0, 1, 99):
                        img = cards.render_page(p, W // 4, H // 4, theme, i, len(pages), vis, kws)
                        self.assertEqual(img.size, (W // 4, H // 4))
        # 版式避开字幕区：竖屏默认底部 22% 不放内容
        L = cards.Layout(1080, 1920, "bottom")
        self.assertLessEqual(L.content[3], 1920 * 0.78)
        self.assertGreater(cards.Layout(1080, 1920, "top").head[1], 1920 * 0.2)

    def test_motion_box_and_frame_ranges(self):
        from svf_ext import render
        cw, ch = 1166, 2074
        for kind in set(render.MOTIONS):
            for u in (0, 0.5, 1):
                x0, y0, x1, y1 = render.motion_box(kind, u, cw, ch)
                self.assertGreaterEqual(x0, -1e-6)
                self.assertGreaterEqual(y0, -1e-6)
                self.assertLessEqual(x1, cw + 1e-6)
                self.assertLessEqual(y1, ch + 1e-6)
                self.assertAlmostEqual((x1 - x0) / (y1 - y0), cw / ch, places=3)
        a = render.motion_box("zoom_in", 0, cw, ch)
        b = render.motion_box("zoom_in", 1, cw, ch)
        self.assertGreater(b[0], a[0])          # 缩放：后面裁得更小 = 画面放大
        pages, _ = sb.build_pages(LOGIC, "逻辑门", None, 40)
        ranges = render.page_frame_ranges(pages, 30)
        self.assertEqual(ranges[0][0], 0)
        for (a0, a1), (b0, _) in zip(ranges, ranges[1:]):
            self.assertEqual(a1, b0)
        self.assertEqual(ranges[-1][1], int(-(-pages[-1].end * 30 // 1)))

    def test_render_video_small(self):
        from svf_ext import render
        pages, kws = sb.build_pages("逻辑门是基本单元。与门要两个输入都为1。或门有一个为1就行。", "逻辑门", None, 3)
        with tempfile.TemporaryDirectory() as tmp:
            out = render.render_video(pages, str(Path(tmp) / "c.mp4"), 216, 384, "dark", kws, fps=10, still_dir=tmp)
            self.assertTrue(Path(out).stat().st_size > 1000)
            self.assertEqual(len(list(Path(tmp).glob("page-*.png"))), len(pages))
            import imageio_ffmpeg
            r = subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-i", out], capture_output=True, text=True)
            self.assertIn("216x384", r.stderr)
            self.assertRegex(r.stderr, r"Duration: 00:00:0[34]")

    def test_hybrid_assigns_stock_only_to_plain_pages(self):
        from svf_ext import pipeline
        pages, _ = sb.build_pages(LOGIC, "逻辑门", None, 40)
        used = pipeline.assign_stock(pages, [("a.mp4", 30.0), ("b.mp4", 1.0)])
        self.assertEqual(used, 1)
        self.assertEqual([p.kind for p in pages].count("stock"), 1)
        self.assertEqual(pages[-1].data["path"], "a.mp4")      # 只有最后的关键词卡是普通页
        self.assertEqual(pages[0].kind, "title")

    def test_safe_zone_follows_subtitle_settings(self):
        from types import SimpleNamespace as NS
        from svf_ext.pipeline import safe_zone
        self.assertEqual(safe_zone(NS(subtitle_enabled=False)), "none")
        self.assertEqual(safe_zone(NS(subtitle_enabled=True, subtitle_position="bottom")), "bottom")
        self.assertEqual(safe_zone(NS(subtitle_enabled=True, subtitle_position="top")), "top")
        self.assertEqual(safe_zone(NS(subtitle_enabled=True, subtitle_position="custom", custom_position=20)), "top")
        self.assertEqual(safe_zone(NS(subtitle_enabled=True, subtitle_position="custom", custom_position=80)), "bottom")


class HintsTest(unittest.TestCase):
    PIXABAY = "12345678-0123456789abcdef012345678"
    PEXELS = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789AbCdEfGhIjKlMnOpQrSt"   # 56 位字母数字

    def test_key_kind(self):
        self.assertEqual(len(self.PEXELS), 56)
        self.assertEqual(hints.key_kind(self.PIXABAY), "pixabay")
        self.assertEqual(hints.key_kind(" " + self.PEXELS + " "), "pexels")
        self.assertIsNone(hints.key_kind("12345678-0123"))
        self.assertIsNone(hints.key_kind("abc-def"))

    def test_key_in_wrong_field(self):
        self.assertIn("这像是 Pixabay 的钥匙", hints.key_format_hint("pexels", self.PIXABAY))
        self.assertIn("这像是 Pexels 的钥匙", hints.key_format_hint("pixabay", self.PEXELS))
        self.assertIn("不要手打", hints.key_format_hint("pixabay", "12345678-0123456789abcdef01234567X"))
        self.assertIsNone(hints.key_format_hint("pixabay", self.PIXABAY))
        self.assertIsNone(hints.key_format_hint("pexels", f"{self.PEXELS}, {self.PEXELS}"))
        self.assertIsNone(hints.key_format_hint("pexels", ""))
        self.assertNotIn(self.PIXABAY, hints.key_format_hint("pexels", self.PIXABAY))   # 提示里只露头尾

    def test_tts_error_classes(self):
        self.assertEqual(hints.classify_tts_error("failed, error: 403, message='Invalid response status', url='wss://…'"), "forbidden")
        self.assertEqual(hints.classify_tts_error("failed, error: No audio was received. Please verify that your parameters are correct."), "no_audio")
        self.assertEqual(hints.classify_tts_error("failed, error: Cannot connect to host speech.platform.bing.com:443 ssl:default"), "network")
        self.assertIsNone(hints.classify_tts_error("completed"))
        hints.note("app.services.voice", "failed, error: No audio was received. Please verify")
        hint = hints.tts_failure_hint()
        self.assertIn("【原因】", hint)
        self.assertIn("语言不一致", hint)
        self.assertNotIn("403", hint)       # 不再一律把 403 放在第一条
        hints.note("app.services.voice", "failed, error: 403, message='Invalid response status'")
        self.assertIn("升级", hints.tts_failure_hint())
        # 新的一次尝试开始后，以前的 403 不再算这次的原因
        hints.note("app.services.voice", "start, voice name: zh-CN-XiaoxiaoNeural, try: 1")
        self.assertNotIn("【原因】", hints.tts_failure_hint())
        hints.note("app.services.voice", "failed, error: Cannot connect to host speech.platform.bing.com:443")
        self.assertIn("连不上微软配音服务器", hints.tts_failure_hint())

    def test_stock_error_classes(self):
        self.assertEqual(hints.classify_stock_error("pexels video search failed: error=JSONDecodeError, detail=Expecting value"), "pexels_key")
        self.assertEqual(hints.classify_stock_error("pexels video search returned an unsupported response"), "pexels_key")
        self.assertEqual(hints.classify_stock_error("pixabay search request failed: status=400, content_type=text/html"), "pixabay_key")
        self.assertEqual(hints.classify_stock_error("pixabay API rate limit exceeded: status=429"), "rate_limit")
        self.assertEqual(hints.classify_stock_error("pixabay search request failed: error=ProxyError, detail=…"), "network")
        hints.note("app.services.material", "pixabay search request failed: status=400, content_type=text/html")
        self.assertIn("不要手打", hints.stock_failure_hint("pixabay"))
        hints.note("app.services.material", "pexels video search failed: error=JSONDecodeError")
        self.assertIn("暂停发放新的 API 钥匙", hints.stock_failure_hint("pexels"))
        hints.note("app.services.material", "searching videos on pixabay: term='logic gate'")
        self.assertIn("【可能的原因】", hints.stock_failure_hint("pixabay"))   # 这一次搜索还没有出错记录

    def test_voice_language_mismatch(self):
        w = hints.voice_language_warning("逻辑门是数字电路的基本单元", "af-ZA-AdriNeural-Female")
        self.assertIn("af-ZA", w)
        self.assertIn("zh-CN", w)
        self.assertIn("zu-ZA", hints.voice_language_warning("讲讲逻辑门", "zu-ZA-ThandoNeural-Female", "azure-tts-v2"))
        self.assertIsNone(hints.voice_language_warning("逻辑门是数字电路的基本单元", "zh-CN-XiaoxiaoNeural-Female"))
        self.assertIsNone(hints.voice_language_warning("Logic gates are switches", "en-US-AriaNeural-Female"))
        self.assertIsNone(hints.voice_language_warning("逻辑门", "af-ZA-AdriNeural-Female", "siliconflow"))   # 只管 Edge/Azure 声音
        self.assertIsNotNone(hints.voice_language_warning("Logic gates", "en-US-AriaNeural-Female", video_language="zh-CN"))
        self.assertIsNone(hints.voice_language_warning("", "af-ZA-AdriNeural-Female"))

    def test_webui_voice_warning_uses_script_then_subject(self):
        from types import SimpleNamespace as NS
        p = NS(video_script="", video_subject="逻辑门", voice_name="zu-ZA-ThandoNeural-Female", video_language="")
        self.assertIsNotNone(ext_webui.voice_warning_text(p, "azure-tts-v1"))
        p.voice_name = "zh-CN-YunxiNeural-Male"
        self.assertIsNone(ext_webui.voice_warning_text(p, "azure-tts-v1"))


class DesktopTest(unittest.TestCase):
    def test_platform_branches(self):
        calls = []
        self.assertTrue(desktop.open_local(r"C:\tasks\x", "win32", startfile=lambda p: calls.append(("startfile", p)),
                                           popen=lambda *a, **k: calls.append(("popen", a))))
        self.assertEqual(calls[-1][0], "startfile")
        self.assertTrue(desktop.open_local("/tmp/x", "darwin", popen=lambda cmd, **k: calls.append(("popen", cmd))))
        self.assertEqual(calls[-1], ("popen", ["open", os.path.abspath("/tmp/x")]))
        self.assertTrue(desktop.open_local("/tmp/x", "linux", popen=lambda cmd, **k: calls.append(("popen", cmd))))
        self.assertEqual(calls[-1], ("popen", ["xdg-open", os.path.abspath("/tmp/x")]))
        self.assertNotIn("file://", repr(calls))

    def test_failure_returns_false(self):
        def boom(*a, **k):
            raise OSError("no handler")
        self.assertFalse(desktop.open_local("/tmp/x", "linux", popen=boom))
        self.assertFalse(desktop.open_local("C:/x", "win32", startfile=boom))

    def test_without_shell_opens_locally_and_reports_missing_folder(self):
        calls = []
        with tempfile.TemporaryDirectory() as d:
            res = desktop.open_folder(d, "linux", popen=lambda cmd, **k: calls.append(cmd), endpoint=())
            self.assertTrue(res["ok"])
            self.assertEqual(res["via"], "local")
            self.assertEqual(calls[-1], ["xdg-open", os.path.abspath(d)])
        res = desktop.open_folder(os.path.join(d, "gone"), "linux", popen=lambda cmd, **k: calls.append(cmd), endpoint=())
        self.assertFalse(res["ok"])
        self.assertIn("不存在", res["error"])
        self.assertEqual(res["path"], os.path.abspath(os.path.join(d, "gone")))

    def test_unreachable_shell_falls_back_to_local(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        calls = []
        with tempfile.TemporaryDirectory() as d:
            res = desktop.open_folder(d, "linux", popen=lambda cmd, **k: calls.append(cmd),
                                      endpoint=(f"http://127.0.0.1:{port}", "T"))
        self.assertTrue(res["ok"])
        self.assertEqual(res["via"], "local")


class ProxyTest(unittest.TestCase):
    def test_proxy_format(self):
        self.assertIsNone(ext_webui.proxy_problem("http://127.0.0.1:7897"))
        self.assertIsNone(ext_webui.proxy_problem("socks5://127.0.0.1:7890"))
        self.assertIsNone(ext_webui.proxy_problem("http://user:pw@proxy.local:3128"))
        self.assertIsNone(ext_webui.proxy_problem(""))
        self.assertIsNotNone(ext_webui.proxy_problem("127.0.0.1:7897"))
        self.assertIsNotNone(ext_webui.proxy_problem("http://127.0.0.1:7897 x"))

    def test_set_proxy_writes_upstream_config(self):
        from types import SimpleNamespace as NS
        cfg = NS(proxy={}, _cfg={})
        ext_webui.set_material_proxy(cfg, " http://127.0.0.1:7897 ")
        self.assertEqual(cfg.proxy, {"http": "http://127.0.0.1:7897", "https": "http://127.0.0.1:7897"})
        self.assertIs(cfg._cfg["proxy"], cfg.proxy)      # 上游保存配置时会写回 [proxy]
        ext_webui.set_material_proxy(cfg, "")
        self.assertEqual(cfg.proxy, {})


class WiringTest(unittest.TestCase):
    def test_sources_and_child_finds_extension(self):
        self.assertEqual(CARD_SOURCES[0], KNOWLEDGE_CARDS)
        self.assertEqual(set(HYBRID_STOCK.values()), {"pixabay", "pexels"})
        from svf import child
        self.assertEqual(Path(child.ext_root(str(ROOT / "svf"))), ROOT)

    def test_upstream_patches_are_marked(self):
        task = (ROOT / "upstream/app/services/task.py").read_text(encoding="utf-8")
        main = (ROOT / "upstream/webui/Main.py").read_text(encoding="utf-8")
        self.assertIn("[AI 短视频工厂补丁 P6]", task)
        self.assertIn("[AI 短视频工厂补丁 P5]", main)
        self.assertNotIn('webbrowser.open(f"file://', main)


@unittest.skipIf(os.environ.get("SVF_SKIP_SLOW") == "1", "SVF_SKIP_SLOW=1")
class OfflineTaskTest(unittest.TestCase):
    """上游完整流水线 + 知识卡片，离线跑一个 14 秒的短任务（约 1–2 分钟）。"""

    def run_check(self, source):
        import imageio_ffmpeg
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, MPT_CONFIG_FILE=str(Path(tmp) / "config.toml"), MPT_STORAGE_DIR=str(Path(tmp) / "storage"),
                       MPT_FONT_DIR=str(ROOT / "fonts"), MPT_SONG_DIR=str(Path(tmp) / "songs"),
                       IMAGEIO_FFMPEG_EXE=imageio_ffmpeg.get_ffmpeg_exe(), PYTHONDONTWRITEBYTECODE="1")
            r = subprocess.run([sys.executable, str(ROOT / "tests" / "cards_task_check.py"), str(ROOT), source],
                               cwd=str(ROOT / "upstream"), env=env, capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=900)
            line = next((ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")), "")
            self.assertTrue(line, r.stdout[-3000:] + r.stderr[-3000:])
            return json.loads(line[7:]), r

    def test_knowledge_cards_task_makes_mp4(self):
        res, r = self.run_check("knowledge_cards")
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["size"], [1080, 1920])
        self.assertTrue(res["subtitle"])
        self.assertEqual(res["pages"][0], "title")
        self.assertEqual(res["stills"], len(res["pages"]))
        self.assertNotIn("generating video terms", r.stdout + r.stderr)   # 纯卡片不调大模型生成关键词

    def test_hybrid_without_key_falls_back_to_cards(self):
        res, r = self.run_check("hybrid_pixabay")
        self.assertTrue(res["ok"], res)
        self.assertNotIn("stock", res["pages"])
        self.assertIn("全部改用知识卡片", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
