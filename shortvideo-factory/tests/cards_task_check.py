"""离线跑通一个「知识讲解卡片」短任务：上游完整流水线（文案 → 配音 → 字幕 → 素材 → 拼接 → 字幕烧录 → 成片），产出 mp4。

不联网、不需要任何钥匙：
- 文案用固定的一段（params.video_script），不调大模型；
- 配音把上游的 voice.tts 换成本地假实现：用 FFmpeg 生成一段低音量正弦波 mp3，按每个字均匀给出逐词时间（edge-tts 的 SubMaker），
  于是上游照常用它生成 subtitle.srt，知识卡片按字幕时间分页；
- 背景音乐关闭。其余（知识卡片画图和编码、上游的拼接、字幕烧录、合成）都是真实执行。

用法（cwd 必须是上游目录，MPT_CONFIG_FILE / MPT_STORAGE_DIR / MPT_FONT_DIR 指向临时目录和字体目录）：

    python cards_task_check.py <svf_ext 所在目录> [knowledge_cards|hybrid_pixabay]

最后一行输出 RESULT {json}。由 tests/test_cards.py（源码）和 packaging/smoke.py（安装包里的随包 Python）调用。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

sys.path.insert(0, os.getcwd())
if len(sys.argv) > 1:
    sys.path.insert(1, sys.argv[1])

SCRIPT = ("逻辑门是数字电路的基本单元。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。非门则把输入取反。"
          "计算机先把信号转成二进制，再交给逻辑门运算，最后输出结果。")
SECONDS_PER_CHAR = 0.16


def fake_tts(text, voice_name, voice_rate, voice_file, **kwargs):
    """假配音：时长按字数算，逐字给出时间。"""
    from edge_tts import SubMaker
    from app.utils import utils
    chars = [c for c in text if c.strip()]
    duration = max(1.0, len(chars) * SECONDS_PER_CHAR + 0.4)
    os.makedirs(os.path.dirname(voice_file), exist_ok=True)
    subprocess.run([utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=440:sample_rate=24000:duration={duration:.2f}", "-af", "volume=0.05",
                    "-c:a", "libmp3lame", "-b:a", "48k", voice_file], check=True)
    sub = SubMaker()
    t = 0.2
    for c in chars:
        if c.isalnum():
            sub.feed({"type": "WordBoundary", "offset": int(t * 1e7), "duration": int(SECONDS_PER_CHAR * 0.9 * 1e7), "text": c})
        t += SECONDS_PER_CHAR
    return sub


def main() -> None:
    source = sys.argv[2] if len(sys.argv) > 2 else "knowledge_cards"
    from app.models.schema import VideoParams
    from app.services import state as sm, task as tm, voice
    from app.utils import utils

    voice.tts = fake_tts
    task_id = str(uuid.uuid4())
    params = VideoParams(video_subject="逻辑门", video_script=SCRIPT, video_source=source, video_aspect="9:16",
                         voice_name="zh-CN-XiaoxiaoNeural-Female", bgm_type="", bgm_volume=0.0, subtitle_enabled=True,
                         font_name="NotoSansSC-Bold.ttf", n_threads=2, video_count=1, video_clip_duration=3,
                         video_terms="logic gate")
    t0 = time.time()
    result = tm.start(task_id, params, stop_at="video") or {}
    task = sm.state.get_task(task_id) or {}
    task_dir = utils.task_dir(task_id)
    videos = result.get("videos") or task.get("videos") or []
    info = {"source": source, "seconds": round(time.time() - t0, 1), "state": task.get("state"), "error": task.get("error"),
            "videos": videos, "task_dir": task_dir}
    if videos and os.path.isfile(videos[0]):
        from moviepy import VideoFileClip
        clip = VideoFileClip(videos[0])
        try:
            info.update(duration=round(clip.duration, 2), size=list(clip.size), has_audio=clip.audio is not None,
                        bytes=os.path.getsize(videos[0]))
        finally:
            clip.close()
    sb = os.path.join(task_dir, "knowledge-cards", "storyboard.json")
    if os.path.isfile(sb):
        pages = json.load(open(sb, encoding="utf-8"))["pages"]
        info["pages"] = [p["kind"] for p in pages]
    info["subtitle"] = os.path.isfile(os.path.join(task_dir, "subtitle.srt"))
    info["stills"] = len([f for f in os.listdir(os.path.join(task_dir, "knowledge-cards"))
                          if f.endswith(".png")]) if os.path.isdir(os.path.join(task_dir, "knowledge-cards")) else 0
    info["ok"] = bool(videos) and info.get("duration", 0) > 5 and info.get("has_audio", False)
    print("RESULT " + json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
