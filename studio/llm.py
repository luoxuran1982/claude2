"""OpenAI 兼容的 Chat Completions 客户端（DeepSeek、硅基流动、通义千问、Kimi、智谱、本地 Ollama 都可用）。

省 token 的三个做法都在这里：
1. 相同请求走本地缓存，重新渲染、改画面都不会再调模型；
2. 固定的系统提示词放在最前面，服务商的前缀缓存按缓存价计费；
3. 记录每次调用的真实 token 用量，界面上能看到花了多少钱。
"""

import hashlib
import json
import re
import time
import urllib.error
import urllib.request

from . import paths
from .media import urlopen
from .storage import read_json, write_json


class LLMError(RuntimeError):
    pass


DEFAULT_MAX_TOKENS = 32000

# 服务商在 400 错误里给出的 max_tokens 上限，例如：
#   DeepSeek：Invalid max_tokens value, the valid range of max_tokens is [1, 393216]
#   OpenAI：max_tokens is too large: 1000000. This model supports at most 16384 completion tokens
_MAX_TOKENS_LIMIT = [
    re.compile(r"\[\s*\d+\s*,\s*(\d+)\s*\]"),
    re.compile(r"at most\s+(\d+)", re.I),
    re.compile(r"(?:<=|less than or equal to)\s*(\d+)", re.I),
]


def max_tokens_limit(msg):
    """从服务商的错误信息里读出 max_tokens 上限；不是这类错误就返回 None。"""
    if "max_tokens" not in (msg or "").lower():
        return None
    for pat in _MAX_TOKENS_LIMIT:
        m = pat.search(msg)
        if m:
            return int(m.group(1))
    return None


