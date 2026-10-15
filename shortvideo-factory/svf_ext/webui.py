"""生成界面（上游 webui/Main.py）里几处小控件和提示，由 [补丁 P5] 调用。所有文字都是中文。

- 「视频来源」下拉里的「程序生成」组：知识讲解卡片、混合；选中时显示说明和「卡片配色」
- 「设置 → 素材来源」：Pexels / Pixabay 钥匙格式检查；「素材下载代理」输入框
- 配音声音和文案语言不一致时提醒（选声音时、点「生成视频」时）
- 配音试听失败、任务失败时的中文原因（hints）
- 「打开目录」按平台打开（desktop）
"""
from __future__ import annotations

import html
import json
import re

from . import desktop, duration, explain, llmlog, tasks
from . import (CARD_GROUP_LABEL, CARD_SOURCE_LABELS, CARD_SOURCES, DEFAULT_THEME, HYBRID_STOCK, KNOWLEDGE_CARDS,
               THEME_CONFIG_KEY, THEME_LABELS, hints)
from .desktop import open_path  # noqa: F401  （1.0.2 的接口，保留兼容）

hints.install_log_sink()
def _install_hooks() -> None:
    """接到上游（只装一次）：防重复提交 / 取消 / 残留任务（tasks）、大模型调用记录（llmlog）、目标时长（本模块）。"""
    tasks.install()
    llmlog.install()
    install_duration_hooks()




__all__ = ["open_folder", "render_open_failure", "CARD_GROUP_LABEL", "CARD_SOURCE_LABELS", "CARD_SOURCES", "HYBRID_SOURCES", "open_path", "render_source_options",
           "render_key_hint", "render_material_proxy", "render_voice_language_warning", "tts_failure_hint",
           "proxy_problem", "set_material_proxy"]

HYBRID_SOURCES = tuple(HYBRID_STOCK)
PROXY_LABEL = "素材下载代理（可选，如 http://127.0.0.1:7897）"


def tts_failure_hint() -> str:
    return hints.tts_failure_hint()


def render_source_options(st, config, params) -> None:
    """视频来源选了「程序生成」组时，在下拉下面显示说明和卡片配色。"""
    source = params.video_source
    if source not in CARD_SOURCES:
        return
    if source == KNOWLEDGE_CARDS:
        st.caption("按配音和字幕的时间把文案分成若干页，由程序画出图文讲解页（标题、编号要点、对比两栏、流程箭头、关键数字、"
                   "关键词高亮），每页缓慢缩放或平移。不联网、不需要素材钥匙、不花钱。适合讲概念、原理、步骤的知识类主题。")
    else:
        stock = HYBRID_STOCK[source]
        key = f"{stock}_api_keys"
        has_key = bool(config.app.get(key))
        st.caption(f"先在 {stock.capitalize()} 搜索素材；搜到的够用就全部用素材，不够或搜不到时，用知识卡片补齐（不会因此失败）。"
                   + ("" if has_key else f"还没填 {stock.capitalize()} 钥匙，这次会全部用知识卡片。"))
    names = list(THEME_LABELS)
    current = str(config.app.get(THEME_CONFIG_KEY, DEFAULT_THEME) or DEFAULT_THEME)
    theme = st.selectbox("卡片配色", names, index=names.index(current) if current in names else 0,
                         format_func=THEME_LABELS.get, key="svf_card_theme_select")
    if theme != current:
        updater = getattr(config, "update_config_nonblocking", None)   # 视频生成中改设置时上游会排队，任务结束再生效
        if updater:
            updater(config.app, THEME_CONFIG_KEY, theme)
        else:
            config.app[THEME_CONFIG_KEY] = theme


def render_key_hint(st, provider: str, value: str) -> None:
    """钥匙填错栏位、格式不对时，在输入框下面给出中文提示。"""
    hint = hints.key_format_hint(provider, value)
    if hint:
        st.warning(hint)
    elif provider == "pexels" and (value or "").strip():
        st.caption("提示：Pexels 目前暂停发放新的 API 钥匙，新钥匙可能用不了；搜不到素材时建议改用 Pixabay。")


def proxy_problem(url: str) -> str | None:
    u = (url or "").strip()
    if not u:
        return None
    if not re.match(r"^(https?|socks5h?)://[^\s/:]+(:\d{1,5})?/?$", u, re.I) and not re.match(r"^(https?|socks5h?)://[^\s@]+@[^\s/:]+(:\d{1,5})?/?$", u, re.I):
        return "代理地址格式不对，应该像 http://127.0.0.1:7897（http:// 或 socks5:// 开头，后面是地址和端口）。"
    return None


