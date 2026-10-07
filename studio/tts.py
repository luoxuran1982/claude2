"""配音引擎。所有引擎输出都经 FFmpeg 统一成 24kHz WAV，并按文本哈希缓存，改一句只重配一句。

- say：macOS 自带，免费离线
- sapi：Windows 自带（System.Speech），免费离线；中文需系统装有中文语音（如 Huihui）
- espeak：Linux 兜底
- api：OpenAI 兼容的 /audio/speech（硅基流动 CosyVoice2 / IndexTTS、OpenAI 等），按字计费
- edge：edge-tts（非官方接口，稳定性和商用授权无保证）
- silent：静音占位，只用于预览排版
"""

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from . import paths
from .media import Runner, ToolError, normalize_audio, wav_frames, write_wav, silence, urlopen, SAMPLE_RATE
from .textutil import spoken_length

ENGINE_NAMES = {
    "say": "macOS 系统语音（免费离线）",
    "sapi": "Windows 系统语音（免费离线）",
    "espeak": "eSpeak（Linux 兜底）",
    "api": "云端 TTS 接口（硅基流动/OpenAI 兼容）",
    "edge": "edge-tts（免费，非官方接口）",
    "silent": "静音占位（只看排版）",
}

SAPI_SCRIPT = r"""
param([string]$TextFile, [string]$OutFile, [string]$Voice = "", [int]$Rate = 0)
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
if ($Voice -ne "") { $s.SelectVoice($Voice) }
else {
  $zh = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like "zh*" } | Select-Object -First 1
  if ($zh) { $s.SelectVoice($zh.VoiceInfo.Name) }
}
$s.Rate = $Rate
$s.SetOutputToWaveFile($OutFile)
$text = [System.IO.File]::ReadAllText($TextFile, [System.Text.Encoding]::UTF8)
$s.Speak($text)
$s.Dispose()
"""

