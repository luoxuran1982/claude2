"""生产流水线。

生成文案：默认一次模型调用拿到标题、分镜、封面标题、简介、事实清单；超过 6 分钟才分大纲+逐章。
渲染成片：逐句配音 → 采样数决定时间轴 → 逐句画帧 → FFmpeg 合成。这一步不调用模型。
"""

import copy
import math
import re
import shutil
import time
from pathlib import Path

from . import compliance, images, llm, prompts, settings as settings_mod, visuals
from . import project as proj
from .media import Runner, read_pcm, require_ffmpeg, silence, write_wav, SAMPLE_RATE, ToolError
from .textutil import CHARS_PER_MINUTE, apply_pronunciation, format_srt_time, split_chunks, subtitle_text
from .tts import TTS

SINGLE_CALL_MAX_MINUTES = 6


class Ctx:
    """任务上下文：进度、日志、取消。"""

    def __init__(self, cancel_event=None, progress=None, log=None):
        self.runner = Runner(cancel_event)
        self.cancel = self.runner.cancel_event
        self._progress = progress or (lambda p, m: None)
        self._log = log or (lambda m: None)

    def progress(self, pct, msg=""):
        self.runner.check()
        self._progress(max(0.0, min(1.0, pct)), msg)

    def log(self, msg):
        self._log(msg)


def _record_usage(p, usage, cfg, purpose):
    rec = dict(usage)
    rec["purpose"] = purpose
    rec["time"] = proj.now_iso()
    rec["cost"] = round(llm.cost_yuan(usage, cfg), 4)
    p.setdefault("usage", []).append(rec)


# ---------------------------------------------------------------- 文案

def generate(pid, ctx, use_cache=True):
    cfg = settings_mod.load()
    llm_cfg = cfg["llm"]
    key = settings_mod.get_secret("llm_key")
    p = proj.load(pid)
    brief = p["brief"]
    if not brief["topic"].strip():
        raise ToolError("请先填写主题")
    minutes = brief["minutes"]
    if minutes <= SINGLE_CALL_MAX_MINUTES:
        ctx.progress(0.05, "调用模型：一次生成文案和分镜")
        text, usage = llm.chat(prompts.single_call(brief), llm_cfg, key, json_mode=True, use_cache=use_cache, cancel=ctx.cancel)
        _record_usage(p, usage, llm_cfg, "文案+分镜")
        ctx.log(_usage_line(usage))
        result = llm.extract_json(text)
    else:
        n_ch = max(2, min(8, int(math.ceil(minutes / 3))))
        ctx.progress(0.05, f"调用模型：生成大纲（共 {n_ch} 章）")
        text, usage = llm.chat(prompts.outline_call(brief, n_ch), llm_cfg, key, json_mode=True, use_cache=use_cache, cancel=ctx.cancel)
        _record_usage(p, usage, llm_cfg, "大纲")
        ctx.log(_usage_line(usage))
        outline = llm.extract_json(text)
        chapters = [c for c in outline.get("chapters") or [] if isinstance(c, dict)]
        if not chapters:
            raise ToolError("模型没有返回章节大纲")
        outline["chapters"] = chapters
        result = {k: outline.get(k) for k in ("title", "cover_titles", "description", "tags")}
        result["scenes"], result["claims"] = [], []
        for i in range(len(chapters)):
            ctx.progress(0.1 + 0.85 * i / len(chapters), f"调用模型：第 {i + 1}/{len(chapters)} 章")
            text, usage = llm.chat(prompts.chapter_call(brief, outline, i), llm_cfg, key, json_mode=True, use_cache=use_cache, cancel=ctx.cancel)
            _record_usage(p, usage, llm_cfg, f"第{i + 1}章")
            ctx.log(_usage_line(usage))
            part = llm.extract_json(text)
            result["scenes"] += part.get("scenes") or []
            result["claims"] += part.get("claims") or []
    apply_generation(p, result)
    images.assign(p, cfg["images"], settings_mod.get_secret("pexels_key"), ctx.log)
    proj.save(p)
    ctx.progress(1.0, f"完成：{len(p['scenes'])} 个分镜")
    return {"scenes": len(p["scenes"])}


