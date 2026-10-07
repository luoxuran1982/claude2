"""本地素材库与配图打分（报告“补充五”的做法）。

1. 只有 category=concrete 的 image 镜头才配图，其余用文字卡；
2. 用英文画面描述 + 中文标题去和图片标签比对，给每张候选图打分；
3. 最高分低于及格线就不用图，退回文字卡（宁可没图，不要错图）；
4. 同一条视频、最近 N 个项目里用过的图不再用；
5. 审片时确认过的好图会被加分，库越用越准。

可选：Pexels 免费素材接口（需要 Key，授权清晰），把搜到的图连同英文描述存进本地库再打分。
"""

import hashlib
import json
import re
import shutil
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from PIL import Image

from . import paths
from .media import urlopen
from .storage import read_json, write_json
from .textutil import bigrams

EXTS = {".jpg", ".jpeg", ".png", ".webp"}
STOP = set(
    "a an the of and or with in on at to for from by is are be as into over under near close up closeup photo image picture "
    "shot view background showing shows".split()
)
_lock = threading.Lock()


def _index_path():
    return paths.library_dir() / "library.json"


def _usage_path():
    return paths.library_dir() / "usage.json"


def load_index():
    with _lock:
        idx = read_json(_index_path(), {"images": []}) or {"images": []}
    return idx


def save_index(idx):
    with _lock:
        write_json(_index_path(), idx)


def images_dir():
    p = paths.library_dir() / "images"
    p.mkdir(parents=True, exist_ok=True)
    return p


def image_path(entry):
    return images_dir() / entry["file"]


def get(image_id):
    for e in load_index()["images"]:
        if e["id"] == image_id:
            return e
    return None


def _clean_tags(tags):
    if isinstance(tags, str):
        tags = re.split(r"[,，;；\n]+", tags)
    out = []
    for t in tags or []:
        t = str(t).strip()
        if t and t not in out:
            out.append(t[:40])
    return out[:40]


def add_image(data: bytes, filename, tags=None, source="local", license_note="", alt=""):
    ext = Path(filename).suffix.lower()
    if ext not in EXTS:
        raise ValueError("只支持 jpg / png / webp 图片")
    digest = hashlib.sha256(data).hexdigest()[:16]
    idx = load_index()
    for e in idx["images"]:
        if e["id"] == digest:
            e["tags"] = _clean_tags(e.get("tags", []) + _clean_tags(tags))
            save_index(idx)
            return e
    fname = digest + ext
    (images_dir() / fname).write_bytes(data)
    try:
        with Image.open(images_dir() / fname) as im:
            w, h = im.size
    except OSError:
        (images_dir() / fname).unlink(missing_ok=True)
        raise ValueError("图片无法读取")
    stem_words = re.sub(r"[_\-.]+", " ", Path(filename).stem)
    entry = {
        "id": digest,
        "file": fname,
        "name": Path(filename).name[:80],
        "tags": _clean_tags(tags) or _clean_tags([stem_words]),
        "alt": str(alt or "")[:300],
        "source": source,
        "license": license_note or ("自有/已授权" if source == "local" else ""),
        "width": w,
        "height": h,
        "added": time.strftime("%Y-%m-%d %H:%M:%S"),
        "approved": 0,
    }
    idx["images"].append(entry)
    save_index(idx)
    return entry


def update_tags(image_id, tags=None, license_note=None):
    idx = load_index()
    for e in idx["images"]:
        if e["id"] == image_id:
            if tags is not None:
                e["tags"] = _clean_tags(tags)
            if license_note is not None:
                e["license"] = str(license_note)[:80]
            save_index(idx)
            return e
    raise KeyError(image_id)


def remove(image_id):
    idx = load_index()
    keep = []
    for e in idx["images"]:
        if e["id"] == image_id:
            (images_dir() / e["file"]).unlink(missing_ok=True)
        else:
            keep.append(e)
    idx["images"] = keep
    save_index(idx)


def mark_approved(image_id):
    idx = load_index()
    for e in idx["images"]:
        if e["id"] == image_id:
            e["approved"] = int(e.get("approved", 0)) + 1
    save_index(idx)


# ---------------------------------------------------------------- 打分

def _en_words(text):
    return [w for w in re.findall(r"[a-z][a-z0-9-]+", (text or "").lower()) if w not in STOP and len(w) > 1]


def _stem(w):
    for suf in ("ies", "es", "s", "ing", "ed"):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            return w[: -len(suf)] + ("y" if suf == "ies" else "")
    return w


def score(scene, entry):
    """0–1。英文描述按词匹配（看图片标签和 alt），中文按二元组匹配标题/说明。"""
    query_en = {_stem(w) for w in _en_words(scene.get("image_query"))}
    text_zh = (scene.get("heading") or "") + " " + ((scene.get("data") or {}).get("caption") or "")
    tag_text = " ".join(entry.get("tags") or []) + " " + (entry.get("alt") or "") + " " + (entry.get("name") or "")
    tag_en = {_stem(w) for w in _en_words(tag_text)}
    parts = []
    if query_en:
        parts.append((len(query_en & tag_en) / max(1, min(len(query_en), 6)), 0.65))
    zh_q = {b for b in bigrams(text_zh) if not b.isascii()}
    zh_t = {b for b in bigrams(tag_text) if not b.isascii()}
    if zh_q and zh_t:
        parts.append((len(zh_q & zh_t) / len(zh_q), 0.35))
    elif zh_q:
        parts.append((0.0, 0.35))
    if not parts:
        return 0.0
    total_w = sum(w for _, w in parts)
    base = sum(v * w for v, w in parts) / total_w
    bonus = min(0.1, 0.03 * int(entry.get("approved", 0)))
    return round(min(1.0, base + bonus), 3)