def set_material_proxy(config, url: str) -> None:
    """把代理写进上游的 config.proxy（http 和 https 都用它）。上游素材搜索和下载的每次请求都传 proxies=config.proxy，
    所以填了马上生效；大模型、配音不读这个设置。保存时随 [proxy] 一节写进 config.toml。"""
    u = (url or "").strip()
    proxy = config.proxy
    if u:
        proxy["http"] = u
        proxy["https"] = u
    else:
        proxy.pop("http", None)
        proxy.pop("https", None)
    try:
        config._cfg["proxy"] = proxy   # 上游 save_config 以 _cfg 为底稿保存，确保 [proxy] 一节一起写回
    except Exception:
        pass


def render_material_proxy(st, config) -> None:
    current = str((config.proxy or {}).get("https") or (config.proxy or {}).get("http") or "")
    url = st.text_input(PROXY_LABEL, value=current, key="svf_material_proxy_input",
                        help="只有素材搜索和下载（Pexels、Pixabay、Coverr，以及 AI 视频服务的接口）走这个代理，不用开系统代理；"
                             "大模型和配音不走。留空表示不用代理。")
    problem = proxy_problem(url)
    if problem:
        st.warning(problem)
        return
    if url.strip() != current:
        set_material_proxy(config, url)
    if url.strip():
        st.caption(f"素材搜索和下载走代理 {url.strip()}；大模型、配音不走。")


def voice_warning_text(params, tts_server: str) -> str | None:
    text = " ".join(str(getattr(params, k, "") or "") for k in ("video_script", "video_subject"))
    return hints.voice_language_warning(text, getattr(params, "voice_name", ""), tts_server,
                                        str(getattr(params, "video_language", "") or ""))


def render_voice_language_warning(st, params, tts_server: str) -> bool:
    """声音和文案语言不一致时显示提醒，返回是否提醒了。"""
    msg = voice_warning_text(params, tts_server)
    if msg:
        st.warning(msg)
        return True
    return False


# ---------------------------------------------------------------- 打开目录

OPEN_FAIL_KEY = "svf_open_failure"


def copy_button_html(text: str, label: str = "复制路径") -> str:
    """「复制路径」按钮。点击由外壳页面脚本 svf/web/inject.js 处理（写入剪贴板），Streamlit 本身不执行页面里的脚本。"""
    return (f'<button type="button" class="svf-copy-path" data-copy="{html.escape(text, quote=True)}" '
            f'title="把完整路径复制到剪贴板">📋 {html.escape(label)}</button>')


def open_folder(st, path) -> bool:
    """打开文件夹（经外壳主进程，见 desktop.open_folder）。失败时记下原因和完整路径，由 render_open_failure 显示。"""
    res = desktop.open_folder(str(path))
    if res.get("ok"):
        st.session_state.pop(OPEN_FAIL_KEY, None)
        st.toast(f"已请系统打开文件夹：{res.get('path')}", icon="📂")
    else:
        st.session_state[OPEN_FAIL_KEY] = {"path": res.get("path") or str(path), "error": res.get("error") or "原因不明"}
        st.toast("没能打开文件夹，完整路径显示在下方，可以复制后手动打开", icon="⚠️")
    return bool(res.get("ok"))


def report_open_failure(st, path, error: str) -> None:
    st.session_state[OPEN_FAIL_KEY] = {"path": str(path), "error": error}
    st.toast("没能打开文件夹，完整路径显示在下方", icon="⚠️")


def _dismiss_open_failure(st):
    st.session_state.pop(OPEN_FAIL_KEY, None)


def render_open_failure(st, where: str) -> None:
    """打开失败的兜底：显示原因、完整路径和「复制路径」按钮。"""
    res = st.session_state.get(OPEN_FAIL_KEY)
    if not res:
        return
    with st.container(border=True):
        st.error(f"没能打开文件夹。原因：{res['error']}")
        st.caption("可以复制下面的完整路径，粘贴到资源管理器（Windows）的地址栏，或访达「前往 → 前往文件夹」（macOS）里打开。")
        st.code(res["path"], language=None)
        st.markdown(copy_button_html(res["path"]), unsafe_allow_html=True)
        st.button("知道了", key=f"svf_open_fail_ok_{where}", on_click=_dismiss_open_failure, args=(st,))


# ---------------------------------------------------------------- 防重复提交、取消、残留任务（tasks.py）

