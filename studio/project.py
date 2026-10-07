"""项目数据：字段校验与规整、保存、内容哈希。

模型返回的分镜和页面保存的分镜走同一个 normalize_scene，任何来源的数据都按同一规则落盘。
"""

import copy
import hashlib
import json
import re
import shutil
import time
import uuid

from . import paths
from .storage import read_json, write_json

SCENE_TYPES = {
    "title": "标题卡",
    "bullets": "要点列表",
    "keyword": "关键词卡",
    "stat": "关键数字",
    "code": "代码",
    "flow": "流程图",
    "chart": "图表",
    "compare": "对比",
    "table": "表格",
    "quote": "引语/观点",
    "image": "配图",
}
CATEGORIES = {
    "concrete": "具体事物（可配图）",
    "abstract": "抽象概念",
    "data": "数据",
    "entity": "人名/机构/产品",
}
THEMES = {"dark": "深色科技", "paper": "浅色纸张", "classroom": "蓝白课堂"}


def _s(v, limit=None):
    if v is None:
        return ""
    if not isinstance(v, str):
        v = str(v)
    v = v.strip()
    return v[:limit] if limit else v


def _list(v, limit, item_limit=60):
    if isinstance(v, str):
        v = [x for x in re.split(r"[\n；;]", v) if x.strip()]
    if not isinstance(v, list):
        return []
    return [_s(x, item_limit) for x in v if _s(x)][:limit]


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v or "").replace(",", ""))
    return float(m.group()) if m else None


def normalize_data(kind, data):
    d = data if isinstance(data, dict) else {}
    if kind == "title":
        return {"subtitle": _s(d.get("subtitle"), 40)}
    if kind == "bullets":
        return {"items": _list(d.get("items"), 6, 40)}
    if kind == "keyword":
        return {"keyword": _s(d.get("keyword"), 20), "note": _s(d.get("note"), 60)}
    if kind == "stat":
        return {"value": _s(d.get("value"), 12), "label": _s(d.get("label"), 40)}
    if kind == "code":
        code = d.get("code") or ""
        if isinstance(code, list):
            code = "\n".join(str(x) for x in code)
        lines = str(code).replace("\t", "    ").splitlines()[:18]
        hl = []
        for x in d.get("highlight") or []:
            n = _num(x)
            if n is not None and 1 <= n <= len(lines):
                hl.append(int(n))
        return {"language": _s(d.get("language"), 20) or "text", "code": "\n".join(l.rstrip() for l in lines), "highlight": hl}
    if kind == "flow":
        return {"nodes": _list(d.get("nodes"), 6, 20)}
    if kind == "chart":
        labels = _list(d.get("labels"), 8, 16)
        raw = d.get("values") if isinstance(d.get("values"), list) else []
        values = [_num(x) for x in raw][: len(labels)]
        pairs = [(l, v) for l, v in zip(labels, values) if v is not None]
        chart = _s(d.get("chart")) if _s(d.get("chart")) in ("bar", "line") else "bar"
        return {
            "chart": chart,
            "labels": [p[0] for p in pairs],
            "values": [p[1] for p in pairs],
            "unit": _s(d.get("unit"), 12),
            "source": _s(d.get("source"), 40),
        }
    if kind == "compare":
        return {
            "left_title": _s(d.get("left_title"), 16),
            "left": _list(d.get("left"), 5, 30),
            "right_title": _s(d.get("right_title"), 16),
            "right": _list(d.get("right"), 5, 30),
        }
    if kind == "table":
        headers = _list(d.get("headers"), 4, 16)
        rows = []
        for r in (d.get("rows") or [])[:6]:
            if isinstance(r, list):
                rows.append([_s(x, 24) for x in r][: max(1, len(headers) or 4)])
        return {"headers": headers, "rows": rows}
    if kind == "quote":
        return {"text": _s(d.get("text"), 80), "source": _s(d.get("source"), 30)}
    if kind == "image":
        return {"caption": _s(d.get("caption"), 30)}
    return {}


def new_scene_id():
    return "s" + uuid.uuid4().hex[:8]


def normalize_scene(sc, used_ids=None):
    sc = sc if isinstance(sc, dict) else {}
    kind = _s(sc.get("type")).lower()
    if kind not in SCENE_TYPES:
        kind = "keyword"
    cat = _s(sc.get("category")).lower()
    if cat not in CATEGORIES:
        cat = "concrete" if kind == "image" else ("data" if kind in ("stat", "chart", "table") else "abstract")
    # 只有具体事物才配图：防止“按关键词硬配图”配错
    if kind == "image" and cat != "concrete":
        kind = "keyword"
    data = normalize_data(kind, sc.get("data"))
    heading = _s(sc.get("heading"), 30)
    narration = _s(sc.get("narration"), 600)
    # 缺数据时退回关键词卡，保证每个分镜都能画
    if kind == "bullets" and not data["items"]:
        kind, data = "keyword", {"keyword": heading or "要点", "note": ""}
    if kind == "flow" and len(data["nodes"]) < 2:
        kind, data = "keyword", {"keyword": heading or "流程", "note": ""}
    if kind == "chart" and len(data["values"]) < 2:
        kind, data = "keyword", {"keyword": heading or "数据", "note": ""}
    if kind == "keyword" and not data.get("keyword"):
        data = {"keyword": heading or narration[:8] or "……", "note": data.get("note", "")}
    if kind == "code" and not data["code"].strip():
        kind, data = "keyword", {"keyword": heading or "代码", "note": ""}
    img = sc.get("image") if isinstance(sc.get("image"), dict) else {}
    sid = _s(sc.get("id"), 20)
    if not re.fullmatch(r"[A-Za-z0-9_-]{2,20}", sid or "") or (used_ids is not None and sid in used_ids):
        sid = new_scene_id()
    if used_ids is not None:
        used_ids.add(sid)
    return {
        "id": sid,
        "type": kind,
        "category": cat,
        "heading": heading,
        "narration": narration,
        "data": data,
        "image_query": _s(sc.get("image_query"), 160) if cat == "concrete" else "",
        "image": {
            "id": _s(img.get("id"), 40),
            "score": float(img.get("score") or 0) if _num(img.get("score")) is not None else 0.0,
            "locked": bool(img.get("locked")),
            "rejected": _list(img.get("rejected"), 50, 40),
        },
        "short": bool(sc.get("short")),
    }


