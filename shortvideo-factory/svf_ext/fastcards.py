"""知识讲解卡片的快速通道：画面、配音、背景音乐、字幕，用 FFmpeg 一次合成成片，只编码一次。

1.0.2 为什么慢（实测 46 秒竖屏 1080×1920：235 秒，最终渲染一步就 161 秒）：
讲解页先编码成 svf-cards.mp4（第 1 次编码），再交回上游 MoviePy 的通用流程——「处理片段」把它裁切缩放后重新编码（第 2 次），
「拼接」用 FFmpeg 再处理一遍（第 3 次），「最终渲染」由 MoviePy 在 Python 里逐帧解码、把每条字幕画成图层叠上去、再编码（第 4 次）。
这套流程是为「几十段尺寸各异的库存素材」设计的，对一条本来就和配音对齐、尺寸正确的讲解视频是多余的。

快速通道（参考「AI 知识视频工坊」studio 的做法）：
- 讲解页逐帧画好（每页缓慢缩放/平移，换页淡入，要点逐条亮起），字幕直接用 Pillow 画进画面——字体用你在界面上选的字幕字体
  （默认随包的思源黑体，Windows 上不依赖系统字体，也不需要 FFmpeg 的 libass/fontconfig），颜色、描边、位置照你的字幕设置；
- 原始帧经管道交给 FFmpeg，同一条命令里读入配音和背景音乐（循环铺满、按音量混音、结尾 3 秒淡出），一次编码出 final-1.mp4；
- 能用硬件编码器（h264_nvenc、h264_qsv、h264_amf、h264_videotoolbox）时自动选用，先用 0.2 秒的小样试编码，失败就回退 libx264。

画面时长 = 配音时长 + 0.6 秒，配音结束后不会黑屏；字幕时间来自 subtitle.srt（上游按配音的逐词时间生成），所以字幕和配音同步。
库存视频来源（Pexels、Pixabay 等）不走这里，流程和上游完全一样。
"""
from __future__ import annotations

import bisect
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont

from . import cards
from .render import CREATE_NO_WINDOW, FPS, _ffmpeg, page_frame_ranges

PLAN_FILE = "svf-cards.plan.json"
FAST_KEY = "svf_card_fast"            # config.toml [app]：false 时退回 1.0.2 的流程（交给上游合成）
MOTION_KEY = "svf_card_motion"        # [app]：false 时讲解页不缩放平移（更快）
HW_KEY = "svf_hw_encoder"             # [app]：auto（默认）/ off
HEARTBEAT = 10.0

HW_CANDIDATES = {
    "win32": ("h264_nvenc", "h264_qsv", "h264_amf"),
    "darwin": ("h264_videotoolbox",),
    "linux": ("h264_nvenc", "h264_qsv"),
}
CODEC_ARGS = {
    "libx264": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"],
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", "21", "-b:v", "0", "-pix_fmt", "yuv420p"],
    "h264_qsv": ["-c:v", "h264_qsv", "-global_quality", "22", "-pix_fmt", "nv12"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "balanced", "-rc", "cqp", "-qp_i", "20", "-qp_p", "22", "-pix_fmt", "nv12"],
    "h264_videotoolbox": ["-c:v", "h264_videotoolbox", "-b:v", "8M", "-pix_fmt", "yuv420p"],
}
_PROBED: dict = {}
_PROBE_LOCK = threading.Lock()


# ---------------------------------------------------------------- 编码器

def _platform_key(platform: str | None = None) -> str:
    p = platform or sys.platform
    return "win32" if p.startswith("win") else ("darwin" if p == "darwin" else "linux")


def encoder_listed(ffmpeg: str) -> set[str]:
    try:
        r = subprocess.run([ffmpeg, "-hide_banner", "-encoders"], capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=20, creationflags=CREATE_NO_WINDOW)
        return {ln.split()[1] for ln in r.stdout.splitlines() if len(ln.split()) > 1 and ln.split()[1].startswith(("h264", "libx264"))}
    except (OSError, subprocess.TimeoutExpired):
        return set()


