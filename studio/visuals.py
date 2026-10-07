"""画面模板。所有像素由 Pillow 在本机绘制；预览和成片共用这里的代码，所见即所得。

横版 1920×1080 与竖版 1080×1920 用同一套模板，版式按比例自适应。
"""

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import paths
from .textutil import overlap

SIZES = {"landscape": (1920, 1080), "portrait": (1080, 1920)}
COVER_SIZES = {"landscape": (1920, 1080), "portrait": (1080, 1440)}

THEMES = {
    "dark": {
        "bg": (13, 17, 28), "bg2": (25, 32, 52), "fg": (236, 240, 248), "muted": (139, 152, 178),
        "accent": (86, 168, 255), "accent2": (255, 184, 76), "card": (32, 40, 62), "card2": (42, 52, 80),
        "code_bg": (9, 12, 20), "line": (60, 72, 100), "good": (110, 214, 150), "str": (152, 220, 130),
        "comment": (110, 122, 150), "num": (255, 160, 110),
    },
    "paper": {
        "bg": (247, 243, 234), "bg2": (238, 231, 216), "fg": (40, 36, 32), "muted": (128, 118, 104),
        "accent": (192, 78, 48), "accent2": (36, 108, 140), "card": (255, 252, 245), "card2": (236, 226, 206),
        "code_bg": (36, 34, 40), "line": (210, 200, 182), "good": (60, 140, 90), "str": (160, 200, 120),
        "comment": (140, 140, 150), "num": (230, 150, 90),
    },
    "classroom": {
        "bg": (236, 243, 251), "bg2": (220, 233, 248), "fg": (22, 40, 68), "muted": (98, 118, 150),
        "accent": (28, 102, 204), "accent2": (236, 128, 36), "card": (255, 255, 255), "card2": (214, 230, 250),
        "code_bg": (24, 32, 48), "line": (190, 208, 232), "good": (40, 150, 100), "str": (150, 210, 130),
        "comment": (120, 135, 160), "num": (250, 170, 100),
    },
}

# ---------------------------------------------------------------- 字体