def apply_generation(p, result):
    if not isinstance(result, dict):
        raise ToolError("模型输出不是 JSON 对象")
    scenes = proj.normalize_scenes(result.get("scenes"))
    if not scenes:
        raise ToolError("模型没有返回分镜")
    p["scenes"] = scenes
    p["title"] = str(result.get("title") or p["title"])[:60]
    p["cover_titles"] = [str(x)[:30] for x in (result.get("cover_titles") or []) if str(x).strip()][:8]
    p["cover_choice"] = 0
    p["description"] = str(result.get("description") or "")[:1000]
    p["tags"] = [str(x)[:20] for x in (result.get("tags") or [])][:12]
    p["claims"] = [c for c in (result.get("claims") or []) if isinstance(c, dict)]
    # 没有选出精简版镜头时，取开头若干镜凑够约 75 秒
    if not any(s["short"] for s in p["scenes"]):
        budget = CHARS_PER_MINUTE * 1.25
        for s in p["scenes"]:
            if budget <= 0:
                break
            s["short"] = True
            budget -= len(s["narration"])


def _usage_line(u):
    if u.get("local_cache"):
        return "命中本地缓存，没有产生费用"
    return f"模型 {u.get('model')}：输入 {u['prompt_tokens']}（缓存命中 {u['cache_hit_tokens']}），输出 {u['completion_tokens']} token"


def rewrite_scene(pid, scene_id, instruction, ctx):
    cfg = settings_mod.load()
    p = proj.load(pid)
    idx = next((i for i, s in enumerate(p["scenes"]) if s["id"] == scene_id), None)
    if idx is None:
        raise ToolError("找不到这个分镜")
    sc = p["scenes"][idx]
    prev_t = p["scenes"][idx - 1]["narration"] if idx > 0 else ""
    next_t = p["scenes"][idx + 1]["narration"] if idx + 1 < len(p["scenes"]) else ""
    ctx.progress(0.1, "调用模型改写本镜")
    text, usage = llm.chat(
        prompts.rewrite_scene_call(p["brief"], p["title"], sc, prev_t, next_t, instruction),
        cfg["llm"], settings_mod.get_secret("llm_key"), json_mode=True, use_cache=False, max_tokens=1500, cancel=ctx.cancel,
    )
    new = llm.extract_json(text)
    if isinstance(new.get("scenes"), list) and new["scenes"]:
        new = new["scenes"][0]
    new["id"] = sc["id"]
    new["image"] = sc.get("image")
    p["scenes"][idx] = proj.normalize_scene(new)
    _record_usage(p, usage, cfg["llm"], "改写分镜")
    proj.save(p)
    ctx.progress(1.0, "已改写")
    return {"scene": p["scenes"][idx]}


def fact_check(pid, ctx):
    cfg = settings_mod.load()
    use_check = cfg["check_llm"]["enabled"] and cfg["check_llm"]["model"]
    llm_cfg = dict(cfg["llm"])
    key = settings_mod.get_secret("llm_key")
    if use_check:
        llm_cfg.update({"base_url": cfg["check_llm"]["base_url"] or llm_cfg["base_url"], "model": cfg["check_llm"]["model"]})
        key = settings_mod.get_secret("check_key") or key
    p = proj.load(pid)
    ctx.progress(0.1, f"调用 {llm_cfg['model']} 核查事实")
    text, usage = llm.chat(prompts.fact_check_call(p["title"], p["scenes"], p["brief"]["materials"]), llm_cfg, key, json_mode=True, cancel=ctx.cancel)
    data = llm.extract_json(text)
    checked = {c["text"] for c in p["claims"] if c.get("checked")}
    claims = [c for c in data.get("claims") or [] if isinstance(c, dict)]
    for c in claims:
        c["checked"] = c.get("text") in checked
    p["claims"] = claims
    _record_usage(p, usage, llm_cfg, "事实核查")
    proj.save(p)
    ctx.progress(1.0, f"列出 {len(claims)} 条事实")
    return {"claims": len(claims)}


