"""准备用户数据目录：上游配置文件、任务目录、字体、背景音乐、日志。

第一次启动时：
- 用上游的 config.example.toml 生成 config.toml，并把界面语言设成中文、默认字幕字体设成随包的思源黑体
  （Noto Sans SC，SIL OFL 1.1）。以后不再改动这个文件，用户在界面里填的 API Key 都保存在这里。
- 把随包的开源字体复制到 fonts/（已存在且大小相同的跳过；用户自己放进去的字体不动）。
- 建好空的 songs/（背景音乐，用户自己放 mp3）和 storage/（生成的视频与缓存）。
"""
from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

from . import DEFAULT_FONT

SUBDIRS = ("storage", "fonts", "songs", "logs")
# 上游自带、许可证清楚（SIL OFL 1.1）的字体，也复制给用户选用
UPSTREAM_OFL_FONTS = ("BeVietnamPro-Bold.ttf", "BeVietnamPro-Medium.ttf", "Charm-Bold.ttf", "Charm-Regular.ttf")
SONGS_README = """把背景音乐（.mp3）放在这个文件夹里，生成视频时在「背景音乐」里选「随机」或指定文件即可。

请只放你有权使用的音乐，例如：
- YouTube 音频库（studio.youtube.com → 音频库）里标明可免费使用的曲目；
- Pixabay Music（pixabay.com/music）上的免版税音乐；
- 自己创作或已购买授权的音乐。

上游 MoneyPrinterTurbo 自带的 30 首默认音乐来源说不清（上游自述「来自 YouTube 视频，如有侵权请删除」），
本应用不随安装包分发。
"""
FONTS_README = """字幕字体放在这个文件夹（支持 .ttf / .ttc），放进去约 30 秒后在「字幕设置 → 字体」里就能选到。

随应用提供（SIL Open Font License 1.1，可免费商用，许可证见同目录 OFL-*.txt）：
- NotoSansSC-Bold.ttf  思源黑体（Noto Sans SC）粗体，默认字幕字体，支持简体中文
- BeVietnamPro-*.ttf、Charm-*.ttf  上游自带的拉丁/越南/泰文字体（不含中文字形）

想用系统里的字体（如 Windows 的微软雅黑 C:\\Windows\\Fonts\\msyh.ttc、macOS 的苹方/冬青黑体），
可自行复制到这里，仅限你自己的电脑使用；这些商业字体不随本应用分发。
"""


def ensure_dirs(data: Path) -> None:
    for sub in SUBDIRS:
        (data / sub).mkdir(parents=True, exist_ok=True)


def initial_config_text(example: str, font: str = DEFAULT_FONT) -> str:
    """在上游示例配置的基础上：界面语言中文、默认字幕字体用随包开源字体。[ui] 是示例里的最后一节。"""
    text = example.replace('\n# language = "zh"\n', '\nlanguage = "zh"\n', 1)
    parsed = tomllib.loads(text)
    ui = parsed.get("ui")
    if ui is None:
        text = text.rstrip("\n") + '\n\n[ui]\nlanguage = "zh"\n'
        ui = {"language": "zh"}
    elif "language" not in ui:
        raise ValueError("config.example.toml 的 [ui] 不是最后一节，无法安全追加默认值")
    if "font_name" not in ui:
        text = text.rstrip("\n") + f'\n\n# AI 短视频工厂：默认字幕字体（SIL OFL 1.1 开源字体，随应用提供）\nfont_name = "{font}"\n'
    check = tomllib.loads(text)
    assert check["ui"]["language"] == "zh" and check["ui"].get("font_name"), "生成的默认配置有误"
    return text


def ensure_config(data: Path, upstream: Path) -> bool:
    """config.toml 不存在时生成。返回是否新建。"""
    cfg = data / "config.toml"
    if cfg.exists():
        return False
    example = (upstream / "config.example.toml").read_text(encoding="utf-8")
    tmp = data / "config.toml.tmp"
    tmp.write_text(initial_config_text(example), encoding="utf-8")
    tmp.replace(cfg)
    return True


def _copy_if_changed(src: Path, dst: Path) -> bool:
    if dst.exists() and dst.stat().st_size == src.stat().st_size:
        return False
    shutil.copy2(src, dst)
    return True


def ensure_fonts(data: Path, bundled_fonts: Path, upstream: Path) -> list[str]:
    target = data / "fonts"
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    sources = [p for p in sorted(bundled_fonts.glob("*")) if p.suffix.lower() in (".ttf", ".ttc", ".txt")]
    sources += [upstream / "resource" / "fonts" / n for n in UPSTREAM_OFL_FONTS if (upstream / "resource" / "fonts" / n).exists()]
    for src in sources:
        if _copy_if_changed(src, target / src.name):
            copied.append(src.name)
    readme = target / "字体说明.txt"
    if not readme.exists():
        readme.write_text(FONTS_README, encoding="utf-8")
    return copied


def ensure_songs(data: Path) -> None:
    readme = data / "songs" / "背景音乐说明.txt"
    if not readme.exists():
        readme.write_text(SONGS_README, encoding="utf-8")


def prepare(data: Path, upstream: Path, bundled_fonts: Path) -> dict:
    ensure_dirs(data)
    created = ensure_config(data, upstream)
    fonts = ensure_fonts(data, bundled_fonts, upstream)
    ensure_songs(data)
    return {"config_created": created, "fonts_copied": fonts}


# ---------------- 外壳自己的设置（缩放等），与上游配置分开 ----------------

ZOOM_STEPS = tuple(range(70, 125, 5))   # 70%–120%，每 5% 一档，共 11 档


def clamp_zoom(value) -> int:
    try:
        v = int(round(float(value) / 5.0) * 5)
    except (TypeError, ValueError):
        return 100
    return min(max(v, ZOOM_STEPS[0]), ZOOM_STEPS[-1])


class ShellSettings:
    def __init__(self, data: Path):
        self.path = data / "shell.json"
        self.values = {"zoom": 100}
        try:
            self.values.update(json.loads(self.path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            pass
        self.values["zoom"] = clamp_zoom(self.values.get("zoom", 100))

    def update(self, changes: dict) -> dict:
        if "zoom" in changes:
            self.values["zoom"] = clamp_zoom(changes["zoom"])
        if "guide_page" in changes and isinstance(changes["guide_page"], str):
            self.values["guide_page"] = changes["guide_page"][:300]
        if "tab" in changes and changes["tab"] in ("studio", "guide", "status"):
            self.values["tab"] = changes["tab"]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.values, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)
        return self.values