def _font_candidates(kind):
    win_fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    user = sorted(str(p) for p in paths.fonts_dir().glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc"))
    if kind == "mono":
        return [
            "/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf",
            str(win_fonts / "consola.ttf"), str(win_fonts / "cour.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
            "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
        ]
    bold = kind == "bold"
    return user + [
        str(win_fonts / ("msyhbd.ttc" if bold else "msyh.ttc")),
        str(win_fonts / "msyh.ttc"), str(win_fonts / "msyh.ttf"),
        str(win_fonts / "simhei.ttf"), str(win_fonts / "Deng.ttf"), str(win_fonts / "simsun.ttc"),
        "/System/Library/Fonts/PingFang.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc" if bold else "/System/Library/Fonts/STHeiti Light.ttc",
        "/System/Library/Fonts/STHeiti Medium.ttc",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf", "/Library/Fonts/Arial Unicode.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
    ]


@lru_cache(maxsize=None)
def font_path(kind="regular"):
    for c in _font_candidates(kind):
        if c and Path(c).is_file():
            return c
    if kind != "regular":
        return font_path("regular")
    return ""


def fake_bold(kind):
    """没有独立粗体文件时用描边模拟粗体。"""
    return kind == "bold" and font_path("bold") == font_path("regular")


@lru_cache(maxsize=512)
def font(size, kind="regular"):
    size = max(8, int(size))
    p = font_path(kind)
    if p:
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    try:
        return ImageFont.load_default(size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def font_status():
    return {"regular": font_path("regular"), "bold": font_path("bold"), "mono": font_path("mono")}


def _is_wide(ch):
    o = ord(ch)
    return o >= 0x2E80


# ---------------------------------------------------------------- 文字排版

def tokens(text):
    """英文单词和数字作为整体，中文逐字，便于换行。"""
    return re.findall(r"[A-Za-z0-9_.%+#/:-]+|\s+|.", text or "")


def wrap(text, fnt, max_w):
    lines = []
    for para in str(text or "").split("\n"):
        line = ""
        for tok in tokens(para):
            trial = line + tok
            if fnt.getlength(trial) <= max_w or not line:
                if not line and fnt.getlength(tok) > max_w:
                    # 超长单词硬切
                    for ch in tok:
                        if fnt.getlength(line + ch) > max_w and line:
                            lines.append(line)
                            line = ""
                        line += ch
                else:
                    line = trial
            else:
                lines.append(line.rstrip())
                line = tok.lstrip()
        # 避免行首标点
        lines.append(line.rstrip())
    out = []
    for ln in lines:
        if out and ln[:1] in "，。、；：！？）》」,.;:!?)" and len(ln) > 0:
            out[-1] += ln[0]
            ln = ln[1:]
        out.append(ln)
    return out


def balanced_wrap(text, fnt, max_w):
    """行数不变的前提下把每行宽度拉平，避免最后一行只剩一两个字。"""
    lines = wrap(text, fnt, max_w)
    k = len(lines)
    if k < 2 or "\n" in str(text):
        return lines
    w = max_w
    best = lines
    while w > max_w * 0.4:
        w *= 0.95
        trial = wrap(text, fnt, w)
        if len(trial) != k:
            break
        best = trial
    return best


def fit_text(text, max_w, max_h, size, min_size, kind="regular", max_lines=None, line_gap=1.35, balance=False):
    s = int(size)
    while True:
        f = font(s, kind)
        lines = balanced_wrap(text, f, max_w) if balance else wrap(text, f, max_w)
        lh = s * line_gap
        if (max_lines is None or len(lines) <= max_lines) and len(lines) * lh <= max_h + s * (line_gap - 1):
            return f, lines, lh
        if s <= min_size:
            limit = max(1, int((max_h + s * (line_gap - 1)) // lh))
            if max_lines:
                limit = min(limit, max_lines)
            if len(lines) > limit:
                lines = lines[:limit]
                last = lines[-1]
                while last and f.getlength(last + "…") > max_w:
                    last = last[:-1]
                lines[-1] = last + "…"
            return f, lines, lh
        s -= 2


def draw_lines(d, lines, f, lh, x, y, w, color, align="left", kind="regular", stroke=0, stroke_fill=None):
    sw = stroke or (max(1, f.size // 28) if fake_bold(kind) else 0)
    sf = stroke_fill if stroke else color
    for i, ln in enumerate(lines):
        lw = f.getlength(ln)
        if align == "center":
            xx = x + (w - lw) / 2
        elif align == "right":
            xx = x + w - lw
        else:
            xx = x
        d.text((xx, y + i * lh), ln, font=f, fill=color, stroke_width=sw, stroke_fill=sf)
    return len(lines) * lh


def text_block(d, text, box, size, min_size, color, kind="regular", align="left", valign="top", max_lines=None, line_gap=1.35, balance=None):
    x0, y0, x1, y1 = box
    if balance is None:
        balance = align == "center"
    f, lines, lh = fit_text(text, x1 - x0, y1 - y0, size, min_size, kind, max_lines, line_gap, balance)
    total = len(lines) * lh - (lh - f.size * 1.1)
    if valign == "center":
        y = y0 + (y1 - y0 - total) / 2
    elif valign == "bottom":
        y = y1 - total
    else:
        y = y0
    draw_lines(d, lines, f, lh, x0, y - f.size * 0.08, x1 - x0, color, align, kind)
    return total


# ---------------------------------------------------------------- 版式

class Layout:
    def __init__(self, W, H):
        self.W, self.H = W, H
        self.portrait = H > W
        self.s = min(W, H) / 1080
        s = self.s
        if self.portrait:
            self.m = 64 * s
            self.head = (self.m, H * 0.095, W - self.m, H * 0.095 + 120 * s)
            self.content = (self.m, H * 0.095 + 170 * s, W - self.m, H * 0.655)
            self.sub_y = H * 0.705
        else:
            self.m = 96 * s
            self.head = (self.m, 64 * s, W - self.m, 64 * s + 96 * s)
            self.content = (self.m, 200 * s, W - self.m, H - 190 * s)
            self.sub_y = H - 108 * s


@lru_cache(maxsize=12)
def _background(theme_name, W, H):
    t = THEMES[theme_name]
    img = Image.new("RGB", (W, H), t["bg"])
    top, bot = t["bg"], t["bg2"]
    grad = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), bot), Image.new("RGB", (W, H), top), grad)
    d = ImageDraw.Draw(img)
    s = min(W, H) / 1080
    if theme_name == "dark":
        step = int(48 * s)
        for x in range(step, W, step):
            for y in range(step, H, step):
                d.point((x, y), fill=t["line"])
        d.ellipse((W - 520 * s, -300 * s, W + 300 * s, 520 * s), outline=t["card2"], width=int(2 * s))
    elif theme_name == "paper":
        inset = 28 * s
        d.rectangle((inset, inset, W - inset, H - inset), outline=t["line"], width=int(2 * s))
        for y in range(int(H * 0.2), H, int(64 * s)):
            d.line((inset * 2, y, W - inset * 2, y), fill=(242, 236, 224), width=1)
    else:
        d.rectangle((0, 0, W, int(14 * s)), fill=t["accent"])
        d.rectangle((0, H - int(6 * s), W, H), fill=t["card2"])
    return img


def new_canvas(theme, W, H):
    return _background(theme, W, H).copy().convert("RGBA")


def rounded(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle([round(v) for v in box], radius=int(r), fill=fill, outline=outline, width=int(width))


# ---------------------------------------------------------------- 逐句展示计划

def scene_items(scene):
    d = scene.get("data") or {}
    t = scene.get("type")
    if t == "bullets":
        return d.get("items") or []
    if t == "flow":
        return d.get("nodes") or []
    if t == "table":
        return [" ".join(r) for r in d.get("rows") or []]
    if t == "compare":
        return (d.get("left") or []) + (d.get("right") or [])
    if t == "chart":
        return d.get("labels") or []
    return []


def reveal_plan(scene, chunk_texts):
    """每个句块显示到第几项、强调哪一项。

    口播里明确提到某一项就跳到那一项；否则按句块进度匀速展开。最后一句一定全部展开。
    返回 [(visible_count, focus_index)]。
    """
    items = scene_items(scene)
    n = len(items)
    steps = max(1, len(chunk_texts))
    out, prev = [], 0
    for j, text in enumerate(chunk_texts or [""]):
        if n == 0:
            out.append((0, -1))
            continue
        scores = sorted(((overlap(it, text), i) for i, it in enumerate(items)), reverse=True)
        best, best_i = scores[0]
        second = scores[1][0] if len(scores) > 1 else 0.0
        prog = math.ceil((j + 1) * n / steps)
        # 明确提到某一项（得分够高且唯一领先）才跳过去
        if best >= 0.3 and best > second:
            cand, focus = best_i + 1, best_i
        else:
            cand, focus = prog, None
        vis = max(prev, cand, 1)
        if j == steps - 1:
            vis = n
        vis = min(n, vis)
        if focus is None or focus >= vis:
            focus = min(vis, max(prev, cand)) - 1
        out.append((vis, focus))
        prev = vis
    return out


# ---------------------------------------------------------------- 各模板

def _draw_heading(d, L, t, text, index, total):
    x0, y0, x1, y1 = L.head
    s = L.s
    rounded(d, (x0, y0 + 14 * s, x0 + 10 * s, y1 - 14 * s), 5 * s, fill=t["accent"])
    text_block(d, text, (x0 + 32 * s, y0, x1 - 160 * s, y1), 60 * s, 34 * s, t["fg"], "bold", valign="center", max_lines=1)
    if total > 1:
        label = f"{index + 1:02d}/{total:02d}"
        f = font(28 * s, "mono")
        d.text((x1 - f.getlength(label), y0 + (y1 - y0) / 2 - 16 * s), label, font=f, fill=t["muted"])


def t_title(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = (L.m, L.head[1], L.W - L.m, L.content[3])
    title = sc.get("heading") or ctx.get("title") or ""
    sub = (sc.get("data") or {}).get("subtitle") or ""
    if ctx.get("series"):
        f = font(30 * s, "regular")
        lab = ctx["series"]
        w = f.getlength(lab) + 48 * s
        cx = (x0 + x1) / 2
        rounded(d, (cx - w / 2, y0 + (y1 - y0) * 0.18, cx + w / 2, y0 + (y1 - y0) * 0.18 + 56 * s), 28 * s, outline=t["accent"], width=2 * s)
        d.text((cx - f.getlength(lab) / 2, y0 + (y1 - y0) * 0.18 + 10 * s), lab, font=f, fill=t["accent"])
    mid = y0 + (y1 - y0) * 0.30
    text_block(d, title, (x0 + 40 * s, mid, x1 - 40 * s, mid + (y1 - y0) * 0.38), (120 if not L.portrait else 104) * s, 56 * s, t["fg"], "bold", "center", "center", max_lines=3, line_gap=1.25)
    ly = mid + (y1 - y0) * 0.38 + 30 * s
    cx = (x0 + x1) / 2
    d.rectangle((cx - 60 * s, ly, cx + 60 * s, ly + 8 * s), fill=t["accent2"])
    if sub:
        text_block(d, sub, (x0 + 80 * s, ly + 40 * s, x1 - 80 * s, ly + 200 * s), 46 * s, 30 * s, t["muted"], "regular", "center", max_lines=2)


def t_keyword(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    kw = data.get("keyword") or sc.get("heading") or ""
    note = data.get("note") or ""
    cx, cy = (x0 + x1) / 2, y0 + (y1 - y0) * 0.42
    r = min(x1 - x0, y1 - y0) * 0.42
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=t["card2"], width=int(3 * s))
    d.ellipse((cx - r * 0.78, cy - r * 0.78, cx + r * 0.78, cy + r * 0.78), fill=t["card"])
    one_line = len(kw) <= 6
    text_block(d, kw, (cx - r * 0.72, cy - r * 0.6, cx + r * 0.72, cy + r * 0.6), 140 * s, 48 * s if not one_line else 40 * s, t["accent"], "bold", "center", "center", max_lines=1 if one_line else 3, line_gap=1.2)
    if note:
        text_block(d, note, (x0 + 60 * s, cy + r + 30 * s, x1 - 60 * s, y1), 44 * s, 28 * s, t["fg"], "regular", "center", max_lines=2)


def t_stat(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    h = y1 - y0
    rounded(d, (x0 + 60 * s, y0 + h * 0.08, x1 - 60 * s, y1 - h * 0.08), 36 * s, fill=t["card"])
    text_block(d, data.get("value") or "", (x0 + 100 * s, y0 + h * 0.15, x1 - 100 * s, y0 + h * 0.62), 260 * s, 80 * s, t["accent"], "bold", "center", "center", max_lines=1)
    text_block(d, data.get("label") or "", (x0 + 120 * s, y0 + h * 0.64, x1 - 120 * s, y1 - h * 0.12), 52 * s, 30 * s, t["fg"], "regular", "center", "center", max_lines=2)


def t_bullets(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    items = (sc.get("data") or {}).get("items") or []
    n = max(len(items), 1)
    gap = 22 * s
    row_h = min(150 * s, (y1 - y0 - gap * (n - 1)) / n)
    total = n * row_h + (n - 1) * gap
    y = y0 + (y1 - y0 - total) / 2
    for i, it in enumerate(items):
        top = y + i * (row_h + gap)
        if i >= vis:
            rounded(d, (x0, top, x1, top + row_h), 22 * s, outline=t["card2"], width=2 * s)
            continue
        cur = i == focus
        rounded(d, (x0, top, x1, top + row_h), 22 * s, fill=t["card2"] if cur else t["card"])
        if cur:
            rounded(d, (x0, top, x0 + 10 * s, top + row_h), 5 * s, fill=t["accent"])
        br = min(row_h * 0.30, 34 * s)
        bx, by = x0 + 40 * s + br, top + row_h / 2
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=t["accent"] if cur else t["line"])
        fnum = font(br * 1.1, "bold")
        num = str(i + 1)
        d.text((bx - fnum.getlength(num) / 2, by - br * 0.68), num, font=fnum, fill=(255, 255, 255))
        text_block(d, it, (bx + br + 30 * s, top + 8 * s, x1 - 30 * s, top + row_h - 8 * s), min(54 * s, row_h * 0.42), 28 * s, t["fg"] if cur else t["muted"] if focus >= 0 and i != focus else t["fg"], "bold" if cur else "regular", "left", "center", max_lines=2)


KEYWORDS = set(
    "def class return if elif else for while in import from as with try except finally raise lambda yield pass break continue "
    "and or not is None True False function const let var new this async await public private static void int float double "
    "char bool struct include using namespace select from where group by order join insert update delete create table".split()
)


def _code_tokens(line, lang):
    comment_mark = "--" if lang.lower() in ("sql",) else ("//" if lang.lower() in ("c", "cpp", "c++", "java", "javascript", "js", "ts", "typescript", "go", "rust", "swift") else "#")
    out = []
    pattern = re.compile(r"(\"[^\"]*\"|'[^']*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)")
    idx = line.find(comment_mark)
    code_part, comment = (line[:idx], line[idx:]) if idx >= 0 and line[:idx].count('"') % 2 == 0 and line[:idx].count("'") % 2 == 0 else (line, "")
    for m in pattern.finditer(code_part):
        st, nm, word, ws, other = m.groups()
        if st:
            out.append((st, "str"))
        elif nm:
            out.append((nm, "num"))
        elif word:
            out.append((word, "kw" if word in KEYWORDS or word.lower() in KEYWORDS and lang.lower() == "sql" else "id"))
        else:
            out.append((ws or other, "id"))
    if comment:
        out.append((comment, "comment"))
    return out


def _mixed_text(d, x, y, text, mono, cjk, color):
    for ch in text:
        f = cjk if _is_wide(ch) else mono
        d.text((x, y), ch, font=f, fill=color)
        x += f.getlength(ch)
    return x


def t_code(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    lines = (data.get("code") or "").splitlines() or [""]
    lang = data.get("language") or "text"
    hl = set(data.get("highlight") or [])
    rounded(d, (x0, y0, x1, y1), 24 * s, fill=t["code_bg"])
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse((x0 + (28 + i * 30) * s, y0 + 24 * s, x0 + (44 + i * 30) * s, y0 + 40 * s), fill=c)
    fl = font(26 * s, "mono")
    d.text((x1 - 24 * s - fl.getlength(lang), y0 + 18 * s), lang, font=fl, fill=(120, 130, 150))
    top = y0 + 70 * s
    avail_h = y1 - top - 24 * s
    avail_w = x1 - x0 - 140 * s
    size = 40 * s
    while size > 18 * s:
        fm = font(size, "mono")
        fc = font(size * 0.95, "regular")
        widest = max(sum((fc if _is_wide(ch) else fm).getlength(ch) for ch in ln) for ln in lines)
        if widest <= avail_w and len(lines) * size * 1.5 <= avail_h:
            break
        size -= 2
    fm, fc = font(size, "mono"), font(size * 0.95, "regular")
    lh = size * 1.5
    colors = {"kw": (198, 120, 221), "str": t["str"], "num": t["num"], "comment": t["comment"], "id": (220, 226, 236)}
    for i, ln in enumerate(lines):
        y = top + i * lh
        if (i + 1) in hl:
            d.rectangle((x0 + 6 * s, y - size * 0.15, x1 - 6 * s, y + lh - size * 0.15), fill=(40, 56, 92))
            d.rectangle((x0 + 6 * s, y - size * 0.15, x0 + 12 * s, y + lh - size * 0.15), fill=t["accent2"])
        num = str(i + 1)
        d.text((x0 + 80 * s - fm.getlength(num), y), num, font=fm, fill=(90, 100, 125))
        x = x0 + 110 * s
        for tok, kind in _code_tokens(ln, lang):
            x = _mixed_text(d, x, y, tok, fm, fc, colors[kind])


def _arrow(d, a, b, color, w, head):
    (ax, ay), (bx, by) = a, b
    d.line((ax, ay, bx, by), fill=color, width=int(w))
    ang = math.atan2(by - ay, bx - ax)
    p1 = (bx - head * math.cos(ang - 0.45), by - head * math.sin(ang - 0.45))
    p2 = (bx - head * math.cos(ang + 0.45), by - head * math.sin(ang + 0.45))
    d.polygon([(bx, by), p1, p2], fill=color)


def t_flow(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    nodes = (sc.get("data") or {}).get("nodes") or []
    n = max(1, len(nodes))
    gap = (70 if n <= 4 else 56) * s
    boxes = []
    if L.portrait:
        bh = min(170 * s, (y1 - y0 - gap * (n - 1)) / n)
        bw = (x1 - x0) * 0.8
        total = n * bh + (n - 1) * gap
        yy = y0 + (y1 - y0 - total) / 2
        for i in range(n):
            bx = x0 + ((x1 - x0) - bw) / 2
            boxes.append((bx, yy + i * (bh + gap), bx + bw, yy + i * (bh + gap) + bh))
    else:
        bw = ((x1 - x0) - gap * (n - 1)) / n
        bh = min((y1 - y0) * 0.5, bw * 0.9)
        cy = y0 + (y1 - y0) / 2
        for i in range(n):
            bx = x0 + i * (bw + gap)
            boxes.append((bx, cy - bh / 2, bx + bw, cy + bh / 2))
    for i in range(n - 1):
        a, b = boxes[i], boxes[i + 1]
        col = t["accent"] if i + 1 < vis else t["line"]
        if L.portrait:
            _arrow(d, ((a[0] + a[2]) / 2, a[3] + 8 * s), ((b[0] + b[2]) / 2, b[1] - 8 * s), col, 5 * s, 22 * s)
        else:
            _arrow(d, (a[2] + 8 * s, (a[1] + a[3]) / 2), (b[0] - 8 * s, (b[1] + b[3]) / 2), col, 5 * s, 22 * s)
    for i, (box, label) in enumerate(zip(boxes, nodes)):
        if i >= vis:
            rounded(d, box, 24 * s, outline=t["line"], width=3 * s)
            continue
        cur = i == focus
        rounded(d, box, 24 * s, fill=t["accent"] if cur else t["card"], outline=t["accent"], width=3 * s)
        fsz = min(54 * s, (box[3] - box[1]) * 0.3)
        text_block(d, label, (box[0] + 18 * s, box[1] + 14 * s, box[2] - 18 * s, box[3] - 14 * s), fsz, 24 * s, (255, 255, 255) if cur else t["fg"], "bold", "center", "center", max_lines=3, line_gap=1.2)


def _fmt(v):
    if abs(v) >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def t_chart(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    labels, values = data.get("labels") or [], data.get("values") or []
    n = len(values)
    if n == 0:
        return t_keyword(d, img, L, t, sc, ctx, vis, focus)
    fl = font(32 * s, "regular")
    if data.get("unit"):
        d.text((x0, y0), "单位：" + data["unit"], font=fl, fill=t["muted"])
    if data.get("source"):
        src = "来源：" + data["source"]
        d.text((x1 - fl.getlength(src), y1 - 34 * s), src, font=fl, fill=t["muted"])
    px0, py0, px1, py1 = x0 + 20 * s, y0 + 120 * s, x1 - 20 * s, y1 - 120 * s
    vmax = max(max(values), 0)
    vmin = min(min(values), 0)
    span = (vmax - vmin) or 1
    def ypos(v):
        return py1 - (v - vmin) / span * (py1 - py0)
    zero = ypos(0)
    d.line((px0, zero, px1, zero), fill=t["line"], width=int(3 * s))
    slot = (px1 - px0) / n
    best = max(range(n), key=lambda i: values[i])
    fv = font(min(44 * s, slot * 0.28), "bold")
    if data.get("chart") == "line":
        pts = [(px0 + slot * (i + 0.5), ypos(values[i])) for i in range(n)]
        shown = pts[: max(vis, 1)]
        if len(shown) > 1:
            d.line(shown, fill=t["accent"], width=int(7 * s), joint="curve")
        for i, (px, py) in enumerate(shown):
            r = 12 * s
            d.ellipse((px - r, py - r, px + r, py + r), fill=t["accent2"] if i == best else t["accent"])
            lab = _fmt(values[i])
            d.text((px - fv.getlength(lab) / 2, py - r - fv.size * 1.3), lab, font=fv, fill=t["fg"])
    else:
        bw = slot * 0.56
        for i in range(n):
            cx = px0 + slot * (i + 0.5)
            if i >= vis:
                rounded(d, (cx - bw / 2, min(zero, ypos(values[i])), cx + bw / 2, max(zero, ypos(values[i]))), 10 * s, outline=t["line"], width=2 * s)
                continue
            col = t["accent2"] if i == best else t["accent"]
            top, bot = sorted((zero, ypos(values[i])))
            rounded(d, (cx - bw / 2, top, cx + bw / 2, max(bot, top + 4 * s)), 10 * s, fill=col)
            lab = _fmt(values[i])
            ly = top - fv.size * 1.3 if values[i] >= 0 else bot + 8 * s
            d.text((cx - fv.getlength(lab) / 2, ly), lab, font=fv, fill=t["fg"])
    for i, lab in enumerate(labels[:n]):
        cx = px0 + slot * (i + 0.5)
        text_block(d, lab, (cx - slot * 0.48, py1 + 18 * s, cx + slot * 0.48, py1 + 110 * s), 34 * s, 20 * s, t["fg"] if i < vis else t["muted"], "regular", "center", max_lines=2, line_gap=1.2)


def t_compare(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    left, right = data.get("left") or [], data.get("right") or []
    gap = 48 * s
    if L.portrait:
        cols = [(x0, y0, x1, y0 + (y1 - y0 - gap) / 2), (x0, y0 + (y1 - y0 + gap) / 2, x1, y1)]
    else:
        cols = [(x0, y0, x0 + (x1 - x0 - gap) / 2, y1), (x0 + (x1 - x0 + gap) / 2, y0, x1, y1)]
    shown_left = min(len(left), vis)
    shown_right = max(0, vis - len(left))
    specs = [(data.get("left_title") or "A", left, shown_left, t["accent"]), (data.get("right_title") or "B", right, shown_right, t["accent2"])]
    for (bx0, by0, bx1, by1), (title, items, shown, col) in zip(cols, specs):
        rounded(d, (bx0, by0, bx1, by1), 28 * s, fill=t["card"])
        hh = 100 * s
        rounded(d, (bx0, by0, bx1, by0 + hh), 28 * s, fill=col)
        d.rectangle((bx0, by0 + hh - 28 * s, bx1, by0 + hh), fill=col)
        text_block(d, title, (bx0 + 30 * s, by0, bx1 - 30 * s, by0 + hh), 52 * s, 28 * s, (255, 255, 255), "bold", "center", "center", max_lines=1)
        m = max(1, len(items))
        ih = min(110 * s, (by1 - by0 - hh - 40 * s) / m)
        for i, it in enumerate(items[:shown]):
            iy = by0 + hh + 24 * s + i * ih
            d.ellipse((bx0 + 36 * s, iy + ih / 2 - 9 * s, bx0 + 54 * s, iy + ih / 2 + 9 * s), fill=col)
            text_block(d, it, (bx0 + 76 * s, iy, bx1 - 30 * s, iy + ih), min(44 * s, ih * 0.42), 24 * s, t["fg"], "regular", "left", "center", max_lines=2)


def t_table(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    headers, rows = data.get("headers") or [], data.get("rows") or []
    ncol = max([len(headers)] + [len(r) for r in rows] + [1])
    nrow = len(rows) + (1 if headers else 0)
    rh = min(120 * s, (y1 - y0) / max(nrow, 1))
    total_h = rh * nrow
    ty = y0 + (y1 - y0 - total_h) / 2
    cw = (x1 - x0) / ncol
    rounded(d, (x0, ty, x1, ty + total_h), 20 * s, fill=t["card"])
    y = ty
    if headers:
        rounded(d, (x0, y, x1, y + rh), 20 * s, fill=t["accent"])
        d.rectangle((x0, y + rh / 2, x1, y + rh), fill=t["accent"])
        for c in range(ncol):
            text_block(d, headers[c] if c < len(headers) else "", (x0 + c * cw + 20 * s, y, x0 + (c + 1) * cw - 20 * s, y + rh), min(44 * s, rh * 0.4), 22 * s, (255, 255, 255), "bold", "center", "center", max_lines=2)
        y += rh
    for r_i, row in enumerate(rows):
        if r_i >= vis:
            break
        if r_i == focus:
            d.rectangle((x0, y, x1, y + rh), fill=t["card2"])
        for c in range(ncol):
            text_block(d, row[c] if c < len(row) else "", (x0 + c * cw + 20 * s, y, x0 + (c + 1) * cw - 20 * s, y + rh), min(40 * s, rh * 0.38), 20 * s, t["fg"], "regular", "center", "center", max_lines=2)
        d.line((x0 + 20 * s, y + rh, x1 - 20 * s, y + rh), fill=t["line"], width=int(2 * s))
        y += rh


def t_quote(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = sc.get("data") or {}
    fq = font(260 * s, "bold")
    d.text((x0 + 10 * s, y0 - 90 * s), "“", font=fq, fill=t["accent"])
    h = y1 - y0
    text_block(d, data.get("text") or sc.get("narration", "")[:60], (x0 + 140 * s, y0 + h * 0.12, x1 - 100 * s, y0 + h * 0.78), 72 * s, 36 * s, t["fg"], "bold", "left", "center", max_lines=4, line_gap=1.4, balance=True)
    if data.get("source"):
        text_block(d, "—— " + data["source"], (x0 + 140 * s, y0 + h * 0.8, x1 - 100 * s, y1), 40 * s, 26 * s, t["muted"], "regular", "right", max_lines=1)


def _cover_fit(im, w, h):
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def t_image(d, img, L, t, sc, ctx, vis, focus):
    s = L.s
    path = ctx.get("image_path")
    if not path or not Path(path).is_file():
        fallback = dict(sc)
        fallback["data"] = {"keyword": sc.get("heading") or (sc.get("data") or {}).get("caption") or "", "note": (sc.get("data") or {}).get("caption", "")}
        return t_keyword(d, img, L, t, fallback, ctx, vis, focus)
    x0, y0, x1, y1 = [int(v) for v in L.content]
    try:
        with Image.open(path) as src:
            src = src.convert("RGB")
            pic = _cover_fit(src, x1 - x0, y1 - y0)
    except OSError:
        return t_keyword(d, img, L, t, dict(sc, data={"keyword": sc.get("heading") or "", "note": ""}), ctx, vis, focus)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.size[0], pic.size[1]), radius=int(28 * s), fill=255)
    img.paste(pic, (x0, y0), mask)
    cap = (sc.get("data") or {}).get("caption")
    if cap:
        f = font(38 * s, "regular")
        w = f.getlength(cap) + 48 * s
        overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
        od = ImageDraw.Draw(overlay)
        od.rounded_rectangle((x0 + 24 * s, y1 - 90 * s, x0 + 24 * s + w, y1 - 24 * s), radius=int(16 * s), fill=(0, 0, 0, 150))
        img.alpha_composite(overlay)
        d2 = ImageDraw.Draw(img)
        d2.text((x0 + 48 * s, y1 - 82 * s), cap, font=f, fill=(255, 255, 255))


TEMPLATES = {
    "title": t_title, "keyword": t_keyword, "stat": t_stat, "bullets": t_bullets, "code": t_code, "flow": t_flow,
    "chart": t_chart, "compare": t_compare, "table": t_table, "quote": t_quote, "image": t_image,
}


# ---------------------------------------------------------------- 字幕与整帧

def draw_subtitle(img, L, text):
    if not text:
        return
    s = L.s
    max_w = L.W - 2 * L.m - 40 * s
    f, lines, lh = fit_text(text, max_w, (52 if L.portrait else 48) * s * 1.4 * 2, (52 if L.portrait else 48) * s, 30 * s, "bold", max_lines=2, line_gap=1.4, balance=True)
    width = max(f.getlength(l) for l in lines) + 56 * s
    height = len(lines) * lh + 24 * s
    cx = L.W / 2
    top = L.sub_y - height / 2
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((cx - width / 2, top, cx + width / 2, top + height), radius=int(16 * s), fill=(0, 0, 0, 165))
    img.alpha_composite(overlay)
    d = ImageDraw.Draw(img)
    draw_lines(d, lines, f, lh, cx - width / 2 + 28 * s, top + 10 * s, width - 56 * s, (255, 255, 255), "center", "bold")


def render_frame(scene, ctx, vis=99, focus=-1, subtitle="", orientation="landscape"):
    """画一帧。ctx: theme, title, series, index, total, image_path, watermark, burn_subtitles。"""
    W, H = SIZES[orientation]
    theme = ctx.get("theme") if ctx.get("theme") in THEMES else "dark"
    t = THEMES[theme]
    L = Layout(W, H)
    img = new_canvas(theme, W, H)
    d = ImageDraw.Draw(img)
    kind = scene.get("type") if scene.get("type") in TEMPLATES else "keyword"
    if kind != "title":
        _draw_heading(d, L, t, scene.get("heading") or "", ctx.get("index", 0), ctx.get("total", 1))
    TEMPLATES[kind](d, img, L, t, scene, ctx, vis, focus)
    d = ImageDraw.Draw(img)
    s = L.s
    if ctx.get("watermark", True):
        f = font(24 * s, "regular")
        label = "AI 辅助制作"
        d.text((W - L.m - f.getlength(label), (14 if not L.portrait else 60) * s + 8 * s), label, font=f, fill=t["muted"])
    total = max(1, ctx.get("total", 1))
    prog = (ctx.get("index", 0) + 1) / total
    d.rectangle((0, H - 6 * s, W * prog, H), fill=t["accent"])
    if ctx.get("burn_subtitles", True):
        draw_subtitle(img, L, subtitle)
    return img.convert("RGB")


def render_cover(title, ctx, orientation="landscape"):
    W, H = COVER_SIZES[orientation]
    theme = ctx.get("theme") if ctx.get("theme") in THEMES else "dark"
    t = THEMES[theme]
    s = min(W, H) / 1080
    img = new_canvas(theme, W, H)
    path = ctx.get("image_path")
    if path and Path(path).is_file():
        try:
            with Image.open(path) as src:
                pic = _cover_fit(src.convert("RGB"), W, H).filter(ImageFilter.GaussianBlur(2))
            shade = Image.new("RGBA", (W, H), t["bg"] + (190,))
            img = Image.alpha_composite(pic.convert("RGBA"), shade)
        except OSError:
            pass
    d = ImageDraw.Draw(img)
    m = 90 * s
    if ctx.get("series"):
        f = font(44 * s, "bold")
        lab = ctx["series"]
        rounded(d, (m, m, m + f.getlength(lab) + 60 * s, m + 80 * s), 16 * s, fill=t["accent"])
        d.text((m + 30 * s, m + 14 * s), lab, font=f, fill=(255, 255, 255))
    text_block(d, title, (m, H * 0.26, W - m, H * 0.78), (150 if W > H else 130) * s, 60 * s, t["fg"], "bold", "left", "center", max_lines=3, line_gap=1.2, balance=True)
    d.rectangle((m, H * 0.82, m + 160 * s, H * 0.82 + 14 * s), fill=t["accent2"])
    if ctx.get("subtitle"):
        text_block(d, ctx["subtitle"], (m, H * 0.85, W - m, H - 40 * s), 48 * s, 28 * s, t["muted"], "regular", "left", max_lines=1)
    return img.convert("RGB")
