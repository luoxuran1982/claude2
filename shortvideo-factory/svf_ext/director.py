"""「AI 导演」：写完文案、配好音以后，多调用一次大模型，请它把文案设计成分镜。

为什么要它：「规则切分」（storyboard.py）只能按标点切句、按句子开头的几个字猜标题和版式，常出现「由这三种基本门」
这种半截标题，也画不出表格、代码这类要先「理解内容」才能整理出来的版式。「AI 知识视频工坊」是先请大模型设计分镜
（studio/prompts.py），画面好看得多。这里照它的做法，但只设计画面、不改口播：文案和配音已经定了。

流程：
1. 把文案拆成编号的句子（和规则切分用同一个 split_sentences），连同主题、画面方向发给大模型，要求只输出 JSON；
2. 程序校验 JSON（validate）：每页要有完整短标题、合法版式、版式需要的结构化数据、对应的句子编号，所有句子按顺序各用一次；
   小毛病（标题太长、要点太多）由程序修整并记在 warnings 里；大问题（不是 JSON、句子编号乱、没有页）算不合格；
3. 不合格就把原因告诉模型、重试一次；还不行（或根本没配大模型、调用出错）就退回「规则切分」，日志里写明原因；
4. 每页的起止时间仍按配音和字幕对齐：句子 → 字幕（sentence_times），一页从它第一句开口到最后一句说完；
   要点、流程节点、表格行按这一页里每句话开始说的时间逐条亮起。

纯 Python，调用大模型的函数由调用方传入（call），方便单元测试。
"""
from __future__ import annotations

import json
import re

from .storyboard import (PUNCT_STRIP, TAIL_SECONDS, Page, _contiguous, extract_keywords, norm_chars, sentence_times,
                         split_sentences)

TYPES = ("title", "keyword", "bullets", "flow", "compare", "table", "code", "quote", "stat", "chart")
MAX_HEADING = 18
MAX_ILLUSTRATIONS = 3
MAX_SENTENCES_PER_PAGE = 5


class DirectorError(ValueError):
    """导演给的 JSON 不合格。"""


# ---------------------------------------------------------------- 提示词

TEMPLATE_HEAD = """你是知识类短视频的分镜导演。下面是一段已经写好、配音也已经录好的口播文案，程序已经把它拆成编号的句子。
请把这些句子分成若干页讲解画面（分镜）：为每页起一个完整的短标题，选一种版式，给出这种版式需要的结构化数据。
口播不能改，你只设计画面。你的输出会被程序直接解析：只输出一个 JSON 对象，不要输出解释，不要用 Markdown 代码块。

## 版式 type 与 data 字段
- title：标题卡，只用于第一页。data: {"subtitle": "副标题，不超过 20 字"}
- keyword：关键词大字，讲一个抽象概念。data: {"keyword": "不超过 8 字", "note": "不超过 24 字的补充"}
- bullets：要点列表，随口播逐条亮起。data: {"items": ["要点", ...]}，2–5 条，每条不超过 16 字
- flow：流程或因果链。data: {"nodes": ["步骤", ...]}，2–5 个节点，每个不超过 8 字
- compare：两栏对比。data: {"left_title": "", "left": ["", ...], "right_title": "", "right": ["", ...]}，每栏 1–3 条，每条不超过 14 字
- table：表格。data: {"headers": ["", ...], "rows": [["", ...], ...]}，不超过 4 列 5 行，每格不超过 8 字
- code：代码片段，只在文案确实讲到代码、命令或公式时用。data: {"language": "python", "code": "不超过 10 行", "highlight": [要强调的行号]}
- quote：引语或一句核心观点。data: {"text": "不超过 40 字", "source": "出处，可以留空"}
- stat：一个关键数字。data: {"value": "73%", "label": "不超过 16 字的说明"}
- chart：数据图，只在文案里有至少 3 个可以比较的数字时用。data: {"chart": "bar 或 line", "labels": [...], "values": [数字, ...], "unit": "单位"}

## 规则
1. sentences 填这一页对应的句子编号（整数数组）。所有句子按顺序、每句恰好用一次；每页 1–4 句，后一页从前一页的下一句开始。
2. heading 是这一页的完整短标题，不超过 14 个字，必须是意思完整的短语（例如「三种基本逻辑门」「与门的规则」），
   不能是从句子里截下来的半句（例如「由这三种基本门」）。
3. 画面上的字是提纲，不要和口播逐字重复；表格、要点、数字只能来自文案，不要编造文案里没有的数字和事实。
4. 第一页用 title；相邻页不要连续 3 次用同一种版式，整体要有变化；能整理成表格、流程、对比的内容，优先用这些版式。
5. 竖屏画面窄：竖屏时每页文字更少，要点不超过 4 条，表格不超过 3 列。"""

