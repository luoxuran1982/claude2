"""检查上游配音调用（voice.azure_tts_v1 → edge_tts.Communicate）在随包的 edge-tts 版本下能走通。

不连微软服务器：把 aiohttp 的 WebSocket 连接换成一个假的服务端，按 Edge 朗读接口的报文格式回一段音频和逐词时间。
这样从上游 azure_tts_v1 → edge-tts 组装请求（含 Sec-MS-GEC 令牌）→ 解析返回 → SubMaker 出字幕整条路径都会执行，
只有「真的发到 speech.platform.bing.com」这一步被替换。真实配音要在用户电脑上验收。

由 tests/test_edge_tts.py 在独立进程里运行（cwd=upstream/，MPT_CONFIG_FILE 指向临时文件），输出一行 JSON。
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

import aiohttp
import edge_tts

sys.path.insert(0, os.getcwd())
from app.services import voice  # noqa: E402

RESULT: dict = {"edge_tts": edge_tts.__version__}
CAPTURED: dict = {}
REFUSED_CALLS = [0]
FAKE_AUDIO = b"\xff\xf3" + b"\x00" * 598   # 内容无所谓，只看能不能原样写进文件


def _text(path: str, body: str) -> aiohttp.WSMessage:
    head = f"X-RequestId:fake\r\nContent-Type:application/json; charset=utf-8\r\nPath:{path}\r\n\r\n"
    return aiohttp.WSMessage(aiohttp.WSMsgType.TEXT, head + body, None)


def _audio(data: bytes) -> aiohttp.WSMessage:
    # 2 字节头长度 + 头（以 \r\n 结尾，计入长度）+ 音频；与 edge-tts 的 get_headers_and_data 解析方式一致
    head = b"X-RequestId:fake\r\nContent-Type:audio/mpeg\r\nPath:audio\r\n"
    return aiohttp.WSMessage(aiohttp.WSMsgType.BINARY, len(head).to_bytes(2, "big") + head + data, None)


def _boundary(text: str, offset: int, duration: int) -> aiohttp.WSMessage:
    meta = {"Metadata": [{"Type": "WordBoundary", "Data": {
        "Offset": offset, "Duration": duration,
        "text": {"Text": text, "Length": len(text), "BoundaryType": "WordBoundary"}}}]}
    return _text("audio.metadata", json.dumps(meta, ensure_ascii=False))


class FakeWS:
    def __init__(self):
        self.sent: list[str] = []
        self.messages = [_text("turn.start", "{}"), _audio(FAKE_AUDIO),
                         _boundary("你好", 1_000_000, 4_000_000), _boundary("世界", 5_500_000, 4_000_000),
                         _text("turn.end", "{}")]

    async def send_str(self, s: str) -> None:
        self.sent.append(s)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        return self.messages.pop(0)


class FakeConnect:
    def __init__(self, url, **kwargs):
        CAPTURED["url"] = url
        CAPTURED["headers"] = dict(kwargs.get("headers") or {})
        self.ws = FakeWS()

    async def __aenter__(self):
        return self.ws

    async def __aexit__(self, *exc):
        CAPTURED["sent"] = self.ws.sent
        return False


class Refused:
    """模拟用户日志里的失败：握手被拒 403 Invalid response status。"""

    def __init__(self, url, **kwargs):
        REFUSED_CALLS[0] += 1

    async def __aenter__(self):
        raise aiohttp.WSServerHandshakeError(mock.Mock(real_url="wss://speech.platform.bing.com/"), (),
                                             status=403, message="Invalid response status",
                                             headers={"Date": "Wed, 14 Oct 2026 08:00:00 GMT"})

    async def __aexit__(self, *exc):
        return False


def main() -> None:
    out = Path(tempfile.mkdtemp(prefix="svf-edge-")) / "sub" / "audio.mp3"
    with mock.patch.object(aiohttp.ClientSession, "ws_connect", lambda self, url, **kw: FakeConnect(url, **kw)):
        sub = voice.azure_tts_v1("你好世界", "zh-CN-XiaoxiaoNeural-Female", 1.0, str(out))
    url = CAPTURED.get("url", "")
    RESULT.update({
        "ok": sub is not None,
        "audio_bytes": out.stat().st_size if out.exists() else 0,
        "audio_same": out.exists() and out.read_bytes() == FAKE_AUDIO,
        "cues": [c.content for c in getattr(sub, "cues", [])] if sub else [],
        "srt": sub.get_srt() if sub else "",
        "gec_version": (re.search(r"Sec-MS-GEC-Version=([^&]+)", url) or [None, ""])[1],
        "gec_token_v2": bool(re.search(r"Sec-MS-GEC=[0-9A-F]{16}-[0-9A-F]{64}(&|$)", url)),
        "voice_in_ssml": any("zh-CN, XiaoxiaoNeural" in s for s in CAPTURED.get("sent", [])),
    })
    out2 = out.with_name("refused.mp3")
    with mock.patch.object(aiohttp.ClientSession, "ws_connect", lambda self, url, **kw: Refused(url, **kw)):
        RESULT["refused_returns_none"] = voice.azure_tts_v1("你好", "zh-CN-XiaoxiaoNeural-Female", 1.0, str(out2)) is None
    RESULT["refused_no_file"] = not out2.exists()
    RESULT["refused_calls"] = REFUSED_CALLS[0]
    print("RESULT " + json.dumps(RESULT, ensure_ascii=False))


if __name__ == "__main__":
    main()