PRESETS = [
    {"name": "DeepSeek", "base_url": "https://api.deepseek.com", "model": "deepseek-flash"},
    {"name": "硅基流动", "base_url": "https://api.siliconflow.cn/v1", "model": "deepseek-ai/DeepSeek-V3"},
    {"name": "通义千问（百炼）", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus"},
    {"name": "Kimi（月之暗面）", "base_url": "https://api.moonshot.cn/v1", "model": "kimi-latest"},
    {"name": "智谱 GLM", "base_url": "https://open.bigmodel.cn/api/paas/v4", "model": "glm-4-flash"},
    {"name": "火山方舟（豆包）", "base_url": "https://ark.cn-beijing.volces.com/api/v3", "model": ""},
    {"name": "本地 Ollama", "base_url": "http://127.0.0.1:11434/v1", "model": "qwen2.5:14b"},
]


def _endpoint(base_url):
    base = (base_url or "").strip().rstrip("/")
    if not base:
        raise LLMError("没有设置模型接口地址")
    if base.endswith("/chat/completions"):
        return base
    return base + "/chat/completions"


def _cache_path(key):
    return paths.cache_dir("llm") / f"{key}.json"


def request_key(cfg, messages, json_mode):
    raw = json.dumps(
        [cfg.get("base_url"), cfg.get("model"), round(float(cfg.get("temperature", 0.7)), 2), json_mode, messages],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def chat(messages, cfg, api_key, json_mode=True, use_cache=True, max_tokens=None, cancel=None):
    """返回 (文本, 用量 dict)。用量里 cached=True 表示命中本地缓存、没有花钱。"""
    key = request_key(cfg, messages, json_mode)
    if use_cache:
        hit = read_json(_cache_path(key))
        if hit and hit.get("text"):
            return hit["text"], {"model": cfg.get("model"), "prompt_tokens": 0, "completion_tokens": 0, "cache_hit_tokens": 0, "local_cache": True}

    is_local = "127.0.0.1" in (cfg.get("base_url") or "") or "localhost" in (cfg.get("base_url") or "")
    if not api_key and not is_local:
        raise LLMError("没有设置模型 API Key（设置页填写；本地 Ollama 可留空）")

    body = {
        "model": cfg.get("model"),
        "messages": messages,
        "temperature": float(cfg.get("temperature", 0.7)),
        "max_tokens": int(max_tokens or cfg.get("max_tokens") or DEFAULT_MAX_TOKENS),
        "stream": False,
    }
    if json_mode and cfg.get("json_mode", True):
        body["response_format"] = {"type": "json_object"}

    # 思考型模型写长稿要几分钟；旧设置里存的是 240 秒，这里至少等 600 秒
    timeout = max(int(cfg.get("timeout") or 0), 600)
    data, sent_max = _post(_endpoint(cfg.get("base_url")), body, api_key, timeout, cancel)
    try:
        choice = data["choices"][0]
        text = choice["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError("模型返回格式不对：" + json.dumps(data, ensure_ascii=False)[:300])
    u = data.get("usage") or {}
    if choice.get("finish_reason") == "length":
        used = int(u.get("completion_tokens") or 0)
        think = int((u.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        detail = f"这次最大输出 {sent_max} token，模型实际输出 {used}" + (f"，其中思考过程占了 {think}" if think else "")
        raise LLMError(
            f"模型输出被截断（达到最大输出 token）：{detail}。思考型模型（如 deepseek-flash）先思考再写稿，思考也算在最大输出里。"
            "请把「设置 → 最大输出 token」调大（例如 32000；填得比服务商上限还大也没关系，会自动降到上限），或缩短目标时长。"
        )
    usage = {
        "model": data.get("model") or cfg.get("model"),
        "prompt_tokens": int(u.get("prompt_tokens") or 0),
        "completion_tokens": int(u.get("completion_tokens") or 0),
        # DeepSeek 字段名；OpenAI 风格在 prompt_tokens_details.cached_tokens
        "cache_hit_tokens": int(u.get("prompt_cache_hit_tokens") or (u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0),
        "local_cache": False,
    }
    if sent_max < body["max_tokens"]:
        usage["note"] = f"最大输出 {body['max_tokens']} 超过服务商上限，已按上限 {sent_max} 请求"
    write_json(_cache_path(key), {"text": text, "usage": usage, "time": time.time()})
    return text, usage


def _post(url, body, api_key, timeout, cancel):
    """返回 (响应 JSON, 实际请求用的 max_tokens)。"""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    last_err = None
    tried_without_json = False
    clamped = False
    for attempt in range(4):
        if cancel is not None and cancel.is_set():
            raise LLMError("已取消")
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(req, timeout) as resp:
                return json.loads(resp.read().decode("utf-8")), body.get("max_tokens")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")[:500]
            limit = max_tokens_limit(msg) if e.code == 400 else None
            if limit and not clamped and int(body.get("max_tokens") or 0) > limit:
                # 最大输出填得比服务商上限还大：按错误信息里给的上限再请求一次
                body = {**body, "max_tokens": limit}
                clamped = True
                continue
            if e.code == 400 and "response_format" in body and not tried_without_json and "max_tokens" not in msg.lower():
                # 有的服务商不支持 JSON 模式，去掉再试一次
                body = {k: v for k, v in body.items() if k != "response_format"}
                tried_without_json = True
                continue
            if e.code in (401, 403):
                raise LLMError(f"API Key 无效或没有权限（HTTP {e.code}）：{msg}")
            if e.code in (429, 500, 502, 503, 504):
                last_err = LLMError(f"模型接口繁忙（HTTP {e.code}）：{msg}")
                time.sleep(3 * (attempt + 1))
                continue
            raise LLMError(f"模型接口 HTTP {e.code}：{msg}")
        except urllib.error.URLError as e:
            last_err = LLMError(f"连接模型接口失败：{e.reason}")
            time.sleep(2 * (attempt + 1))
        except TimeoutError:
            # 请求已经发出去了，模型可能在服务商那边写完并计费；不自动重试，免得重复花钱
            raise LLMError(f"等了 {timeout} 秒模型还没写完。可以缩短目标时长后再试，或稍后重新生成。")
        except OSError as e:
            last_err = LLMError(f"模型接口超时或网络错误：{e}")
            time.sleep(2 * (attempt + 1))
        except json.JSONDecodeError:
            raise LLMError("模型接口返回的不是 JSON")
    raise last_err or LLMError("模型接口调用失败")


def extract_json(text):
    """从模型输出里取出 JSON 对象，容忍 ```json 包裹和前后多余文字。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        frag = t[start : end + 1]
        try:
            return json.loads(frag)
        except json.JSONDecodeError:
            # 常见毛病：尾随逗号
            frag2 = re.sub(r",\s*([}\]])", r"\1", frag)
            try:
                return json.loads(frag2)
            except json.JSONDecodeError:
                pass
    raise LLMError("模型输出不是合法 JSON，前 200 字：" + t[:200])


def cost_yuan(usage, cfg):
    if usage.get("local_cache"):
        return 0.0
    hit = usage.get("cache_hit_tokens", 0)
    miss = max(0, usage.get("prompt_tokens", 0) - hit)
    return (
        miss * float(cfg.get("price_input", 0))
        + hit * float(cfg.get("price_cached", 0))
        + usage.get("completion_tokens", 0) * float(cfg.get("price_output", 0))
    ) / 1_000_000