# ---------------------------------------------------------------- 零 token：用户自己的稿子

def script_to_scenes(text):
    """把一段现成的稿子按段落切成分镜，不调用模型。

    规则：第一段作开场标题卡；``` 包起来的是代码；连续的“1. / - / •”开头行是要点列表；
    短段落里有带单位的数字用关键数字卡；其他段落用关键词卡（取第一个逗号前的短语作关键词）。
    """
    text = (text or "").replace("\r\n", "\n").strip()
    # 先把 ``` 代码块整体切出来，再按空行分段
    parts = re.split(r"```([^\n`]*)\n(.*?)```", text, flags=re.S)
    blocks = []
    for k in range(0, len(parts), 3):
        blocks += [("text", para.strip()) for para in re.split(r"\n\s*\n", parts[k]) if para.strip()]
        if k + 2 < len(parts):
            blocks.append(("code", parts[k + 1].strip(), parts[k + 2].rstrip()))
    scenes = []
    i = 0
    while i < len(blocks):
        blk = blocks[i]
        i += 1
        if blk[0] == "code":
            narration = ""
            if i < len(blocks) and blocks[i][0] == "text":
                narration = blocks[i][1]
                i += 1
            scenes.append({"type": "code", "heading": "代码示例", "narration": narration, "data": {"language": blk[1] or "text", "code": blk[2]}})
            continue
        b = blk[1]
        lines = [l.strip() for l in b.splitlines() if l.strip()]
        heading = ""
        if len(lines) > 1 and len(lines[0]) <= 18 and not re.search(r"[。！？]$", lines[0]):
            heading = lines[0].lstrip("#").strip()
            lines = lines[1:]
        items = [re.sub(r"^(\d+[.、)]|[-•*])\s*", "", l) for l in lines if re.match(r"^(\d+[.、)]|[-•*])\s*", l)]
        if len(items) >= 2 and len(items) == len(lines):
            scenes.append({"type": "bullets", "heading": heading or "要点", "narration": "；".join(items) + "。", "data": {"items": [x[:16] for x in items]}})
            continue
        body = " ".join(lines)
        if not scenes:
            scenes.append({"type": "title", "heading": heading or body[:20], "narration": body, "data": {"subtitle": ""}})
            continue
        num = re.search(r"\d+(?:\.\d+)?\s*(?:%|万|亿|倍|元|美元|小时|年)", body)
        kw = re.split(r"[，,。：:！？；]", body)[0][:10]
        if num and len(body) < 120:
            scenes.append({"type": "stat", "category": "data", "heading": heading or kw, "narration": body, "data": {"value": num.group(0).replace(" ", ""), "label": kw}})
        else:
            scenes.append({"type": "keyword", "heading": heading or kw, "narration": body, "data": {"keyword": heading or kw, "note": ""}})
    return proj.normalize_scenes(scenes)


# ---------------------------------------------------------------- 渲染

def scene_context(p, i, total, orientation, cfg, image_cache=None):
    sc = p["scenes"][i]
    img_path = ""
    iid = (sc.get("image") or {}).get("id")
    if sc["type"] == "image" and iid:
        e = images.get(iid) if image_cache is None else image_cache.get(iid)
        if e:
            img_path = str(images.image_path(e))
    return {
        "theme": p["theme"],
        "title": p["title"],
        "series": p["brief"].get("series", ""),
        "index": i,
        "total": total,
        "image_path": img_path,
        "watermark": cfg["render"]["ai_watermark"],
        "burn_subtitles": cfg["render"]["burn_subtitles"],
    }


