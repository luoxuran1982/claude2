"""每页画面存成 PNG、写分镜清单 svf_storyboard.json，供外壳的「审片 / 分镜」视图读取（只读，不影响一键生成）。

卡片类来源（规则切分、AI 导演、卡片 + AI 插图、混合）：在素材这一步、讲解页定下来以后，把每页「全部要点都亮起」的完整画面
画一张 PNG（knowledge-cards/page-01.png …）和一张小缩略图（thumb-01.jpg），混合来源里放库存片段的页取那段素材的一帧。

库存视频来源（Pexels、Pixabay 等）：成片完成后，按上游记在 script.json 里的素材清单（material_sources：文件名、时长、
搜索关键词），从每段素材取一帧做缩略图；每段在成片里的起止时间是按「片段最长时长」累加估算的（上游拼接时还会裁切、
随机打乱顺序时更不准），界面上标「约」。

清单格式见 write_manifest。外壳（svf/review.py）只读这个文件和图片，不需要 Pillow。
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

MANIFEST = "svf_storyboard.json"
SHOTS_DIR = "knowledge-cards"
THUMB_WIDTH = 360


def _kind_label(kind: str) -> str:
    from .studio_cards import TYPE_LABELS
    return TYPE_LABELS.get(kind, kind)


def _rel(task_dir: Path, path) -> str:
    if not path:
        return ""
    try:
        return Path(path).resolve().relative_to(Path(task_dir).resolve()).as_posix()
    except ValueError:
        return ""


def _save(img, png: Path, thumb: Path) -> None:
    img.save(png, compress_level=3)
    w = THUMB_WIDTH
    h = max(1, round(img.height * w / img.width))
    img.convert("RGB").resize((w, h)).save(thumb, quality=82)


def _video_frame(ffmpeg: str, src: str, at: float, W: int, H: int):
    """从视频里取一帧（铺满 W×H 后居中裁切），失败返回 None。"""
    from PIL import Image
    from .render import CREATE_NO_WINDOW
    vf = f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},format=rgb24"
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-nostdin", "-ss", f"{max(0.0, at):.2f}", "-i", src, "-frames:v", "1",
                            "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, timeout=60,
                           creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or len(r.stdout) < W * H * 3:
        return None
    return Image.frombytes("RGB", (W, H), r.stdout[: W * H * 3])


def save_stills(pages, task_dir, W: int, H: int, theme: str, keywords=(), safe: str = "bottom", ffmpeg: str | None = None) -> list[dict]:
    """每页一张完整画面 PNG + 缩略图 JPG。返回 [{"image", "thumb"}]（相对任务目录的路径）。"""
    from . import cards
    out_dir = Path(task_dir) / SHOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    card_pages = [p for p in pages if p.kind != "stock"]
    numbering = {id(p): k for k, p in enumerate(card_pages)}
    res = []
    for k, p in enumerate(pages, 1):
        png, thumb = out_dir / f"page-{k:02d}.png", out_dir / f"thumb-{k:02d}.jpg"
        img = None
        if p.kind == "stock":
            from .render import _ffmpeg
            img = _video_frame(ffmpeg or _ffmpeg(), p.data.get("path", ""), min(1.0, p.duration / 2), W, H)
        else:
            img = cards.render_page(p, W, H, theme, numbering[id(p)], len(card_pages), 99, keywords, safe)
        if img is None:
            res.append({"image": "", "thumb": ""})
            continue
        _save(img, png, thumb)
        res.append({"image": f"{SHOTS_DIR}/{png.name}", "thumb": f"{SHOTS_DIR}/{thumb.name}"})
    return res


def page_entry(k: int, p, task_dir, still: dict, sentences=None) -> dict:
    data = dict(p.data or {})
    illus = _rel(task_dir, data.pop("image_path", ""))
    if p.kind == "stock":
        data = {"clip": Path(str(data.get("path", ""))).name, "card_kind": data.get("card_kind", "")}
    text = p.text or ""
    from .duration import count_units
    return {"index": k, "kind": p.kind, "kind_label": _kind_label(p.kind), "heading": p.heading,
            "subtitle": (p.data or {}).get("subtitle", "") if p.kind == "title" else "",
            "start": round(p.start, 2), "end": round(p.end, 2), "duration": round(p.duration, 2), "text": text,
            "chars": count_units(text, "zh"), "sentences": sentences, "data": data, "reveals": [round(r, 2) for r in p.reveals],
            "image": still.get("image", ""), "thumb": still.get("thumb", ""), "illustration": illus, "approx": False}


def write_manifest(task_dir, info: dict, entries: list[dict]) -> Path:
    """info：source、source_label、mode（rule / ai / stock）、theme、W、H、director、illustration、keywords、note 等。"""
    path = Path(task_dir) / MANIFEST
    data = {"version": 1, "created": time.strftime("%Y-%m-%d %H:%M:%S"), **info, "pages": entries}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)
    return path


def read_manifest(task_dir) -> dict:
    try:
        return json.loads((Path(task_dir) / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ---------------------------------------------------------------- 库存视频来源

def _find_clip(name: str, task_dir: Path) -> str:
    cands = [task_dir / name]
    try:
        from app.config import config
        from app.utils import utils
        md = str(config.app.get("material_directory", "") or "").strip()
        if md and md != "task":
            cands.append(Path(md) / name)
        cands.append(Path(utils.storage_dir("cache_videos")) / name)
    except Exception:  # noqa: BLE001
        pass
    for c in cands:
        if c.is_file():
            return str(c)
    return ""


def stock_storyboard(task_id: str, params, W: int, H: int, audio_seconds: float, ffmpeg: str | None = None) -> Path | None:
    """库存视频来源：每段素材取一帧做缩略图，配上它的搜索关键词和估算的起止时间。"""
    from PIL import Image  # noqa: F401  （确认 Pillow 可用）
    from app.utils import utils
    from .render import _ffmpeg
    task_dir = Path(utils.task_dir(task_id))
    try:
        script = json.loads((task_dir / "script.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sources = [s for s in script.get("material_sources") or [] if isinstance(s, dict) and s.get("local_file")]
    if not sources:
        return None
    ffmpeg = ffmpeg or _ffmpeg()
    clip_max = float(getattr(params, "video_clip_duration", 0) or 0) or 5.0
    out_dir = task_dir / SHOTS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    entries, t = [], 0.0
    total = float(audio_seconds or 0)
    for k, s in enumerate(sources, 1):
        if total and t >= total:
            break
        dur = min(float(s.get("duration") or clip_max), clip_max)
        src = _find_clip(str(s["local_file"]), task_dir)
        still = {"image": "", "thumb": ""}
        if src:
            img = _video_frame(ffmpeg, src, min(1.0, dur / 2), W, H)
            if img is not None:
                png, thumb = out_dir / f"page-{k:02d}.png", out_dir / f"thumb-{k:02d}.jpg"
                _save(img, png, thumb)
                still = {"image": f"{SHOTS_DIR}/{png.name}", "thumb": f"{SHOTS_DIR}/{thumb.name}"}
        end = min(t + dur, total) if total else t + dur
        term = str(s.get("search_term") or "")
        entries.append({"index": k, "kind": "stock", "kind_label": f"{str(s.get('provider') or '库存').capitalize()} 素材",
                        "heading": term or s["local_file"], "subtitle": "", "start": round(t, 2), "end": round(end, 2),
                        "duration": round(end - t, 2), "text": "", "chars": 0, "sentences": None,
                        "data": {"search_term": term, "clip": s["local_file"], "provider": s.get("provider", ""),
                                 "source_page": s.get("source_page", ""), "clip_seconds": s.get("duration")},
                        "reveals": [], "image": still["image"], "thumb": still["thumb"], "illustration": "", "approx": True})
        t = end
    concat = str(getattr(params, "video_concat_mode", "") or "")
    note = ("每段素材的起止时间是按「片段最长时长」累加估算的，上游拼接时会裁切素材" +
            ("；拼接方式是「随机」，实际顺序和这里不同" if "random" in concat else "") + "。")
    return write_manifest(task_dir, {"task_id": task_id, "source": params.video_source, "source_label": params.video_source,
                                     "mode": "stock", "theme": "", "W": W, "H": H, "keywords": list(script.get("search_terms") or []),
                                     "note": note}, entries)


def install() -> None:
    """包住上游 task.generate_final_videos：库存视频来源出片后写分镜清单（卡片类来源在素材这一步已经写好）。"""
    from app.services import task as task_service
    fn = task_service.generate_final_videos
    if getattr(fn, "_svf_shots", False):
        return

    def generate_final_videos(task_id, params, *args, **kwargs):
        out = fn(task_id, params, *args, **kwargs)
        try:
            videos = out[0] if isinstance(out, tuple) else None
            from app.utils import utils
            task_dir = Path(utils.task_dir(task_id))
            if videos and not (task_dir / MANIFEST).is_file():
                from app.models.schema import VideoAspect
                from .duration import read_task
                W, H = VideoAspect(params.video_aspect or VideoAspect.portrait.value).to_resolution()
                stock_storyboard(task_id, params, W, H, float(read_task(task_dir).get("actual_seconds") or 0))
        except Exception as exc:  # noqa: BLE001
            try:
                from loguru import logger
                logger.warning(f"写分镜清单（库存素材缩略图）失败：{exc}")
            except ImportError:
                pass
        return out
    generate_final_videos._svf_shots = True
    generate_final_videos.__wrapped__ = fn
    task_service.generate_final_videos = generate_final_videos