TEMPLATE_ILLUS = """
6. 挑出最多 {n} 页最适合配一张插图的概念页（看得见、画得出的具体事物或场景），把它的 illustrate 设为 true，
   并在 image_prompt 里用 10–20 个英文单词描述画面（不要出现文字、不要人脸特写、不要商标）；其余页 illustrate 为 false，image_prompt 为空字符串。"""

TEMPLATE_SCHEMA = """

## 输出 JSON 结构
{"title": "整个视频的标题，不超过 20 字",
 "pages": [{"sentences": [1, 2], "type": "版式", "heading": "完整短标题", "data": {}, "illustrate": false, "image_prompt": ""}]}"""


def numbered(sentences: list[str]) -> str:
    return "\n".join(f"[{i}] {s}" for i, s in enumerate(sentences, 1))


def orientation_label(W: int, H: int) -> str:
    if H > W * 1.2:
        return "竖屏 9:16"
    if W > H * 1.2:
        return "横屏 16:9"
    return "方形 1:1"


def build_prompt(subject: str, sentences: list[str], orientation: str, illustrations: int = 0) -> tuple[str, list[tuple]]:
    """返回（提示词, 片段）。片段是 [(文字, 类别, 名称)]，类别 tpl=固定模板、var=程序填进去的变量；
    「模型调用」页用它给提示词着色。"""
    parts: list[tuple] = [(TEMPLATE_HEAD, "tpl", "")]
    if illustrations:
        a, b = TEMPLATE_ILLUS.split("{n}")
        parts += [(a, "tpl", ""), (str(illustrations), "var", "插图张数上限"), (b, "tpl", "")]
    parts += [(TEMPLATE_SCHEMA + "\n\n## 视频主题\n", "tpl", ""), (subject or "（没有填主题）", "var", "视频主题"),
              ("\n\n## 画面方向\n", "tpl", ""), (orientation, "var", "画面方向（按视频比例）"),
              (f"\n\n## 文案（共 ", "tpl", ""), (str(len(sentences)), "var", "句子数"), (" 句）\n", "tpl", ""),
              (numbered(sentences), "var", "编号的文案句子（程序把文案拆句后编号）"),
              ("\n\n请输出完整的 JSON。", "tpl", "")]
    return "".join(p[0] for p in parts), parts


def retry_suffix(reason: str) -> str:
    return f"\n\n## 你上一次的输出没有通过程序校验\n原因：{reason}\n请改正后重新输出完整的 JSON（仍然只输出 JSON）。"


# ---------------------------------------------------------------- 解析与校验