def normalize_scenes(scenes):
    used = set()
    return [normalize_scene(s, used) for s in (scenes or [])[:120] if isinstance(s, dict)]


def default_brief():
    return {
        "topic": "",
        "audience": "",
        "minutes": 3,
        "views": "",
        "materials": "",
        "series": "",
        "style": "",
    }


def normalize_brief(b):
    b = b if isinstance(b, dict) else {}
    out = default_brief()
    for k in ("topic", "audience", "series"):
        out[k] = _s(b.get(k), 200)
    out["views"] = _s(b.get("views"), 3000)
    out["materials"] = _s(b.get("materials"), 60000)
    out["style"] = _s(b.get("style"), 1000)
    m = _num(b.get("minutes"))
    out["minutes"] = max(0.5, min(30.0, m if m is not None else 3))
    return out


def normalize_project(p):
    p = p if isinstance(p, dict) else {}
    claims = []
    for c in (p.get("claims") or [])[:80]:
        if isinstance(c, dict) and _s(c.get("text")):
            conf = _s(c.get("confidence")).lower()
            claims.append(
                {
                    "text": _s(c.get("text"), 200),
                    "confidence": conf if conf in ("high", "medium", "low") else "medium",
                    "note": _s(c.get("note"), 200),
                    "checked": bool(c.get("checked")),
                }
            )
    ct = _list(p.get("cover_titles"), 8, 30)
    choice = int(_num(p.get("cover_choice")) or 0)
    out = {
        "id": _s(p.get("id"), 64),
        "schema": 1,
        "created": _s(p.get("created")),
        "updated": _s(p.get("updated")),
        "brief": normalize_brief(p.get("brief")),
        "theme": p.get("theme") if p.get("theme") in THEMES else "dark",
        "title": _s(p.get("title"), 60),
        "description": _s(p.get("description"), 1000),
        "tags": _list(p.get("tags"), 12, 20),
        "cover_titles": ct,
        "cover_choice": max(0, min(choice, max(0, len(ct) - 1))),
        "scenes": normalize_scenes(p.get("scenes")),
        "claims": claims,
        "ai_declared": bool(p.get("ai_declared")),
        "music_file": _s(p.get("music_file"), 300),
        "usage": [u for u in (p.get("usage") or []) if isinstance(u, dict)][-200:],
        "exports": [e for e in (p.get("exports") or []) if isinstance(e, dict)][-50:],
    }
    return out


def content_hash(p):
    """只算影响成片的字段。"""
    keep = {
        "theme": p.get("theme"),
        "title": p.get("title"),
        "scenes": p.get("scenes", []),
        "music": p.get("music_file"),
    }
    raw = json.dumps(keep, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:16]


def now_iso():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def project_dir(pid):
    if not re.fullmatch(r"[A-Za-z0-9_-]{4,64}", pid or ""):
        raise ValueError("项目编号不合法")
    return paths.projects_dir() / pid


def create(brief):
    pid = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    p = normalize_project({"id": pid, "brief": brief, "created": now_iso()})
    p["title"] = p["brief"]["topic"][:60]
    save(p)
    return p


def load(pid):
    data = read_json(project_dir(pid) / "project.json")
    if data is None:
        raise FileNotFoundError(pid)
    p = normalize_project(data)
    p["id"] = pid
    return p


def save(p):
    p = normalize_project(p)
    if not p["id"]:
        raise ValueError("缺少项目编号")
    p["updated"] = now_iso()
    write_json(project_dir(p["id"]) / "project.json", p)
    return p


def update(pid, fn):
    p = load(pid)
    fn(p)
    return save(p)


def list_projects():
    out = []
    for d in sorted(paths.projects_dir().iterdir(), reverse=True):
        if not d.is_dir() or d.name.startswith("."):
            continue
        data = read_json(d / "project.json")
        if not data:
            continue
        scenes = data.get("scenes") or []
        chars = sum(len(s.get("narration") or "") for s in scenes if isinstance(s, dict))
        out.append(
            {
                "id": d.name,
                "title": data.get("title") or (data.get("brief") or {}).get("topic") or d.name,
                "updated": data.get("updated", ""),
                "scenes": len(scenes),
                "chars": chars,
                "exports": len(data.get("exports") or []),
                "theme": data.get("theme", "dark"),
            }
        )
    out.sort(key=lambda x: x["updated"], reverse=True)
    return out


def trash(pid):
    src = project_dir(pid)
    if src.exists():
        dst = paths.sub("trash") / f"{pid}-{int(time.time())}"
        shutil.move(str(src), str(dst))


def duplicate(pid):
    p = load(pid)
    q = copy.deepcopy(p)
    q["id"] = time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]
    q["created"] = now_iso()
    q["exports"] = []
    q["usage"] = []
    q["title"] = (p["title"] + "（副本）")[:60]
    return save(q)
