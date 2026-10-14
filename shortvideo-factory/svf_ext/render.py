"""把讲解页做成一段视频：每页缓慢缩放或平移（Ken Burns），换页时 0.3 秒淡入，要点按配音逐条亮起；
「混合」来源里分到库存片段的页，直接放那段素材（裁成同样画幅）。整条时间线编码成一个 mp4 交给上游。

画法：每页先按画面尺寸的 1.08 倍画一张大图，逐帧用 Pillow 按小数坐标裁一块再缩到成片尺寸——
小数坐标让缩放/平移没有一像素一像素的抖动。逐帧计算放在线程池里（Pillow 缩放时释放 GIL），
原始 RGB 帧经管道交给 FFmpeg（随包的 imageio-ffmpeg）编码成 H.264。
"""
from __future__ import annotations

import math
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PIL import Image

from . import cards
from .storyboard import Page

FPS = 30
OVERSCAN = 1.08          # 大图比成片大多少（给缩放留清晰度）
ZOOM = 1.06              # 缩放类动作的幅度：画面放大 6%
PAN_ZOOM = 1.045         # 平移时固定放大 4.5%，在多出来的边上平移
FADE_SECONDS = 0.3
MOTIONS = ("zoom_in", "pan_right", "zoom_out", "pan_up", "pan_left", "zoom_in", "pan_down", "zoom_out")
CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


def motion_for(index: int) -> str:
    return MOTIONS[index % len(MOTIONS)]


def motion_box(kind: str, u: float, cw: float, ch: float) -> tuple[float, float, float, float]:
    """动作 kind 在进度 u（0–1）时，从大图（cw×ch）里裁哪一块。返回小数坐标 (x0, y0, x1, y1)，比例与大图相同。"""
    u = min(max(u, 0.0), 1.0)
    if kind in ("zoom_in", "zoom_out"):
        z = 1 + (ZOOM - 1) * (u if kind == "zoom_in" else 1 - u)
        w, h = cw / z, ch / z
        x0, y0 = (cw - w) / 2, (ch - h) / 2
        return (x0, y0, x0 + w, y0 + h)
    z = PAN_ZOOM
    w, h = cw / z, ch / z
    sx, sy = cw - w, ch - h
    x0, y0 = sx / 2, sy / 2
    if kind == "pan_right":
        x0 = sx * u
    elif kind == "pan_left":
        x0 = sx * (1 - u)
    elif kind == "pan_down":
        y0 = sy * u
    elif kind == "pan_up":
        y0 = sy * (1 - u)
    return (x0, y0, x0 + w, y0 + h)


def page_frame_ranges(pages: list[Page], fps: int = FPS) -> list[tuple[int, int]]:
    """每页占用的帧区间 [a, b)，首尾相接，总帧数 = ceil(总时长 × fps)。"""
    total = int(math.ceil(pages[-1].end * fps)) if pages else 0
    out = []
    prev = 0
    for k, p in enumerate(pages):
        b = total if k == len(pages) - 1 else max(prev + 1, int(round(p.end * fps)))
        out.append((prev, b))
        prev = b
    return out


def _ffmpeg() -> str:
    exe = os.environ.get("IMAGEIO_FFMPEG_EXE")
    if exe:
        return exe
    try:
        from app.utils import utils
        return utils.get_ffmpeg_binary()
    except Exception:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()