BUSY_LABEL = tasks.BUSY_LABEL
TASK_LABELS = {tasks.CANCEL_STAGE: "已取消", tasks.INTERRUPT_STAGE: "已中断"}


def session_id() -> str:
    try:
        from streamlit.runtime.scriptrunner import get_script_run_ctx
        ctx = get_script_run_ctx()
        return ctx.session_id if ctx else ""
    except Exception:  # noqa: BLE001
        return ""


def generation_busy() -> str | None:
    """有任务正在生成或排队时返回它的 ID（「生成视频」按钮据此置灰）。"""
    return tasks.busy_task()


def render_busy_note(st, busy_id: str) -> None:
    progress = ""
    try:
        from app.services import state as sm
        rec = sm.state.get_task(busy_id) or {}
        progress = f"（进度 {int(rec.get('progress', 0) or 0)}%）"
    except Exception:  # noqa: BLE001
        pass
    st.caption(f"已有任务在生成{progress}，完成后才能再生成，避免同时跑几个任务。要停下它，打开顶部「任务管理」点「取消」。")


def claim_generation(task_id: str, params) -> str | None:
    """点「生成视频」后、提交前的最后检查：放行返回 None，否则返回原因（同时点的几次只放行一个；同参数 90 秒内只保留一个）。"""
    return tasks.claim(task_id, params, session_id())


def confirm_generation(task_id: str) -> None:
    tasks.confirm(task_id)


def release_generation(task_id: str) -> None:
    tasks.release(task_id)


def reconcile_tasks(state_obj, active: dict, pending_id) -> None:
    try:
        tasks.reconcile(state_obj, active, pending_id)
    except Exception as exc:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"检查残留任务失败：{exc}")


def task_status(task: dict) -> str | None:
    return tasks.status_of(task)


def task_label(task: dict, default: str) -> str:
    return TASK_LABELS.get(tasks.status_of(task) or "", default)


def task_filter_key(task: dict) -> str | None:
    """已取消、已中断归到「失败」标签页，不算生成中，可以删除。"""
    return "failed" if tasks.status_of(task) else None


def cancel_task(task_id: str) -> str:
    return tasks.cancel(task_id)


# ---------------------------------------------------------------- 目标时长（duration.py）

