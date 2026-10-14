"""用 Pillow 画讲解页。画法移植自本仓库「AI 知识视频工坊」的 studio/visuals.py（已在那边验证过的版式和排字），
改动：只用随包的开源字体思源黑体 Noto Sans SC Bold；版式避开上游烧录字幕的区域；关键词用强调色高亮；
新增「文字卡」；不画字幕（字幕由上游照常生成和烧录）。

画面比例跟随所选的视频比例（竖屏 1080×1920、横屏 1920×1080、方形 1080×1080），版式按比例自适应。
"""
from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .storyboard import Page, highlight_spans

FONT_FILE = "NotoSansSC-Bold.ttf"

THEMES = {
    "classroom": {
        "bg": (236, 243, 251), "bg2": (220, 233, 248), "fg": (22, 40, 68), "muted": (98, 118, 150),
        "accent": (28, 102, 204), "accent2": (236, 128, 36), "card": (255, 255, 255), "card2": (214, 230, 250),
        "line": (190, 208, 232), "mark": (255, 226, 160),
    },
    "dark": {
        "bg": (13, 17, 28), "bg2": (25, 32, 52), "fg": (236, 240, 248), "muted": (139, 152, 178),
        "accent": (86, 168, 255), "accent2": (255, 184, 76), "card": (32, 40, 62), "card2": (42, 52, 80),
        "line": (60, 72, 100), "mark": (92, 72, 30),
    },
    "paper": {
        "bg": (247, 243, 234), "bg2": (238, 231, 216), "fg": (40, 36, 32), "muted": (128, 118, 104),
        "accent": (192, 78, 48), "accent2": (36, 108, 140), "card": (255, 252, 245), "card2": (236, 226, 206),
        "line": (210, 200, 182), "mark": (250, 222, 170),
    },
}
WHITE = (255, 255, 255)


# ---------------------------------------------------------------- 字体

def _font_candidates() -> list[Path]:
    here = Path(__file__).resolve().parent
    out = []
    if os.environ.get("MPT_FONT_DIR"):
        out.append(Path(os.environ["MPT_FONT_DIR"]) / FONT_FILE)
    out += [here.parent / "fonts" / FONT_FILE,            # 源码：shortvideo-factory/fonts/
            here.parent.parent / "fonts" / FONT_FILE]     # 安装包：payload/child/svf_ext → payload/fonts/
    try:
        from app.utils import utils
        out.append(Path(utils.font_dir()) / FONT_FILE)
    except Exception:
        pass
    return out


@lru_cache(maxsize=1)
def font_path() -> str:
    for p in _font_candidates():
        if p.is_file():
            return str(p)
    return ""


@lru_cache(maxsize=256)
def font(size: float) -> ImageFont.FreeTypeFont:
    size = max(8, int(size))
    p = font_path()
    if p:
        return ImageFont.truetype(p, size)
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- 排字

def tokens(text: str) -> list[str]:
    """英文单词和数字作为整体，中文逐字，便于换行。"""
    return re.findall(r"[A-Za-z0-9_.%+#/:-]+|\s+|.", text or "")


NO_LINE_START = "，。、；：！？）》」』,.;:!?)%"


def wrap_rich(spans: list[tuple[str, bool]], fnt, max_w: float) -> list[list[tuple[str, bool]]]:
    """带高亮标记的换行：返回若干行，每行是 [(片段, 是否高亮)]。"""
    toks = [(tok, mk) for text, mk in spans for tok in tokens(text)]
    lines: list[list[tuple[str, bool]]] = [[]]
    width = 0.0
    for tok, mk in toks:
        w = fnt.getlength(tok)
        if lines[-1] and width + w > max_w and tok[:1] not in NO_LINE_START:
            if tok.isspace():
                continue
            lines.append([])
            width = 0.0
        if not lines[-1] and tok.isspace():
            continue
        if w > max_w:   # 超长单词硬切
            for ch in tok:
                cw = fnt.getlength(ch)
                if width + cw > max_w and lines[-1]:
                    lines.append([])
                    width = 0.0
                lines[-1].append((ch, mk))
                width += cw
            continue
        lines[-1].append((tok, mk))
        width += w
    return [ln for ln in lines if ln]


def _line_width(line, fnt) -> float:
    return sum(fnt.getlength(t) for t, _ in line)