class _Stock:
    """把一段库存视频解码成成片尺寸的 RGB 帧（铺满后居中裁切）。"""

    def __init__(self, ffmpeg: str, path: str, W: int, H: int, fps: int, seconds: float):
        self.size = W * H * 3
        self.W, self.H = W, H
        vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={fps},format=rgb24"
        self.proc = subprocess.Popen([ffmpeg, "-v", "error", "-nostdin", "-i", path, "-t", f"{seconds + 0.5:.3f}", "-an",
                                      "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        self.last: bytes | None = None

    def next(self) -> Image.Image:
        buf = b""
        while len(buf) < self.size and self.proc.stdout:
            chunk = self.proc.stdout.read(self.size - len(buf))
            if not chunk:
                break
            buf += chunk
        if len(buf) == self.size:
            self.last = buf
        elif self.last is None:
            self.last = b"\x00" * self.size
        return Image.frombytes("RGB", (self.W, self.H), self.last)

    def close(self):
        try:
            self.proc.kill()
        except OSError:
            pass
        self.proc.wait()


def render_video(pages: list[Page], out_path: str, W: int, H: int, theme: str = "classroom", keywords=(),
                 safe: str = "bottom", fps: int = FPS, progress=None, still_dir: str | None = None,
                 workers: int | None = None) -> str:
    """把讲解页（和库存片段）编码成一个 mp4，返回路径。progress(0–1) 可选。still_dir 给出时把每页的完整画面另存为 PNG。"""
    if not pages:
        raise ValueError("没有可画的讲解页")
    ffmpeg = _ffmpeg()
    W, H = int(W), int(H)
    cw, ch = int(round(W * OVERSCAN)), int(round(H * OVERSCAN))
    ranges = page_frame_ranges(pages, fps)
    total_frames = ranges[-1][1]
    out_path = str(out_path)
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cards-", suffix=".mp4", dir=str(Path(out_path).parent))
    os.close(fd)
    err = tempfile.TemporaryFile()
    enc = subprocess.Popen([ffmpeg, "-y", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(fps),
                            "-i", "-", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                            "-r", str(fps), "-movflags", "+faststart", tmp],
                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err, creationflags=CREATE_NO_WINDOW)
    card_pages = [p for p in pages if p.kind != "stock"]
    numbering = {id(p): k for k, p in enumerate(card_pages)}
    workers = workers or max(2, min(8, (os.cpu_count() or 2)))
    done = 0
    prev_last: Image.Image | None = None
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="svf-cards") as pool:
            for k, (p, (a, b)) in enumerate(zip(pages, ranges)):
                nf = b - a
                if nf <= 0:
                    continue
                fade_n = min(int(FADE_SECONDS * fps), nf // 3) if prev_last is not None else 0
                if p.kind == "stock":
                    stock = _Stock(ffmpeg, p.data["path"], W, H, fps, nf / fps)
                    try:
                        frames = [stock.next() for _ in range(nf)]
                    finally:
                        stock.close()
                    for j in range(fade_n):
                        frames[j] = Image.blend(prev_last, frames[j], (j + 1) / (fade_n + 1))
                    for fr in frames:
                        enc.stdin.write(fr.tobytes())
                    prev_last = frames[-1]
                    done += nf
                    if progress:
                        progress(done / total_frames)
                    continue
                idx = numbering[id(p)]
                kind = motion_for(idx)
                times = [(a + j) / fps for j in range(nf)]
                states = sorted({p.visible_at(t) for t in times} | {p.visible_at(p.end)})
                canvases = {v: cards.render_page(p, cw, ch, theme, idx, len(card_pages), v if p.reveals else 99, keywords, safe)
                            for v in states}
                if still_dir:
                    Path(still_dir).mkdir(parents=True, exist_ok=True)
                    canvases[states[-1]].resize((W, H), Image.LANCZOS).save(Path(still_dir) / f"page-{idx + 1:02d}.png")

                def frame(j, p=p, kind=kind, nf=nf, canvases=canvases, times=times):
                    img = canvases[p.visible_at(times[j])] if p.reveals else canvases[states[-1]]
                    box = motion_box(kind, j / max(1, nf - 1), cw, ch)
                    return img.resize((W, H), Image.BILINEAR, box=box)

                batch = max(8, workers * 4)
                for j0 in range(0, nf, batch):
                    frames = list(pool.map(frame, range(j0, min(nf, j0 + batch))))
                    for off, fr in enumerate(frames):
                        j = j0 + off
                        if j < fade_n:
                            fr = Image.blend(prev_last, fr, (j + 1) / (fade_n + 1))
                        enc.stdin.write(fr.tobytes())
                    prev_last = frames[-1]
                    done += len(frames)
                    if progress:
                        progress(done / total_frames)
        enc.stdin.close()
        code = enc.wait()
        if code != 0:
            err.seek(0)
            raise RuntimeError(f"FFmpeg 编码讲解视频失败（{code}）：{err.read().decode('utf-8', 'replace')[-800:]}")
        os.replace(tmp, out_path)
        tmp = ""
        return out_path
    except BrokenPipeError as exc:   # FFmpeg 中途退出：把它的报错带出来
        enc.wait()
        err.seek(0)
        raise RuntimeError(f"FFmpeg 编码讲解视频中途退出：{err.read().decode('utf-8', 'replace')[-800:]}") from exc
    except BaseException:
        try:
            enc.kill()
        except OSError:
            pass
        enc.wait()
        raise
    finally:
        err.close()
        if tmp and os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
