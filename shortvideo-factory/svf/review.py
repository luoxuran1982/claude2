"""外壳的「审片 / 分镜」和「模型调用」两个顶层标签页的数据：只读任务目录里的文件，不碰生成流程。

任务目录（数据目录/storage/tasks/<任务 ID>/）里由生成界面后台（svf_ext）写下的文件：
- svf_storyboard.json + knowledge-cards/page-NN.png、thumb-NN.jpg：分镜清单和每页画面（svf_ext/shots.py）；
- llm_calls.jsonl：每次大模型调用一行，外加 {"kind": "use"} 行记「程序怎么用了这次的输出」（svf_ext/llmlog.py）；
- script.json（上游）：文案、关键词、参数；svf_task.json（svf_ext）：目标时长、字数控制等；final-1.mp4：成片。

这里额外算两样东西给「模型调用」页：
- 提示词着色：记录里的 parts 把提示词分成「固定模板 / 程序填进去的变量」；
- 调用链：前面某次调用的输出（按句切开后逐句查找）出现在后面某次调用的输入里，标出位置和来自第几次调用。
纯标准库，不需要 Pillow（外壳主进程是 PyInstaller 打的包，只带标准库和 tornado）。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

TASK_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,80}$")
STEP_LABELS = {"script": "写文案", "terms": "提取关键词", "revise": "按字数改写文案", "director": "AI 导演设计分镜",
               "social": "生成发布标题和话题", "test": "测试连接", "other": "其他"}
MIN_PIECE = 6          # 输出切出来的一句话至少几个字才拿去找（太短容易误配）
MERGE_GAP = re.compile(r"^[\s\[\]\d。！？!?，,；;：:、.…\"“”'’「」()（）]{0,10}$")


def tasks_root(data: Path) -> Path:
    return Path(data) / "storage" / "tasks"


def task_dir(data: Path, task_id: str) -> Path | None:
    if not TASK_ID_RE.match(task_id or ""):
        return None
    d = tasks_root(data) / task_id
    return d if d.is_dir() else None


def _read_json(path: Path) -> dict:
    try:
        v = json.loads(path.read_text(encoding="utf-8"))
        return v if isinstance(v, dict) else {}
    except (OSError, ValueError):
        return {}


def _read_jsonl(path: Path) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        v = json.loads(ln)
                    except ValueError:
                        continue
                    if isinstance(v, dict):
                        out.append(v)
    except OSError:
        pass
    return out


def list_tasks(data: Path, limit: int = 200) -> list[dict]:
    root = tasks_root(data)
    out = []
    try:
        dirs = [d for d in root.iterdir() if d.is_dir() and TASK_ID_RE.match(d.name)]
    except OSError:
        return []
    dirs.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    for d in dirs[:limit]:
        script = _read_json(d / "script.json")
        params = script.get("params") if isinstance(script.get("params"), dict) else {}
        info = _read_json(d / "svf_task.json")
        manifest = d / "svf_storyboard.json"
        calls = [r for r in _read_jsonl(d / "llm_calls.jsonl") if r.get("kind") != "use"]
        out.append({
            "id": d.name, "mtime": time.strftime("%Y-%m-%d %H:%M", time.localtime(d.stat().st_mtime)),
            "subject": str(params.get("video_subject") or "")[:60], "source": str(params.get("video_source") or info.get("source") or ""),
            "video": "final-1.mp4" if (d / "final-1.mp4").is_file() else "", "storyboard": manifest.is_file(), "calls": len(calls),
        })
    return out


# ---------------------------------------------------------------- 审片 / 分镜

def storyboard(data: Path, task_id: str) -> dict | None:
    d = task_dir(data, task_id)
    if d is None:
        return None
    m = _read_json(d / "svf_storyboard.json")
    script = _read_json(d / "script.json")
    params = script.get("params") if isinstance(script.get("params"), dict) else {}
    return {"task_id": task_id, "subject": params.get("video_subject", ""), "source": params.get("video_source", ""),
            "video": "final-1.mp4" if (d / "final-1.mp4").is_file() else "", "manifest": m or None,
            "script": script.get("script", ""), "info": _read_json(d / "svf_task.json")}


# ---------------------------------------------------------------- 模型调用：调用链与着色

def pieces(text: str) -> list[str]:
    """把一次调用的输出按句切开（找它在后面调用的输入里出现的位置）。"""
    out = []
    for p in re.split(r"(?<=[。！？!?；;\n])", text or ""):
        p = p.strip()
        if len(re.sub(r"\W", "", p)) >= MIN_PIECE:
            out.append(p)
    return out


def find_links(prompt: str, earlier: list[tuple[int, str]]) -> list[tuple[int, int, int]]:
    """earlier：[(调用序号, 输出)]。返回 [(起, 止, 来自第几次调用)]，后面的调用优先（最近一次的输出最可能是直接来源）。"""
    spans: list[tuple[int, int, int]] = []
    for idx, out in sorted(earlier, key=lambda x: -x[0]):
        for piece in pieces(out):
            pos = prompt.find(piece)
            while pos >= 0:
                a, b = pos, pos + len(piece)
                if not any(a < sb and sa < b for sa, sb, _ in spans):
                    spans.append((a, b, idx))
                    break
                pos = prompt.find(piece, pos + 1)
    spans.sort()
    merged: list[list[int]] = []
    for a, b, idx in spans:   # 同一来源、中间只隔着标点或「[3] 」这类编号的相邻片段合成一段
        if merged and merged[-1][2] == idx and a >= merged[-1][1] and MERGE_GAP.match(prompt[merged[-1][1]:a]):
            merged[-1][1] = b
        else:
            merged.append([a, b, idx])
    return [tuple(x) for x in merged]


def runs(content: str, parts: list | None, links: list[tuple[int, int, int]]) -> list[dict]:
    """把一条 message 切成连续的小段：{t 文字, k tpl/var/prog/raw, n 变量名, from 来自第几次调用的输出或 None}。"""
    segs = []
    if parts and "".join(str(p.get("t", "")) for p in parts) == content:
        pos = 0
        for p in parts:
            t = str(p.get("t", ""))
            segs.append((pos, pos + len(t), p.get("k") or "tpl", p.get("n") or ""))
            pos += len(t)
    else:
        segs.append((0, len(content), "raw", ""))
    cuts = sorted({0, len(content)} | {x for a, b, _ in links for x in (a, b)} | {x for a, b, _, _ in segs for x in (a, b)})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if a >= b:
            continue
        k, n = next(((k, n) for sa, sb, k, n in segs if sa <= a < sb), ("raw", ""))
        src = next((idx for la, lb, idx in links if la <= a < lb), None)
        if out and out[-1]["k"] == k and out[-1]["n"] == n and out[-1]["from"] == src:
            out[-1]["t"] += content[a:b]
        else:
            out.append({"t": content[a:b], "k": k, "n": n, "from": src})
    return out


def merge_uses(rows: list[dict]) -> list[dict]:
    calls = [dict(r) for r in rows if r.get("kind") != "use"]
    by_id = {c.get("id"): c for c in calls if c.get("id")}
    for r in rows:
        if r.get("kind") == "use" and r.get("ref") in by_id:
            by_id[r["ref"]].setdefault("use_notes", []).append(r.get("note", ""))
    return calls


def default_use(c: dict) -> str:
    if not c.get("ok"):
        return f"这次调用失败了（{c.get('error') or '模型没有返回内容'}），程序没有用它的输出" + \
            ("，上游会再试一次。" if c.get("step") in ("script", "terms") else "。")
    if c.get("step") == "test":
        return "只用来检查连接是否正常，输出不用于视频。"
    return "（这条记录没有写「程序怎么用」：1.1.0 以前的记录，或这一步不由外壳记录用途。）"


def annotate(rows: list[dict]) -> list[dict]:
    """按时间排好序的调用记录 → 给界面用的数据（编号、着色片段、调用链、用途）。"""
    calls = merge_uses(rows)
    calls.sort(key=lambda c: float(c.get("ts") or 0))
    out = []
    for k, c in enumerate(calls, 1):
        earlier = [(j, str(p.get("output") or "")) for j, p in enumerate(calls[:k - 1], 1) if p.get("ok")]
        msgs = []
        sources: dict[int, int] = {}
        for i, m in enumerate(c.get("messages") or []):
            content = str(m.get("content") or "")
            links = find_links(content, earlier) if m.get("role") != "system" else []
            for a, b, idx in links:
                sources[idx] = sources.get(idx, 0) + (b - a)
            parts = c.get("parts") if i == len(c.get("messages") or []) - 1 else None
            msgs.append({"role": m.get("role", ""), "content": content, "runs": runs(content, parts, links)})
        raw = {key: v for key, v in c.items() if key not in ("use_notes",)}
        out.append({
            "index": k, "id": c.get("id", ""), "step": c.get("step", ""), "step_label": STEP_LABELS.get(c.get("step"), c.get("step_label", "")),
            "time": c.get("time", ""), "attempt": c.get("attempt", 1), "ok": bool(c.get("ok")), "error": c.get("error", ""),
            "from_ui": bool(c.get("from_ui")), "provider": c.get("provider_label") or c.get("provider") or "", "model": c.get("model", ""),
            "base_url": c.get("base_url", ""), "elapsed": c.get("elapsed_seconds"), "usage": c.get("usage") or {},
            "messages": msgs, "reasoning": c.get("reasoning", ""), "output": c.get("output", ""),
            "uses": c.get("use_notes") or [default_use(c)], "inputs_from": [{"call": j, "chars": n} for j, n in sorted(sources.items())],
            "raw": raw,
        })
    for c in out:   # 反过来：这次的输出进了后面哪几次调用
        c["outputs_to"] = [{"call": o["index"], "chars": f["chars"]} for o in out for f in o["inputs_from"] if f["call"] == c["index"]]
    return out


def llm_calls(data: Path, task_id: str) -> dict | None:
    d = task_dir(data, task_id)
    if d is None:
        return None
    script = _read_json(d / "script.json")
    params = script.get("params") if isinstance(script.get("params"), dict) else {}
    return {"task_id": task_id, "subject": params.get("video_subject", ""), "info": _read_json(d / "svf_task.json"),
            "calls": annotate(_read_jsonl(d / "llm_calls.jsonl"))}


def task_file(data: Path, task_id: str, rel: str) -> Path | None:
    """任务目录里的一个文件（只允许图片、视频、json），防止跳出任务目录。"""
    d = task_dir(data, task_id)
    if d is None or not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts:
        return None
    p = (d / rel)
    if p.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".mp4", ".json"):
        return None
    try:
        if os.path.commonpath([os.path.realpath(p), os.path.realpath(d)]) != os.path.realpath(d):
            return None
    except ValueError:
        return None
    return p if p.is_file() else None
