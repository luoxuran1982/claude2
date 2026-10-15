"""「工坊主题」：把「AI 知识视频工坊」studio/visuals.py 的深色主题和全部版式移植过来，给知识讲解卡片用。

和工坊原版的对应关系（画法、尺寸比例、配色都照搬，下面只列改动）：
- 版式：title 标题卡、keyword 关键词大字、bullets 要点逐条亮起、stat 关键数字、code 代码高亮（带行号和强调行）、
  flow 流程箭头、chart 柱状/折线图、compare 对比两栏、table 表格（逐行出现）、quote 引言（大引号）、image 配图；
- 版面避开上游烧录字幕的区域（cards.Layout 的 safe 参数），工坊是把字幕画在固定位置；
- 字体：中文统一用随包的思源黑体 Bold（工坊按系统找微软雅黑 / 苹方），代码用系统等宽字体（Menlo / Consolas / DejaVu Sans Mono），
  找不到时也用思源黑体；
- 页面数据结构是本应用的 storyboard.Page（kind / heading / data / text / reveals），工坊是 scene 字典；
- 有 AI 插图的页（data.image_path）：插图铺满作背景，盖一层主题底色的半透明蒙版，再画版式（工坊的 image 版式另外保留）；
- 不画「AI 辅助制作」水印（工坊为发布平台的 AI 声明加的；本应用由用户自己决定）。

主题：studio 是工坊的深色主题原样；classroom / dark / paper 用 cards.py 里同名主题的颜色，补上代码配色，
所以「规则切分」选浅色主题时，遇到表格、代码这些 1.0.x 没有的版式也能画。
"""
from __future__ import annotations

import math
import os
import re
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import cards

CODE_COLORS = {
    "studio": {"code_bg": (9, 12, 20), "good": (110, 214, 150), "str": (152, 220, 130), "comment": (110, 122, 150), "num": (255, 160, 110)},
    "dark": {"code_bg": (9, 12, 20), "good": (110, 214, 150), "str": (152, 220, 130), "comment": (110, 122, 150), "num": (255, 160, 110)},
    "paper": {"code_bg": (36, 34, 40), "good": (60, 140, 90), "str": (160, 200, 120), "comment": (140, 140, 150), "num": (230, 150, 90)},
    "classroom": {"code_bg": (24, 32, 48), "good": (40, 150, 100), "str": (150, 210, 130), "comment": (120, 135, 160), "num": (250, 170, 100)},
}
# 工坊 visuals.py 的深色主题（原样）
STUDIO = {
    "bg": (13, 17, 28), "bg2": (25, 32, 52), "fg": (236, 240, 248), "muted": (139, 152, 178),
    "accent": (86, 168, 255), "accent2": (255, 184, 76), "card": (32, 40, 62), "card2": (42, 52, 80),
    "line": (60, 72, 100), "mark": (92, 72, 30),
}
WHITE = (255, 255, 255)
TYPE_LABELS = {"title": "标题", "keyword": "关键词", "bullets": "要点", "stat": "数字", "code": "代码", "flow": "流程",
               "chart": "图表", "compare": "对比", "table": "表格", "quote": "引言", "image": "配图", "text": "文字",
               "stock": "库存素材"}


def palette(theme: str) -> dict:
    base = STUDIO if theme == "studio" else cards.THEMES.get(theme, cards.THEMES["dark"])
    return {**base, **CODE_COLORS.get(theme, CODE_COLORS["dark"])}


# ---------------------------------------------------------------- 字体