def probe_encoder(ffmpeg: str, codec: str) -> bool:
    """真的用这个编码器编 0.2 秒小样：编译进了 FFmpeg 不等于这台电脑的显卡和驱动能用。"""
    key = (ffmpeg, codec)
    with _PROBE_LOCK:
        if key in _PROBED:
            return _PROBED[key]
    with tempfile.TemporaryDirectory(prefix="svf-enc-") as d:
        out = os.path.join(d, "probe.mp4")
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=320x568:r=30:d=0.2", *CODEC_ARGS[codec], out]
        try:
            ok = subprocess.run(cmd, capture_output=True, timeout=30, creationflags=CREATE_NO_WINDOW).returncode == 0 \
                and os.path.getsize(out) > 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
    with _PROBE_LOCK:
        _PROBED[key] = ok
    return ok


def choose_encoders(ffmpeg: str, mode: str = "auto", preferred: str = "", platform: str | None = None,
                    listed: set[str] | None = None, probe=None) -> list[str]:
    """按优先顺序返回要尝试的编码器，最后一个总是 libx264。"""
    order = []
    if mode != "off":
        listed = encoder_listed(ffmpeg) if listed is None else listed
        probe = probe or (lambda c: probe_encoder(ffmpeg, c))
        cands = ([preferred] if preferred in CODEC_ARGS and preferred != "libx264" else []) + \
            [c for c in HW_CANDIDATES[_platform_key(platform)] if c != preferred]
        for c in cands:
            if c in listed and probe(c):
                order.append(c)
                break
    return order + ["libx264"]


# ---------------------------------------------------------------- 字幕（直接画进画面）

def _color(value, default):
    try:
        return ImageColor.getrgb(str(value)) if value else default
    except ValueError:
        return default


def _resolve_font(name: str) -> str:
    if name:
        try:
            from app.utils import file_security, utils
            return file_security.resolve_path_within_directory(utils.font_dir(), name)
        except Exception:  # noqa: BLE001
            pass
    return cards.font_path()


def _wrap(text: str, fnt, max_w: int) -> list[str]:
    lines = []
    for para in str(text).split("\n"):
        cur = ""
        for tok in cards.tokens(para):
            if cur and fnt.getlength(cur + tok) > max_w:
                lines.append(cur.rstrip())
                cur = tok.lstrip()
            else:
                cur += tok
        if cur.strip():
            lines.append(cur.rstrip())
    return lines or [""]


