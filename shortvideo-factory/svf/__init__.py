"""AI 短视频工厂（ShortVideoFactory）：把开源项目 MoneyPrinterTurbo 包成可安装的桌面应用。

本包只放「外壳」：启动器、窗口、反向代理、使用指南、运行状态、缩放。视频生成全部由
upstream/ 里的 MoneyPrinterTurbo 完成（Streamlit WebUI 作为子进程运行）。
"""

APP_NAME = "AI 短视频工厂"
APP_ID = "ShortVideoFactory"
__version__ = "1.0.3"

# 上游来源（升级上游时同步修改，并更新 UPSTREAM.md）
UPSTREAM_NAME = "MoneyPrinterTurbo"
UPSTREAM_URL = "https://github.com/harry0703/MoneyPrinterTurbo"
UPSTREAM_COMMIT = "bbe7eb9"
UPSTREAM_VERSION = "1.3.8"

# 随包分发的默认字幕字体（SIL OFL 1.1，见 fonts/OFL-NotoSansSC.txt）
DEFAULT_FONT = "NotoSansSC-Bold.ttf"

# 默认配音声音（Edge TTS 晓晓，普通话女声）。新用户、以及配置里还没有 voice_name 时写入 config.toml 的 [ui]；
# 用户在界面里选过别的声音就不再改动。上游自己的兜底是「按界面语言挑第一个匹配的声音」。
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural-Female"
