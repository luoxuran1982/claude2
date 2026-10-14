"""AI 短视频工厂的「外壳扩展」：运行在生成界面后台（上游 Streamlit 进程）里的代码。

和 svf/（启动器、窗口、本机服务）不同，这个包由随包 Python 加载，和上游 MoneyPrinterTurbo 在同一个进程里，
所以能用上游的依赖（Pillow、MoviePy、requests、loguru）。上游源码只在少数几处用「[补丁 P5]–[补丁 P7]」
调用这里的函数（见 UPSTREAM.md）；单独运行上游、找不到本包时，那几处退回上游原来的行为。

- storyboard：把文案按配音/字幕时间切成讲解页，按句子特征选版式（纯 Python，不画图）
- cards：用 Pillow 画讲解页（标题、编号要点、对比两栏、流程箭头、数字、关键词卡），三套配色
- render：讲解页加缓慢缩放/平移、页间淡入淡出，编码成一段 mp4；「混合」来源把库存片段插进时间线
- pipeline：上游素材流程的接入点（[补丁 P6]）
- hints：配音失败、素材钥匙、素材搜索失败的中文原因；声音和文案语言不一致的检测
- desktop：「打开目录」按平台打开文件夹（Windows os.startfile / macOS open / Linux xdg-open）
- webui：Streamlit 界面上的几处小控件（卡片配色、钥匙格式提示、素材下载代理、语言提醒）
"""

# 「程序生成」组里的视频来源（config.toml 的 app.video_source 取值）
KNOWLEDGE_CARDS = "knowledge_cards"
HYBRID_PIXABAY = "hybrid_pixabay"
HYBRID_PEXELS = "hybrid_pexels"
CARD_SOURCES = (KNOWLEDGE_CARDS, HYBRID_PIXABAY, HYBRID_PEXELS)
HYBRID_STOCK = {HYBRID_PIXABAY: "pixabay", HYBRID_PEXELS: "pexels"}

CARD_GROUP_LABEL = "程序生成"
CARD_SOURCE_LABELS = {
    KNOWLEDGE_CARDS: "知识讲解卡片（免费，不需要素材钥匙）",
    HYBRID_PIXABAY: "混合：Pixabay 素材 + 知识卡片补齐",
    HYBRID_PEXELS: "混合：Pexels 素材 + 知识卡片补齐",
}

# 讲解卡片配色（config.toml 的 app.svf_card_theme）
THEME_LABELS = {"classroom": "课堂蓝（浅色）", "dark": "深夜蓝（深色）", "paper": "暖纸（米色）"}
DEFAULT_THEME = "classroom"
THEME_CONFIG_KEY = "svf_card_theme"