def preview(p, scene_id, orientation="landscape"):
    cfg = settings_mod.load()
    idx = next((i for i, s in enumerate(p["scenes"]) if s["id"] == scene_id), None)
    if idx is None:
        raise ToolError("找不到分镜")
    sc = p["scenes"][idx]
    chunks = [c for c, _ in split_chunks(sc["narration"], 22 if orientation == "portrait" else 26)] or [""]
    vis, focus = visuals.reveal_plan(sc, chunks)[-1]
    return visuals.render_frame(sc, scene_context(p, idx, len(p["scenes"]), orientation, cfg), vis, focus, subtitle_text(chunks[0]), orientation)


def render(pid, ctx, orientation="landscape", mode="full"):
    cfg = settings_mod.load()
    rc = cfg["render"]
    ffmpeg = require_ffmpeg(rc.get("ffmpeg_path"))
    p = proj.load(pid)
    snapshot = copy.deepcopy(p)
    scenes = [s for s in p["scenes"] if mode == "full" or s["short"]]
    if not scenes:
        raise ToolError("没有可渲染的分镜" + ("（精简版需要在分镜里勾选“竖版精简”）" if mode == "short" else ""))
    chash = proj.content_hash(p)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    kind = f"{orientation}-{mode}"
    out_dir = proj.project_dir(pid) / "exports" / f"{stamp}-{kind}"
    work = out_dir / "_work"
    work.mkdir(parents=True, exist_ok=True)
    tts_key = settings_mod.get_secret("tts_key")
    tts = TTS(cfg["tts"], tts_key, ffmpeg, ctx.runner)
    ctx.log(f"配音引擎：{tts.engine}；输出：{out_dir}")
    pron = cfg["pronunciation"]
    max_len = 22 if orientation == "portrait" else 26

    # 1) 逐句配音
    units = []  # (scene_index_in_list, chunk_index, text, is_end, wav, frames)
    plan = []
    for si, sc in enumerate(scenes):
        chunks = split_chunks(sc["narration"], max_len)
        plan.append(chunks)
        units += [(si, ci, c, end) for ci, (c, end) in enumerate(chunks)]
    total_units = max(1, len(units))
    audio = {}
    for n, (si, ci, text, end) in enumerate(units):
        ctx.progress(0.02 + 0.45 * n / total_units, f"配音 {n + 1}/{total_units}")
        wav, frames = tts.synthesize(apply_pronunciation(text, pron))
        audio[(si, ci)] = (wav, frames)

    # 2) 时间轴：采样数 + 间隔，字幕和画面共用
    pcm = bytearray(silence(rc["lead_in"]))
    t = rc["lead_in"]
    frames_plan = []  # (scene_idx, step, start, end, subtitle)
    srt = []
    for si, chunks in enumerate(plan):
        if not chunks:
            frames_plan.append((si, 0, t, t + 2.5, ""))
            pcm += silence(2.5)
            t += 2.5
        for ci, (text, end) in enumerate(chunks):
            wav, nframes = audio[(si, ci)]
            pcm += read_pcm(wav)
            dur = nframes / SAMPLE_RATE
            gap = rc["gap_sentence"] if end else rc["gap_clause"]
            if ci == len(chunks) - 1:
                gap = rc["gap_scene"]
            frames_plan.append((si, ci, t, t + dur + gap, subtitle_text(text)))
            srt.append((t, t + dur, subtitle_text(text)))
            pcm += silence(gap)
            t += dur + gap
    pcm += silence(rc["tail"])
    t += rc["tail"]
    frames_plan[-1] = frames_plan[-1][:3] + (t,) + frames_plan[-1][4:]
    narration = work / "narration.wav"
    write_wav(narration, bytes(pcm))
    total_dur = len(pcm) / 2 / SAMPLE_RATE

    # 3) 逐句画帧
    image_cache = {e["id"]: e for e in images.load_index()["images"]}
    reveal = [visuals.reveal_plan(sc, [c for c, _ in plan[si]] or [""]) for si, sc in enumerate(scenes)]
    fps = int(rc["fps"])
    concat = ["ffconcat version 1.0"]
    last_file = None
    prev_key = None
    acc_frames = 0
    for n, (si, ci, start, end, sub) in enumerate(frames_plan):
        ctx.progress(0.48 + 0.3 * n / len(frames_plan), f"绘制画面 {n + 1}/{len(frames_plan)}")
        vis, focus = reveal[si][min(ci, len(reveal[si]) - 1)]
        sctx = scene_context({**p, "scenes": scenes}, si, len(scenes), orientation, cfg, image_cache)
        key = (si, vis, focus, sub if rc["burn_subtitles"] else "")
        # 帧时长按帧数量化，累计误差不超过一帧
        end_frames = int(round(end * fps))
        nfr = max(1, end_frames - acc_frames)
        acc_frames += nfr
        if key == prev_key and last_file:
            # 画面没变就延长上一张
            concat[-1] = f"duration {float(concat[-1].split()[1]) + nfr / fps:.6f}"
            continue
        fname = f"f{n:05d}.jpg"
        visuals.render_frame(scenes[si], sctx, vis, focus, sub, orientation).save(work / fname, quality=92)
        concat += [f"file '{fname}'", f"duration {nfr / fps:.6f}"]
        last_file, prev_key = fname, key
    concat.append(f"file '{last_file}'")
    (work / "timeline.ffconcat").write_text("\n".join(concat) + "\n", encoding="utf-8")

    # 4) 合成
    base = _safe_name(p["title"] or "video") + ("-竖版" if orientation == "portrait" else "-横版") + ("-精简" if mode == "short" else "")
    mp4 = out_dir / f"{base}.mp4"
    partial = out_dir / f"{base}.partial.mp4"
    args = [ffmpeg, "-hide_banner", "-y", "-f", "concat", "-safe", "0", "-i", work / "timeline.ffconcat", "-i", narration]
    music = p.get("music_file") or rc.get("music_file")
    music_path = _music_path(music)
    if music_path:
        args += ["-stream_loop", "-1", "-i", music_path]
        vol = float(rc.get("music_volume", 0.08))
        args += ["-filter_complex", f"[2:a]volume={vol:.3f}[m];[1:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]", "-map", "0:v", "-map", "[a]"]
    else:
        args += ["-map", "0:v", "-map", "1:a"]
    args += [
        "-vf", f"fps={fps},format=yuv420p",
        "-c:v", "libx264", "-preset", rc.get("preset", "medium"), "-crf", str(rc.get("crf", 20)), "-tune", "stillimage",
        "-c:a", "aac", "-b:a", "160k", "-ar", "48000",
        "-t", f"{total_dur:.3f}", "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", partial,
    ]

    def on_line(line):
        if line.startswith("out_time_ms=") or line.startswith("out_time_us="):
            try:
                us = int(line.split("=")[1])
                ctx._progress(0.8 + 0.18 * min(1.0, us / 1e6 / total_dur), f"编码视频 {us / 1e6:.0f}/{total_dur:.0f} 秒")
            except ValueError:
                pass

    ctx.progress(0.8, "编码视频")
    ctx.runner.run(args, on_line=on_line)
    partial.replace(mp4)

    # 5) 附件：字幕、口播稿、封面、发布包、项目快照
    srt_path = out_dir / f"{base}.srt"
    srt_path.write_text("\n".join(f"{i + 1}\n{format_srt_time(a)} --> {format_srt_time(b)}\n{txt}\n" for i, (a, b, txt) in enumerate(srt)), encoding="utf-8")
    (out_dir / "口播稿.txt").write_text("\n\n".join(f"[{i + 1}] {s['heading']}\n{s['narration']}" for i, s in enumerate(scenes)), encoding="utf-8")
    cover_title = p["cover_titles"][p["cover_choice"]] if p["cover_titles"] else p["title"]
    first_img = next((images.image_path(image_cache[s["image"]["id"]]) for s in scenes if s["type"] == "image" and s["image"].get("id") in image_cache), "")
    cover_ctx = {"theme": p["theme"], "series": p["brief"].get("series", ""), "image_path": str(first_img) if first_img else "", "subtitle": p["title"] if cover_title != p["title"] else ""}
    visuals.render_cover(cover_title, cover_ctx, "landscape").save(out_dir / "封面-横版.jpg", quality=92)
    visuals.render_cover(cover_title, cover_ctx, "portrait").save(out_dir / "封面-竖版.jpg", quality=92)
    (out_dir / "发布信息.txt").write_text(publish_kit(p, orientation, mode, total_dur), encoding="utf-8")
    from .storage import write_json

    snapshot["export_hash"] = chash
    write_json(out_dir / "project-snapshot.json", snapshot)
    if not rc.get("keep_work"):
        shutil.rmtree(work, ignore_errors=True)
    images.record_usage(pid, images.project_image_ids(p))

    rec = {
        "time": proj.now_iso(),
        "kind": kind,
        "dir": out_dir.name,
        "video": mp4.name,
        "srt": srt_path.name,
        "duration": round(total_dur, 2),
        "hash": chash,
        "tts": tts.engine,
        "scenes": len(scenes),
    }

    def add(q):
        q["exports"].append(rec)

    proj.update(pid, add)
    ctx.progress(1.0, f"完成：{mp4.name}（{total_dur:.0f} 秒）")
    return rec