class SubtitleStyle:
    def __init__(self, params, W: int, H: int):
        self.W, self.H = W, H
        self.enabled = bool(getattr(params, "subtitle_enabled", True))
        self.font_path = _resolve_font(str(getattr(params, "font_name", "") or ""))
        self.size = max(8, int(getattr(params, "font_size", 60) or 60))
        self.fore = _color(getattr(params, "text_fore_color", "#FFFFFF"), (255, 255, 255))
        self.stroke = _color(getattr(params, "stroke_color", "#000000"), (0, 0, 0))
        self.stroke_width = max(0, int(getattr(params, "stroke_width", 1.5) or 0))
        bg = getattr(params, "text_background_color", True)
        self.bg = ((0, 0, 0) if bg else None) if isinstance(bg, bool) else _color(bg, None)
        self.rounded = bool(getattr(params, "rounded_subtitle_background", False)) and self.bg is not None
        self.position = str(getattr(params, "subtitle_position", "bottom") or "bottom")
        try:
            self.custom = float(getattr(params, "custom_position", 70.0))
        except (TypeError, ValueError):
            self.custom = 70.0
        try:
            self.font = ImageFont.truetype(self.font_path, self.size)
        except OSError:
            self.font_path = cards.font_path()
            self.font = ImageFont.truetype(self.font_path, self.size)

    def render(self, text: str) -> tuple[Image.Image, int, int]:
        """一条字幕画成 RGBA 小图，返回（图, x, y）。排版和上游 MoviePy 的字幕一致：最宽 90% 画面、居中、按位置放。"""
        f = self.font
        pad_x = int(self.size * (0.4 if self.rounded else 0.6)) if self.bg else 0
        max_w = int(self.W * 0.9) - 2 * pad_x
        lines = _wrap(text, f, max(1, max_w))
        ascent, descent = f.getmetrics()
        lh = ascent + descent
        gap = int(self.size * 0.25)
        sw = self.stroke_width
        text_w = max(int(f.getlength(ln)) for ln in lines) + 2 * sw
        text_h = lh * len(lines) + gap * (len(lines) - 1) + 2 * sw
        box_w = min(int(self.W * 0.9), text_w + 2 * pad_x) if self.rounded else (int(self.W * 0.9) if self.bg else text_w)
        pad_y = int(self.size * 0.3) if self.bg else 0
        box_h = text_h + 2 * pad_y
        img = Image.new("RGBA", (max(1, box_w), max(1, box_h)), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if self.bg:
            alpha = 140 if self.rounded else 255
            d.rounded_rectangle((0, 0, box_w - 1, box_h - 1), radius=max(8, int(self.size * 0.4)) if self.rounded else 0,
                                fill=self.bg + (alpha,))
        y = pad_y + sw
        for ln in lines:
            d.text((box_w / 2, y), ln, font=f, fill=self.fore + (255,), anchor="ma",
                   stroke_width=sw, stroke_fill=self.stroke + (255,))
            y += lh + gap
        x = (self.W - box_w) // 2
        if self.position == "top":
            yy = int(self.H * 0.05)
        elif self.position == "center":
            yy = (self.H - box_h) // 2
        elif self.position in ("two_thirds_bottom", "two_thirds", "2/3_bottom"):
            yy = int((self.H - box_h) / 3.0)
        elif self.position == "custom":
            yy = int(min(max(10, (self.H - box_h) * self.custom / 100), self.H - box_h - 10))
        else:
            yy = int(self.H * 0.95 - box_h)
        return img, x, yy


class Subtitles:
    """按时间查当前该显示哪条字幕；每条字幕只画一次。"""

    def __init__(self, cues, style: SubtitleStyle | None):
        self.cues = sorted([c for c in (cues or []) if c[1] > c[0] and str(c[2]).strip()], key=lambda c: c[0])
        self.starts = [c[0] for c in self.cues]
        self.style = style
        self.cache: dict[int, tuple] = {}

    def at(self, t: float):
        if not self.style or not self.style.enabled or not self.cues:
            return None
        i = bisect.bisect_right(self.starts, t) - 1
        if i < 0 or t >= self.cues[i][1]:
            return None
        if i not in self.cache:
            self.cache[i] = self.style.render(self.cues[i][2])
        return self.cache[i]


def burn(frame: Image.Image, sub) -> Image.Image:
    if not sub:
        return frame
    img, x, y = sub
    frame = frame.copy()
    frame.paste(img, (x, y), img)
    return frame


# ---------------------------------------------------------------- 音频

def audio_args(audio_file: str, bgm_file: str, voice_volume: float, bgm_volume: float, total: float,
               loop_bgm: bool = True) -> tuple[list[str], list[str]]:
    """返回（额外的输入参数, 滤镜和映射参数）。视频是第 0 路输入（管道），配音第 1 路，背景音乐第 2 路。"""
    inputs = ["-i", audio_file]
    vv = 1.0 if voice_volume is None else float(voice_volume)
    if bgm_file:
        inputs += (["-stream_loop", "-1"] if loop_bgm else []) + ["-i", bgm_file]
        fade_start = max(0.0, total - 3.0)
        flt = (f"[1:a]volume={vv:.3f}[v];[2:a]volume={float(bgm_volume):.3f},atrim=0:{total:.3f},"
               f"afade=t=out:st={fade_start:.3f}:d=3[m];[v][m]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0,"
               f"apad[a]")
    else:
        flt = f"[1:a]volume={vv:.3f},apad[a]"
    return inputs, ["-filter_complex", flt, "-map", "0:v", "-map", "[a]"]


def resolve_bgm(params, task_id: str) -> str:
    """和上游 video.generate_video 同样的规则：音量为 0 不用；随机或指定文件。返回空串表示不用背景音乐。"""
    try:
        from app.services import bgm as bgm_service
        from app.services import video
        if not bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
            return ""
        return video.get_bgm_file(bgm_type=params.bgm_type, bgm_file=params.bgm_file) or ""
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------- 合成

def encode(frames, total_frames: int, out_path: str, W: int, H: int, fps: int, audio_file: str, bgm_file: str,
           voice_volume: float, bgm_volume: float, codec: str, ffmpeg: str, progress=None, check=None, log=None) -> None:
    total = total_frames / fps
    a_in, a_map = audio_args(audio_file, bgm_file, voice_volume, bgm_volume, total)
    cmd = [ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps), "-i", "-",
           *a_in, *a_map, *CODEC_ARGS[codec], "-r", str(fps), "-c:a", "aac", "-b:a", "192k", "-ar", "44100",
           "-shortest", "-movflags", "+faststart", out_path]
    err = tempfile.TemporaryFile()
    enc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, creationflags=CREATE_NO_WINDOW)
    t0 = last_beat = time.monotonic()
    done = 0
    try:
        for fr in frames:
            enc.stdin.write(fr.tobytes())
            done += 1
            if done % 15 == 0:
                if check:
                    check()
                if progress:
                    progress(done / total_frames)
                now = time.monotonic()
                if log and now - last_beat >= HEARTBEAT:
                    last_beat = now
                    log(f"知识卡片快速通道 still running: elapsed={now - t0:.0f}s（已编码 {done}/{total_frames} 帧）")
        enc.stdin.close()
        code = enc.wait()
        if code != 0:
            err.seek(0)
            raise RuntimeError(f"FFmpeg 合成失败（{codec}，返回码 {code}）：{err.read().decode('utf-8', 'replace')[-800:]}")
    except BrokenPipeError as exc:
        enc.wait()
        err.seek(0)
        raise RuntimeError(f"FFmpeg 合成中途退出（{codec}）：{err.read().decode('utf-8', 'replace')[-800:]}") from exc
    except BaseException:
        try:
            enc.kill()
        except OSError:
            pass
        enc.wait()
        raise
    finally:
        err.close()


