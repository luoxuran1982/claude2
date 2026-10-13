"""关键词检查：直接用上游的解析函数（trendradar/core/frequency.py）解析 frequency_words.txt，并试匹配标题。

上游的包在 import 时会连带加载 LiteLLM 等大依赖（trendradar/__init__.py → context → ai），
主进程只需要这一个纯函数文件，所以按文件路径单独加载它，不触发包初始化。安装包里打包了同一份文件。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import tempfile
from functools import lru_cache
from pathlib import Path

from . import paths


@lru_cache(maxsize=1)
def _freq():
    src = paths.upstream_dir() / "trendradar" / "core" / "frequency.py"
    spec = importlib.util.spec_from_file_location("hotradar_upstream_frequency", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _word_view(w: dict) -> dict:
    return {"word": w["word"], "regex": bool(w.get("is_regex")), "display": w.get("display_name")}


def parse(text: str) -> dict:
    """解析关键词文本。返回词组、过滤词、全局过滤词，以及上游打印的警告（例如正则写错）。"""
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            groups, filters, global_filters = _freq().load_frequency_words(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass
    return {
        "groups": [{
            "name": g.get("display_name") or g["group_key"],
            "normal": [_word_view(w) for w in g["normal"]],
            "required": [_word_view(w) for w in g["required"]],
            "max_count": g.get("max_count") or 0,
        } for g in groups],
        "filters": [_word_view(w) for w in filters],
        "global_filters": [_word_view(w) for w in global_filters],
        "warnings": [l for l in out.getvalue().splitlines() if l.strip()],
        "_raw": (groups, filters, global_filters),
    }


def try_match(text: str, titles: list[str]) -> dict:
    """用上游的匹配规则逐条试匹配标题，给出是否命中、命中哪些词组、被哪个过滤词排除。"""
    parsed = parse(text)
    groups, filters, global_filters = parsed.pop("_raw")
    fm = _freq()
    results = []
    for title in titles[:50]:
        title = str(title or "").strip()
        if not title:
            continue
        low = title.lower()
        matched = fm.matches_word_groups(title, groups, filters, global_filters)
        reason = ""
        hit_groups = []
        g_block = next((w for w in global_filters if fm._word_matches(w, low)), None)
        f_block = next((w for w in filters if fm._word_matches(w, low)), None)
        if g_block:
            reason = f"被全局过滤词「{g_block['word']}」排除"
        elif f_block and groups:
            reason = f"被过滤词「!{f_block['word']}」排除（上游实现中 ! 过滤词对所有词组生效）"
        else:
            for g in groups:
                req_ok = all(fm._word_matches(w, low) for w in g["required"]) if g["required"] else True
                norm_ok = any(fm._word_matches(w, low) for w in g["normal"]) if g["normal"] else True
                if req_ok and norm_ok:
                    hit_groups.append(g.get("display_name") or g["group_key"])
            if not groups:
                reason = "没有设置任何词组：上游此时显示全部新闻"
            elif not hit_groups:
                reason = "没有命中任何词组"
        results.append({"title": title, "matched": bool(matched), "groups": hit_groups, "reason": reason})
    return {"results": results, "summary": parsed}


def summary(ws: Path) -> dict:
    text = (ws / "config" / "frequency_words.txt").read_text(encoding="utf-8")
    p = parse(text)
    p.pop("_raw", None)
    return p
