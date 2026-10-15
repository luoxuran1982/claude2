"""上游素材流程的接入点：上游 app/services/task.py 的 get_video_materials 在视频来源是「程序生成」组时调用
prepare_materials（[补丁 P6]）。这时配音和字幕都已经生成好了。

做的事：
1. 读本任务的文案（script.json）和字幕（subtitle.srt，没开字幕时按朗读长度估算时间），切成讲解页；
2. 「混合」来源先照常去 Pixabay / Pexels 搜索下载：素材够覆盖整条配音就直接交回上游（和选库存来源一样）；
   不够或搜索失败（没钥匙、钥匙无效、网络不通）时，把下到的片段放进普通讲解页，其余页用知识卡片补齐；
3. 把整条时间线画成一个 mp4（svf-cards.mp4），作为唯一的素材交回上游。上游随后照常做转场、字幕、配音、
   背景音乐和拼接。为了让画面和配音对得上，本任务里把「最大片段时长」临时设成整条时间线的长度、
   「片段播放速度」设成 1（只影响本次任务的参数，不改设置）。

每页的完整画面另存在任务目录的 knowledge-cards/ 下（PNG），分镜清单写在 svf_storyboard.json（外壳「审片 / 分镜」视图读它）。

1.1.0 分三种「卡片模式」：
- 规则切分（knowledge_cards）：storyboard.build_pages，1.0.2 起的做法，原样保留；
- AI 导演（knowledge_cards_ai）：director.direct 多调用一次大模型设计分镜，不合格重试一次、再退回规则切分；
- 卡片 + AI 插图（knowledge_cards_illus）：AI 导演 + illustrate.generate 给最多 3 页配文生图插图；没配置文生图时退回纯卡片；
- 混合来源补齐用哪种卡片由设置 svf_hybrid_card_mode 决定（rule 默认 / ai）。
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

from . import (AI_SOURCES, AI_THEME_CONFIG_KEY, CARD_SOURCE_LABELS, CARD_SOURCES, CARDS_ILLUS, DEFAULT_AI_THEME, DEFAULT_THEME,
               HYBRID_MODE_KEY, HYBRID_STOCK, KNOWLEDGE_CARDS, THEME_CONFIG_KEY, fastcards, hints, llmlog, shots, tasks)
from .storyboard import Page, build_pages, parse_srt

hints.install_log_sink()

__all__ = ["CARD_SOURCES", "KNOWLEDGE_CARDS", "needs_terms", "prepare_materials", "fast_final_videos", "safe_zone",
           "tts_failure_hint", "material_failure_hint"]

_PLANS: dict = {}     # task_id → 快速通道要用的讲解页（素材这一步算好，最终渲染那一步用）


def needs_terms(source: str) -> bool:
    """纯知识卡片不需要搜索关键词（省一次大模型调用，也少一个失败点）；混合来源要搜素材，需要。"""
    return source not in (KNOWLEDGE_CARDS,) + AI_SOURCES


def tts_failure_hint() -> str:
    return " " + hints.tts_failure_hint()


def material_failure_hint(source: str) -> str:
    provider = HYBRID_STOCK.get(source, source)
    if provider not in ("pexels", "pixabay", "coverr"):
        return ""
    return " " + hints.stock_failure_hint(provider)


def safe_zone(params) -> str:
    """字幕会占的区域，讲解页的内容避开它。"""
    if not getattr(params, "subtitle_enabled", True):
        return "none"
    pos = str(getattr(params, "subtitle_position", "bottom") or "bottom")
    if pos == "top":
        return "top"
    if pos == "center":
        return "none"
    if pos == "custom":
        try:
            return "top" if float(getattr(params, "custom_position", 70.0)) < 50 else "bottom"
        except (TypeError, ValueError):
            return "bottom"
    return "bottom"


def _resolution(params) -> tuple[int, int]:
    from app.models.schema import VideoAspect
    return VideoAspect(params.video_aspect or VideoAspect.portrait.value).to_resolution()


def _read_script(task_dir: Path, params) -> str:
    try:
        data = json.loads((task_dir / "script.json").read_text(encoding="utf-8"))
        if isinstance(data, dict) and str(data.get("script") or "").strip():
            return str(data["script"])
    except (OSError, ValueError):
        pass
    return str(getattr(params, "video_script", "") or "")


def _read_cues(task_dir: Path) -> list:
    srt = task_dir / "subtitle.srt"
    try:
        return parse_srt(srt.read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return []


def card_mode(source: str) -> str:
    """rule（规则切分）/ ai（AI 导演）。"""
    if source in AI_SOURCES:
        return "ai"
    if source in HYBRID_STOCK:
        return "ai" if str(_app_flag_raw(HYBRID_MODE_KEY, "rule")).strip().lower() == "ai" else "rule"
    return "rule"


def _theme(mode: str = "rule") -> str:
    key, default = (AI_THEME_CONFIG_KEY, DEFAULT_AI_THEME) if mode == "ai" else (THEME_CONFIG_KEY, DEFAULT_THEME)
    try:
        from app.config import config
        theme = str(config.app.get(key, default) or default)
    except Exception:
        theme = default
    from .cards import ALL_THEMES
    return theme if theme in ALL_THEMES else default


def _director(task_id: str, script: str, params, cues, audio_duration: float, W: int, H: int, illustrations: int, log):
    """调用 AI 导演（经上游 llm._generate_response，所以支持界面上能选的所有大模型服务，并记进调用记录）。"""
    from app.services import llm
    from . import director
    ctxs: dict = {}

    def call(prompt, parts, attempt):
        with llmlog.step("director", task_id, parts=[{"t": t, "k": k, "n": n} for t, k, n in parts]) as ctx:
            ctxs[attempt] = ctx
            return llm._generate_response(prompt)
    res = director.direct(script, getattr(params, "video_subject", "") or "", cues, audio_duration, W, H, call,
                          illustrations, log=log.info)
    for a in res["attempts"]:   # 「模型调用」页的「程序最后怎么用它」
        ctx = ctxs.get(a["attempt"])
        if not ctx:
            continue
        if a["ok"]:
            note = f"解析成分镜 JSON：{director.summary(res['spec'])}。每页的时间按这一页对应的句子和字幕对齐。"
            if a.get("warnings"):
                note += "程序修整了：" + "；".join(a["warnings"])
        elif a["attempt"] == 1 and len(res["attempts"]) > 1:
            note = f"校验没通过：{a['reason']}。程序把原因告诉模型，重试一次（见下一次调用）。"
        else:
            note = f"没有采用：{a['reason']}。退回「规则切分」（程序按标点切句、按句子特征选版式）。"
        llmlog.note_use(note, ctx)
    return res


def _stock_clips(task_id, params, video_terms, audio_duration, stock: str, log) -> list[tuple[str, float]]:
    """混合来源：照常搜索下载库存素材，返回 [(文件, 时长)]；任何失败都返回空列表（交给卡片补齐）。"""
    from app.models.schema import VideoConcatMode
    from app.services import material
    try:
        files = material.download_videos(
            task_id=task_id, search_terms=video_terms or [getattr(params, "video_subject", "")], source=stock,
            video_aspect=params.video_aspect,
            video_concat_mode=VideoConcatMode.sequential if params.match_materials_to_script else params.video_concat_mode,
            audio_duration=audio_duration * params.video_count, max_clip_duration=params.video_clip_duration,
            match_script_order=params.match_materials_to_script)
    except Exception as exc:  # noqa: BLE001 没钥匙（ValueError）、网络错误等都改用卡片
        hints.note("app.services.material", f"{stock} {type(exc).__name__}: {exc}")
        log.warning(f"混合来源：{stock} 素材搜索失败，全部改用知识卡片（{type(exc).__name__}: {exc}）")
        return []
    out = []
    for f in files or []:
        try:
            out.append((f, material._get_downloaded_video_duration(f)))
        except Exception as exc:  # noqa: BLE001
            log.warning(f"混合来源：读不出素材时长，跳过 {os.path.basename(f)}（{exc}）")
    return out


def _app_flag(key: str, default=True):
    try:
        from app.config import config
        v = config.app.get(key, default)
    except Exception:  # noqa: BLE001
        return default
    if isinstance(v, str):
        return v.strip().lower() not in ("false", "0", "off", "no")
    return bool(v)


def use_fast_path(params) -> bool:
    """知识卡片（和用卡片补齐的混合来源）默认走快速通道。例外：设置里关掉了；背景音乐要由 Sonilo / ElevenLabs
    按成片画面现做（它们要先拿到一条合成好的画面），这时仍走上游流程。"""
    if not _app_flag(fastcards.FAST_KEY, True):
        return False
    try:
        from app.services import bgm as bgm_service
        from app.services import task as task_service
        providers = getattr(task_service, "_VIDEO_MUSIC_PROVIDERS", {}) or {}
        if params.bgm_type in providers and bgm_service.should_use_bgm(params.bgm_type, params.bgm_volume):
            return False
    except Exception:  # noqa: BLE001
        pass
    return True


def fast_final_videos(task_id, params, downloaded_videos, audio_file, subtitle_path, audio_duration, partial_results=None):
    """[补丁 P8] 上游 generate_final_videos 开头调用：素材这一步留下了快速通道的讲解页时，一次合成成片并返回
    (成片列表, [], 警告)；否则返回 None，上游照常走 MoviePy 流程。"""
    plan = _PLANS.pop(task_id, None)
    if plan is None:
        if any(str(v).endswith(fastcards.PLAN_FILE) for v in downloaded_videos or []):
            raise RuntimeError("知识卡片快速通道的讲解页不见了（生成界面后台中途重启过？），请重新生成")
        return None
    from loguru import logger as log
    from app.services import task as task_service
    from app.utils import utils
    task_dir = Path(utils.task_dir(task_id))
    out = task_dir / "final-1.mp4"
    log.info(f"\n\n## 知识卡片快速通道：一次合成画面、配音、背景音乐和字幕 => {out}")
    cues = []
    if subtitle_path and os.path.isfile(subtitle_path):
        cues = parse_srt(Path(subtitle_path).read_text(encoding="utf-8"))
    bgm = fastcards.resolve_bgm(params, task_id)
    log.info(f"知识卡片快速通道：字幕 {len(cues) if params.subtitle_enabled else 0} 条（直接画进画面），"
             f"背景音乐 {os.path.basename(bgm) if bgm else '不用'}，画面 {plan['W']}×{plan['H']}，"
             f"{'每页缓慢缩放/平移' if plan['motion'] else '静止画面'}")
    try:
        from app.services import video
        preferred = video._get_configured_video_codec()
    except Exception:  # noqa: BLE001
        preferred = ""
    hw = str(_app_flag_raw(fastcards.HW_KEY, "auto")).strip().lower()
    res = fastcards.render_final(
        plan["pages"], cues, params, audio_file, str(out), plan["W"], plan["H"], plan["theme"], plan["keywords"], plan["safe"],
        bgm_file=bgm, motion=plan["motion"], hw_mode=hw, preferred=preferred,
        progress=task_service._stage_progress_reporter(task_id, 50, 99), check=lambda: tasks.check(task_id),
        log=log.info, still_dir=None)
    finals = fastcards.copy_extra(str(out), int(getattr(params, "video_count", 1) or 1))
    seconds = plan["pages"][-1].end
    log.success(f"知识卡片快速通道：成片 {out.name}（{seconds:.1f} 秒，{plan['W']}×{plan['H']}，编码器 {res['codec']}，"
                f"合成用时 {res['seconds']:.0f} 秒）")
    if partial_results is not None:
        partial_results.update(videos=list(finals), combined_videos=[], warnings=[])
    return finals, [], []


def _app_flag_raw(key: str, default):
    try:
        from app.config import config
        return config.app.get(key, default) or default
    except Exception:  # noqa: BLE001
        return default


def assign_stock(pages: list[Page], clips: list[tuple[str, float]]) -> int:
    """把库存片段按顺序放进普通讲解页（关键词卡、文字卡；片段要比这页长）。标题、要点、对比、流程、数字页保留卡片。返回用了几段。"""
    used = 0
    queue = list(clips)
    for p in pages[1:]:
        if not queue:
            break
        if p.kind not in ("keyword", "text"):
            continue
        k = next((i for i, (_, d) in enumerate(queue) if d + 0.05 >= p.duration), None)
        if k is None:
            continue
        path, _ = queue.pop(k)
        p.data = dict(p.data, path=path, card_kind=p.kind)
        p.kind = "stock"
        used += 1
    return used


def prepare_materials(task_id: str, params, video_terms, audio_duration) -> list[str] | None:
    """返回给上游的素材文件列表；失败时把中文原因写进任务状态并返回 None。"""
    from loguru import logger as log
    from app.services import task as task_service
    from app.utils import utils

    source = params.video_source
    task_dir = Path(utils.task_dir(task_id))
    try:
        audio_duration = float(audio_duration or 0)
        script = _read_script(task_dir, params)
        cues = _read_cues(task_dir)
        W, H = _resolution(params)
        mode = card_mode(source)
        theme = _theme(mode)
        director_info: dict = {"used": False}
        illus_info: dict = {"wanted": source == CARDS_ILLUS}
        sentences = None
        if mode == "ai":
            from . import illustrate
            n_illus = 0
            if source == CARDS_ILLUS:
                if illustrate.configured():
                    n_illus = illustrate.MAX_IMAGES
                else:
                    illus_info["skipped"] = illustrate.NOT_CONFIGURED
                    log.warning(f"AI 插图：{illustrate.NOT_CONFIGURED}；这次退回纯卡片（AI 导演照常）")
            log.info("AI 导演：请大模型把文案设计成分镜（每页的标题、版式、结构化数据、对应的句子）")
            res = _director(task_id, script, params, cues, audio_duration, W, H, n_illus, log)
            pages, keywords = res["pages"], res["keywords"]
            director_info = {"used": True, "ok": res["ok"], "fallback_reason": res["fallback_reason"],
                             "warnings": res["warnings"], "attempts": len(res["attempts"]),
                             "title": (res["spec"] or {}).get("title", "")}
            if res["ok"]:
                sentences = [p["sentences"] for p in res["spec"]["pages"]]
                log.success(f"AI 导演：分镜通过校验（第 {len(res['attempts'])} 次），{len(pages)} 页"
                            + (f"；程序修整了 {len(res['warnings'])} 处" if res["warnings"] else ""))
                if n_illus:
                    illus_info["results"] = illustrate.generate(pages, res["spec"], task_dir, params.video_aspect, log=log.info)
            else:
                log.warning(f"AI 导演：没有用上（{res['fallback_reason']}），退回「规则切分」")
                if source == CARDS_ILLUS and n_illus:
                    illus_info["skipped"] = "AI 导演没有用上，没有可配图的分镜"
        else:
            from .storyboard import build_pages
            pages, keywords = build_pages(script, getattr(params, "video_subject", "") or "", cues, audio_duration)
        log.info(f"知识卡片：{len(pages)} 页，时间依据：{'字幕' if cues else '按朗读长度估算'}，"
                 f"分镜：{'AI 导演' if director_info.get('ok') else '规则切分'}，主题：{theme}，关键词：{'、'.join(keywords[:8])}")

        if source in HYBRID_STOCK:
            stock = HYBRID_STOCK[source]
            clips = _stock_clips(task_id, params, video_terms, audio_duration, stock, log)
            usable = sum(min(d, params.video_clip_duration) for _, d in clips)
            if clips and usable + 0.5 >= audio_duration:
                log.info(f"混合来源：{stock} 素材共 {usable:.1f} 秒，够覆盖 {audio_duration:.1f} 秒配音，直接使用素材")
                return [f for f, _ in clips]
            used = assign_stock(pages, clips)
            if not clips:
                log.warning(f"混合来源：{stock} 没有可用素材，全部改用知识卡片。{hints.stock_failure_hint(stock)}")
            log.info(f"混合来源：{stock} 找到 {len(clips)} 段素材（可用 {usable:.1f} 秒），不够覆盖 {audio_duration:.1f} 秒配音；"
                     f"{used} 段放进讲解页，其余 {sum(p.kind != 'stock' for p in pages)} 页用知识卡片补齐。")

        safe = safe_zone(params)
        (task_dir / "knowledge-cards").mkdir(exist_ok=True)
        (task_dir / "knowledge-cards" / "storyboard.json").write_text(
            json.dumps({"keywords": keywords, "pages": [p.to_dict() for p in pages]}, ensure_ascii=False, indent=2),
            encoding="utf-8")
        try:   # 「审片 / 分镜」视图：每页一张 PNG + 分镜清单
            stills = shots.save_stills(pages, task_dir, W, H, theme, keywords, safe)
            entries = [shots.page_entry(k, p, task_dir, st, sentences[k - 1] if sentences else None)
                       for k, (p, st) in enumerate(zip(pages, stills), 1)]
            shots.write_manifest(task_dir, {"task_id": task_id, "source": source, "source_label": CARD_SOURCE_LABELS.get(source, source),
                                            "mode": "ai" if director_info.get("ok") else "rule", "theme": theme, "W": W, "H": H,
                                            "keywords": keywords, "director": director_info, "illustration": illus_info,
                                            "video": "final-1.mp4"}, entries)
            log.info(f"分镜画面：{len(pages)} 张 PNG 存在 knowledge-cards/，清单 {shots.MANIFEST}（顶部「审片 / 分镜」里看）")
        except Exception as exc:  # noqa: BLE001  只影响查看，不影响出片
            log.warning(f"保存分镜画面失败（不影响出片）：{type(exc).__name__}: {exc}")

        if use_fast_path(params):
            motion = _app_flag(fastcards.MOTION_KEY, True)
            _PLANS[task_id] = {"pages": pages, "keywords": keywords, "W": W, "H": H, "theme": theme,
                               "safe": safe, "motion": motion}
            plan = fastcards.save_plan(task_dir, pages, keywords, {"W": W, "H": H, "motion": motion})
            log.success(f"知识卡片快速通道：讲解页已画好（{len(pages)} 页，{pages[-1].end:.1f} 秒），"
                        "最终渲染一步一次合成画面、配音、背景音乐和字幕，不再经过「处理片段」「拼接」")
            return [str(plan)]
        out = task_dir / "svf-cards.mp4"
        progress = task_service._stage_progress_reporter(task_id, 40, 50)
        from .render import render_video
        render_video(pages, str(out), W, H, theme, keywords, safe, progress=progress)
        # 整条时间线是一个片段：不能被「最大片段时长」截断，也不能变速，否则画面和配音对不上
        params.video_clip_duration = int(math.ceil(pages[-1].end)) + 1
        params.video_clip_speed = 1.0
        log.success(f"知识卡片视频已生成：{out.name}（{pages[-1].end:.1f} 秒，{W}×{H}）")
        return [str(out)]
    except Exception as exc:  # noqa: BLE001
        log.exception(f"知识卡片生成失败：{exc}")
        task_service._mark_task_failed(task_id, "materials", f"知识卡片生成失败：{type(exc).__name__}: {exc}")
        return None