def render_final(pages, cues, params, audio_file: str, out_path: str, W: int, H: int, theme: str, keywords, safe: str,
                 bgm_file: str = "", fps: int = FPS, motion: bool = True, hw_mode: str = "auto", preferred: str = "",
                 progress=None, check=None, log=None, ffmpeg: str | None = None, still_dir: str | None = None) -> dict:
    """一次合成成片。返回 {"codec", "seconds", "frames", "fallback"}。"""
    from .render import iter_frames
    ffmpeg = ffmpeg or _ffmpeg()
    total_frames = page_frame_ranges(pages, fps)[-1][1]
    style = SubtitleStyle(params, W, H)
    subs = Subtitles(cues, style)
    voice_volume = getattr(params, "voice_volume", 1.0)
    bgm_volume = float(getattr(params, "bgm_volume", 0.2) or 0.0)
    order = choose_encoders(ffmpeg, hw_mode, preferred)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".final-", suffix=".mp4", dir=str(Path(out_path).parent))
    os.close(fd)
    errors = []
    t0 = time.monotonic()
    try:
        for codec in order:
            if log:
                log(f"知识卡片快速通道：编码器 {codec}" + ("（硬件编码）" if codec != "libx264" else "（软件编码）"))

            def frames(stills=still_dir if not errors else None):
                for i, fr in enumerate(iter_frames(pages, W, H, theme, keywords, safe, fps, stills, motion=motion, ffmpeg=ffmpeg)):
                    yield burn(fr, subs.at(i / fps))
            try:
                encode(frames(), total_frames, tmp, W, H, fps, audio_file, bgm_file, voice_volume, bgm_volume, codec, ffmpeg,
                       progress, check, log)
                os.replace(tmp, out_path)
                tmp = ""
                return {"codec": codec, "seconds": round(time.monotonic() - t0, 1), "frames": total_frames,
                        "fallback": errors}
            except RuntimeError as exc:
                if codec == "libx264":
                    raise
                errors.append(f"{codec}: {exc}")
                if log:
                    log(f"知识卡片快速通道：硬件编码器 {codec} 失败，改用 libx264 重来（{str(exc)[:200]}）")
        raise RuntimeError("没有可用的视频编码器")
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def save_plan(task_dir: Path, pages, keywords, info: dict) -> Path:
    path = Path(task_dir) / PLAN_FILE
    path.write_text(json.dumps({"keywords": keywords, "pages": [p.to_dict() for p in pages], **info}, ensure_ascii=False),
                    encoding="utf-8")
    return path


def copy_extra(final: str, count: int) -> list[str]:
    """要求一次出多条时：讲解页内容是固定的，多条完全一样，只复制文件。"""
    out = [final]
    for i in range(2, count + 1):
        dst = str(Path(final).with_name(f"final-{i}.mp4"))
        shutil.copyfile(final, dst)
        out.append(dst)
    return out
