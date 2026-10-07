"""口播拆句、读音替换、中英混排计数。全部本地规则，不调用模型。"""

import re

SENT_END = "。！？!?；;…"
CLAUSE = "，,、：:"
CHARS_PER_MINUTE = 280  # 中文口播大约每分钟 260–300 字


def is_cjk(ch):
    o = ord(ch)
    return (0x2E80 <= o <= 0x9FFF) or (0xF900 <= o <= 0xFAFF) or (0xFF00 <= o <= 0xFFEF) or (0x3000 <= o <= 0x303F)


def spoken_length(text):
    """估算朗读长度（按中文字计）：一个英文单词或一组数字约等于 1.5 个字。"""
    n = 0.0
    for tok in re.findall(r"[A-Za-z]+|\d+(?:\.\d+)?|[^\sA-Za-z\d]", text or ""):
        if tok[0].isascii() and tok[0].isalnum():
            n += 1.5 if tok[0].isalpha() else max(1.0, len(tok) * 0.8)
        elif is_cjk(tok) and tok not in SENT_END + CLAUSE and tok not in "“”‘’（）《》【】「」":
            n += 1
    return n


def estimate_seconds(text):
    return spoken_length(text) / CHARS_PER_MINUTE * 60


def split_sentences(text):
    """按句末标点拆句，保留标点。"""
    text = re.sub(r"\s+", " ", (text or "").strip())
    if not text:
        return []
    out, buf = [], ""
    i = 0
    while i < len(text):
        ch = text[i]
        buf += ch
        if ch in SENT_END:
            # 吞掉紧跟的引号/括号
            while i + 1 < len(text) and text[i + 1] in "”’」）)\"' ":
                i += 1
                if text[i] != " ":
                    buf += text[i]
            if buf.strip():
                out.append(buf.strip())
            buf = ""
        i += 1
    if buf.strip():
        out.append(buf.strip())
    return out


def split_chunks(text, max_len=24):
    """把口播拆成字幕/配音句块：先按句，再把过长的句子按逗号切开。

    返回 [(chunk_text, is_sentence_end)]。
    """
    chunks = []
    for sent in split_sentences(text):
        if spoken_length(sent) <= max_len:
            chunks.append((sent, True))
            continue
        parts, buf = [], ""
        for ch in sent:
            buf += ch
            if ch in CLAUSE and spoken_length(buf) >= 6:
                parts.append(buf)
                buf = ""
        if buf:
            parts.append(buf)
        # 合并过短的片段，避免一闪而过的字幕
        merged = []
        for p in parts:
            if merged and spoken_length(merged[-1]) + spoken_length(p) <= max_len:
                merged[-1] += p
            else:
                merged.append(p)
        # 仍然过长的片段（没有逗号）硬切
        final = []
        for p in merged:
            while spoken_length(p) > max_len * 1.6:
                cut = _hard_cut_index(p, max_len)
                final.append(p[:cut])
                p = p[cut:]
            final.append(p)
        for j, p in enumerate(final):
            chunks.append((p, j == len(final) - 1))
    return [(c.strip(), end) for c, end in chunks if c.strip()]


def _hard_cut_index(text, max_len):
    n = 0.0
    for i, ch in enumerate(text):
        n += 1 if is_cjk(ch) else 0.5
        if n >= max_len:
            # 不在英文单词中间切
            j = i + 1
            while j < len(text) and text[j].isascii() and text[j].isalnum() and text[j - 1].isascii() and text[j - 1].isalnum():
                j += 1
            return j
    return len(text)


def subtitle_text(chunk):
    """字幕去掉句末标点，更干净。"""
    return chunk.rstrip("，,。；;：:、 ")


def apply_pronunciation(text, mapping):
    """只替换送给 TTS 的文本。英文词按词边界匹配，避免把 OpenAI 里的 AI 拆开。"""
    if not mapping:
        return text
    for src in sorted(mapping, key=len, reverse=True):
        dst = mapping[src]
        if not src:
            continue
        if re.fullmatch(r"[A-Za-z0-9_.+#-]+", src):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(src) + r"(?![A-Za-z0-9])"
            text = re.sub(pattern, lambda m: dst, text)
        else:
            text = text.replace(src, dst)
    return text


def parse_pronunciation_lines(text):
    out = {}
    for line in (text or "").splitlines():
        if "=" in line:
            a, b = line.split("=", 1)
            if a.strip():
                out[a.strip()] = b.strip()
    return out


def bigrams(text):
    """中文二元组，用于粗略的文本相似度。"""
    s = re.sub(r"[^\w一-鿿]", "", (text or "").lower())
    toks = set()
    for w in re.findall(r"[a-z0-9]+", s):
        if len(w) > 1:
            toks.add(w)
    cjk = re.sub(r"[a-z0-9_]", "", s)
    for i in range(len(cjk) - 1):
        toks.add(cjk[i : i + 2])
    return toks


def overlap(a, b):
    ta, tb = bigrams(a), bigrams(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


def format_srt_time(sec):
    ms = int(round(max(0.0, sec) * 1000))
    h, ms = divmod(ms, 3600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def review_terms(text):
    """审片清单：英文术语、数字、年份，这些最容易读错。"""
    terms = set(re.findall(r"[A-Za-z][A-Za-z0-9+#.-]*", text or ""))
    nums = set(re.findall(r"\d+(?:\.\d+)?%?|\d{4}年", text or ""))
    return sorted(terms), sorted(nums)
