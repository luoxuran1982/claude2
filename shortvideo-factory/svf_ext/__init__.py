"""AI 短视频工厂的「外壳扩展」：运行在生成界面后台（上游 Streamlit 进程）里的代码。

和 svf/（启动器、窗口、本机服务）不同，这个包由随包 Python 加载，和上游 MoneyPrinterTurbo 在同一个进程里，
所以能用上游的依赖（Pillow、MoviePy、requests、loguru）。上游源码只在少数几处用「[补丁 P5]」「[补丁 P6]」
调用这里的函数（见 UPSTREAM.md）；单独运行上游、找不到本包时，那几处退回上游原来的行为。

- storyboard：把文案按配音/字幕时间切成讲解页，按句子特征选版式（「规则切分」，纯 Python，不画图）
- director：「AI 导演」：请大模型设计分镜（每页的标题、版式、结构化数据、对应原文句子），校验 JSON，不合格重试一次、再退回规则切分
- cards：用 Pillow 画讲解页（标题、编号要点、对比两栏、流程箭头、数字、关键词卡），三套浅/深配色
- studio_cards：「工坊主题」：移植自 AI 知识视频工坊 studio/visuals.py 的深色主题和全部版式（表格、代码高亮、引言、图表…）
- illustrate：「卡片 + AI 插图」：给导演标记的少数概念页调用 OpenAI 兼容文生图
- shots：每页画面存成 PNG、写分镜清单 svf_storyboard.json（供外壳的「审片 / 分镜」视图）
- render：讲解页加缓慢缩放/平移、页间淡入淡出，编码成一段 mp4；「混合」来源把库存片段插进时间线
- pipeline：上游素材流程的接入点（[补丁 P6]）
- hints：配音失败、素材钥匙、素材搜索失败的中文原因；声音和文案语言不一致的检测
- desktop：「打开目录」按平台打开文件夹（Windows os.startfile / macOS open / Linux xdg-open）
- webui：Streamlit 界面上的几处小控件（卡片配色、钥匙格式提示、素材下载代理、语言提醒）
"""

# 「程序生成」组里的视频来源（config.toml 的 app.video_source 取值）
KNOWLEDGE_CARDS = "knowledge_cards"            # 规则切分（1.0.2 起的方案，原样保留）
CARDS_AI = "knowledge_cards_ai"                # AI 导演（1.1.0）
CARDS_ILLUS = "knowledge_cards_illus"          # AI 导演 + AI 插图（1.1.0）
HYBRID_PIXABAY = "hybrid_pixabay"
HYBRID_PEXELS = "hybrid_pexels"
CARD_SOURCES = (KNOWLEDGE_CARDS, CARDS_AI, CARDS_ILLUS, HYBRID_PIXABAY, HYBRID_PEXELS)
HYBRID_STOCK = {HYBRID_PIXABAY: "pixabay", HYBRID_PEXELS: "pexels"}
AI_SOURCES = (CARDS_AI, CARDS_ILLUS)

CARD_GROUP_LABEL = "程序生成"
CARD_SOURCE_LABELS = {
    KNOWLEDGE_CARDS: "知识讲解卡片（规则切分，免费，不调用大模型画分镜）",
    CARDS_AI: "知识讲解卡片（AI 导演，多一次大模型调用）",
    CARDS_ILLUS: "知识讲解卡片 + AI 插图（AI 导演 + 文生图，最多 3 张）",
    HYBRID_PIXABAY: "混合：Pixabay 素材 + 知识卡片补齐",
    HYBRID_PEXELS: "混合：Pexels 素材 + 知识卡片补齐",
}

# 讲解卡片主题（config.toml 的 [app]）。规则切分和 AI 导演各存一个，默认不同：规则切分沿用 1.0.2 的浅色「课堂蓝」，
# AI 导演默认「工坊深色」。每种主题都能画全部版式（表格、代码等由 studio_cards 按该主题配色画）。
THEME_LABELS = {"studio": "工坊深色（AI 知识视频工坊同款）", "classroom": "课堂蓝（浅色）", "dark": "深夜蓝（深色）",
                "paper": "暖纸（米色）"}
DEFAULT_THEME = "classroom"
DEFAULT_AI_THEME = "studio"
THEME_CONFIG_KEY = "svf_card_theme"
AI_THEME_CONFIG_KEY = "svf_ai_card_theme"
# 混合来源补齐时用哪种卡片：rule（规则切分，默认）/ ai（AI 导演）
HYBRID_MODE_KEY = "svf_hybrid_card_mode"