def _mono_candidates() -> list[str]:
    win = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
    return ["/System/Library/Fonts/Menlo.ttc", "/System/Library/Fonts/Monaco.ttf", str(win / "consola.ttf"), str(win / "cour.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf", "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf"]


@lru_cache(maxsize=1)
def mono_path() -> str:
    for c in _mono_candidates():
        if Path(c).is_file():
            return c
    return cards.font_path()


@lru_cache(maxsize=256)
def font(size: float, kind: str = "regular") -> ImageFont.FreeTypeFont:
    if kind != "mono":
        return cards.font(size)
    size = max(8, int(size))
    p = mono_path()
    try:
        return ImageFont.truetype(p, size) if p else ImageFont.load_default(size)
    except OSError:
        return cards.font(size)


def _is_wide(ch: str) -> bool:
    return ord(ch) >= 0x2E80


# ---------------------------------------------------------------- 排字（工坊 wrap / balanced_wrap / fit_text / text_block）

def wrap(text, fnt, max_w):
    lines = []
    for para in str(text or "").split("\n"):
        line = ""
        for tok in cards.tokens(para):
            trial = line + tok
            if fnt.getlength(trial) <= max_w or not line:
                if not line and fnt.getlength(tok) > max_w:
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
        lines.append(line.rstrip())
    out = []
    for ln in lines:
        if out and ln[:1] in "，。、；：！？）》」,.;:!?)":
            out[-1] += ln[0]
            ln = ln[1:]
        out.append(ln)
    return out


def balanced_wrap(text, fnt, max_w):
    lines = wrap(text, fnt, max_w)
    k = len(lines)
    if k < 2 or "\n" in str(text):
        return lines
    w, best = max_w, lines
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


def draw_lines(d, lines, f, lh, x, y, w, color, align="left"):
    for i, ln in enumerate(lines):
        lw = f.getlength(ln)
        xx = x + (w - lw) / 2 if align == "center" else (x + w - lw if align == "right" else x)
        d.text((xx, y + i * lh), ln, font=f, fill=color)
    return len(lines) * lh


def text_block(d, text, box, size, min_size, color, kind="regular", align="left", valign="top", max_lines=None,
               line_gap=1.35, balance=None):
    x0, y0, x1, y1 = box
    if balance is None:
        balance = align == "center"
    f, lines, lh = fit_text(text, x1 - x0, y1 - y0, size, min_size, kind, max_lines, line_gap, balance)
    total = len(lines) * lh - (lh - f.size * 1.1)
    y = y0 + (y1 - y0 - total) / 2 if valign == "center" else (y1 - total if valign == "bottom" else y0)
    draw_lines(d, lines, f, lh, x0, y - f.size * 0.08, x1 - x0, color, align)
    return total


rounded = cards.rounded


# ---------------------------------------------------------------- 背景

@lru_cache(maxsize=12)
def _background(theme: str, W: int, H: int) -> Image.Image:
    if theme != "studio":
        return cards._background(theme if theme in cards.THEMES else "dark", W, H)
    t = STUDIO
    grad = Image.linear_gradient("L").resize((W, H))
    img = Image.composite(Image.new("RGB", (W, H), t["bg2"]), Image.new("RGB", (W, H), t["bg"]), grad)
    d = ImageDraw.Draw(img)
    s = min(W, H) / 1080
    step = max(8, int(48 * s))
    for x in range(step, W, step):
        for y in range(step, H, step):
            d.point((x, y), fill=t["line"])
    d.ellipse((W - 520 * s, -300 * s, W + 300 * s, 520 * s), outline=t["card2"], width=max(1, int(2 * s)))
    return img


def _cover_fit(im, w, h):
    iw, ih = im.size
    scale = max(w / iw, h / ih)
    nw, nh = max(1, int(iw * scale + 0.5)), max(1, int(ih * scale + 0.5))
    im = im.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - w) // 2, (nh - h) // 2
    return im.crop((left, top, left + w, top + h))


def _illustrated_background(theme: str, W: int, H: int, path: str) -> Image.Image | None:
    """AI 插图铺满作背景，盖一层主题底色的半透明蒙版（文字仍然清楚）。"""
    try:
        with Image.open(path) as src:
            pic = _cover_fit(src.convert("RGB"), W, H)
    except (OSError, ValueError):
        return None
    t = palette(theme)
    shade = Image.new("RGBA", (W, H), tuple(t["bg"]) + (182,))
    return Image.alpha_composite(pic.convert("RGBA"), shade).convert("RGB")