SAPI_LIST = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$s.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo.Name + "|" + $_.VoiceInfo.Culture.Name }
"""


def available_engines():
    out = []
    if paths.IS_MAC and shutil.which("say"):
        out.append("say")
    if paths.IS_WIN:
        out.append("sapi")
    if shutil.which("espeak-ng") or shutil.which("espeak"):
        out.append("espeak")
    out.append("api")
    try:
        import edge_tts  # noqa: F401

        out.append("edge")
    except Exception:
        pass
    out.append("silent")
    return out


def resolve_engine(engine):
    if engine and engine != "auto":
        return engine
    avail = available_engines()
    for e in ("say", "sapi", "espeak"):
        if e in avail:
            return e
    return "silent"


def list_voices(engine, runner=None):
    runner = runner or Runner()
    engine = resolve_engine(engine)
    try:
        if engine == "say":
            raw = runner.run(["say", "-v", "?"], timeout=20).decode("utf-8", "replace")
            voices = []
            for line in raw.splitlines():
                parts = line.split("#")[0].split()
                if len(parts) >= 2:
                    name, lang = " ".join(parts[:-1]), parts[-1]
                    voices.append({"name": name, "lang": lang})
            zh = [v for v in voices if v["lang"].lower().startswith("zh")]
            return zh + [v for v in voices if v not in zh]
        if engine == "sapi":
            raw = _powershell(runner, SAPI_LIST, [], timeout=30).decode("utf-8", "replace")
            voices = []
            for line in raw.splitlines():
                if "|" in line:
                    name, lang = line.strip().split("|", 1)
                    voices.append({"name": name, "lang": lang})
            return voices
        if engine == "edge":
            return [{"name": n, "lang": "zh-CN"} for n in ("zh-CN-YunxiNeural", "zh-CN-XiaoxiaoNeural", "zh-CN-YunyangNeural", "zh-CN-XiaoyiNeural", "zh-CN-YunjianNeural")]
    except ToolError:
        return []
    return []


def _powershell(runner, script, args, timeout=120):
    fd, ps1 = tempfile.mkstemp(suffix=".ps1")
    with os.fdopen(fd, "w", encoding="utf-8-sig") as f:
        f.write(script)
    try:
        return runner.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", ps1] + list(args), timeout=timeout)
    finally:
        try:
            os.unlink(ps1)
        except OSError:
            pass


def cache_key(engine, cfg, text):
    if engine == "api":
        ident = [cfg.get("api_base_url"), cfg.get("api_model"), cfg.get("api_voice")]
    elif engine == "edge":
        ident = [cfg.get("edge_voice")]
    else:
        ident = [cfg.get("voice")]
    raw = json.dumps([engine, ident, round(float(cfg.get("rate") or 1.0), 2), text], ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


class TTS:
    def __init__(self, cfg, api_key="", ffmpeg="", runner=None):
        self.cfg = cfg
        self.engine = resolve_engine(cfg.get("engine"))
        self.api_key = api_key
        self.ffmpeg = ffmpeg
        self.runner = runner or Runner()
        self.cache = paths.cache_dir("tts")
        if self.engine == "say" and not (cfg.get("voice") or "").strip():
            # 系统默认语音常是英文，自动换成中文语音（婷婷/美佳等）
            zh = [v["name"] for v in list_voices("say", self.runner) if v["lang"].lower().startswith("zh")]
            pick = next((v for v in zh if v.startswith("Tingting")), zh[0] if zh else "")
            self.cfg = dict(cfg, voice=pick)

    def synthesize(self, text):
        """返回 (wav 路径, 采样数)。"""
        text = text.strip()
        key = cache_key(self.engine, self.cfg, text)
        out = self.cache / f"{key}.wav"
        if out.exists() and out.stat().st_size > 44:
            return out, wav_frames(out)
        if self.engine == "silent" or not text:
            write_wav(out, silence(max(0.4, spoken_length(text) / 4.6)))
            return out, wav_frames(out)
        with tempfile.TemporaryDirectory() as td:
            raw = Path(td) / "raw"
            self._raw(text, raw)
            if not raw.exists() or raw.stat().st_size == 0:
                raise ToolError("配音引擎没有输出音频")
            normalize_audio(self.runner, self.ffmpeg, raw, out)
        return out, wav_frames(out)

    def _raw(self, text, raw):
        rate = float(self.cfg.get("rate") or 1.0)
        voice = (self.cfg.get("voice") or "").strip()
        tmp_text = Path(str(raw) + ".txt")
        tmp_text.write_text(text, encoding="utf-8")
        if self.engine == "say":
            args = ["say", "-f", tmp_text, "-o", str(raw) + ".aiff"]
            if voice:
                args[1:1] = ["-v", voice]
            if abs(rate - 1.0) > 0.01:
                args[1:1] = ["-r", str(int(185 * rate))]
            self.runner.run(args, timeout=120)
            os.replace(str(raw) + ".aiff", raw)
        elif self.engine == "sapi":
            r = max(-10, min(10, int(round((rate - 1.0) * 10))))
            args = ["-TextFile", tmp_text, "-OutFile", str(raw) + ".wav", "-Rate", str(r)]
            if voice:
                args += ["-Voice", voice]
            _powershell(self.runner, SAPI_SCRIPT, args)
            os.replace(str(raw) + ".wav", raw)
        elif self.engine == "espeak":
            exe = shutil.which("espeak-ng") or shutil.which("espeak")
            self.runner.run([exe, "-v", voice or "cmn", "-s", str(int(170 * rate)), "-f", tmp_text, "-w", raw], timeout=120)
        elif self.engine == "api":
            self._api(text, raw, rate)
        elif self.engine == "edge":
            self._edge(text, raw, rate)
        else:
            raise ToolError(f"未知配音引擎：{self.engine}")

    def _api(self, text, raw, rate):
        if not self.api_key:
            raise ToolError("云端 TTS 需要在设置里填写 TTS API Key")
        url = self.cfg.get("api_base_url", "").rstrip("/") + "/audio/speech"
        body = {
            "model": self.cfg.get("api_model"),
            "input": text,
            "voice": self.cfg.get("api_voice"),
            "response_format": "wav",
            "speed": rate,
        }
        req = urllib.request.Request(
            url,
            data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {self.api_key}"},
            method="POST",
        )
        last = None
        for attempt in range(3):
            self.runner.check()
            try:
                with urlopen(req, 120) as resp:
                    data = resp.read()
                ctype = resp.headers.get("Content-Type", "")
                if "json" in ctype:
                    raise ToolError("TTS 接口返回了错误：" + data.decode("utf-8", "replace")[:300])
                raw.write_bytes(data)
                return
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "replace")[:300]
                last = ToolError(f"TTS 接口 HTTP {e.code}：{msg}")
                if e.code not in (429, 500, 502, 503, 504):
                    raise last
            except urllib.error.URLError as e:
                last = ToolError(f"TTS 接口连接失败：{e.reason}")
            time.sleep(2 * (attempt + 1))
        raise last

    def _edge(self, text, raw, rate):
        try:
            import edge_tts
        except Exception as e:
            raise ToolError("未安装 edge-tts：pip install edge-tts") from e
        pct = int(round((rate - 1.0) * 100))
        voice = self.cfg.get("edge_voice") or "zh-CN-YunxiNeural"

        async def go():
            await edge_tts.Communicate(text, voice, rate=f"{pct:+d}%").save(str(raw))

        asyncio.run(go())


__all__ = ["TTS", "available_engines", "resolve_engine", "list_voices", "ENGINE_NAMES", "SAMPLE_RATE"]
