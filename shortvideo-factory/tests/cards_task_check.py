"""离线跑通一个「知识讲解卡片」短任务：上游完整流水线（文案 → 配音 → 字幕 → 素材 → 拼接 → 字幕烧录 → 成片），产出 mp4。

不联网、不需要任何钥匙：
- 文案用固定的一段（params.video_script），不调大模型；
- 配音把上游的 voice.tts 换成本地假实现：用 FFmpeg 生成一段低音量正弦波 mp3，按每个字均匀给出逐词时间（edge-tts 的 SubMaker），
  于是上游照常用它生成 subtitle.srt，知识卡片按字幕时间分页；
- 背景音乐关闭。其余（知识卡片画图和编码、上游的拼接、字幕烧录、合成）都是真实执行。

用法（cwd 必须是上游目录，MPT_CONFIG_FILE / MPT_STORAGE_DIR / MPT_FONT_DIR 指向临时目录和字体目录）：

    python cards_task_check.py <svf_ext 所在目录> [knowledge_cards|hybrid_pixabay] [--long] [--bgm] [--landscape]

--long 用约 50 秒的长文案（用来测出片用时，见 docs/02「知识卡片快速通道」的实测）；--bgm 生成一段本地背景音乐并开启混音；
--landscape 用 16:9 横屏；--static 讲解页不缩放平移；--legacy 关掉快速通道（1.0.2 的流程，用来对比用时）。

1.1.0：来源 knowledge_cards_ai（AI 导演）/ knowledge_cards_illus（卡片 + AI 插图）时，起一个本机的假大模型服务
（OpenAI 兼容 /v1/chat/completions：按提示词里的编号句子返回固定结构的导演 JSON；/v1/images/generations：返回一张 PNG），
把上游的大模型和文生图设置指向它；--director-bad 让假导演两次都返回不合格的 JSON，检查退回规则切分。

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
LONG_SCRIPT = ("逻辑门是数字电路的基本单元，也是计算机里最小的“判断员”。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。"
               "非门则把输入取反，1变0，0变1。学习逻辑门有三个要点：第一，记住真值表。第二，会画电路符号。第三，能把门组合成加法器。"
               "计算机处理数据，先把信号转成二进制，再交给逻辑门运算，最后输出结果。晶体管的开关速度可以达到每秒10亿次。"
               "CPU里有数十亿个晶体管，而早期的计算机只有几千个电子管。常见的门电路包括与门、或门、非门和异或门。"
               "把这些门组合起来，就能做出加法器、存储器，乃至整个CPU。下次看到电脑在飞快地计算，你就知道，它其实是在做无数次简单的判断。")
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


def _make_bgm(path: str, seconds: float = 20.0) -> None:
    from app.utils import utils
    os.makedirs(os.path.dirname(path), exist_ok=True)
    subprocess.run([utils.get_ffmpeg_binary(), "-v", "error", "-y", "-f", "lavfi", "-i",
                    f"sine=frequency=220:sample_rate=44100:duration={seconds:.1f}", "-c:a", "libmp3lame", "-b:a", "96k", path],
                   check=True)


# ---------------------------------------------------------------- 假大模型（AI 导演）和假文生图

def fake_director_json(prompt: str, bad: bool = False) -> str:
    """按提示词里的「[n] 句子」返回一份合法的导演 JSON：第 1 句标题，之后每两句一页，版式轮流用要点、表格、流程、代码、对比、引言。"""
    import re as _re
    if bad:
        return "我觉得这样分镜比较好：第一页讲定义……（没有 JSON）"
    n = len(_re.findall(r"^\[(\d+)\] ", prompt, flags=_re.M))
    illus = "illustrate" in prompt and "最多" in prompt and "image_prompt 里用" in prompt
    pages = [{"sentences": [1], "type": "title", "heading": "逻辑门是什么", "data": {"subtitle": "数字电路的基本单元"},
              "illustrate": illus, "image_prompt": "a glowing logic gate circuit on a dark blue background" if illus else ""}]
    kinds = [
        ("bullets", "三种基本逻辑门", {"items": ["与门 AND", "或门 OR", "非门 NOT"]}),
        ("table", "三种门的规则", {"headers": ["门", "什么时候输出 1"], "rows": [["与门", "两个输入都是 1"], ["或门", "有一个是 1"], ["非门", "输入是 0"]]}),
        ("flow", "计算机怎样运算", {"nodes": ["转成二进制", "逻辑门运算", "输出结果"]}),
        ("code", "用 Python 模拟", {"language": "python", "code": "a, b = 1, 0\nprint(a and b)  # 与门\nprint(a or b)   # 或门", "highlight": [2]}),
        ("compare", "晶体管与电子管", {"left_title": "晶体管", "left": ["数十亿个"], "right_title": "电子管", "right": ["几千个"]}),
        ("quote", "一句话记住", {"text": "电脑其实是在做无数次简单的判断", "source": ""}),
    ]
    k, i = 0, 2
    while i <= n:
        kind, head, data = kinds[k % len(kinds)]
        pages.append({"sentences": list(range(i, min(n, i + 1) + 1)), "type": kind, "heading": head, "data": data,
                      "illustrate": False, "image_prompt": ""})
        i += 2
        k += 1
    return json.dumps({"title": "逻辑门入门", "pages": pages}, ensure_ascii=False)


def start_fake_llm(bad: bool = False):
    import base64
    import io
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from PIL import Image, ImageDraw
    seen = {"chat": 0, "images": 0}

    class Fake(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            if self.path.endswith("/images/generations"):
                seen["images"] += 1
                img = Image.new("RGB", (512, 768), (30, 60, 120))
                ImageDraw.Draw(img).ellipse((100, 200, 412, 512), fill=(240, 180, 60))
                buf = io.BytesIO()
                img.save(buf, "PNG")
                out = {"created": 1, "data": [{"b64_json": base64.b64encode(buf.getvalue()).decode()}]}
            else:
                seen["chat"] += 1
                prompt = body["messages"][-1]["content"]
                text = fake_director_json(prompt, bad)
                out = {"id": "chatcmpl-d", "object": "chat.completion", "created": 1, "model": body.get("model"),
                       "choices": [{"index": 0, "message": {"role": "assistant", "content": text,
                                                            "reasoning_content": "先看有几句，再决定每页的版式。"},
                                    "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 1200, "completion_tokens": 300, "total_tokens": 1500}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    srv = HTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    from app.config import config
    base = f"http://127.0.0.1:{srv.server_port}/v1"
    config.app.update({"llm_provider": "openai", "openai_api_key": "sk-svfcheck-DIRECTOR123456789", "openai_base_url": base,
                       "openai_model_name": "fake-director", "openai_image_base_url": base, "openai_image_model": "fake-image",
                       "openai_image_api_keys": ""})
    return seen


def main() -> None:
    args = [a for a in sys.argv[2:] if not a.startswith("--")]
    flags = {a for a in sys.argv[2:] if a.startswith("--")}
    source = args[0] if args else "knowledge_cards"
    from app.models.schema import VideoParams
    from app.services import state as sm, task as tm, voice
    from app.utils import utils

    voice.tts = fake_tts
    from app.config import config
    seen = None
    if source in ("knowledge_cards_ai", "knowledge_cards_illus"):
        seen = start_fake_llm(bad="--director-bad" in flags)
        import svf_ext.webui  # noqa: F401  接上大模型调用记录（和应用里一样）
    if "--static" in flags:
        config.app["svf_card_motion"] = False
    if "--legacy" in flags:
        config.app["svf_card_fast"] = False
    task_id = str(uuid.uuid4())
    bgm_type, bgm_file, bgm_volume = "", "", 0.0
    if "--bgm" in flags:
        from app.utils import utils as _u
        bgm_file = "svf-check-bgm.mp3"
        _make_bgm(os.path.join(_u.song_dir(), bgm_file))
        bgm_type, bgm_volume = "custom", 0.2
    params = VideoParams(video_subject="逻辑门", video_script=LONG_SCRIPT if "--long" in flags else SCRIPT,
                         video_source=source, video_aspect="16:9" if "--landscape" in flags else "9:16",
                         voice_name="zh-CN-XiaoxiaoNeural-Female", bgm_type=bgm_type, bgm_file=bgm_file,
                         bgm_volume=bgm_volume, subtitle_enabled=True,
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
    man = os.path.join(task_dir, "svf_storyboard.json")
    if os.path.isfile(man):
        m = json.load(open(man, encoding="utf-8"))
        info["manifest"] = {"mode": m.get("mode"), "theme": m.get("theme"), "kinds": [p["kind"] for p in m["pages"]],
                            "headings": [p["heading"] for p in m["pages"]],
                            "images": sum(1 for p in m["pages"] if p.get("image") and os.path.isfile(os.path.join(task_dir, p["image"]))),
                            "thumbs": sum(1 for p in m["pages"] if p.get("thumb") and os.path.isfile(os.path.join(task_dir, p["thumb"]))),
                            "illustrations": sum(1 for p in m["pages"] if p.get("illustration")),
                            "fallback": (m.get("director") or {}).get("fallback_reason", ""),
                            "contiguous": all(abs(a["end"] - b["start"]) < 0.02 for a, b in zip(m["pages"], m["pages"][1:]))}
    if seen is not None:
        from svf_ext import llmlog
        calls = llmlog.read_task(task_id)
        info["llm_calls"] = [(c["step"], c.get("ok"), len(c.get("use_notes") or [])) for c in calls]
        info["fake_server"] = seen
    info["stills"] = len([f for f in os.listdir(os.path.join(task_dir, "knowledge-cards"))
                          if f.endswith(".png")]) if os.path.isdir(os.path.join(task_dir, "knowledge-cards")) else 0
    info["ok"] = bool(videos) and info.get("duration", 0) > 5 and info.get("has_audio", False)
    print("RESULT " + json.dumps(info, ensure_ascii=False))


if __name__ == "__main__":
    main()