# ---------------------------------------------------------------- 逐条展示：工坊的 scene_items

def items_of(kind: str, data: dict) -> list:
    d = data or {}
    if kind == "bullets":
        return list(d.get("items") or [])
    if kind == "flow":
        return list(d.get("nodes") or [])
    if kind == "table":
        return [" ".join(str(c) for c in r) for r in d.get("rows") or []]
    if kind == "compare":
        return list(d.get("left") or []) + list(d.get("right") or [])
    if kind == "chart":
        return list(d.get("labels") or [])
    return []


# ---------------------------------------------------------------- 各版式（照搬工坊；sc 换成 Page）

def _heading(d, L, t, text, index, total):
    x0, y0, x1, y1 = L.head
    s = L.s
    rounded(d, (x0, y0 + 14 * s, x0 + 10 * s, y1 - 14 * s), 5 * s, fill=t["accent"])
    text_block(d, text, (x0 + 32 * s, y0, x1 - 160 * s, y1), 60 * s, 34 * s, t["fg"], "bold", valign="center", max_lines=1)
    if total > 1:
        label = f"{index + 1:02d}/{total:02d}"
        f = font(28 * s, "mono")
        d.text((x1 - f.getlength(label), y0 + (y1 - y0) / 2 - 16 * s), label, font=f, fill=t["muted"])