def fit_rich(spans, max_w, max_h, size, min_size, max_lines=None, line_gap=1.35):
    s = int(size)
    while True:
        f = font(s)
        lines = wrap_rich(spans, f, max_w)
        lh = s * line_gap
        if (max_lines is None or len(lines) <= max_lines) and len(lines) * lh <= max_h + s * (line_gap - 1):
            return f, lines, lh
        if s <= min_size:
            limit = max(1, int((max_h + s * (line_gap - 1)) // lh))
            if max_lines:
                limit = min(limit, max_lines)
            if len(lines) > limit:
                lines = lines[:limit]
                last = list(lines[-1])
                while last and _line_width(last, f) + f.getlength("…") > max_w:
                    t, mk = last.pop()
                    if len(t) > 1:
                        last.append((t[:-1], mk))
                last.append(("…", False))
                lines[-1] = last
            return f, lines, lh
        s -= 2


def text_block(d, text, box, size, min_size, color, align="left", valign="top", max_lines=None, line_gap=1.35,
               keywords=(), mark=None, hl_color=None):
    """在方框里排一段文字（自动缩字号、换行），关键词用 hl_color 着色、底下垫 mark 色的荧光笔。返回占用高度。"""
    x0, y0, x1, y1 = box
    spans = highlight_spans(text or "", list(keywords)) if keywords else [(text or "", False)]
    f, lines, lh = fit_rich(spans, x1 - x0, y1 - y0, size, min_size, max_lines, line_gap)
    total = len(lines) * lh - (lh - f.size * 1.1)
    if valign == "center":
        y = y0 + (y1 - y0 - total) / 2
    elif valign == "bottom":
        y = y1 - total
    else:
        y = y0
    y -= f.size * 0.08
    for i, line in enumerate(lines):
        lw = _line_width(line, f)
        x = x0 + ((x1 - x0 - lw) / 2 if align == "center" else (x1 - x0 - lw) if align == "right" else 0)
        yy = y + i * lh
        for tok, mk in line:
            w = f.getlength(tok)
            if mk and mark is not None:
                d.rounded_rectangle((x - f.size * 0.06, yy + f.size * 0.62, x + w + f.size * 0.06, yy + f.size * 1.22),
                                    radius=int(f.size * 0.12), fill=mark)
            d.text((x, yy), tok, font=f, fill=(hl_color or color) if mk else color)
            x += w
    return total


def rounded(d, box, r, fill=None, outline=None, width=1):
    d.rounded_rectangle([round(v) for v in box], radius=int(r), fill=fill, outline=outline, width=int(max(1, width)))


# ---------------------------------------------------------------- 版式

class Layout:
    """页面分区。safe 是字幕要占用的区域：'bottom'（默认）、'top'、'none'（不开字幕或字幕在中间）。"""

    def __init__(self, W: int, H: int, safe: str = "bottom"):
        self.W, self.H = W, H
        self.portrait = H > W * 1.2
        self.s = s = min(W, H) / 1080
        self.m = (64 if self.portrait else 90) * s
        top, bottom = 0.0, float(H)
        if safe == "bottom":
            bottom = H * (0.76 if self.portrait else 0.80)
        elif safe == "top":
            top = H * (0.20 if self.portrait else 0.18)
        head_top = top + (H * 0.07 if self.portrait else 52 * s)
        self.head = (self.m, head_top, W - self.m, head_top + (118 if self.portrait else 100) * s)
        self.content = (self.m, self.head[3] + (52 if self.portrait else 40) * s, W - self.m,
                        bottom - (40 if self.portrait else 36) * s)


@lru_cache(maxsize=12)
def _background(theme: str, W: int, H: int) -> Image.Image:
    t = THEMES[theme]
    grad = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), t["bg2"]), Image.new("RGB", (W, H), t["bg"]), grad)
    d = ImageDraw.Draw(img)
    s = min(W, H) / 1080
    if theme == "dark":
        step = max(8, int(48 * s))
        for x in range(step, W, step):
            for y in range(step, H, step):
                d.point((x, y), fill=t["line"])
        d.ellipse((W - 520 * s, -300 * s, W + 300 * s, 520 * s), outline=t["card2"], width=max(1, int(2 * s)))
    elif theme == "paper":
        inset = 28 * s
        d.rectangle((inset, inset, W - inset, H - inset), outline=t["line"], width=max(1, int(2 * s)))
        for y in range(int(H * 0.2), H, max(8, int(64 * s))):
            d.line((inset * 2, y, W - inset * 2, y), fill=(242, 236, 224), width=1)
    else:
        d.rectangle((0, 0, W, int(14 * s)), fill=t["accent"])
        d.ellipse((-260 * s, H - 420 * s, 360 * s, H + 200 * s), outline=t["card2"], width=max(1, int(3 * s)))
    return img


