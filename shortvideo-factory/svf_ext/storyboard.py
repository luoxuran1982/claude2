"""把文案切成「讲解页」并按句子特征选版式。纯 Python，不画图、不依赖上游，方便单元测试。

流程：
1. 拆句：按句末标点和换行拆；太长的句子在逗号处再切开。
2. 定时：有字幕文件（subtitle.srt）时，按「文案字数 ↔ 字幕字数」对齐，得到每句话真实的开始/结束时间
   （逐句字幕和逐词字幕都适用）；没有字幕时按每句的朗读长度把配音总时长按比例分配。
3. 分页：连续的「第一…第二…」「首先…然后…最后」、主语同类的并列句（与门…或门…非门…）合成一页要点或对比；
   单句里有「先…再…最后」用流程箭头；有「A…，而 B…」用对比两栏；有「包括 A、B、C」用编号要点；
   短句里有带单位的数字用大数字卡；其余用关键词卡。太短的普通句子和下一句合并，避免画面一闪而过。
4. 要点逐条出现：每条要点对应的那句话开始说时，这一条才亮起来。

时间单位都是秒。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

SENT_END = "。！？!?；;…"
CLOSERS = "”’」』）)\"'】》"
CLAUSE_SPLIT = re.compile(r"[，,：:；;、]")
PUNCT_STRIP = "，,。．.！!？?；;：:、…—-·~～ \t\"'“”‘’「」『』（）()【】《》"

MIN_PAGE_SECONDS = 3.2       # 普通句子短于这个时长就和下一句合并
MAX_MERGED_SECONDS = 9.0     # 合并后的一页不超过这个时长
MAX_ITEMS = 5                # 一页最多几条要点 / 几个流程节点
TAIL_SECONDS = 0.6           # 时间线比配音多留一点，避免最后一帧黑屏

# 列举、顺序、对比的提示词
ORDINAL = re.compile(
    r"^(?:第[一二三四五六七八九十\d]+(?:点|步|个|种|条|层|阶段)?[，,、:：]?|首先[，,]?|其次[，,]?|再次[，,]?|然后[，,]?|接着[，,]?|"
    r"随后[，,]?|之后[，,]?|最后[，,]?|另外[，,]?|此外[，,]?|[一二三四五六七八九十]是|[一二三四五六七八九十][、.．]|"
    r"\d{1,2}[.、．)）]\s*|[①②③④⑤⑥⑦⑧⑨⑩]\s*|[-•*]\s+)"
)
SEQ_MARK = re.compile(r"(?:^|[，,；;。])\s*(?:首先|先|然后|接着|再|之后|随后|最后|最终|进而)")
ARROW = re.compile(r"\s*(?:→|->|—>|＞)\s*")
FROM_TO = re.compile(r"从([^，,。；;从到]{1,10}?)(?:到|变成|变为)([^，,。；;到]{1,10}?)(?:(?:，|,)?再到([^，,。；;到]{1,10}?))?(?:[，,。；;]|$)")
CONTRAST = ("，而", "，但是", "，但", "，却", "；而", "；但", "，然而", "。而", "，相比之下", "，相反", "，另一方面", "，不同的是", "，反之")
LIST_LEAD = re.compile(r"(?:包括|包含|分为|分成|分别是|有|是|如|比如|例如|像|：|:)")
_UNITS = r"%|％|倍|年|个|次|种|秒|分钟|小时|天|元|美元|伏|V|GHz|MHz|nm|纳米|位|bit|字节|KB|MB|GB|TB|岁|米|公里|千米|度|℃"
NUMBER_UNIT = re.compile(rf"(\d+(?:\.\d+)?\s*(?:(?:万亿|万|亿|千|百)\s*(?:{_UNITS})?|(?:{_UNITS})))")
TOPIC = re.compile(
    r"^([^，,。；;：:！？!?\s]{1,8}?)(?:是|指的是|指|叫做|叫|称为|被称为|只有|只要|则|就是|就|会|能够|能|可以|负责|用来|用于|表示|代表|的作用|的意思|：|:)"
)
TOPIC_BAD_PREFIX = ("这", "那", "它", "他", "她", "我", "你", "如果", "因为", "所以", "但是", "但", "而", "当", "其实", "比如", "例如",
                    "也就是", "也", "还", "就", "只", "都", "又", "再", "先", "把", "被", "让", "在", "从", "对", "给", "跟", "和", "与其",
                    "今天", "现在", "接下来", "下面", "大家", "简单", "总之", "总的来说", "所谓")
# 不该出现在关键词里的字（虚词、代词、量词等）
STOP_CHARS = set("的了是在和就都而及与也很把被这那个们我你他它她之其或但如果因为所以一不有为以对上下中会能可要说到着吗呢吧啊么什样种些每多少")


@dataclass
class Sentence:
    text: str
    start: float = 0.0
    end: float = 0.0
    kind: str = "plain"          # plain / ordinal
    marker: str = ""
    rest: str = ""
    topic: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass
class Page:
    kind: str                    # title / keyword / text / bullets / compare / flow / stat / stock
    heading: str
    start: float
    end: float
    text: str = ""
    data: dict = field(default_factory=dict)
    reveals: list = field(default_factory=list)   # 第 k 条要点亮起的时间（秒，绝对时间）

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def visible_at(self, t: float) -> int:
        """t 时刻已经亮起几条要点（没有要点的版式返回 0）。"""
        return sum(1 for r in self.reveals if r <= t + 1e-6)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "heading": self.heading, "start": round(self.start, 3), "end": round(self.end, 3),
                "text": self.text, "data": self.data, "reveals": [round(r, 3) for r in self.reveals]}


# ---------------------------------------------------------------- 文本工具

def is_cjk(ch: str) -> bool:
    o = ord(ch)
    return 0x3400 <= o <= 0x9FFF or 0xF900 <= o <= 0xFAFF


def norm_chars(text: str) -> str:
    """只留下会被读出来的字（汉字、字母、数字），用于文案与字幕对齐。"""
    return "".join(ch.lower() for ch in (text or "") if ch.isalnum())


def spoken_length(text: str) -> float:
    """朗读长度（按汉字计）：一个英文单词约 1.5 个字，一串数字约每位 0.8 个字。"""
    n = 0.0
    for tok in re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|\S", text or ""):
        if tok[0].isascii() and tok[0].isalpha():
            n += 1.5
        elif tok[0].isdigit():
            n += max(1.0, len(tok) * 0.8)
        elif tok.isalnum():
            n += 1
    return n


def clean_script(text: str) -> str:
    """去掉停顿标签（上游的 [pause 1s] 之类）、Markdown 符号和多余空白，保留换行作分句依据。"""
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    try:   # 上游的停顿标签规则；单独测试时没有上游，用近似规则
        from app.utils.utils import remove_pause_tags
        text = remove_pause_tags(text)
    except Exception:
        text = re.sub(r"[\[\(]\s*(?:pause|break|停顿|暂停)\b[^\]\)]*[\]\)]", " ", text, flags=re.I)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*", "", text, flags=re.M)
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def split_sentences(text: str, max_len: float = 46) -> list[str]:
    """按句末标点和换行拆句（保留标点）；太长的句子在逗号处切开。"""
    out: list[str] = []
    for para in clean_script(text).split("\n"):
        para = para.strip()
        if not para:
            continue
        buf = ""
        i = 0
        while i < len(para):
            ch = para[i]
            buf += ch
            end = ch in SENT_END or (ch == "." and (i + 1 == len(para) or para[i + 1] == " ") and i > 0 and para[i - 1].isalpha())
            if end:
                while i + 1 < len(para) and para[i + 1] in CLOSERS:
                    i += 1
                    buf += para[i]
                if norm_chars(buf):
                    out.append(buf.strip())
                buf = ""
            i += 1
        if norm_chars(buf):
            out.append(buf.strip())
    # 「有三个要点：第一，……」在冒号处断开，让「第一」成为句首
    split_colon: list[str] = []
    for s in out:
        m = re.search(r"[：:](?=\s*(?:第[一二三四五六七八九十\d]|首先|一是|1[.、．]|①))", s)
        if m and norm_chars(s[:m.end()]) and norm_chars(s[m.end():]):
            split_colon += [s[:m.end()].strip(), s[m.end():].strip()]
        else:
            split_colon.append(s)
    result: list[str] = []
    for s in split_colon:
        if spoken_length(s) <= max_len:
            result.append(s)
            continue
        parts, cur = [], ""
        for ch in s:
            cur += ch
            if ch in "，," and spoken_length(cur) >= max_len * 0.45:
                parts.append(cur)
                cur = ""
        if cur:
            if parts and spoken_length(cur) < 6:
                parts[-1] += cur
            else:
                parts.append(cur)
        result += [p.strip() for p in parts if norm_chars(p)]
    return result


def short(text: str, limit: int = 14) -> str:
    """压成一行能放下的短语：去掉序号和结尾标点，优先取第一个分句。"""
    t = ORDINAL.sub("", (text or "").strip(), count=1).strip(PUNCT_STRIP)
    first = CLAUSE_SPLIT.split(t)[0].strip(PUNCT_STRIP)
    if len(first) >= 4 or len(t) <= limit:
        t = first if len(first) >= 4 else t
    t = t.strip(PUNCT_STRIP)
    if len(t) > limit:
        t = t[: limit - 1].rstrip(PUNCT_STRIP) + "…"
    return t


def condense(text: str, limit: int = 30) -> str:
    """一句话的精简版（保留逗号，去掉句末标点），超长时在分句边界截断。"""
    t = ORDINAL.sub("", (text or "").strip(), count=1).strip(PUNCT_STRIP)
    if len(t) <= limit:
        return t
    out = ""
    for part in re.split(r"(?<=[，,；;])", t):
        if len(out) + len(part) > limit:
            break
        out += part
    out = out.strip(PUNCT_STRIP)
    return out if len(out) >= limit * 0.4 else t[: limit - 1].rstrip(PUNCT_STRIP) + "…"


def topic_of(text: str) -> str:
    """句子开头的主语（「与门只有…」→ 与门），没有明显主语时返回空。"""
    t = ORDINAL.sub("", (text or "").strip(), count=1).strip()
    m = TOPIC.match(t)
    if not m:
        return ""
    topic = m.group(1).strip(PUNCT_STRIP)
    if not topic or topic.startswith(TOPIC_BAD_PREFIX) or not norm_chars(topic):
        return ""
    if len(topic) == 1 and not is_cjk(topic):
        return ""
    return topic


def analyze(text: str) -> Sentence:
    s = Sentence(text=text)
    m = ORDINAL.match(text.strip())
    if m and len(text.strip()) > len(m.group(0)) + 1:
        s.kind, s.marker, s.rest = "ordinal", m.group(0).strip(), text.strip()[m.end():].strip()
    s.topic = topic_of(text)
    return s


# ---------------------------------------------------------------- 关键词

def extract_keywords(script: str, subject: str = "", limit: int = 14) -> list[str]:
    """全文关键词：引号/书名号里的词、英文缩写、句子主语、主题，以及重复出现的 2–4 字词。按长度从长到短返回。"""
    text = clean_script(script)
    score: Counter = Counter()
    for m in re.finditer(r"[「“《『‘\"]([^」”》』’\"\n]{1,12})[」”》』’\"]", text):
        score[m.group(1).strip()] += 3
    cjk_ratio = sum(is_cjk(c) for c in text) / max(1, len(norm_chars(text)))
    if cjk_ratio > 0.3:
        for m in re.finditer(r"(?<![A-Za-z])([A-Z][A-Za-z0-9+#]{1,11}|[A-Za-z]+\d+[A-Za-z0-9]*)(?![A-Za-z])", text):
            score[m.group(1)] += 2
    subj = (subject or "").strip(PUNCT_STRIP)
    if subj and len(subj) <= 12:
        score[subj] += 3
    topics = set()
    for s in split_sentences(text):
        t = topic_of(s)
        if t and len(t) <= 6:
            score[t] += 2
            topics.add(t)
    # 重复出现的汉字词（2–4 字、不含虚词）；长词与其中的短词次数相同时只留长词
    runs = re.findall(r"[㐀-鿿]+", text)
    grams: Counter = Counter()
    for run in runs:
        for n in (2, 3, 4):
            for i in range(len(run) - n + 1):
                g = run[i:i + n]
                if not (set(g) & STOP_CHARS):
                    grams[g] += 1
    repeated = {g: c for g, c in grams.items() if c >= 2}
    def straddles(g: str) -> bool:   # 「门只」「门组合」这类横跨在主语词边界上的片段
        for t in topics:
            if g in t or t in g:
                continue
            if any(t.endswith(g[:k]) or t.startswith(g[-k:]) for k in range(1, min(len(g), len(t)))):
                return True
        return False

    for g, c in repeated.items():
        if any(len(o) > len(g) and g in o and repeated[o] >= c for o in repeated) or straddles(g):
            continue
        score[g] += c
    words = [w for w, _ in score.most_common() if w and len(w) <= 12]
    # 去掉被更长关键词完全覆盖、得分又不高的词
    keep: list[str] = []
    for w in words:
        if any(w in k and len(k) > len(w) and score[k] >= score[w] for k in keep):
            continue
        keep.append(w)
        if len(keep) >= limit:
            break
    return sorted(keep, key=len, reverse=True)


def highlight_spans(text: str, keywords: list[str]) -> list[tuple[str, bool]]:
    """把文字切成 [(片段, 是否关键词)]，关键词按长的优先匹配，不重叠。"""
    marks = [False] * len(text)
    for kw in sorted({k for k in keywords if k}, key=len, reverse=True):
        start = 0
        while True:
            i = text.find(kw, start)
            if i < 0:
                break
            if not any(marks[i:i + len(kw)]):
                for j in range(i, i + len(kw)):
                    marks[j] = True
            start = i + len(kw)
    out: list[tuple[str, bool]] = []
    for ch, mk in zip(text, marks):
        if out and out[-1][1] == mk:
            out[-1] = (out[-1][0] + ch, mk)
        else:
            out.append((ch, mk))
    return out


# ---------------------------------------------------------------- 字幕与定时

def _srt_time(s: str) -> float:
    h, m, rest = s.strip().replace(".", ",").split(":")
    sec, ms = (rest.split(",") + ["0"])[:2]
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms.ljust(3, "0")[:3]) / 1000


def parse_srt(text: str) -> list[tuple[float, float, str]]:
    cues = []
    for block in re.split(r"\n\s*\n", (text or "").replace("\r\n", "\n").strip()):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        for k, ln in enumerate(lines):
            if "-->" in ln:
                a, b = ln.split("-->", 1)
                try:
                    start, end = _srt_time(a), _srt_time(b.split()[0])
                except (ValueError, IndexError):
                    break
                body = " ".join(lines[k + 1:]).strip()
                if norm_chars(body) and end >= start:
                    cues.append((start, end, body))
                break
    cues.sort(key=lambda c: c[0])
    return cues


def sentence_times(sentences: list[str], cues: list[tuple[float, float, str]], audio_duration: float) -> list[tuple[float, float]]:
    """每句话「开始说 / 说完」的时间。有字幕时按字数对齐字幕，否则按朗读长度比例分配。"""
    n = len(sentences)
    if n == 0:
        return []
    total = max(0.5, float(audio_duration or 0))
    lens = [len(norm_chars(s)) for s in sentences]
    cue_lens = [len(norm_chars(c[2])) for c in cues]
    if cues and sum(cue_lens) > 0 and sum(lens) > 0:
        scale = sum(cue_lens) / sum(lens)
        cum = [0]
        for c in cue_lens:
            cum.append(cum[-1] + c)

        def at(pos: float, prefer_start: bool) -> float:
            x = pos * scale
            for j, (a, b, _) in enumerate(cues):
                lo, hi = cum[j], cum[j + 1]
                if hi == lo:
                    continue
                if prefer_start and x < hi - 1e-9 and x >= lo - 1e-9:
                    return a + (b - a) * max(0.0, x - lo) / (hi - lo)
                if not prefer_start and x <= hi + 1e-9 and x > lo + 1e-9:
                    return a + (b - a) * (x - lo) / (hi - lo)
            return cues[-1][1] if x > 0 else cues[0][0]

        out = []
        pos = 0
        for ln in lens:
            out.append((at(pos, True), at(pos + max(ln, 1), False)))
            pos += ln
        return [(min(a, b), max(a, b)) for a, b in out]
    weights = [max(1.0, spoken_length(s)) for s in sentences]
    tot_w = sum(weights)
    out, t = [], 0.0
    for w in weights:
        d = total * w / tot_w
        out.append((t, t + d))
        t += d
    return out


def _contiguous(spans: list[tuple[float, float]], total: float) -> list[tuple[float, float]]:
    """把「说话时间」变成首尾相接的画面时间：每页从上一页结束开始，换页点放在下一句开口前 0.15 秒。"""
    out = []
    n = len(spans)
    prev_end = 0.0
    for k in range(n):
        start = prev_end
        if k + 1 < n:
            nxt_start, this_end = spans[k + 1][0], spans[k][1]
            end = max(this_end, nxt_start - 0.15) if nxt_start > this_end else nxt_start
            end = max(end, start + 0.4)
        else:
            end = max(total, start + 0.4)
        out.append((start, end))
        prev_end = end
    return out


# ---------------------------------------------------------------- 单句版式

def _contrast_parts(text: str) -> tuple[str, str] | None:
    t = text.strip()
    for c in CONTRAST:
        i = t.find(c)
        if i > 3 and len(t) - i - len(c) > 3:
            return t[:i].strip(PUNCT_STRIP), t[i + len(c):].strip(PUNCT_STRIP)
    m = re.search(r"(?:一方面)(.+?)[，,；;](?:另一方面)(.+)", t)
    if m:
        return m.group(1).strip(PUNCT_STRIP), m.group(2).strip(PUNCT_STRIP)
    return None


def _flow_nodes(text: str) -> list[str] | None:
    t = text.strip().strip(PUNCT_STRIP)
    if ARROW.search(t):
        nodes = [n.strip(PUNCT_STRIP) for n in ARROW.split(t) if n.strip(PUNCT_STRIP)]
        if 2 <= len(nodes) <= MAX_ITEMS + 1:
            return [short(n, 10) for n in nodes[:MAX_ITEMS]]
    if len(SEQ_MARK.findall(t)) >= 2:
        parts = [p for p in re.split(r"[，,；;。]", t) if norm_chars(p)]
        nodes = []
        for p in parts:
            p2 = re.sub(r"^\s*(?:首先|先|然后|接着|再|之后|随后|最后|最终|进而)", "", p).strip(PUNCT_STRIP)
            if p2:
                nodes.append(short(p2, 10))
        if 2 <= len(nodes) <= MAX_ITEMS:
            return nodes
    m = FROM_TO.search(t)
    if m:
        nodes = [g.strip(PUNCT_STRIP) for g in m.groups() if g and g.strip(PUNCT_STRIP)]
        if len(nodes) >= 2:
            return [short(n, 10) for n in nodes]
    return None


def _list_items(text: str) -> tuple[str, list[str]] | None:
    """「常见的逻辑门有与门、或门和非门」→（常见的逻辑门, [与门, 或门, 非门]）。"""
    for clause in re.split(r"[，,。；;！？!?]", text):
        if clause.count("、") < 1:
            continue
        first = clause.find("、")
        lead_end = 0
        for m in LIST_LEAD.finditer(clause[:first]):
            lead_end = m.end()
        lead, body = clause[:lead_end], clause[lead_end:]
        items = [x.strip(PUNCT_STRIP) for x in body.split("、")]
        if items:   # 最后一项里的「和 / 以及 / 还有」再拆一次（不拆「与」，免得把「与门」拆开）
            last = re.split(r"以及|还有|或者|和|及", items[-1], maxsplit=1)
            items = items[:-1] + last
        items = [x.strip(PUNCT_STRIP) for x in items if x.strip(PUNCT_STRIP)]
        if 3 <= len(items) <= 8 and all(1 <= len(x) <= 10 for x in items):
            head = re.sub(r"(包括|包含|分为|分成|分别是|有|是|如|比如|例如|像|：|:)$", "", lead.strip()).strip(PUNCT_STRIP)
            return head, items[:MAX_ITEMS + 1]
    return None


def _stat(text: str) -> tuple[str, str] | None:
    if spoken_length(text) > 40:
        return None
    m = NUMBER_UNIT.search(text)
    if not m:
        return None
    clause = next((c for c in re.split(r"[，,。；;！？!?]", text) if m.group(1) in c), text)
    before, _, after = clause.partition(m.group(1))
    label = before.strip(PUNCT_STRIP) if len(before.strip(PUNCT_STRIP)) >= 4 else (before + after).strip(PUNCT_STRIP)
    if len(label) < 2:
        label = short(text, 16)
    return m.group(1).replace(" ", ""), short(label, 18)


def _common_suffix(words: list[str]) -> str:
    suf = ""
    for chars in zip(*[w[::-1] for w in words]):
        if len(set(chars)) == 1:
            suf = chars[0] + suf
        else:
            break
    return suf


# ---------------------------------------------------------------- 分页

def _time_of(sentences: list[Sentence], idx: int) -> float:
    return sentences[idx].start


def build_pages(script: str, subject: str = "", cues=None, audio_duration: float = 0.0) -> tuple[list[Page], list[str]]:
    """文案 → 讲解页列表（首尾相接、覆盖整条配音）和全文关键词。"""
    raw = split_sentences(script)
    if not raw:
        raw = [subject or "知识讲解"]
    total = max(float(audio_duration or 0), 1.0)
    spans = sentence_times(raw, list(cues or []), total)
    frames = _contiguous(spans, total + TAIL_SECONDS)
    sents = [analyze(t) for t in raw]
    for s, (a, b) in zip(sents, frames):
        s.start, s.end = a, b
    keywords = extract_keywords(script, subject)
    pages: list[Page] = []
    n = len(sents)
    i = 0

    def add(kind, heading, j0, j1, data=None, reveals=None, text=None):
        pages.append(Page(kind=kind, heading=heading, start=sents[j0].start, end=sents[j1 - 1].end,
                          text=text if text is not None else "".join(s.text for s in sents[j0:j1]),
                          data=data or {}, reveals=reveals or []))

    # 1) 开场标题页：第一句（太短就带上第二句）
    subj = (subject or "").strip().strip(PUNCT_STRIP)
    j1 = 1
    if n > 2 and sents[0].duration < 2.5 and sents[1].kind == "plain":
        j1 = 2
    first = sents[0]
    title = subj if subj and len(subj) <= 24 else (first.topic or short(first.text, 16))
    subtitle = condense(" ".join(s.text for s in sents[:j1]), 34)
    if subtitle == title:
        subtitle = ""
    add("title", title, 0, j1, {"subtitle": subtitle})
    i = j1

    while i < n:
        s = sents[i]
        # 2) 连续的序号句 → 编号要点（前一句是引子时放进标题）
        if s.kind == "ordinal":
            j = i
            while j < n and sents[j].kind == "ordinal" and j - i < MAX_ITEMS:
                j += 1
            if j - i >= 2:
                items = [short(x.rest, 16) for x in sents[i:j]]
                heading = ""
                if pages and pages[-1].kind in ("keyword", "text") and pages[-1].end - pages[-1].start < 4.5 \
                        and re.search(r"[：:]$|几|以下|如下|分为|三|四|五|两", pages[-1].text):
                    intro = pages.pop()
                    heading = short(intro.text, 14)
                    start = intro.start
                else:
                    start = s.start
                heading = heading or _heading_for(sents[i:j], keywords) or "要点"
                pages.append(Page("bullets", heading, start, sents[j - 1].end, "".join(x.text for x in sents[i:j]),
                                  {"items": items}, [x.start for x in sents[i:j]]))
                if heading and start < s.start:
                    pages[-1].reveals[0] = start
                i = j
                continue
        # 3) 主语同类的并列句（与门… 或门… 非门…）→ 对比两栏或编号要点
        if s.topic and len(s.topic) <= 6:
            j = i + 1
            while j < n and j - i < MAX_ITEMS and sents[j].topic and len(sents[j].topic) <= 6 \
                    and _common_suffix([s.topic, sents[j].topic]) and sents[j].topic != s.topic:
                j += 1
            group = sents[i:j]
            if len(group) >= 2 and _common_suffix([g.topic for g in group]):
                topics = [g.topic for g in group]
                bodies = [_after_topic(g.text, g.topic) for g in group]
                if len(group) == 2:
                    add("compare", f"{topics[0]} 与 {topics[1]}", i, j,
                        {"left_title": topics[0], "right_title": topics[1],
                         "left": _points(bodies[0]), "right": _points(bodies[1])},
                        [group[0].start, group[1].start])
                else:
                    heading = "、".join(topics) if len("、".join(topics)) <= 14 else f"几种{_common_suffix(topics)}"
                    add("bullets", heading, i, j, {"items": [f"{t}：{short(b, 16)}" for t, b in zip(topics, bodies)]},
                        [g.start for g in group])
                i = j
                continue
        # 4) 单句版式
        text = s.text
        flow = _flow_nodes(text)
        if flow:
            add("flow", s.topic or _heading_for([s], keywords) or "流程", i, i + 1, {"nodes": flow},
                _spread(s, text, flow))
            i += 1
            continue
        contrast = _contrast_parts(text)
        if contrast:
            left, right = contrast
            lt0, rt0 = topic_of(left), topic_of(right)
            lt = lt0 or _heading_for([analyze(left)], keywords, 6) or short(left, 6)
            rt = rt0 or _heading_for([analyze(right)], keywords, 6) or short(right, 6)
            add("compare", s.topic or _heading_for([s], keywords) or "对比", i, i + 1,
                {"left_title": lt, "right_title": rt, "left": _points(_after_topic(left, lt0)),
                 "right": _points(_after_topic(right, rt0))},
                [s.start, s.start + s.duration * min(0.85, max(0.3, len(left) / max(1, len(text))))])
            i += 1
            continue
        lst = _list_items(text)
        if lst:
            head, items = lst
            add("bullets", short(head, 14) if len(head) >= 2 else (s.topic or _heading_for([s], keywords) or "要点"),
                i, i + 1, {"items": items}, _spread(s, text, items))
            i += 1
            continue
        st = _stat(text)
        if st:
            add("stat", s.topic or _heading_for([s], keywords) or "关键数字", i, i + 1, {"value": st[0], "label": st[1]})
            i += 1
            continue
        # 5) 普通句子：太短就和后面的普通句子合并
        j = i + 1
        while j < n and sents[j - 1].end - s.start < MIN_PAGE_SECONDS and sents[j].kind == "plain" \
                and not _special(sents[j].text) and sents[j].end - s.start <= MAX_MERGED_SECONDS \
                and not (sents[j].topic and j + 1 < n and sents[j + 1].topic):
            j += 1
        group = sents[i:j]
        kw = s.topic or _heading_for(group, keywords)
        note = condense("".join(g.text for g in group), 40)
        if kw and len(kw) <= 10:
            add("keyword", kw, i, j, {"keyword": kw, "note": note})
        else:
            add("text", _heading_for(group, keywords) or short(group[0].text, 12), i, j, {"body": note})
        i = j

    # 收尾：首尾相接、最后一页延长到时间线终点
    for k in range(1, len(pages)):
        pages[k].start = pages[k - 1].end
    pages[0].start = 0.0
    pages[-1].end = max(pages[-1].end, total + TAIL_SECONDS)
    for p in pages:
        p.reveals = [min(max(r, p.start), p.end) for r in p.reveals]
    return pages, keywords


def _special(text: str) -> bool:
    return bool(_flow_nodes(text) or _contrast_parts(text) or _list_items(text) or _stat(text) or ORDINAL.match(text.strip()))


def _after_topic(text: str, topic: str) -> str:
    t = ORDINAL.sub("", (text or "").strip(), count=1).strip()
    if topic and t.startswith(topic):
        t = t[len(topic):]
        t = re.sub(r"^(?:则|就是|就|即|也就是)", "", t)
    return t.strip(PUNCT_STRIP)


def _points(text: str, limit: int = 2) -> list[str]:
    parts = [p.strip(PUNCT_STRIP) for p in re.split(r"[，,；;。！？!?]", text or "") if len(norm_chars(p)) >= 2]
    return [short(p, 14) for p in parts[:limit]] or [short(text, 14)]


def _spread(s: Sentence, text: str, items: list[str]) -> list[float]:
    """单句里的多个要点：按每条在句子里出现的位置估算亮起时间。"""
    out = []
    plain = text or ""
    for k, it in enumerate(items):
        core = it.rstrip("…")[:4]
        pos = plain.find(core) if core else -1
        frac = pos / max(1, len(plain)) if pos >= 0 else k / max(1, len(items))
        out.append(s.start + s.duration * min(0.9, frac))
    for k in range(1, len(out)):
        out[k] = max(out[k], out[k - 1])
    return out


def _heading_for(group: list[Sentence], keywords: list[str], limit: int = 10) -> str:
    """一页的标题：这页里出现的最长关键词，没有就用第一句的主语。"""
    text = "".join(g.text for g in group)
    for kw in keywords:   # 已按长度从长到短
        if kw in text and len(kw) <= limit:
            return kw
    for g in group:
        if g.topic:
            return g.topic
    return ""


def total_duration(pages: list[Page]) -> float:
    return pages[-1].end if pages else 0.0


def frames_count(pages: list[Page], fps: int) -> int:
    return int(math.ceil(total_duration(pages) * fps))
