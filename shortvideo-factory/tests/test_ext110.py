"""1.1.0 的单元测试：AI 导演 JSON 的校验与回退、工坊主题各版式渲染、AI 插图未配置/出错时的回退、调用链标注、
字数控制三档、分镜数据（分镜图和清单）。不需要网络，也不需要上游。

    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from svf import review  # noqa: E402
from svf_ext import cards, director, duration, illustrate, llmlog, shots, studio_cards  # noqa: E402
from svf_ext.storyboard import Page, split_sentences  # noqa: E402

SCRIPT = ("逻辑门是数字电路的基本单元。常见的逻辑门有与门、或门和非门，计算机里的一切运算都由这三种基本门组合而成。"
          "与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。非门则把输入取反。"
          "在Python里，可以用and、or、not三个关键字模拟它们。计算机先把信号转成二进制，再交给逻辑门运算，最后输出结果。")
GOOD = {"title": "逻辑门入门", "pages": [
    {"sentences": [1], "type": "title", "heading": "逻辑门是什么", "data": {"subtitle": "数字电路的基本单元"}},
    {"sentences": [2], "type": "bullets", "heading": "三种基本逻辑门", "data": {"items": ["与门 AND", "或门 OR", "非门 NOT"]}},
    {"sentences": [3, 4, 5], "type": "table", "heading": "三种门的规则",
     "data": {"headers": ["门", "输入", "输出"], "rows": [["与门", "都为1", "1"], ["或门", "有一个1", "1"], ["非门", "取反", "相反"]]}},
    {"sentences": [6], "type": "code", "heading": "用 Python 模拟",
     "data": {"language": "python", "code": "a, b = 1, 0\nprint(a and b)", "highlight": [2]}},
    {"sentences": [7], "type": "flow", "heading": "计算机怎样运算", "data": {"nodes": ["转成二进制", "逻辑门运算", "输出结果"]}},
]}


def cues_for(script: str, cps: float = 5.0):
    """按每秒 cps 个字给每句生成一条字幕。"""
    out, t = [], 0.3
    for s in split_sentences(script):
        d = max(0.5, len(s) / cps)
        out.append((t, t + d, s))
        t += d + 0.2
    return out


class DirectorValidateTest(unittest.TestCase):
    def test_good_json_passes(self):
        spec, warn = director.validate(GOOD, 7)
        self.assertEqual([p["type"] for p in spec["pages"]], ["title", "bullets", "table", "code", "flow"])
        self.assertEqual(spec["pages"][2]["sentences"], [3, 4, 5])
        self.assertEqual(warn, [])

    def test_extract_json_from_fence_and_think(self):
        text = "<think>先数一下句子</think>\n```json\n" + json.dumps(GOOD, ensure_ascii=False) + "\n```"
        self.assertEqual(director.extract_json(text)["title"], "逻辑门入门")
        self.assertEqual(director.extract_json("好的，分镜如下：" + json.dumps(GOOD) + " 以上。")["title"], "逻辑门入门")
        with self.assertRaises(director.DirectorError):
            director.extract_json("我建议分五页……")

    def test_hard_errors(self):
        bad = json.loads(json.dumps(GOOD))
        bad["pages"][1]["sentences"] = [9]
        with self.assertRaisesRegex(director.DirectorError, "超出范围"):
            director.validate(bad, 7)
        bad = json.loads(json.dumps(GOOD))
        bad["pages"][2]["sentences"] = [2, 3]
        with self.assertRaisesRegex(director.DirectorError, "重复"):
            director.validate(bad, 7)
        bad = json.loads(json.dumps(GOOD))
        bad["pages"][2]["sentences"] = [3, 5]
        with self.assertRaisesRegex(director.DirectorError, "不连续"):
            director.validate(bad, 7)
        with self.assertRaisesRegex(director.DirectorError, "pages"):
            director.validate({"title": "x"}, 7)
        bad = json.loads(json.dumps(GOOD))
        bad["pages"][1]["heading"] = ""
        with self.assertRaisesRegex(director.DirectorError, "heading"):
            director.validate(bad, 7)

    def test_soft_fixes(self):
        spec = json.loads(json.dumps(GOOD))
        spec["pages"][1]["heading"] = "这是一个特别特别特别特别长的标题超过了十八个字"
        spec["pages"][3]["type"] = "hologram"
        spec["pages"][4]["data"] = {}
        del spec["pages"][2]                       # 第 3–5 句没分到任何一页
        out, warn = director.validate(spec, 7)
        self.assertLessEqual(len(out["pages"][1]["heading"]), director.MAX_HEADING)
        self.assertEqual(out["pages"][1]["sentences"], [2, 3, 4, 5])   # 并进上一页
        self.assertEqual(out["pages"][2]["type"], "keyword")          # 不认识的版式
        self.assertEqual(out["pages"][3]["type"], "keyword")          # flow 缺 nodes
        self.assertTrue(any("截短" in w for w in warn) and any("不认识" in w for w in warn))

    def test_illustration_limit(self):
        spec = json.loads(json.dumps(GOOD))
        for p in spec["pages"]:
            p["illustrate"], p["image_prompt"] = True, "a circuit"
        out, warn = director.validate(spec, 7, max_illustrations=3)
        self.assertEqual(sum(p["illustrate"] for p in out["pages"]), 3)
        out, _ = director.validate(spec, 7, max_illustrations=0)
        self.assertEqual(sum(p["illustrate"] for p in out["pages"]), 0)

    def test_portrait_limits(self):
        spec = json.loads(json.dumps(GOOD))
        spec["pages"][1]["data"]["items"] = list("ABCDEF")
        spec["pages"][2]["data"]["headers"] = ["a", "b", "c", "d"]
        out, _ = director.validate(spec, 7, portrait=True)
        self.assertEqual(len(out["pages"][1]["data"]["items"]), 4)
        self.assertEqual(len(out["pages"][2]["data"]["headers"]), 3)


class DirectorFlowTest(unittest.TestCase):
    def test_success_pages_follow_subtitles(self):
        cues = cues_for(SCRIPT)
        calls = []
        res = director.direct(SCRIPT, "逻辑门", cues, cues[-1][1], 1080, 1920,
                              lambda p, parts, a: calls.append((p, parts)) or json.dumps(GOOD, ensure_ascii=False))
        self.assertTrue(res["ok"])
        self.assertEqual(len(calls), 1)
        pages = res["pages"]
        self.assertEqual([p.heading for p in pages][:3], ["逻辑门是什么", "三种基本逻辑门", "三种门的规则"])
        self.assertEqual(pages[0].start, 0.0)
        for a, b in zip(pages, pages[1:]):
            self.assertAlmostEqual(a.end, b.start)
        # 第 3 页从第 3 句开口前后开始（字幕里第 3 条的开始时间）
        self.assertAlmostEqual(pages[2].start, cues[2][0], delta=0.6)
        self.assertGreaterEqual(pages[-1].end, cues[-1][1])
        # 表格 3 行，对应 3 句话，逐行亮起的时间单调不减、落在这一页里
        r = pages[2].reveals
        self.assertEqual(len(r), 3)
        self.assertEqual(r, sorted(r))
        self.assertTrue(all(pages[2].start <= x <= pages[2].end for x in r))
        self.assertGreater(r[1], r[0])
        # 提示词：编号句子、画面方向是变量，规则是模板
        prompt, parts = calls[0]
        self.assertIn("[3] 与门只有两个输入都为1时才输出1。", prompt)
        self.assertEqual("".join(p[0] for p in parts), prompt)
        names = {p[2] for p in parts if p[1] == "var"}
        self.assertIn("视频主题", names)
        self.assertIn("画面方向（按视频比例）", names)
        self.assertNotIn("illustrate 设为 true", prompt)

    def test_retry_once_then_fallback(self):
        calls = []

        def call(p, parts, a):
            calls.append(p)
            return "不给 JSON"
        res = director.direct(SCRIPT, "逻辑门", [], 30, 1080, 1920, call)
        self.assertFalse(res["ok"])
        self.assertEqual(len(calls), 2)
        self.assertIn("没有通过程序校验", calls[1])
        self.assertIn("JSON", res["fallback_reason"])
        self.assertTrue(res["pages"] and res["pages"][0].kind == "title")   # 规则切分的结果

    def test_retry_fixes(self):
        outs = [json.dumps({"pages": [{"sentences": [1, 9], "type": "title", "heading": "x"}]}), json.dumps(GOOD)]
        res = director.direct(SCRIPT, "逻辑门", [], 30, 1080, 1920, lambda p, parts, a: outs[a - 1])
        self.assertTrue(res["ok"])
        self.assertEqual([a["ok"] for a in res["attempts"]], [False, True])

    def test_call_error_falls_back_without_retry(self):
        calls = []

        def call(p, parts, a):
            calls.append(a)
            raise ConnectionError("连不上")
        res = director.direct(SCRIPT, "逻辑门", [], 30, 1080, 1920, call)
        self.assertFalse(res["ok"])
        self.assertEqual(calls, [1])
        self.assertIn("连不上", res["fallback_reason"])
        res = director.direct(SCRIPT, "逻辑门", [], 30, 1080, 1920, lambda p, parts, a: "Error: 401 invalid api key")
        self.assertFalse(res["ok"])
        self.assertIn("401", res["fallback_reason"])

    def test_prompt_with_illustrations(self):
        prompt, parts = director.build_prompt("逻辑门", ["a。", "b。"], "横屏 16:9", illustrations=3)
        self.assertIn("最多 3 页", prompt)
        self.assertIn("image_prompt", prompt)
        self.assertEqual(director.orientation_label(1920, 1080), "横屏 16:9")


class StudioThemeTest(unittest.TestCase):
    KINDS = {
        "title": {"subtitle": "副标题"}, "keyword": {"keyword": "逻辑门", "note": "数字电路的基本单元"},
        "bullets": {"items": ["与门", "或门", "非门"]}, "flow": {"nodes": ["输入", "运算", "输出"]},
        "compare": {"left_title": "A", "left": ["一"], "right_title": "B", "right": ["二"]},
        "table": {"headers": ["门", "输出"], "rows": [["与门", "1"], ["或门", "1"]]},
        "code": {"language": "python", "code": "print(1 and 0)  # 与门", "highlight": [1]},
        "quote": {"text": "电脑其实是在做无数次简单的判断", "source": "作者"}, "stat": {"value": "10亿", "label": "每秒开关次数"},
        "chart": {"chart": "bar", "labels": ["A", "B", "C"], "values": [1, 3, 2], "unit": "次"},
    }

    def test_every_layout_renders_in_every_theme(self):
        for theme in cards.ALL_THEMES:
            for (W, H) in ((270, 480), (480, 270)):
                for kind, data in self.KINDS.items():
                    p = Page(kind=kind, heading="三种基本逻辑门", start=0, end=3, text="与门、或门、非门。", data=data,
                             reveals=[0, 1, 2] if kind in ("bullets", "flow", "table") else [])
                    img = cards.render_page(p, W, H, theme, 1, 5, vis=2)
                    self.assertEqual(img.size, (W, H), (theme, kind))
                    self.assertGreater(len(set(img.resize((40, 40)).getdata())), 3, (theme, kind))

    def test_studio_theme_is_dark_and_classroom_keeps_light(self):
        p = Page(kind="table", heading="规则", start=0, end=3, data=self.KINDS["table"])
        dark = studio_cards.render_page(p, 270, 480, "studio").convert("L").resize((1, 1)).getpixel((0, 0))
        light = cards.render_page(p, 270, 480, "classroom").convert("L").resize((1, 1)).getpixel((0, 0))
        self.assertLess(dark, 80)
        self.assertGreater(light, 170)
        # 规则切分的浅色主题照旧由 cards.py 画（1.0.x 的版式）
        kw = Page(kind="keyword", heading="逻辑门", start=0, end=3, data={"keyword": "逻辑门"})
        self.assertGreater(cards.render_page(kw, 270, 480, "classroom").convert("L").resize((1, 1)).getpixel((0, 0)), 170)

    def test_reveal_progress(self):
        p = Page(kind="table", heading="规则", start=0, end=3, data=self.KINDS["table"], reveals=[0, 1])
        a = studio_cards.render_page(p, 270, 480, "studio", vis=1)
        b = studio_cards.render_page(p, 270, 480, "studio", vis=2)
        self.assertNotEqual(a.tobytes(), b.tobytes())

    def test_illustration_background(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            pic = Path(d) / "x.png"
            Image.new("RGB", (64, 96), (250, 30, 30)).save(pic)
            p = Page(kind="keyword", heading="芯片", start=0, end=3, data={"keyword": "芯片", "image_path": str(pic)})
            img = cards.render_page(p, 270, 480, "classroom")   # 有插图时交给 studio_cards
            r, g, b = img.resize((1, 1)).getpixel((0, 0))
            self.assertGreater(r, g)       # 红色插图透过蒙版

    def test_code_tokens(self):
        toks = studio_cards.code_tokens('print("hi", 1)  # 注释', "python")
        kinds = {k for _, k in toks}
        self.assertTrue({"str", "num", "comment"} <= kinds)


class IllustrateTest(unittest.TestCase):
    def pages(self):
        return [Page(kind="title", heading="a", start=0, end=1), Page(kind="keyword", heading="b", start=1, end=2)]

    def test_not_configured_without_upstream(self):
        self.assertFalse(illustrate.configured({}))
        self.assertIn("设置 → 素材来源设置", illustrate.NOT_CONFIGURED)

    def test_generate_sets_image_and_stops_on_error(self):
        from PIL import Image
        spec = {"pages": [{"illustrate": True, "image_prompt": "chip"}, {"illustrate": True, "image_prompt": "board"}]}
        with tempfile.TemporaryDirectory() as d:
            def gen(prompt, save_dir):
                f = Path(save_dir) / f"{prompt}.png"
                Image.new("RGB", (8, 8)).save(f)
                return str(f)
            pages = self.pages()
            res = illustrate.generate(pages, spec, d, "9:16", generator=gen)
            self.assertEqual([r["ok"] for r in res], [True, True])
            self.assertTrue(pages[0].data["image_path"].endswith("chip.png"))
            self.assertTrue((Path(d) / "illustrations" / "board.png").is_file())

            def boom(prompt, save_dir):
                raise TimeoutError("结果不明")
            pages = self.pages()
            res = illustrate.generate(pages, spec, d, "9:16", generator=boom)
            self.assertEqual(len(res), 1)           # 出错后不再请求下一张（避免重复计费）
            self.assertNotIn("image_path", pages[0].data)
            self.assertEqual(illustrate.generate(self.pages(), {"pages": []}, d, "9:16", generator=gen), [])


class LengthModeTest(unittest.TestCase):
    P = {"target_seconds": 60, "lang": "zh", "target_chars": 270, "unit": "字"}

    def test_three_modes_prompt(self):
        strict = duration.prompt_addition(self.P, "strict")
        check = duration.prompt_addition(self.P, "check")
        self.assertIn("between 243 and 297", strict)
        self.assertNotIn("between", check)
        self.assertIn("do not count characters one by one", check)
        self.assertEqual(duration.prompt_addition(self.P, "off"), "")
        self.assertEqual(duration.normalize_mode("bogus"), "check")
        self.assertEqual(duration.DEFAULT_MODE, "check")

    def test_needs_revision_threshold(self):
        self.assertFalse(duration.needs_revision(270, 270))
        self.assertFalse(duration.needs_revision(305, 270))   # +13%
        self.assertTrue(duration.needs_revision(320, 270))    # +18.5%
        self.assertTrue(duration.needs_revision(200, 270))    # -26%
        self.assertFalse(duration.needs_revision(100, 0))

    def test_revise_prompt_and_parts(self):
        prompt, variables = duration.revise_prompt("逻辑门是……", 120, 270, "zh")
        self.assertIn("当前约 120 字", prompt)
        self.assertIn("扩写到约 270 字", prompt)
        parts = llmlog.make_parts(prompt, variables)
        self.assertEqual("".join(p["t"] for p in parts), prompt)
        named = {p["n"]: p["t"] for p in parts if p["k"] == "var"}
        self.assertEqual(named["程序数出的当前字数"], "120")
        self.assertEqual(named["目标字数"], "270")
        self.assertEqual(named["文案（写文案这一步的输出）"], "逻辑门是……")
        self.assertIn("缩写", duration.revise_prompt("x", 400, 270, "zh")[0])

    def test_length_line(self):
        line = duration.length_line({"length_mode": "check", "target_chars": 270, "first_chars": 150, "final_chars": 262,
                                     "revised": True, "unit": "字"})
        self.assertIn("程序检查", line)
        self.assertIn("150", line)
        self.assertIn("262", line)
        self.assertIn("不改写", duration.length_line({"length_mode": "check", "target_chars": 270, "first_chars": 280}))
        self.assertIn("严格", duration.length_line({"length_mode": "strict", "target_chars": 270, "first_chars": 280}))
        self.assertIn("只按段落数", duration.length_line({"length_mode": "off"}))


class CallChainTest(unittest.TestCase):
    def rows(self):
        script = "逻辑门是数字电路的基本单元。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。"
        p1 = "写一段口播。\n- video subject: 逻辑门"
        p2 = "### Video Script\n" + script + "\n请返回 JSON。"
        p3 = "## 文案（共 3 句）\n[1] 逻辑门是数字电路的基本单元。\n[2] 与门只有两个输入都为1时才输出1。\n[3] 或门只要有一个输入为1就输出1。"
        return [
            {"id": "a", "ts": 1, "step": "script", "ok": True, "messages": [{"role": "user", "content": p1}], "output": script,
             "parts": llmlog.make_parts(p1, [("视频主题", "逻辑门", "- video subject: ")]), "reasoning": "想一想"},
            {"kind": "use", "ref": "a", "note": "取作视频文案"},
            {"id": "b", "ts": 2, "step": "terms", "ok": True, "messages": [{"role": "user", "content": p2}], "output": '["logic gate"]'},
            {"id": "c", "ts": 3, "step": "director", "ok": True, "messages": [{"role": "user", "content": p3}], "output": "{}"},
        ]

    def test_annotate_chain_and_uses(self):
        out = review.annotate(self.rows())
        self.assertEqual([c["step_label"] for c in out], ["写文案", "提取关键词", "AI 导演设计分镜"])
        self.assertEqual(out[0]["uses"], ["取作视频文案"])
        self.assertEqual(out[0]["reasoning"], "想一想")
        self.assertEqual([f["call"] for f in out[1]["inputs_from"]], [1])
        self.assertEqual([f["call"] for f in out[2]["inputs_from"]], [1])
        self.assertEqual([f["call"] for f in out[0]["outputs_to"]], [2, 3])
        # 导演提示词里 3 句被「[2] 」这类编号隔开，合成一段来源
        runs = [r for r in out[2]["messages"][0]["runs"] if r["from"] == 1]
        self.assertEqual(len(runs), 1)
        self.assertIn("[2]", runs[0]["t"])
        # 第一次调用：模板 / 变量着色
        k = [(r["k"], r["n"], r["t"]) for r in out[0]["messages"][0]["runs"]]
        self.assertIn(("var", "视频主题", "逻辑门"), k)
        self.assertTrue(out[1]["uses"][0].startswith("（这条记录没有写"))

    def test_failed_call_not_a_source(self):
        rows = self.rows()
        rows[0]["ok"] = False
        rows[0]["error"] = "401"
        out = review.annotate(rows)
        self.assertEqual(out[1]["inputs_from"], [])

    def test_make_parts_and_merge_uses(self):
        parts = llmlog.make_parts("主题：猫。字数：200。主题：猫", [("主题", "猫"), ("字数", "200", "字数："), ("没有", "狗")])
        self.assertEqual("".join(p["t"] for p in parts), "主题：猫。字数：200。主题：猫")
        self.assertEqual([p["n"] for p in parts if p["k"] == "var"], ["主题", "字数"])
        merged = llmlog.merge_uses([{"id": "x"}, {"kind": "use", "ref": "x", "note": "n1"}, {"kind": "use", "ref": "y", "note": "n2"}])
        self.assertEqual(merged, [{"id": "x", "use_notes": ["n1"]}])

    def test_reasoning_extraction(self):
        raw = [{"choices": [{"message": {"content": "答案", "reasoning_content": "思考过程"}}]}]
        self.assertEqual(llmlog.reasoning_of(raw), "思考过程")
        self.assertEqual(llmlog.reasoning_of([{"choices": [{"message": {"content": "<think>想</think>答"}}]}]), "想")
        self.assertEqual(llmlog.reasoning_of([]), "")
        u = llmlog._usage([{"usage": {"prompt_tokens": 1, "completion_tokens": 1668, "total_tokens": 1669,
                                      "completion_tokens_details": {"reasoning_tokens": 1482}}}])
        self.assertEqual(u["reasoning_tokens"], 1482)


class ShotsAndReviewTest(unittest.TestCase):
    def test_stills_manifest_and_shell_reader(self):
        with tempfile.TemporaryDirectory() as data:
            tdir = Path(data) / "storage" / "tasks" / "task-1"
            tdir.mkdir(parents=True)
            res = director.direct(SCRIPT, "逻辑门", [], 30, 270, 480, lambda p, parts, a: json.dumps(GOOD, ensure_ascii=False))
            stills = shots.save_stills(res["pages"], tdir, 270, 480, "studio")
            self.assertTrue(all((tdir / s["image"]).is_file() and (tdir / s["thumb"]).is_file() for s in stills))
            entries = [shots.page_entry(k, p, tdir, st, res["spec"]["pages"][k - 1]["sentences"])
                       for k, (p, st) in enumerate(zip(res["pages"], stills), 1)]
            shots.write_manifest(tdir, {"task_id": "task-1", "mode": "ai", "W": 270, "H": 480}, entries)
            (tdir / "script.json").write_text(json.dumps({"script": SCRIPT, "params": {"video_subject": "逻辑门",
                                                                                      "video_source": "knowledge_cards_ai"}}), "utf-8")
            sb = review.storyboard(Path(data), "task-1")
            pages = sb["manifest"]["pages"]
            self.assertEqual(pages[2]["kind_label"], "表格")
            self.assertEqual(pages[2]["sentences"], [3, 4, 5])
            self.assertIn("与门只有两个输入", pages[2]["text"])
            self.assertGreater(pages[2]["chars"], 20)
            self.assertEqual(pages[0]["subtitle"], "数字电路的基本单元")
            tasks = review.list_tasks(Path(data))
            self.assertEqual(tasks[0]["id"], "task-1")
            self.assertTrue(tasks[0]["storyboard"])
            self.assertIsNotNone(review.task_file(Path(data), "task-1", pages[0]["thumb"]))
            self.assertIsNone(review.task_file(Path(data), "task-1", "../task-1/script.json"))
            self.assertIsNone(review.task_file(Path(data), "task-1", "script.txt"))
            self.assertIsNone(review.task_dir(Path(data), "../x"))
            self.assertIsNone(review.storyboard(Path(data), "nope"))


if __name__ == "__main__":
    unittest.main()