def _heading(d, L, t, text, index, total):
    x0, y0, x1, y1 = L.head
    s = L.s
    rounded(d, (x0, y0 + 14 * s, x0 + 10 * s, y1 - 14 * s), 5 * s, fill=t["accent"])
    text_block(d, text, (x0 + 32 * s, y0, x1 - 150 * s, y1), 62 * s, 34 * s, t["fg"], valign="center", max_lines=1)
    if total > 1:
        label = f"{index + 1:02d}/{total:02d}"
        f = font(30 * s)
        d.text((x1 - f.getlength(label), y0 + (y1 - y0) / 2 - 18 * s), label, font=f, fill=t["muted"])


def t_title(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = (L.m, L.head[1], L.W - L.m, L.content[3])
    h = y1 - y0
    cx = (x0 + x1) / 2
    f = font(30 * s)
    lab = "知识讲解"
    w = f.getlength(lab) + 56 * s
    ty = y0 + h * 0.16
    rounded(d, (cx - w / 2, ty, cx + w / 2, ty + 58 * s), 29 * s, outline=t["accent"], width=2 * s)
    d.text((cx - f.getlength(lab) / 2, ty + 10 * s), lab, font=f, fill=t["accent"])
    mid = y0 + h * 0.28
    text_block(d, p.heading, (x0 + 30 * s, mid, x1 - 30 * s, mid + h * 0.36), (128 if not L.portrait else 112) * s, 56 * s,
               t["fg"], "center", "center", max_lines=3, line_gap=1.22)
    ly = mid + h * 0.36 + 34 * s
    d.rectangle((cx - 64 * s, ly, cx + 64 * s, ly + 9 * s), fill=t["accent2"])
    sub = (p.data or {}).get("subtitle") or ""
    if sub:
        text_block(d, sub, (x0 + 60 * s, ly + 46 * s, x1 - 60 * s, min(y1, ly + 250 * s)), 50 * s, 30 * s, t["muted"],
                   "center", max_lines=3, keywords=kw, hl_color=t["accent"])


def t_keyword(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    word = data.get("keyword") or p.heading
    note = data.get("note") or ""
    h = y1 - y0
    cx, cy = (x0 + x1) / 2, y0 + h * (0.36 if note else 0.48)
    r = min(x1 - x0, h * (0.62 if note else 0.9)) * 0.42
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=t["card2"], width=int(4 * s))
    d.ellipse((cx - r * 0.8, cy - r * 0.8, cx + r * 0.8, cy + r * 0.8), fill=t["card"])
    a = math.radians(-35)
    d.ellipse((cx + r * math.cos(a) - 14 * s, cy + r * math.sin(a) - 14 * s, cx + r * math.cos(a) + 14 * s,
               cy + r * math.sin(a) + 14 * s), fill=t["accent2"])
    one = len(word) <= 6
    text_block(d, word, (cx - r * 0.7, cy - r * 0.58, cx + r * 0.7, cy + r * 0.58), 150 * s, 44 * s, t["accent"],
               "center", "center", max_lines=1 if one else 3, line_gap=1.2)
    if note:
        text_block(d, note, (x0 + 40 * s, cy + r + 44 * s, x1 - 40 * s, y1), 50 * s, 30 * s, t["fg"], "center",
                   max_lines=3, keywords=[k for k in kw if k != word], mark=t["mark"], hl_color=t["accent"])


def t_text(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    body = (p.data or {}).get("body") or p.text
    rounded(d, (x0, y0 + 20 * s, x1, y1 - 20 * s), 32 * s, fill=t["card"])
    rounded(d, (x0, y0 + 20 * s, x0 + 12 * s, y1 - 20 * s), 6 * s, fill=t["accent"])
    text_block(d, body, (x0 + 70 * s, y0 + 70 * s, x1 - 60 * s, y1 - 70 * s), 74 * s, 36 * s, t["fg"], "left", "center",
               max_lines=5, line_gap=1.45, keywords=kw, mark=t["mark"], hl_color=t["accent"])


def t_stat(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    h = y1 - y0
    rounded(d, (x0 + 30 * s, y0 + h * 0.08, x1 - 30 * s, y1 - h * 0.08), 36 * s, fill=t["card"])
    text_block(d, data.get("value") or "", (x0 + 80 * s, y0 + h * 0.14, x1 - 80 * s, y0 + h * 0.56), min(260 * s, h * 0.34), 60 * s,
               t["accent"], "center", "center", max_lines=1)
    cx = (x0 + x1) / 2
    d.rectangle((cx - 50 * s, y0 + h * 0.62, cx + 50 * s, y0 + h * 0.62 + 8 * s), fill=t["accent2"])
    text_block(d, data.get("label") or "", (x0 + 90 * s, y0 + h * 0.66, x1 - 90 * s, y1 - h * 0.12), 54 * s, 30 * s,
               t["fg"], "center", "center", max_lines=2, keywords=kw, hl_color=t["accent"])


def t_bullets(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    items = (p.data or {}).get("items") or []
    n = max(len(items), 1)
    gap = 24 * s
    row_h = min((200 if L.portrait else 150) * s, (y1 - y0 - gap * (n - 1)) / n)
    total = n * row_h + (n - 1) * gap
    y = y0 + (y1 - y0 - total) / 2
    focus = vis - 1
    for i, it in enumerate(items):
        top = y + i * (row_h + gap)
        if i >= vis:
            rounded(d, (x0, top, x1, top + row_h), 22 * s, outline=t["card2"], width=3 * s)
            continue
        cur = i == focus
        rounded(d, (x0, top, x1, top + row_h), 22 * s, fill=t["card2"] if cur else t["card"])
        if cur:
            rounded(d, (x0, top, x0 + 10 * s, top + row_h), 5 * s, fill=t["accent"])
        br = min(row_h * 0.30, 36 * s)
        bx, by = x0 + 44 * s + br, top + row_h / 2
        d.ellipse((bx - br, by - br, bx + br, by + br), fill=t["accent"] if cur else t["line"])
        fnum = font(br * 1.1)
        num = str(i + 1)
        d.text((bx - fnum.getlength(num) / 2, by - br * 0.72), num, font=fnum, fill=WHITE)
        text_block(d, it, (bx + br + 32 * s, top + 10 * s, x1 - 30 * s, top + row_h - 10 * s), min(56 * s, row_h * 0.4),
                   28 * s, t["fg"] if cur else t["muted"], "left", "center", max_lines=2, keywords=kw, hl_color=t["accent"])


def _arrow(d, a, b, color, w, head):
    (ax, ay), (bx, by) = a, b
    d.line((ax, ay, bx, by), fill=color, width=int(w))
    ang = math.atan2(by - ay, bx - ax)
    p1 = (bx - head * math.cos(ang - 0.45), by - head * math.sin(ang - 0.45))
    p2 = (bx - head * math.cos(ang + 0.45), by - head * math.sin(ang + 0.45))
    d.polygon([(bx, by), p1, p2], fill=color)


def t_flow(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    nodes = (p.data or {}).get("nodes") or []
    n = max(1, len(nodes))
    gap = (76 if n <= 4 else 60) * s
    boxes = []
    vertical = L.portrait or n > 4
    if vertical:
        bh = min(170 * s, (y1 - y0 - gap * (n - 1)) / n)
        bw = (x1 - x0) * 0.82
        total = n * bh + (n - 1) * gap
        yy = y0 + (y1 - y0 - total) / 2
        for i in range(n):
            bx = x0 + ((x1 - x0) - bw) / 2
            boxes.append((bx, yy + i * (bh + gap), bx + bw, yy + i * (bh + gap) + bh))
    else:
        bw = ((x1 - x0) - gap * (n - 1)) / n
        bh = min((y1 - y0) * 0.55, bw * 0.9)
        cy = y0 + (y1 - y0) / 2
        for i in range(n):
            bx = x0 + i * (bw + gap)
            boxes.append((bx, cy - bh / 2, bx + bw, cy + bh / 2))
    for i in range(n - 1):
        a, b = boxes[i], boxes[i + 1]
        col = t["accent"] if i + 1 < vis else t["line"]
        if vertical:
            _arrow(d, ((a[0] + a[2]) / 2, a[3] + 8 * s), ((b[0] + b[2]) / 2, b[1] - 8 * s), col, 6 * s, 24 * s)
        else:
            _arrow(d, (a[2] + 8 * s, (a[1] + a[3]) / 2), (b[0] - 8 * s, (b[1] + b[3]) / 2), col, 6 * s, 24 * s)
    for i, (box, label) in enumerate(zip(boxes, nodes)):
        if i >= vis:
            rounded(d, box, 24 * s, outline=t["line"], width=3 * s)
            continue
        cur = i == vis - 1
        rounded(d, box, 24 * s, fill=t["accent"] if cur else t["card"], outline=t["accent"], width=3 * s)
        bw_in = box[2] - box[0] - 40 * s
        fsz = min(56 * s, (box[3] - box[1]) * 0.32, bw_in / max(1, min(len(label), 7)))
        text_block(d, label, (box[0] + 20 * s, box[1] + 14 * s, box[2] - 20 * s, box[3] - 14 * s), fsz, 22 * s,
                   WHITE if cur else t["fg"], "center", "center", max_lines=3, line_gap=1.2)


def t_compare(d, L, t, p: Page, vis, kw):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    left, right = data.get("left") or [], data.get("right") or []
    gap = 48 * s
    if L.portrait:
        cols = [(x0, y0, x1, y0 + (y1 - y0 - gap) / 2), (x0, y0 + (y1 - y0 + gap) / 2, x1, y1)]
    else:
        cols = [(x0, y0, x0 + (x1 - x0 - gap) / 2, y1), (x0 + (x1 - x0 + gap) / 2, y0, x1, y1)]
    specs = [(data.get("left_title") or "A", left, vis >= 1, t["accent"]), (data.get("right_title") or "B", right, vis >= 2, t["accent2"])]
    for (bx0, by0, bx1, by1), (title, items, shown, col) in zip(cols, specs):
        hh = 104 * s
        if not shown:
            rounded(d, (bx0, by0, bx1, by1), 28 * s, outline=t["line"], width=3 * s)
            text_block(d, title, (bx0 + 30 * s, by0, bx1 - 30 * s, by0 + hh), 52 * s, 28 * s, t["muted"], "center", "center", max_lines=1)
            continue
        rounded(d, (bx0, by0, bx1, by1), 28 * s, fill=t["card"])
        rounded(d, (bx0, by0, bx1, by0 + hh), 28 * s, fill=col)
        d.rectangle((bx0, by0 + hh - 28 * s, bx1, by0 + hh), fill=col)
        text_block(d, title, (bx0 + 30 * s, by0, bx1 - 30 * s, by0 + hh), 54 * s, 28 * s, WHITE, "center", "center", max_lines=1)
        m = max(1, len(items))
        ih = min(140 * s, (by1 - by0 - hh - 40 * s) / m)
        for i, it in enumerate(items):
            iy = by0 + hh + 24 * s + i * ih
            d.ellipse((bx0 + 36 * s, iy + ih / 2 - 10 * s, bx0 + 56 * s, iy + ih / 2 + 10 * s), fill=col)
            text_block(d, it, (bx0 + 80 * s, iy, bx1 - 30 * s, iy + ih), min(48 * s, ih * 0.4), 24 * s, t["fg"], "left",
                       "center", max_lines=2, keywords=kw, hl_color=col)


TEMPLATES = {"title": t_title, "keyword": t_keyword, "text": t_text, "stat": t_stat, "bullets": t_bullets,
             "flow": t_flow, "compare": t_compare}


def render_page(page: Page, W: int, H: int, theme: str = "classroom", index: int = 0, total: int = 1, vis: int = 99,
                keywords=(), safe: str = "bottom") -> Image.Image:
    """画一页（RGB）。vis 是已经亮起的要点条数。"""
    theme = theme if theme in THEMES else "classroom"
    t = THEMES[theme]
    L = Layout(W, H, safe)
    img = _background(theme, W, H).copy()
    d = ImageDraw.Draw(img)
    kind = page.kind if page.kind in TEMPLATES else "text"
    if kind != "title":
        _heading(d, L, t, page.heading, index, total)
    TEMPLATES[kind](d, L, t, page, vis, list(keywords))
    s = L.s
    prog = (index + 1) / max(1, total)
    d.rectangle((0, H - 8 * s, W * prog, H), fill=t["accent"])
    return img