def recent_usage(exclude_project, limit_projects):
    usage = read_json(_usage_path(), {"projects": []}) or {"projects": []}
    used = set()
    for rec in usage["projects"][-limit_projects:]:
        if rec.get("project") != exclude_project:
            used.update(rec.get("images", []))
    return used


def record_usage(project_id, image_ids):
    usage = read_json(_usage_path(), {"projects": []}) or {"projects": []}
    usage["projects"] = [r for r in usage["projects"] if r.get("project") != project_id]
    usage["projects"].append({"project": project_id, "images": sorted(set(image_ids)), "time": time.time()})
    usage["projects"] = usage["projects"][-200:]
    write_json(_usage_path(), usage)


def candidates(scene, project_id, settings, taken=(), limit=10):
    idx = load_index()
    blocked = set(scene.get("image", {}).get("rejected") or []) | set(taken)
    blocked |= recent_usage(project_id, int(settings.get("no_repeat_projects", 30)))
    out = []
    for e in idx["images"]:
        if e["id"] in blocked or not image_path(e).exists():
            continue
        out.append({"id": e["id"], "file": e["file"], "score": score(scene, e), "name": e.get("name"), "tags": e.get("tags"), "source": e.get("source"), "license": e.get("license")})
    out.sort(key=lambda x: x["score"], reverse=True)
    return out[:limit]


def assign(project, settings, pexels_key="", log=None):
    """给项目里所有 concrete 的 image 镜头选图。已锁定（人工选过）的不动。返回统计。"""
    min_score = float(settings.get("min_score", 0.34))
    taken = set()
    stats = {"assigned": 0, "fallback": 0, "locked": 0, "downloaded": 0}
    for sc in project["scenes"]:
        img = sc.setdefault("image", {})
        if sc["type"] != "image":
            continue
        if img.get("locked") and img.get("id"):
            taken.add(img["id"])
            stats["locked"] += 1
            continue
        cands = candidates(sc, project["id"], settings, taken)
        if (not cands or cands[0]["score"] < min_score) and settings.get("pexels_enabled") and pexels_key and sc.get("image_query"):
            try:
                n = pexels_fetch(sc["image_query"], pexels_key, int(settings.get("pexels_per_query", 8)))
                stats["downloaded"] += n
                if log:
                    log(f"Pexels 下载 {n} 张：{sc['image_query']}")
                cands = candidates(sc, project["id"], settings, taken)
            except Exception as e:  # 网络失败不影响整体流程
                if log:
                    log(f"Pexels 检索失败：{e}")
        if cands and cands[0]["score"] >= min_score:
            img["id"], img["score"] = cands[0]["id"], cands[0]["score"]
            taken.add(img["id"])
            stats["assigned"] += 1
        else:
            img["id"], img["score"] = "", cands[0]["score"] if cands else 0.0
            stats["fallback"] += 1
    return stats


def project_image_ids(project):
    return [s["image"]["id"] for s in project["scenes"] if s["type"] == "image" and s.get("image", {}).get("id")]


# ---------------------------------------------------------------- Pexels（可选）

def pexels_fetch(query, key, per_page=8):
    url = "https://api.pexels.com/v1/search?" + urllib.parse.urlencode({"query": query, "per_page": per_page, "orientation": "landscape"})
    req = urllib.request.Request(url, headers={"Authorization": key, "User-Agent": "AIVideoStudio/0.1"})
    with urlopen(req, 30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    n = 0
    for ph in data.get("photos", []):
        src = (ph.get("src") or {}).get("large2x") or (ph.get("src") or {}).get("large")
        if not src:
            continue
        with urlopen(urllib.request.Request(src, headers={"User-Agent": "AIVideoStudio/0.1"}), 60) as r:
            blob = r.read()
        alt = ph.get("alt") or ""
        add_image(
            blob,
            f"pexels-{ph.get('id')}.jpg",
            tags=[query] + ([alt] if alt else []),
            source="pexels",
            license_note=f"Pexels License · {ph.get('photographer', '')}",
            alt=alt,
        )
        n += 1
    return n


def import_folder(folder):
    """批量导入文件夹，文件名当作初始标签（建议用英文描述命名，如 server-room-racks.jpg）。"""
    n = 0
    for p in sorted(Path(folder).rglob("*")):
        if p.suffix.lower() in EXTS and p.is_file():
            sidecar = p.with_suffix(".txt")
            tags = sidecar.read_text(encoding="utf-8").strip() if sidecar.exists() else None
            add_image(p.read_bytes(), p.name, tags=tags)
            n += 1
    return n


def copy_to(entry, dst):
    shutil.copy2(image_path(entry), dst)