def _target_from_config() -> int:
    try:
        from app.config import config
        return int(config.ui.get(duration.CONFIG_KEY, 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _voice_rate_from_config() -> float:
    try:
        from app.config import config
        return float(config.ui.get("voice_rate", 1.0) or 1.0)
    except Exception:  # noqa: BLE001
        return 1.0


def _task_dir(task_id: str):
    from pathlib import Path
    from app.utils import utils
    return Path(utils.task_dir()) / task_id


def install_duration_hooks() -> None:
    """① 写文案的提示词（llm.build_script_prompt）末尾加上目标时长换算的字数；② 配音完成后记下实际时长并写一行日志。"""
    from loguru import logger
    from app.services import llm
    from app.services import task as task_service

    if not getattr(llm.build_script_prompt, "_svf_duration", False):
        orig_build = llm.build_script_prompt

        def build_script_prompt(video_subject, language="", *args, **kwargs):
            prompt = orig_build(video_subject, language, *args, **kwargs)
            ctx = llmlog._ctx.get() or {}
            p = None
            if ctx.get("task_id"):
                info = duration.read_task(_task_dir(ctx["task_id"]))
                if info.get("target_seconds"):
                    p = info
            if p is None:
                p = duration.plan(_target_from_config(), duration.language_of(video_subject, language),
                                  _voice_rate_from_config())
            return prompt + duration.prompt_addition(p) if p else prompt
        build_script_prompt._svf_duration = True
        build_script_prompt.__wrapped__ = orig_build
        llm.build_script_prompt = build_script_prompt

    fn = task_service.generate_audio
    if not getattr(fn, "_svf_duration", False):
        def generate_audio(task_id, params, video_script, *args, **kwargs):
            out = fn(task_id, params, video_script, *args, **kwargs)
            try:
                audio_file, audio_duration = out[0], out[1]
                if audio_file and audio_duration:
                    d = _task_dir(task_id)
                    lang = duration.language_of(video_script, getattr(params, "video_language", ""))
                    info = dict(duration.read_task(d))
                    note = ""
                    try:
                        from app.services import voice as voice_service
                        if voice_service.is_no_voice(getattr(params, "voice_name", "")):
                            note = "无配音：按文案长度估算的静音时长"
                    except Exception:  # noqa: BLE001
                        pass
                    if getattr(params, "custom_audio_file", ""):
                        note = "用的是你上传的配音"
                    info.update(duration.record_actual(d, video_script, float(audio_duration), lang,
                                                       getattr(params, "voice_rate", 1.0), learn=not note, note=note))
                    logger.info(duration.summary_line(info))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"记录实际配音时长失败：{exc}")
            return out
        generate_audio._svf_duration = True
        generate_audio.__wrapped__ = fn
        task_service.generate_audio = generate_audio


def render_target_duration(st, config, params) -> None:
    """「目标时长（秒）」：30 / 60 / 90 / 120 / 自定义；换算成文案字数写进写文案的提示词。"""
    current = int(config.ui.get(duration.CONFIG_KEY, 0) or 0)
    options = list(duration.PRESETS) + [duration.CUSTOM]
    sel_default = current if current in duration.PRESETS else duration.CUSTOM
    choice = st.selectbox("目标时长（秒）", options, index=options.index(sel_default), format_func=duration.label,
                          key="svf_target_seconds_select",
                          help="按配音语速把时长换算成文案字数，写进给大模型的写文案要求里（中文大约每秒 4–5 个字）。"
                               "自己写好文案时不改写你的文案，生成后在「日志解读」里对照目标和实际时长。")
    value = choice
    if choice == duration.CUSTOM:
        value = int(st.number_input("自定义目标时长（秒）", min_value=duration.MIN_SECONDS, max_value=duration.MAX_SECONDS,
                                    value=current if current >= duration.MIN_SECONDS else 45, step=5,
                                    key="svf_target_seconds_custom"))
    if value != current:
        updater = getattr(config, "update_config_nonblocking", None)
        if updater:
            updater(config.ui, duration.CONFIG_KEY, value)
        else:
            config.ui[duration.CONFIG_KEY] = value
    p = duration.plan(value, duration.language_of(getattr(params, "video_subject", ""), getattr(params, "video_language", "")),
                      config.ui.get("voice_rate", 1.0))
    if p:
        st.caption(f"约 {p['target_chars']} {p['unit']}（{p['chars_per_second']:.1f} {p['unit'][-1] if p['lang'] == 'zh' else '词'}/秒，"
                   f"{p['basis']}）。写文案时告诉大模型这个字数；段落数照旧。")


def note_submission(task_id: str, params) -> None:
    """提交时记下这次的目标时长（svf_task.json），并把界面上刚生成的文案调用记录复制进任务。"""
    try:
        script = str(getattr(params, "video_script", "") or "")
        lang = duration.language_of(script or getattr(params, "video_subject", ""), getattr(params, "video_language", ""))
        info = {"submitted": __import__("time").strftime("%Y-%m-%d %H:%M:%S"), "source": getattr(params, "video_source", ""),
                "user_script": bool(script.strip())}
        p = duration.plan(_target_from_config(), lang, getattr(params, "voice_rate", 1.0))
        info.update(p or {"target_seconds": 0})
        duration.write_task(_task_dir(task_id), info)
        if script.strip():
            llmlog.adopt_ui_calls(task_id, script)
    except Exception as exc:  # noqa: BLE001
        from loguru import logger
        logger.warning(f"记录任务信息失败：{exc}")


# ---------------------------------------------------------------- 日志解读、大模型调用记录（生成页，日志上方）

STATUS_ICON = {"done": "✅", "running": "⏳", "failed": "❌", "skipped": "⏭️", "pending": "▫️"}
STATUS_TEXT = {"done": "完成", "running": "进行中", "failed": "失败", "skipped": "跳过", "pending": "还没开始"}


def _md(text) -> str:
    return re.sub(r"([\\`*_\[\]<>#|~$])", r"\\\1", str(text))


def _task_info(task_id: str) -> dict:
    try:
        return duration.read_task(_task_dir(task_id))
    except Exception:  # noqa: BLE001
        return {}


def explain_markdown(res: dict, info: dict) -> str:
    blocks = []
    dl = explain.duration_line(info)
    for k, s in enumerate(res["steps"], 1):
        head = f"{STATUS_ICON[s.status]} **{k}. {s.title}**　{STATUS_TEXT[s.status]}"
        if s.start_time:
            el = s.elapsed(res["last_time"])
            head += f" · {s.start_time[11:]} 开始" + (f" · 用时 {el:.0f} 秒" if el is not None else "")
            head += f" · 原始日志第 {s.start_line} 行起"
        if s.status == "pending":
            blocks.append(head)
            continue
        if s.status == "skipped":
            blocks.append(head + f"  \n{_md(s.what)}")
            continue
        body = (f"**在做什么**：{_md(s.what)}  \n**为什么**：{_md(s.why)}  \n"
                f"**正常要多久**：{_md(s.typical)}  \n**出错通常是因为**：{_md(s.errors)}")
        if s.key == "audio" and dl:
            body += f"  \n**时长**：{_md(dl)}"
        notes = s.notes
        if len(notes) > 10:
            notes = notes[:3] + [(None, "", f"…（中间 {len(notes) - 8} 条略）")] + notes[-5:]
        items = "\n".join(f"- {('第 %s 行 · %s　' % (line, t[11:])) if line else ''}{_md(text)}" for line, t, text in notes)
        blocks.append(head + "\n\n" + body + ("\n\n" + items if items else ""))
    return "\n\n---\n\n".join(blocks)


def render_task_insights(st, task_id: str, log_records: list) -> None:
    """生成页：原始日志上方的「日志解读」和「大模型调用记录」。原始日志本身不动（只加了行号，方便对照）。"""
    info = _task_info(task_id)
    res = explain.explain(log_records or [], {"user_script": info.get("user_script"), "source": info.get("source")})
    if res["state"] == "running" and tasks.lookup_stage(task_id) == tasks.CANCEL_STAGE:
        res["state"], res["summary"] = "cancelled", "已取消。"
    with st.expander("日志解读：每一步在做什么、为什么、要多久、常见出错原因", expanded=True):
        st.markdown(f"**{_md(res['summary'])}**")
        dl = explain.duration_line(info)
        if dl:
            st.caption(dl)
        if not log_records:
            st.caption("还没有日志（设置里关掉了「显示日志」时这里也没有）。")
        st.markdown(explain_markdown(res, info))
        st.caption("「第 N 行」对应下面原始日志左边的行号。heartbeat 类的 still running 行只是「还在运行」的报平安。")
    render_llm_calls(st, task_id)
    st.caption("原始日志")


def render_llm_calls(st, task_id: str) -> None:
    calls = llmlog.read_task(task_id)
    label = f"大模型调用记录（{len(calls)} 次，原汁原味）" if calls else "大模型调用记录（这个任务没有调用大模型）"
    with st.expander(label, expanded=False):
        if not calls:
            st.caption("自己写好文案、又用「知识讲解卡片」时，整个任务都不需要大模型。记录文件：任务目录的 llm_calls.jsonl。")
            return
        st.caption("每次调用一条：发给模型的完整提示词、模型返回的完整原始输出、耗时、token 用量。API Key 不会出现在记录里。"
                   "文件在任务目录的 llm_calls.jsonl。")
        for k, c in enumerate(calls, 1):
            usage = c.get("usage") or {}
            tok = (f" · token：输入 {usage.get('prompt_tokens', '?')} / 输出 {usage.get('completion_tokens', '?')} / "
                   f"合计 {usage.get('total_tokens', '?')}") if usage else " · token 用量：接口没有返回"
            st.markdown(f"**#{k} {_md(c.get('step_label', ''))}**（第 {c.get('attempt', 1)} 次尝试，"
                        f"{'成功' if c.get('ok') else '失败'}）· {_md(c.get('provider_label') or c.get('provider') or '?')} / "
                        f"`{_md(c.get('model') or '?')}` · {c.get('elapsed_seconds', 0):.1f} 秒{tok} · {c.get('time', '')}"
                        + ("　（在界面上点「生成文案」时调用的）" if c.get("from_ui") else ""))
            if c.get("base_url"):
                st.caption(f"接口地址：{c['base_url']}")
            for m in c.get("messages") or []:
                st.caption(f"发给模型的 {m.get('role')} 消息（原文）")
                st.code(m.get("content", ""), language=None, wrap_lines=True)
            st.caption("模型返回的原始输出" + ("" if c.get("ok") else f"（出错：{c.get('error', '')}）"))
            st.code(c.get("output", "") or "（空）", language=None, wrap_lines=True)
            if c.get("raw_response"):
                st.caption("接口返回的原始 JSON")
                st.code(json.dumps(c["raw_response"], ensure_ascii=False, indent=2)[:60000], language="json", wrap_lines=True)
            st.divider()


try:   # 导入上游失败时（只跑外壳单元测试）跳过
    _install_hooks()
except Exception as _exc:  # noqa: BLE001
    try:
        from loguru import logger
        logger.warning(f"外壳扩展没有接上上游（防重复提交、大模型调用记录、目标时长）：{_exc}")
    except ImportError:
        pass
