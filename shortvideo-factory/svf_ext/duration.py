"""目标时长：把「目标时长（秒）」换算成文案字数，写进写文案的提示词。

上游只有「段落数」，大模型写多长全看它自己，配音后才知道视频多长。这里按朗读速度估算：
- 中文：普通语速每秒约 4.5 个字（不算标点）；英文约每秒 2.6 个词；
- 跟配音语速联动：语速 1.2 倍就按 4.5 × 1.2 字/秒算；
- 每次出片后记下「实际字数 / 实际配音秒数」（数据目录 storage/svf_speech_rate.json），之后用最近几次的中位数，越用越准。
提示词里同时保留上游的段落数（段落数决定结构，字数决定长度）。生成后在日志解读里显示「目标多少秒、实际多少秒」。
"""
from __future__ import annotations

import json
import re
import statistics
import time
from pathlib import Path

CONFIG_KEY = "svf_target_seconds"     # config.toml 的 [ui]；0 表示不限（沿用上游，只按段落数）
PRESETS = (0, 30, 60, 90, 120)
CUSTOM = -1
DEFAULT_CPS = {"zh": 4.5, "en": 2.6}  # 中文字/秒、英文词/秒（语速 1.0）
CPS_RANGE = {"zh": (3.0, 7.0), "en": (1.5, 4.0)}
MIN_SECONDS, MAX_SECONDS = 10, 600
TASK_FILE = "svf_task.json"
STATS_FILE = "svf_speech_rate.json"
_CJK = re.compile(r"[㐀-鿿豈-﫿]")


def label(v: int) -> str:
    if v == 0:
        return "不限（按段落数）"
    if v == CUSTOM:
        return "自定义…"
    return f"{v} 秒"


def language_of(text: str, language: str = "") -> str:
    lang = (language or "").lower()
    if lang.startswith("zh"):
        return "zh"
    if lang and not lang.startswith("zh"):
        return "en" if not _CJK.search(text or "") else "zh"
    return "zh" if _CJK.search(text or "") else "en"


def count_units(text: str, lang: str) -> int:
    """中文数汉字和字母数字（不算标点、空格）；英文数词。"""
    text = text or ""
    if lang == "zh":
        return len(_CJK.findall(text)) + len(re.findall(r"[A-Za-z]+|\d+", text))
    return len(re.findall(r"[A-Za-z0-9']+", text))


def unit_name(lang: str) -> str:
    return "字" if lang == "zh" else "个英文单词"


def _stats_path() -> Path | None:
    try:
        from app.utils import utils
        return Path(utils.storage_dir()) / STATS_FILE
    except Exception:  # noqa: BLE001
        return None


def _load_stats(path: Path | None) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path else {}
    except (OSError, ValueError):
        return {}


def chars_per_second(lang: str = "zh", voice_rate: float = 1.0, stats: dict | None = None) -> tuple[float, str]:
    """返回（每秒字数, 依据）。语速 1.0 时的基准优先用实测中位数。"""
    stats = _load_stats(_stats_path()) if stats is None else stats
    samples = [s for s in (stats.get(lang) or []) if isinstance(s, (int, float))]
    lo, hi = CPS_RANGE.get(lang, (1.0, 8.0))
    if len(samples) >= 2:
        base, why = statistics.median(samples[-10:]), f"按最近 {min(len(samples), 10)} 次出片的实测中位数"
    else:
        base, why = DEFAULT_CPS.get(lang, 4.5), "按常见语速估算"
    base = min(hi, max(lo, base))
    try:
        rate = float(voice_rate or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    rate = min(2.0, max(0.5, rate))
    return base * rate, why + (f"，乘以配音语速 {rate:g} 倍" if abs(rate - 1) > 1e-6 else "")


def target_units(seconds: float, cps: float) -> int:
    n = seconds * cps
    return int(round(n / 10.0) * 10) if n >= 50 else max(10, int(round(n)))


def plan(seconds, lang: str = "zh", voice_rate: float = 1.0, stats: dict | None = None) -> dict | None:
    try:
        seconds = int(seconds or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None
    seconds = min(MAX_SECONDS, max(MIN_SECONDS, seconds))
    cps, why = chars_per_second(lang, voice_rate, stats)
    return {"target_seconds": seconds, "lang": lang, "chars_per_second": round(cps, 2), "target_chars": target_units(seconds, cps),
            "unit": unit_name(lang), "basis": why}


def prompt_addition(p: dict) -> str:
    n, unit, sec = p["target_chars"], p["unit"], p["target_seconds"]
    lo, hi = int(n * 0.9), int(n * 1.1)
    if p["lang"] == "zh":
        count = f"about {n} Chinese characters in total (between {lo} and {hi}, not counting punctuation)"
    else:
        count = f"about {n} words in total (between {lo} and {hi})"
    return (f"\n\n# Length Requirement:\n"
            f"- The script will be read aloud by text-to-speech; the narration must last about {sec} seconds.\n"
            f"- Therefore write {count}.\n"
            f"- Keep the number of paragraphs requested above, and adjust the length of each paragraph so the total length meets this requirement.\n"
            f"- 目标时长约 {sec} 秒，全文约 {n} {unit}（{lo}–{hi}）。")


def read_task(task_dir) -> dict:
    try:
        return json.loads((Path(task_dir) / TASK_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}


def write_task(task_dir, data: dict) -> None:
    d = Path(task_dir)
    try:
        d.mkdir(parents=True, exist_ok=True)
        cur = read_task(d)
        cur.update(data)
        tmp = d / (TASK_FILE + ".tmp")
        tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(d / TASK_FILE)
    except OSError:
        pass


def record_actual(task_dir, script: str, audio_seconds: float, lang: str, voice_rate: float = 1.0,
                  stats_path: Path | None = None) -> dict:
    """出片后记下实际字数和配音时长；同时更新实测语速（只记语速 1.0 附近的，换算回 1.0 倍）。"""
    units = count_units(script, lang)
    info = {"actual_seconds": round(float(audio_seconds or 0), 2), "script_chars": units, "lang": lang,
            "unit": unit_name(lang)}
    write_task(task_dir, info)
    try:
        rate = float(voice_rate or 1.0)
    except (TypeError, ValueError):
        rate = 1.0
    if audio_seconds and audio_seconds > 3 and units >= 20 and rate > 0:
        cps = units / float(audio_seconds) / rate
        lo, hi = CPS_RANGE.get(lang, (1.0, 8.0))
        if lo <= cps <= hi:
            path = stats_path or _stats_path()
            stats = _load_stats(path)
            stats.setdefault(lang, []).append(round(cps, 3))
            stats[lang] = stats[lang][-30:]
            stats["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            try:
                if path:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps(stats, ensure_ascii=False), encoding="utf-8")
            except OSError:
                pass
    return info


def summary_line(info: dict) -> str:
    """日志里的一行，日志解读会原样显示。"""
    parts = []
    if info.get("target_seconds"):
        parts.append(f"目标时长 {info['target_seconds']} 秒（请大模型写约 {info.get('target_chars')} {info.get('unit', '字')}）")
    if info.get("actual_seconds"):
        a = float(info["actual_seconds"])
        s = f"实际配音 {a:.1f} 秒，文案 {info.get('script_chars', 0)} {info.get('unit', '字')}"
        if info.get("target_seconds"):
            d = a - float(info["target_seconds"])
            s += f"，比目标{'长' if d > 0 else '短'} {abs(d):.0f} 秒"
        parts.append(s)
    return "；".join(parts)
