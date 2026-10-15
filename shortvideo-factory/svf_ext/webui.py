"""生成界面（上游 webui/Main.py）里几处小控件和提示，由 [补丁 P5] 调用。所有文字都是中文。

- 「视频来源」下拉里的「程序生成」组：知识讲解卡片、混合；选中时显示说明和「卡片配色」
- 「设置 → 素材来源」：Pexels / Pixabay 钥匙格式检查；「素材下载代理」输入框
- 配音声音和文案语言不一致时提醒（选声音时、点「生成视频」时）
- 配音试听失败、任务失败时的中文原因（hints）
- 「打开目录」按平台打开（desktop）
"""
from __future__ import annotations

import html
import re

from . import desktop
from . import (CARD_GROUP_LABEL, CARD_SOURCE_LABELS, CARD_SOURCES, DEFAULT_THEME, HYBRID_STOCK, KNOWLEDGE_CARDS,
               THEME_CONFIG_KEY, THEME_LABELS, hints)
from .desktop import open_path  # noqa: F401  （1.0.2 的接口，保留兼容）

hints.install_log_sink()

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
        st.error(f"没能打开文件夹：{res['error']}")
        st.caption("可以复制下面的完整路径，粘贴到资源管理器（Windows）的地址栏，或访达「前往 → 前往文件夹」（macOS）里打开。")
        st.code(res["path"], language=None)
        st.markdown(copy_button_html(res["path"]), unsafe_allow_html=True)
        st.button("知道了", key=f"svf_open_fail_ok_{where}", on_click=_dismiss_open_failure, args=(st,))
