"""设置与密钥。公开设置和密钥分两个文件；密钥文件不会经接口返回给页面。"""

import copy
import threading

from . import paths
from .storage import read_json, write_json

DEFAULT_SETTINGS = {
    "llm": {
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-flash",
        "temperature": 0.7,
        "max_tokens": 8000,
        "timeout": 240,
        "json_mode": True,
        # 元/百万 token，只用于在界面上估算花费
        "price_input": 2.0,
        "price_cached": 0.04,
        "price_output": 8.0,
    },
    # 可选：用另一个模型做事实核查（报告第4步）。不启用就不产生额外调用。
    "check_llm": {"enabled": False, "base_url": "", "model": ""},
    "tts": {
        "engine": "auto",  # auto | say | sapi | espeak | edge | api | silent
        "voice": "",
        "rate": 1.0,
        "api_base_url": "https://api.siliconflow.cn/v1",
        "api_model": "FunAudioLLM/CosyVoice2-0.5B",
        "api_voice": "FunAudioLLM/CosyVoice2-0.5B:alex",
        "edge_voice": "zh-CN-YunxiNeural",
    },
    "render": {
        "fps": 24,
        "gap_sentence": 0.22,
        "gap_clause": 0.08,
        "gap_scene": 0.35,
        "lead_in": 0.3,
        "tail": 0.8,
        "burn_subtitles": True,
        "ai_watermark": True,
        "music_file": "",
        "music_volume": 0.08,
        "crf": 20,
        "preset": "medium",
        "ffmpeg_path": "",
    },
    "images": {
        "min_score": 0.34,
        "no_repeat_projects": 30,
        "pexels_enabled": False,
        "pexels_per_query": 8,
    },
    # 只替换送给 TTS 的文字，字幕保持原文
    "pronunciation": {
        "SQL": "S Q L",
        "GPU": "G P U",
        "CPU": "C P U",
        "API": "A P I",
        "LLM": "L L M",
        "AI": "A I",
    },
}

SECRET_NAMES = ("llm_key", "check_key", "tts_key", "pexels_key")

_lock = threading.Lock()


def _settings_path():
    return paths.data_dir() / "settings.json"


def _secrets_path():
    return paths.private_dir() / "secrets.json"


def _merge(base, override):
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if k not in base:
            continue
        if isinstance(base[k], dict) and isinstance(v, dict):
            if k == "pronunciation":
                out[k] = {str(a): str(b) for a, b in v.items() if str(a).strip()}
            else:
                out[k] = _merge(base[k], v)
        else:
            want = type(base[k])
            try:
                if want is bool:
                    out[k] = bool(v)
                elif want is int:
                    out[k] = int(v)
                elif want is float:
                    out[k] = float(v)
                elif want is str:
                    out[k] = str(v)
                else:
                    out[k] = v
            except (TypeError, ValueError):
                pass
    return out


def load():
    with _lock:
        return _merge(DEFAULT_SETTINGS, read_json(_settings_path(), {}))


def save(new_settings):
    merged = _merge(DEFAULT_SETTINGS, new_settings)
    with _lock:
        write_json(_settings_path(), merged)
    return merged


def get_secret(name):
    with _lock:
        return (read_json(_secrets_path(), {}) or {}).get(name, "")


def set_secrets(updates):
    """updates 里值为 None 的键保持不变，空字符串表示清除。"""
    with _lock:
        cur = read_json(_secrets_path(), {}) or {}
        for k, v in (updates or {}).items():
            if k in SECRET_NAMES and v is not None:
                if v == "":
                    cur.pop(k, None)
                else:
                    cur[k] = str(v).strip()
        write_json(_secrets_path(), cur, private=True)


def secret_status():
    with _lock:
        cur = read_json(_secrets_path(), {}) or {}
    return {k: bool(cur.get(k)) for k in SECRET_NAMES}
