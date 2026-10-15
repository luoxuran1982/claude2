"""中文提示：配音失败的原因、素材钥匙格式、素材搜索失败的原因、声音与文案语言不一致。

上游出错时只在日志里写英文原因（如 edge-tts 的 `403, message='Invalid response status'`、
`No audio was received`，Pixabay 的 `status=400`）。这里装一个 loguru 日志接收器，只记下配音（app.services.voice）
和素材（app.services.material）模块最近的警告/错误，出错时据此给出对应的中文说明。不改上游的日志和报错原文。
"""
from __future__ import annotations

import re
import threading
import time
from collections import deque

# ---------------------------------------------------------------- 最近的出错日志

_RECENT: deque = deque(maxlen=200)
_LOCK = threading.Lock()
_INSTALLED = [False]
WATCH_MODULES = ("app.services.voice", "app.services.material")


def _sink(message) -> None:
    rec = message.record
    with _LOCK:
        _RECENT.append((time.time(), rec["name"] or "", str(rec["message"])))


def install_log_sink() -> None:
    """只装一次；没有 loguru 时什么也不做。"""
    if _INSTALLED[0]:
        return
    try:
        from loguru import logger
    except ImportError:
        return
    # INFO 也要：配音每次尝试开头的「start, voice name」、素材每次搜索开头的「searching videos on」用来划出「这一次」的范围
    logger.add(_sink, level="INFO", filter=lambda r: (r["name"] or "").startswith(WATCH_MODULES), enqueue=False,
               format="{message}", catch=True)
    _INSTALLED[0] = True


def note(module: str, message: str) -> None:
    """测试和外壳自己用：直接记一条。"""
    with _LOCK:
        _RECENT.append((time.time(), module, message))


def recent(module_prefix: str, within: float = 180.0) -> list[str]:
    now = time.time()
    with _LOCK:
        return [m for t, n, m in _RECENT if n.startswith(module_prefix) and now - t <= within]


def _latest_reason(module_prefix: str, within: float, classify, start_marker: re.Pattern, provider: str | None = None):
    """从最近的日志往前找第一条能分类的出错信息；碰到「这一次尝试的开头」就停，免得拿以前的错误当这次的原因。"""
    for msg in reversed(recent(module_prefix, within)):
        if provider and provider not in msg.lower() and "_api_keys" not in msg and not start_marker.search(msg):
            continue
        code = classify(msg)
        if code:
            return code
        if start_marker.search(msg):
            return None
    return None


VOICE_START = re.compile(r"^start, voice name")
MATERIAL_START = re.compile(r"^searching videos on")


# ---------------------------------------------------------------- 配音

TTS_REASONS = {
    "forbidden": "微软 Edge 配音服务拒绝了连接（403，服务验证问题）：微软改了验证方式，旧版本会被拒绝，请升级到最新版 AI 短视频工厂。",
    "no_audio": "微软服务没有返回声音（No audio was received）：多半是所选声音和文案语言不一致（例如中文文案选了 af-ZA、zu-ZA 开头的声音），"
                "请换成 zh-CN 开头的中文声音；声音没选错的话，可能是代理节点被微软拒绝，换个节点或暂时关掉代理再试。",
    "network": "连不上微软配音服务器（speech.platform.bing.com）：请检查网络是否通、代理是否开着，以及防火墙或杀毒软件有没有拦截。",
}
TTS_GENERIC = ("【可能的原因】① 选的声音和文案语言不一致（中文文案请选 zh-CN 开头的声音）；② 网络或代理连不上微软配音服务器；"
               "③ 日志里出现 403 时是服务验证问题，请升级到最新版。也可以在「配音服务」里换别的引擎，或改用「上传音频」。"
               "详见「使用指南 → 出了问题怎么办」。")


def classify_tts_error(text: str) -> str | None:
    t = (text or "").lower()
    if not t:
        return None
    if "no audio was received" in t or "stream contained no audio" in t or "noaudioreceived" in t:
        return "no_audio"
    if re.search(r"\b403\b", t) or "invalid response status" in t or "forbidden" in t:
        return "forbidden"
    if any(k in t for k in ("cannot connect", "clientconnector", "timed out", "timeout", "name or service not known",
                            "getaddrinfo", "nodename nor servname", "connection reset", "connection refused",
                            "network is unreachable", "ssl", "certificate", "proxy", "server disconnected",
                            "temporary failure in name resolution", "no route to host", "connection aborted")):
        return "network"
    return None


def tts_failure_reason(within: float = 300.0) -> str | None:
    """最近一次配音失败的原因代码（forbidden / no_audio / network），看不出来时返回 None。"""
    return _latest_reason("app.services.voice", within, classify_tts_error, VOICE_START)


def tts_failure_hint(within: float = 300.0) -> str:
    """接在配音失败提示后面的中文说明：能判断原因就只说那一条，否则列出常见原因。"""
    code = tts_failure_reason(within)
    if code:
        return f"【原因】{TTS_REASONS[code]}"
    return TTS_GENERIC


# ---------------------------------------------------------------- 素材钥匙

PIXABAY_KEY = re.compile(r"^\d{5,12}-[0-9a-f]{25}$")
PEXELS_KEY = re.compile(r"^[A-Za-z0-9]{50,60}$")


def key_kind(key: str) -> str | None:
    """按格式认出是哪家的钥匙：Pixabay 是「数字-25 位十六进制」，Pexels 是约 56 位字母数字、不含横杠。"""
    k = (key or "").strip()
    if PIXABAY_KEY.match(k):
        return "pixabay"
    if PEXELS_KEY.match(k):
        return "pexels"
    return None


NAMES = {"pexels": "Pexels", "pixabay": "Pixabay"}