def t_title(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = (L.m, L.head[1], L.W - L.m, L.content[3])
    sub = (p.data or {}).get("subtitle") or ""
    lab = (p.data or {}).get("series") or "知识讲解"
    f = font(30 * s)
    w = f.getlength(lab) + 48 * s
    cx = (x0 + x1) / 2
    ty = y0 + (y1 - y0) * 0.18
    rounded(d, (cx - w / 2, ty, cx + w / 2, ty + 56 * s), 28 * s, outline=t["accent"], width=2 * s)
    d.text((cx - f.getlength(lab) / 2, ty + 10 * s), lab, font=f, fill=t["accent"])
    mid = y0 + (y1 - y0) * 0.30
    text_block(d, p.heading, (x0 + 40 * s, mid, x1 - 40 * s, mid + (y1 - y0) * 0.38), (120 if not L.portrait else 104) * s,
               56 * s, t["fg"], "bold", "center", "center", max_lines=3, line_gap=1.25)
    ly = mid + (y1 - y0) * 0.38 + 30 * s
    d.rectangle((cx - 60 * s, ly, cx + 60 * s, ly + 8 * s), fill=t["accent2"])
    if sub:
        text_block(d, sub, (x0 + 80 * s, ly + 40 * s, x1 - 80 * s, min(y1, ly + 200 * s)), 46 * s, 30 * s, t["muted"],
                   "regular", "center", max_lines=2)


def t_keyword(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    kw = data.get("keyword") or p.heading or ""
    note = data.get("note") or ""
    cx, cy = (x0 + x1) / 2, y0 + (y1 - y0) * 0.42
    r = min(x1 - x0, y1 - y0) * 0.42
    d.ellipse((cx - r, cy - r, cx + r, cy + r), outline=t["card2"], width=int(3 * s))
    d.ellipse((cx - r * 0.78, cy - r * 0.78, cx + r * 0.78, cy + r * 0.78), fill=t["card"])
    one_line = len(kw) <= 6
    text_block(d, kw, (cx - r * 0.72, cy - r * 0.6, cx + r * 0.72, cy + r * 0.6), 140 * s, 40 * s if one_line else 48 * s,
               t["accent"], "bold", "center", "center", max_lines=1 if one_line else 3, line_gap=1.2)
    if note:
        text_block(d, note, (x0 + 60 * s, cy + r + 30 * s, x1 - 60 * s, y1), 44 * s, 28 * s, t["fg"], "regular", "center",
                   max_lines=2)


def t_stat(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    h = y1 - y0
    rounded(d, (x0 + 60 * s, y0 + h * 0.08, x1 - 60 * s, y1 - h * 0.08), 36 * s, fill=t["card"])
    text_block(d, str(data.get("value") or ""), (x0 + 100 * s, y0 + h * 0.15, x1 - 100 * s, y0 + h * 0.62), 260 * s, 80 * s,
               t["accent"], "bold", "center", "center", max_lines=1)
    text_block(d, data.get("label") or "", (x0 + 120 * s, y0 + h * 0.64, x1 - 120 * s, y1 - h * 0.12), 52 * s, 30 * s, t["fg"],
               "regular", "center", "center", max_lines=2)


def t_bullets(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    items = (p.data or {}).get("items") or []
    n = max(len(items), 1)
    gap = 22 * s
    row_h = min((170 if L.portrait else 150) * s, (y1 - y0 - gap * (n - 1)) / n)
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
        d.text((bx - fnum.getlength(num) / 2, by - br * 0.72), num, font=fnum, fill=WHITE)
        color = t["fg"] if cur or focus < 0 else t["muted"]
        text_block(d, str(it), (bx + br + 30 * s, top + 8 * s, x1 - 30 * s, top + row_h - 8 * s), min(54 * s, row_h * 0.42),
                   28 * s, color, "bold" if cur else "regular", "left", "center", max_lines=2)


KEYWORDS = set(
    "def class return if elif else for while in import from as with try except finally raise lambda yield pass break continue "
    "and or not is None True False function const let var new this async await public private static void int float double "
    "char bool struct include using namespace select from where group by order join insert update delete create table".split()
)


def code_tokens(line, lang):
    lang = (lang or "").lower()
    mark = "--" if lang == "sql" else ("//" if lang in ("c", "cpp", "c++", "java", "javascript", "js", "ts", "typescript", "go",
                                                        "rust", "swift") else "#")
    out = []
    pattern = re.compile(r"(\"[^\"]*\"|'[^']*')|(\b\d+(?:\.\d+)?\b)|([A-Za-z_][A-Za-z0-9_]*)|(\s+)|(.)")
    idx = line.find(mark)
    if idx >= 0 and line[:idx].count('"') % 2 == 0 and line[:idx].count("'") % 2 == 0:
        code_part, comment = line[:idx], line[idx:]
    else:
        code_part, comment = line, ""
    for m in pattern.finditer(code_part):
        st, nm, word, ws, other = m.groups()
        if st:
            out.append((st, "str"))
        elif nm:
            out.append((nm, "num"))
        elif word:
            out.append((word, "kw" if word in KEYWORDS or (lang == "sql" and word.lower() in KEYWORDS) else "id"))
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


def t_code(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    lines = str(data.get("code") or "").splitlines() or [""]
    lang = str(data.get("language") or "text")
    hl = set()
    for v in data.get("highlight") or []:
        try:
            hl.add(int(v))
        except (TypeError, ValueError):
            pass
    rounded(d, (x0, y0, x1, y1), 24 * s, fill=t["code_bg"])
    for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        d.ellipse((x0 + (28 + i * 30) * s, y0 + 24 * s, x0 + (44 + i * 30) * s, y0 + 40 * s), fill=c)
    fl = font(26 * s, "mono")
    d.text((x1 - 24 * s - fl.getlength(lang), y0 + 18 * s), lang, font=fl, fill=(120, 130, 150))
    top = y0 + 70 * s
    avail_h, avail_w = y1 - top - 24 * s, x1 - x0 - 140 * s
    size = 40 * s
    while size > 18 * s:
        fm, fc = font(size, "mono"), font(size * 0.95)
        widest = max(sum((fc if _is_wide(ch) else fm).getlength(ch) for ch in ln) for ln in lines)
        if widest <= avail_w and len(lines) * size * 1.5 <= avail_h:
            break
        size -= 2
    fm, fc = font(size, "mono"), font(size * 0.95)
    lh = size * 1.5
    colors = {"kw": (198, 120, 221), "str": t["str"], "num": t["num"], "comment": t["comment"], "id": (220, 226, 236)}
    for i, ln in enumerate(lines):
        y = top + i * lh
        if y + lh > y1:
            break
        if (i + 1) in hl:
            d.rectangle((x0 + 6 * s, y - size * 0.15, x1 - 6 * s, y + lh - size * 0.15), fill=(40, 56, 92))
            d.rectangle((x0 + 6 * s, y - size * 0.15, x0 + 12 * s, y + lh - size * 0.15), fill=t["accent2"])
        num = str(i + 1)
        d.text((x0 + 80 * s - fm.getlength(num), y), num, font=fm, fill=(90, 100, 125))
        x = x0 + 110 * s
        for tok, kind in code_tokens(ln, lang):
            x = _mixed_text(d, x, y, tok, fm, fc, colors[kind])


def t_flow(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    nodes = (p.data or {}).get("nodes") or []
    n = max(1, len(nodes))
    gap = (70 if n <= 4 else 56) * s
    boxes = []
    vertical = L.portrait or n > 4
    if vertical:
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
        if vertical:
            cards._arrow(d, ((a[0] + a[2]) / 2, a[3] + 8 * s), ((b[0] + b[2]) / 2, b[1] - 8 * s), col, 5 * s, 22 * s)
        else:
            cards._arrow(d, (a[2] + 8 * s, (a[1] + a[3]) / 2), (b[0] - 8 * s, (b[1] + b[3]) / 2), col, 5 * s, 22 * s)
    for i, (box, label) in enumerate(zip(boxes, nodes)):
        if i >= vis:
            rounded(d, box, 24 * s, outline=t["line"], width=3 * s)
            continue
        cur = i == focus
        rounded(d, box, 24 * s, fill=t["accent"] if cur else t["card"], outline=t["accent"], width=3 * s)
        fsz = min(54 * s, (box[3] - box[1]) * 0.3)
        text_block(d, str(label), (box[0] + 18 * s, box[1] + 14 * s, box[2] - 18 * s, box[3] - 14 * s), fsz, 24 * s,
                   WHITE if cur else t["fg"], "bold", "center", "center", max_lines=3, line_gap=1.2)


def _fmt(v):
    if abs(v) >= 100 or float(v).is_integer():
        return f"{v:,.0f}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def t_chart(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    labels = [str(x) for x in data.get("labels") or []]
    values = []
    for v in data.get("values") or []:
        try:
            values.append(float(v))
        except (TypeError, ValueError):
            values.append(0.0)
    n = len(values)
    if n == 0:
        return t_keyword(d, img, L, t, p, vis, focus)
    fl = font(32 * s)
    if data.get("unit"):
        d.text((x0, y0), "单位：" + str(data["unit"]), font=fl, fill=t["muted"])
    if data.get("source"):
        src = "来源：" + str(data["source"])
        d.text((x1 - fl.getlength(src), y1 - 34 * s), src, font=fl, fill=t["muted"])
    px0, py0, px1, py1 = x0 + 20 * s, y0 + 120 * s, x1 - 20 * s, y1 - 120 * s
    vmax, vmin = max(max(values), 0), min(min(values), 0)
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
            top, bot = sorted((zero, ypos(values[i])))
            if i >= vis:
                rounded(d, (cx - bw / 2, top, cx + bw / 2, max(bot, top + 4 * s)), 10 * s, outline=t["line"], width=2 * s)
                continue
            rounded(d, (cx - bw / 2, top, cx + bw / 2, max(bot, top + 4 * s)), 10 * s, fill=t["accent2"] if i == best else t["accent"])
            lab = _fmt(values[i])
            ly = top - fv.size * 1.3 if values[i] >= 0 else bot + 8 * s
            d.text((cx - fv.getlength(lab) / 2, ly), lab, font=fv, fill=t["fg"])
    for i, lab in enumerate(labels[:n]):
        cx = px0 + slot * (i + 0.5)
        text_block(d, lab, (cx - slot * 0.48, py1 + 18 * s, cx + slot * 0.48, py1 + 110 * s), 34 * s, 20 * s,
                   t["fg"] if i < vis else t["muted"], "regular", "center", max_lines=2, line_gap=1.2)


def t_compare(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    left, right = list(data.get("left") or []), list(data.get("right") or [])
    gap = 48 * s
    if L.portrait:
        cols = [(x0, y0, x1, y0 + (y1 - y0 - gap) / 2), (x0, y0 + (y1 - y0 + gap) / 2, x1, y1)]
    else:
        cols = [(x0, y0, x0 + (x1 - x0 - gap) / 2, y1), (x0 + (x1 - x0 + gap) / 2, y0, x1, y1)]
    shown_left = min(len(left), vis)
    shown_right = max(0, vis - len(left))
    specs = [(data.get("left_title") or "A", left, shown_left, t["accent"]),
             (data.get("right_title") or "B", right, shown_right, t["accent2"])]
    for (bx0, by0, bx1, by1), (title, items, shown, col) in zip(cols, specs):
        rounded(d, (bx0, by0, bx1, by1), 28 * s, fill=t["card"])
        hh = 100 * s
        rounded(d, (bx0, by0, bx1, by0 + hh), 28 * s, fill=col)
        d.rectangle((bx0, by0 + hh - 28 * s, bx1, by0 + hh), fill=col)
        text_block(d, str(title), (bx0 + 30 * s, by0, bx1 - 30 * s, by0 + hh), 52 * s, 28 * s, WHITE, "bold", "center", "center",
                   max_lines=1)
        m = max(1, len(items))
        ih = min(110 * s, (by1 - by0 - hh - 40 * s) / m)
        for i, it in enumerate(items[:shown]):
            iy = by0 + hh + 24 * s + i * ih
            d.ellipse((bx0 + 36 * s, iy + ih / 2 - 9 * s, bx0 + 54 * s, iy + ih / 2 + 9 * s), fill=col)
            text_block(d, str(it), (bx0 + 76 * s, iy, bx1 - 30 * s, iy + ih), min(44 * s, ih * 0.42), 24 * s, t["fg"], "regular",
                       "left", "center", max_lines=2)


def t_table(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    headers = [str(h) for h in data.get("headers") or []]
    rows = [[str(c) for c in r] for r in data.get("rows") or [] if isinstance(r, (list, tuple))]
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
            text_block(d, headers[c] if c < len(headers) else "", (x0 + c * cw + 20 * s, y, x0 + (c + 1) * cw - 20 * s, y + rh),
                       min(44 * s, rh * 0.4), 22 * s, WHITE, "bold", "center", "center", max_lines=2)
        y += rh
    for r_i, row in enumerate(rows):
        if r_i >= vis:
            break
        if r_i == focus:
            d.rectangle((x0, y, x1, y + rh), fill=t["card2"])
        for c in range(ncol):
            text_block(d, row[c] if c < len(row) else "", (x0 + c * cw + 20 * s, y, x0 + (c + 1) * cw - 20 * s, y + rh),
                       min(40 * s, rh * 0.38), 20 * s, t["fg"], "regular", "center", "center", max_lines=2)
        d.line((x0 + 20 * s, y + rh, x1 - 20 * s, y + rh), fill=t["line"], width=int(2 * s))
        y += rh


def t_quote(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    data = p.data or {}
    fq = font(260 * s, "bold")
    d.text((x0 + 10 * s, y0 - 90 * s), "“", font=fq, fill=t["accent"])
    h = y1 - y0
    text_block(d, data.get("text") or (p.text or "")[:60], (x0 + 140 * s, y0 + h * 0.12, x1 - 100 * s, y0 + h * 0.78), 72 * s,
               36 * s, t["fg"], "bold", "left", "center", max_lines=4, line_gap=1.4, balance=True)
    if data.get("source"):
        text_block(d, "—— " + str(data["source"]), (x0 + 140 * s, y0 + h * 0.8, x1 - 100 * s, y1), 40 * s, 26 * s, t["muted"],
                   "regular", "right", max_lines=1)


def t_image(d, img, L, t, p, vis, focus):
    """工坊的配图版式：插图放在内容区（圆角），左下角配说明。没有插图时退回关键词卡。"""
    s = L.s
    data = p.data or {}
    path = data.get("image_path")
    if not path or not Path(path).is_file():
        fb = _with_data(p, {"keyword": p.heading or data.get("caption") or "", "note": data.get("caption", "")})
        return t_keyword(d, img, L, t, fb, vis, focus)
    x0, y0, x1, y1 = [int(v) for v in L.content]
    try:
        with Image.open(path) as src:
            pic = _cover_fit(src.convert("RGB"), x1 - x0, y1 - y0)
    except OSError:
        return t_keyword(d, img, L, t, _with_data(p, {"keyword": p.heading or "", "note": ""}), vis, focus)
    mask = Image.new("L", pic.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, pic.size[0], pic.size[1]), radius=int(28 * s), fill=255)
    img.paste(pic, (x0, y0), mask)
    cap = data.get("caption")
    if cap:
        f = font(38 * s)
        w = f.getlength(cap) + 48 * s
        d.rounded_rectangle((x0 + 24 * s, y1 - 90 * s, x0 + 24 * s + w, y1 - 24 * s), radius=int(16 * s), fill=(0, 0, 0))
        d.text((x0 + 48 * s, y1 - 82 * s), cap, font=f, fill=WHITE)


def t_text(d, img, L, t, p, vis, focus):
    s = L.s
    x0, y0, x1, y1 = L.content
    body = (p.data or {}).get("body") or p.text
    rounded(d, (x0, y0 + 20 * s, x1, y1 - 20 * s), 32 * s, fill=t["card"])
    rounded(d, (x0, y0 + 20 * s, x0 + 12 * s, y1 - 20 * s), 6 * s, fill=t["accent"])
    text_block(d, body, (x0 + 70 * s, y0 + 70 * s, x1 - 60 * s, y1 - 70 * s), 68 * s, 34 * s, t["fg"], "regular", "left",
               "center", max_lines=5, line_gap=1.45)


def _with_data(p, data):
    from .storyboard import Page
    return Page(kind=p.kind, heading=p.heading, start=p.start, end=p.end, text=p.text, data=data, reveals=list(p.reveals))


TEMPLATES = {"title": t_title, "keyword": t_keyword, "stat": t_stat, "bullets": t_bullets, "code": t_code, "flow": t_flow,
             "chart": t_chart, "compare": t_compare, "table": t_table, "quote": t_quote, "image": t_image, "text": t_text}


def render_page(page, W: int, H: int, theme: str = "studio", index: int = 0, total: int = 1, vis: int = 99,
                keywords=(), safe: str = "bottom") -> Image.Image:
    """画一页（RGB）。vis 是已经亮起的条数（要点、流程节点、表格行、对比两栏里的条目、图表的柱子）。"""
    theme = theme if theme in CODE_COLORS else "studio"
    t = palette(theme)
    L = cards.Layout(W, H, safe)
    img = None
    if (page.data or {}).get("image_path") and page.kind != "image":
        img = _illustrated_background(theme, W, H, page.data["image_path"])
    img = (img or _background(theme, W, H)).copy()
    d = ImageDraw.Draw(img)
    kind = page.kind if page.kind in TEMPLATES else "keyword"
    if kind != "title":
        _heading(d, L, t, page.heading, index, total)
    n = len(items_of(kind, page.data))
    vis_n = min(vis, n) if n else vis
    focus = vis_n - 1 if n and vis_n < n else -1
    TEMPLATES[kind](d, img, L, t, page, vis_n, focus)
    s = L.s
    d.rectangle((0, H - 6 * s, W * (index + 1) / max(1, total), H), fill=t["accent"])
    return img