def _music_path(name):
    if not name:
        return ""
    from . import paths

    p = Path(name)
    if not p.is_absolute():
        p = paths.music_dir() / name
    return str(p) if p.is_file() else ""


def _safe_name(s):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', " ", s).strip()
    return (s or "video")[:40]


def publish_kit(p, orientation, mode, duration):
    title = p["cover_titles"][p["cover_choice"]] if p["cover_titles"] else p["title"]
    tags = " ".join("#" + t for t in p["tags"])
    disclosure = "本视频由作者策划、撰写观点并审核，配音与画面使用 AI 工具辅助生成。"
    targets = {
        ("landscape", "full"): "YouTube（长视频）、B站",
        ("portrait", "short"): "抖音、视频号、YouTube Shorts",
        ("portrait", "full"): "抖音、视频号",
        ("landscape", "short"): "B站动态、YouTube",
    }[(orientation, mode)]
    return f"""适合发布到：{targets}
时长：{duration:.0f} 秒

标题：{p['title']}
封面标题：{title}

简介：
{p['description']}

{disclosure}

标签：{tags}

—— 发布前检查 ——
[ ] 各平台发布页勾选“内容由 AI 生成/包含 AI 内容”声明（抖音、B站、视频号、YouTube 的 Altered or synthetic content）
[ ] 读音：多音字、英文术语、数字
[ ] 画面与旁白是否错位，配图是否配错
[ ] 事实：低把握度条目已核对
[ ] 背景音乐、字体、图片均有商用授权
[ ] 投资类：没有具体标的的买卖建议，没有提到自己的持仓
[ ] 手动发布，不用群控或脚本；每个平台每个题材一个号
"""


def estimate(p):
    cfg = settings_mod.load()
    chars = sum(len(s["narration"]) for s in p["scenes"])
    target = int(p["brief"]["minutes"] * CHARS_PER_MINUTE)
    cost = sum(u.get("cost", 0) for u in p.get("usage", []))
    calls = sum(1 for u in p.get("usage", []) if not u.get("local_cache"))
    return {
        "chars": chars,
        "target_chars": target,
        "seconds": round(chars / CHARS_PER_MINUTE * 60),
        "llm_calls": calls,
        "llm_cost": round(cost, 4),
        "planned_calls": 1 if p["brief"]["minutes"] <= SINGLE_CALL_MAX_MINUTES else 1 + max(2, min(8, int(math.ceil(p["brief"]["minutes"] / 3)))),
        "tts_engine": cfg["tts"]["engine"],
        "hash": proj.content_hash(p),
    }


def review(p, recent=None):
    cfg = settings_mod.load()
    return {
        "issues": compliance.check(p, recent),
        "pronunciation": compliance.pronunciation_checklist(p, cfg["pronunciation"]),
    }