def extract_json(text: str):
    """去掉 ``` 代码块和 <think> 段，取出第一个完整的 JSON 对象。"""
    t = re.sub(r"<think>.*?</think>", "", str(text or ""), flags=re.S).strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```\s*$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for i, ch in enumerate(t):
        if ch == "{":
            try:
                return dec.raw_decode(t[i:])[0]
            except ValueError:
                continue
    raise DirectorError("输出里找不到完整的 JSON 对象")


def _s(v, limit: int) -> str:
    t = str(v if v is not None else "").strip()
    return t if len(t) <= limit else t[: limit - 1].rstrip(PUNCT_STRIP) + "…"


def _str_list(v, n_max: int, limit: int) -> list[str]:
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n；;]", v) if x.strip()]
    if not isinstance(v, (list, tuple)):
        return []
    return [_s(x, limit) for x in v if str(x).strip()][:n_max]


def clean_data(kind: str, data, portrait: bool, warn: list, label: str) -> tuple[str, dict]:
    """按版式整理 data；缺必需字段时把这一页改成关键词卡（记一条 warning）。返回（版式, data）。"""
    d = data if isinstance(data, dict) else {}
    out: dict = {}
    if kind == "title":
        out = {"subtitle": _s(d.get("subtitle", ""), 24)}
    elif kind == "keyword":
        out = {"keyword": _s(d.get("keyword", ""), 10), "note": _s(d.get("note", ""), 30)}
    elif kind == "bullets":
        out = {"items": _str_list(d.get("items"), 4 if portrait else 5, 18)}
        if len(out["items"]) < 1:
            out = None
    elif kind == "flow":
        out = {"nodes": _str_list(d.get("nodes"), 5, 10)}
        if len(out["nodes"]) < 2:
            out = None
    elif kind == "compare":
        out = {"left_title": _s(d.get("left_title", ""), 10) or "A", "left": _str_list(d.get("left"), 3, 16),
               "right_title": _s(d.get("right_title", ""), 10) or "B", "right": _str_list(d.get("right"), 3, 16)}
        if not out["left"] or not out["right"]:
            out = None
    elif kind == "table":
        cols = 3 if portrait else 4
        headers = _str_list(d.get("headers"), cols, 10)
        rows = [[_s(c, 10) for c in r][:cols] for r in (d.get("rows") or []) if isinstance(r, (list, tuple)) and r][:5]
        out = {"headers": headers, "rows": rows}
        if not rows:
            out = None
    elif kind == "code":
        code = str(d.get("code") or "").strip("\n")
        lines = code.splitlines()[:12]
        hl = [int(x) for x in (d.get("highlight") or []) if isinstance(x, (int, float)) or str(x).isdigit()]
        out = {"language": _s(d.get("language", "") or "text", 16), "code": "\n".join(ln[:80] for ln in lines), "highlight": hl}
        if not lines:
            out = None
    elif kind == "quote":
        out = {"text": _s(d.get("text", ""), 48), "source": _s(d.get("source", ""), 20)}
        if not out["text"]:
            out = None
    elif kind == "stat":
        out = {"value": _s(d.get("value", ""), 12), "label": _s(d.get("label", ""), 20)}
        if not out["value"]:
            out = None
    elif kind == "chart":
        labels = _str_list(d.get("labels"), 8, 10)
        try:
            values = [float(x) for x in (d.get("values") or [])][:len(labels)]
        except (TypeError, ValueError):
            values = []
        out = {"chart": "line" if d.get("chart") == "line" else "bar", "labels": labels[:len(values)], "values": values,
               "unit": _s(d.get("unit", ""), 10)}
        if len(values) < 2:
            out = None
    if out is None:
        warn.append(f"{label}：版式 {kind} 缺少需要的数据，改成关键词卡")
        return "keyword", {"keyword": _s(d.get("keyword", "") or "", 10), "note": ""}
    return kind, out


def validate(obj, n_sentences: int, portrait: bool = True, max_illustrations: int = 0) -> tuple[dict, list[str]]:
    """校验导演 JSON。返回（整理后的 {"title", "pages": [...]}，修整说明）；不合格时抛 DirectorError（带中文原因）。"""
    if not isinstance(obj, dict):
        raise DirectorError("最外层不是 JSON 对象")
    pages = obj.get("pages") if "pages" in obj else obj.get("scenes")
    if not isinstance(pages, list) or not pages:
        raise DirectorError("没有 pages 数组，或数组是空的")
    warn: list[str] = []
    out = []
    expect = 1
    bad_types = 0
    illus = 0
    for k, pg in enumerate(pages, 1):
        label = f"第 {k} 页"
        if not isinstance(pg, dict):
            raise DirectorError(f"{label}不是 JSON 对象")
        sents = pg.get("sentences")
        if isinstance(sents, (int, float)):
            sents = [sents]
        if not isinstance(sents, list) or not sents:
            raise DirectorError(f"{label}没有 sentences（对应的句子编号）")
        try:
            nums = [int(x) for x in sents]
        except (TypeError, ValueError):
            raise DirectorError(f"{label}的 sentences 不是整数编号：{sents}") from None
        if any(x < 1 or x > n_sentences for x in nums):
            raise DirectorError(f"{label}的句子编号 {nums} 超出范围（共 {n_sentences} 句）")
        nums = sorted(set(nums))
        if nums != list(range(nums[0], nums[-1] + 1)):
            raise DirectorError(f"{label}的句子编号 {nums} 不连续")
        if nums[0] < expect:
            raise DirectorError(f"{label}的句子编号 {nums} 和前面的页重复了（应该从第 {expect} 句开始）")
        if nums[0] > expect:
            if out:   # 漏了句子：并进上一页
                warn.append(f"第 {expect}–{nums[0] - 1} 句没有被分到任何一页，并进了上一页")
                out[-1]["sentences"] = list(range(out[-1]["sentences"][0], nums[0]))
            else:
                warn.append(f"第 1–{nums[0] - 1} 句没有被分到任何一页，并进了第一页")
                nums = list(range(1, nums[-1] + 1))
        expect = nums[-1] + 1
        kind = str(pg.get("type") or "").strip().lower()
        if kind not in TYPES:
            bad_types += 1
            warn.append(f"{label}的版式「{kind or '空'}」不认识，改成关键词卡")
            kind = "keyword"
        heading = str(pg.get("heading") or "").strip().strip(PUNCT_STRIP)
        if not heading:
            raise DirectorError(f"{label}没有 heading（标题）")
        if len(heading) > MAX_HEADING:
            warn.append(f"{label}的标题超过 {MAX_HEADING} 字，截短了")
            heading = _s(heading, MAX_HEADING)
        if k == 1 and kind != "title":
            warn.append("第一页不是 title，程序没有改它")
        kind, data = clean_data(kind, pg.get("data"), portrait, warn, label)
        if kind == "keyword" and not data.get("keyword"):
            data["keyword"] = _s(heading, 10)
        want = bool(pg.get("illustrate")) and str(pg.get("image_prompt") or "").strip()
        item = {"sentences": nums, "type": kind, "heading": heading, "data": data, "illustrate": False, "image_prompt": ""}
        if want and max_illustrations:
            if illus < max_illustrations:
                item["illustrate"], item["image_prompt"] = True, _s(pg.get("image_prompt"), 300)
                illus += 1
            else:
                warn.append(f"{label}也想配图，但每个视频最多 {max_illustrations} 张，没有配")
        out.append(item)
    if bad_types > len(pages) / 2:
        raise DirectorError(f"一半以上的页用了不认识的版式（{bad_types}/{len(pages)}）")
    if expect <= n_sentences:
        warn.append(f"第 {expect}–{n_sentences} 句没有被分到任何一页，并进了最后一页")
        out[-1]["sentences"] = list(range(out[-1]["sentences"][0], n_sentences + 1))
    long_pages = [i + 1 for i, p in enumerate(out) if len(p["sentences"]) > MAX_SENTENCES_PER_PAGE]
    if long_pages:
        warn.append(f"第 {'、'.join(map(str, long_pages))} 页句子偏多（超过 {MAX_SENTENCES_PER_PAGE} 句），画面会停留较久")
    return {"title": _s(obj.get("title", ""), 30), "pages": out}, warn


# ---------------------------------------------------------------- 分镜 → 带时间的讲解页

def _overlap(a: str, b: str) -> float:
    """a 的字有多少出现在 b 里（只看汉字、字母、数字），0–1。"""
    na, nb = norm_chars(a), set(norm_chars(b))
    return sum(1 for ch in na if ch in nb) / len(na) if na else 0.0


def _items_for(kind: str, data: dict) -> list[str]:
    from .studio_cards import items_of
    return [str(x) for x in items_of(kind, data)]


def reveal_times(kind: str, data: dict, sents: list[str], spans: list[tuple[float, float]], start: float) -> list[float]:
    """每一条（要点、节点、表格行、对比条目、图表柱）亮起的时间。照工坊 reveal_plan：口播里明确提到某一条就在那句开始时亮，
    否则按句子进度匀速展开；只有一句时按条目在句子里的位置估算。"""
    items = _items_for(kind, data)
    n = len(items)
    if n == 0:
        return []
    m = len(sents)
    out: list[float] = []
    if m <= 1:
        a, b = spans[0] if spans else (start, start + 1)
        text = sents[0] if sents else ""
        for k, it in enumerate(items):
            core = norm_chars(it)[:3]
            pos = norm_chars(text).find(core) if core else -1
            frac = pos / max(1, len(norm_chars(text))) if pos >= 0 else k / n
            out.append(start if k == 0 else a + (b - a) * min(0.9, max(frac, k / n * 0.8)))
    else:
        for k, it in enumerate(items):
            scores = sorted(((_overlap(it, s), j) for j, s in enumerate(sents)), reverse=True)
            best, j = scores[0]
            second = scores[1][0] if len(scores) > 1 else 0.0
            if not (best >= 0.5 and best > second):
                j = min(m - 1, int(k * m / n))
            out.append(start if k == 0 else spans[j][0])
    for k in range(1, n):
        out[k] = max(out[k], out[k - 1])
    return out


def to_pages(spec: dict, sentences: list[str], cues, audio_duration: float) -> list[Page]:
    total = max(float(audio_duration or 0), 1.0)
    spans = sentence_times(sentences, list(cues or []), total)
    frames = _contiguous(spans, total + TAIL_SECONDS)
    pages: list[Page] = []
    for item in spec["pages"]:
        idx = [i - 1 for i in item["sentences"]]
        a, b = frames[idx[0]][0], frames[idx[-1]][1]
        texts = [sentences[i] for i in idx]
        p = Page(kind=item["type"], heading=item["heading"], start=a, end=b, text="".join(texts), data=dict(item["data"]))
        p.reveals = reveal_times(p.kind, p.data, texts, [spans[i] for i in idx], a)
        pages.append(p)
    for k in range(1, len(pages)):
        pages[k].start = pages[k - 1].end
    pages[0].start = 0.0
    pages[-1].end = max(pages[-1].end, total + TAIL_SECONDS)
    for p in pages:
        p.reveals = [min(max(r, p.start), p.end) for r in p.reveals]
    return pages


# ---------------------------------------------------------------- 整个导演流程

def direct(script: str, subject: str, cues, audio_duration: float, W: int, H: int, call, illustrations: int = 0,
           log=None) -> dict:
    """call(prompt, parts, attempt) → 模型输出文字（出错时抛异常或返回 "Error: …"）。

    返回 {"ok", "pages", "keywords", "spec", "sentences", "warnings", "fallback_reason", "attempts"}；
    ok=False 时 pages 是规则切分的结果。"""
    from .storyboard import build_pages
    log = log or (lambda m: None)
    sentences = split_sentences(script) or [subject or "知识讲解"]
    portrait = H > W * 1.2
    prompt, parts = build_prompt(subject, sentences, orientation_label(W, H), illustrations)
    reason = ""
    attempts = []
    for attempt in (1, 2):
        p, pp = (prompt, parts) if attempt == 1 else (prompt + retry_suffix(reason), parts + [(retry_suffix(reason), "var",
                                                                                                "上一次没通过校验的原因（程序加的）")])
        try:
            text = call(p, pp, attempt)
        except Exception as exc:  # noqa: BLE001  没配大模型、网络错误等
            reason = f"调用大模型出错：{type(exc).__name__}: {exc}"
            attempts.append({"attempt": attempt, "ok": False, "reason": reason})
            break   # 调用本身失败（不是格式问题），重试多半也一样，直接退回
        if not isinstance(text, str) or not text.strip() or text.startswith("Error:"):
            reason = f"大模型没有返回内容：{str(text or '')[:200]}"
            attempts.append({"attempt": attempt, "ok": False, "reason": reason})
            break
        try:
            spec, warns = validate(extract_json(text), len(sentences), portrait, illustrations)
        except DirectorError as exc:
            reason = str(exc)
            attempts.append({"attempt": attempt, "ok": False, "reason": reason})
            log(f"AI 导演：第 {attempt} 次输出没有通过校验（{reason}）" + ("，把原因告诉模型重试一次" if attempt == 1 else ""))
            continue
        attempts.append({"attempt": attempt, "ok": True, "warnings": warns})
        pages = to_pages(spec, sentences, cues, audio_duration)
        return {"ok": True, "pages": pages, "keywords": extract_keywords(script, subject), "spec": spec, "sentences": sentences,
                "warnings": warns, "fallback_reason": "", "attempts": attempts}
    pages, keywords = build_pages(script, subject, cues, audio_duration)
    return {"ok": False, "pages": pages, "keywords": keywords, "spec": None, "sentences": sentences, "warnings": [],
            "fallback_reason": reason, "attempts": attempts}


def summary(spec: dict) -> str:
    from .studio_cards import TYPE_LABELS
    kinds = [TYPE_LABELS.get(p["type"], p["type"]) for p in spec["pages"]]
    return f"{len(kinds)} 页（{'、'.join(kinds)}）"
