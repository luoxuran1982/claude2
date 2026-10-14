"""运行上游 MoneyPrinterTurbo 自带的测试（upstream/test/），跳过在本项目里注定不能通过的少数几个。

    python tests/run_upstream_tests.py              # 需要先装 requirements-runtime.txt 和 pytest
    python tests/run_upstream_tests.py --with-ext   # 同时加载外壳扩展 svf_ext（补丁 P5、P6 生效，和应用里一样）

上游测试大多是离线的（网络请求都打了桩）。在容器里实测（2026-10-13，Python 3.11）：2043 个通过、54 个按上游条件跳过，
下面 DESELECT 里的 29 个（14 组）不能通过，原因只有三类，都和上游代码本身无关：
1. 测试写死了上游自带的商业字体（微软雅黑、华文黑体）——本项目按版权要求删掉了这些字体；
2. 测试写死了上游自带的背景音乐 output000.mp3 等——来源不明，本项目删掉了；
3. FFmpeg 版本相关：测试期望 FFmpeg 解码被截断的 MP3 时报错，系统 FFmpeg 6.1 和 imageio 自带的 7.0 都能容错解码。
运行时和应用一样把 IMAGEIO_FFMPEG_EXE 指向随包的 imageio-ffmpeg，配置文件放临时目录，不往仓库里写 config.toml。
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / "upstream"

COMMERCIAL_FONTS = [
    "test/services/test_cli.py::TestCli::test_prepare_cli_files_accepts_relative_and_absolute_materials",
    "test/services/test_cli.py::TestCli::test_prepare_cli_files_rejects_missing_material_before_task_start",
    "test/services/test_cli.py::TestCli::test_run_cli_returns_error_for_structured_task_failure",
    "test/services/test_cli.py::TestCli::test_run_cli_returns_error_when_task_fails",
    "test/services/test_subtitle_background_settings.py::TestSubtitleBackgroundSettings::test_wrap_text_keeps_closing_punctuation_with_text",
    "test/services/test_subtitle_color_preflight.py::test_http_preserves_supported_pillow_colors_and_unused_stroke",
    "test/services/test_subtitle_manual_wrap.py::test_manual_subtitle_lines_fit_without_rewrapping",
    "test/services/test_subtitle_manual_wrap.py::test_long_manual_line_wraps_without_splitting_the_next_line",
    "test/services/test_video.py::TestVideoService::test_generate_video_accepts_bundled_font_before_opening_media",
    "test/services/test_video.py::TestVideoService::test_multilingual_textclip_last_line_keeps_a_visible_bottom_margin",
    "test/services/test_video.py::TestVideoService::test_wrap_text",
    "test/services/test_video.py::TestVideoService::test_wrap_text_counts_existing_subtitle_line_breaks",
]
BUNDLED_SONGS = [
    "test/services/test_webui_settings_transfer.py::test_settings_preset_round_trip_preserves_builtin_bgm_filename",
]
FFMPEG_VERSION = [
    "test/services/test_remote_speech_complete_decode.py::test_accepted_audio_requires_full_decode_and_preserves_previous_export",
]
DESELECT = COMMERCIAL_FONTS + BUNDLED_SONGS + FFMPEG_VERSION
# --with-ext：加载外壳扩展 svf_ext（和应用里一样），检查补丁 P5、P6 接上以后上游测试仍通过。
# 下面两项是 1.0.2 有意改变的行为，加载 svf_ext 时跳过：视频来源下拉多了「程序生成」组；试听失败提示后面接了中文原因。
EXT_CHANGED = [
    "test/services/test_webui_grouped_select.py::test_grouped_video_source_keeps_groups_and_accessible_label_binding",
    "test/services/test_webui_voice_preview.py::test_full_preview_reports_when_tts_returns_no_audio",
]


def main() -> int:
    with_ext = "--with-ext" in sys.argv[1:]
    tmp = Path(tempfile.mkdtemp(prefix="mpt-test-"))
    env = dict(os.environ, MPT_CONFIG_FILE=str(tmp / "config.toml"), PYTHONDONTWRITEBYTECODE="1")
    if with_ext:
        env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    else:   # 不加载外壳扩展：P5、P6 退回上游原行为
        env.pop("PYTHONPATH", None)
    try:
        import imageio_ffmpeg
        env.setdefault("IMAGEIO_FFMPEG_EXE", imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as exc:  # noqa: BLE001
        print("没有 imageio-ffmpeg：", exc)
    args = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-o", "addopts=", "test"]
    deselect = DESELECT + (EXT_CHANGED if with_ext else [])
    for node in deselect:
        args += ["--deselect", node]
    print(f"跳过 {len(deselect)} 组（商业字体 {len(COMMERCIAL_FONTS)}、自带音乐 {len(BUNDLED_SONGS)}、FFmpeg 版本 {len(FFMPEG_VERSION)}"
          + (f"、加载 svf_ext 后有意改变的行为 {len(EXT_CHANGED)}）" if with_ext else "）") + ("，加载外壳扩展 svf_ext" if with_ext else ""), flush=True)
    return subprocess.call(args, cwd=UPSTREAM, env=env)


if __name__ == "__main__":
    sys.exit(main())