def key_format_hint(provider: str, value: str) -> str | None:
    """检查填在 provider 栏里的钥匙（可逗号分隔多个）。格式没问题返回 None，否则返回中文提示。"""
    keys = [k for k in re.split(r"[,，\s]+", value or "") if k]
    if not keys:
        return None
    name = NAMES.get(provider, provider)
    problems = []
    for k in keys:
        kind = key_kind(k)
        if kind == provider:
            continue
        if kind and kind != provider:
            problems.append(f"「{_mask(k)}」这像是 {NAMES[kind]} 的钥匙，请填到「{NAMES[kind]} API Key」那一栏")
        elif provider == "pixabay":
            problems.append(f"「{_mask(k)}」不像 Pixabay 钥匙（应为「数字-25 位十六进制」，如 12345678-0123456789abcdef012345678）"
                            "，请从 Pixabay 网站复制，不要手打")
        elif provider == "pexels":
            problems.append(f"「{_mask(k)}」不像 Pexels 钥匙（约 56 位字母和数字、不含横杠），请从 Pexels 网站复制")
    if not problems:
        return None
    return "；".join(problems) + f"。（这里只检查 {name} 钥匙的格式，能不能用以实际搜索为准）"


def _mask(k: str) -> str:
    return k if len(k) <= 10 else f"{k[:4]}…{k[-4:]}"


# ---------------------------------------------------------------- 素材搜索失败

STOCK_REASONS = {
    "pexels_key": "Pexels 没有正常返回结果（返回的不是 JSON 或 401）：钥匙无效；另外 Pexels 目前暂停发放新的 API 钥匙，"
                  "新申请的钥匙可能用不了。建议改用 Pixabay（免费，https://pixabay.com/api/docs/ 登录后直接显示钥匙），"
                  "或选「知识讲解卡片」不需要钥匙。",
    "pixabay_key": "Pixabay 返回 400：钥匙无效。请从 Pixabay 网站（https://pixabay.com/api/docs/ 登录后页面里的 key）整段复制，"
                   "不要手打，必要时在网站上重新生成。",
    "rate_limit": "素材站返回 429：请求太频繁，被暂时限流，过几分钟再试。",
    "blocked": "素材站拦截了请求（Cloudflare 验证页），常见于公司网络或代理节点：换个网络或代理节点，或在设置里填「素材下载代理」。",
    "network": "连不上素材站：请检查网络；需要代理才能访问时，在「设置 → 素材来源」里填「素材下载代理」（如 http://127.0.0.1:7897）。",
    "no_key": "还没填素材钥匙：在「设置 → 素材来源」里填 Pixabay 或 Pexels 的 API Key，或选「知识讲解卡片」。",
    "no_result": "素材站没搜到合适的视频（关键词太专业或画面比例不匹配）。讲知识类主题可以选「知识讲解卡片」或「混合」。",
}


def classify_stock_error(message: str) -> str | None:
    t = (message or "").lower()
    if "pexels" in t and ("jsondecodeerror" in t or "unsupported response" in t or "401" in t or "expecting value" in t):
        return "pexels_key"
    if "pixabay" in t and re.search(r"status=(400|401|403)\b", t):
        return "pixabay_key"
    if "status=429" in t or "rate limit" in t:
        return "rate_limit"
    if "cloudflare" in t:
        return "blocked"
    if "_api_keys is not set" in t:
        return "no_key"
    if any(k in t for k in ("connectionerror", "connecttimeout", "readtimeout", "proxyerror", "sslerror", "timed out",
                            "max retries exceeded", "name or service not known", "getaddrinfo")):
        return "network"
    return None


def stock_failure_reason(provider: str | None = None, within: float = 900.0) -> str | None:
    return _latest_reason("app.services.material", within, classify_stock_error, MATERIAL_START, provider)


def stock_failure_hint(provider: str | None = None, within: float = 900.0) -> str:
    code = stock_failure_reason(provider, within)
    return f"【原因】{STOCK_REASONS[code]}" if code else f"【可能的原因】{STOCK_REASONS['no_result']}"


# ---------------------------------------------------------------- 声音与文案语言

def text_language(text: str) -> str | None:
    """文案主要是中文返回 'zh'，主要是其他文字返回 'other'，太短判断不了返回 None。"""
    chars = [c for c in (text or "") if c.isalpha()]
    if len(chars) < 2:
        return None
    cjk = sum(1 for c in chars if "㐀" <= c <= "鿿" or "豈" <= c <= "﫿")
    if cjk / len(chars) >= 0.3:
        return "zh"
    return "other"


EDGE_SERVERS = ("azure-tts-v1", "azure-tts-v2")


def voice_language_warning(text: str, voice_name: str, tts_server: str = "azure-tts-v1", video_language: str = "") -> str | None:
    """文案是中文、选的 Edge/Azure 声音却不是 zh- 开头时，返回中文提醒；其他情况返回 None。"""
    if tts_server and tts_server not in EDGE_SERVERS:
        return None
    voice = (voice_name or "").strip()
    if not voice or not re.match(r"^[a-z]{2,3}-[A-Za-z]{2,4}", voice) or voice.lower().startswith("no-voice"):
        return None   # 1.0.3：选「无配音」（no-voice）时不提醒
    lang = text_language(text)
    if (video_language or "").lower().startswith("zh"):
        lang = "zh"
    if lang != "zh" or voice.lower().startswith("zh-"):
        return None
    locale = "-".join(voice.split("-")[:2])
    return (f"文案主要是中文，但选的配音声音是「{voice}」（{locale}，不是中文）。微软配音会因语言不匹配返回「No audio was received」"
            "导致配音失败。请在「配音声音」里选 zh-CN 开头的声音，例如 zh-CN-XiaoxiaoNeural（晓晓）。")
