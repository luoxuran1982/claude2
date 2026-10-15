"""日志解读：把生成页下方的原始日志，按步骤翻译成中文，讲清楚现在在做什么、为什么、正常要多久、出错通常是什么原因。

纯 Python，不依赖上游和 Streamlit，方便单元测试；界面由 svf_ext/webui.py 的 render_log_explain 画。

输入是上游 webui_task.get_task_logs(task_id) 的快照：每条记录形如
    2026-10-15 08:26:53 | INFO | "./app/services/video.py:1556": generate_video - generating video: 1080 x 1920
一条记录可能有多行（上游常以 "\\n\\n## …" 开头），界面上的原始日志把记录用换行连起来、带行号显示，
所以这里按同样的方式算每条记录从第几行开始，解读里的「第 N 行」能直接对上原始日志。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

LINE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\s*\|\s*([A-Z]+)\s*\|\s*(?:\"[^\"]*\":\s*\S+\s*-\s*)?(.*)$", re.S)


@dataclass
class Step:
    key: str
    title: str
    what: str            # 在做什么
    why: str             # 为什么要做
    typical: str         # 正常大概要多久
    errors: str          # 出错通常是什么原因
    status: str = "pending"      # pending / running / done / skipped / failed
    start_line: int = 0
    start_time: str = ""
    end_time: str = ""
    notes: list = field(default_factory=list)   # [(行号, 时间, 说明)]

    def elapsed(self, now: str = "") -> float | None:
        a, b = _ts(self.start_time), _ts(self.end_time or now)
        return (b - a) if a is not None and b is not None and b >= a else None


STEPS = [
    ("script", "写文案",
     "把你填的视频主题和要求发给大模型，让它写一段口播文案。自己在「视频文案」框里写好了文案的话，这一步直接用你的文案，不调用大模型。",
     "后面的配音、字幕、画面都以这段文案为准。",
     "通常 5–30 秒，取决于模型和网络；推理模型（带「思考」的）可能要一两分钟。",
     "API Key 填错或余额不足（日志里常见 401、403、insufficient）、模型名写错（404、model not found）、"
     "接口地址不对、网络或代理连不上（timeout、Connection）。在「设置 → 大模型」点「测试连接」可以单独检查。"),
    ("terms", "提取关键词",
     "再请大模型从文案里提几个英文搜索词（比如 logic gate、circuit），用来去素材站搜视频。",
     "Pexels、Pixabay 这些素材站只认英文关键词；「知识讲解卡片」不需要搜素材，会跳过这一步。",
     "通常 3–15 秒。",
     "模型没按要求返回 JSON 数组（上游会自动重试几次）、API 或网络问题（同「写文案」）。"),
    ("audio", "配音",
     "把文案交给配音服务（默认微软 Edge 在线朗读）合成成 mp3，同时拿到每个词的时间点。",
     "成片的长度由配音决定，字幕和画面都跟着配音的时间走。",
     "Edge 在线配音一般每 100 字 3–10 秒；文案越长越久。",
     "403（微软改了验证，需要升级应用）、No audio was received（选的声音和文案语言不一致，比如中文文案选了 zu-ZA 的声音；"
     "或代理节点被拒）、连不上 speech.platform.bing.com（网络、代理、防火墙）。"),
    ("subtitle", "字幕",
     "按配音返回的逐词时间，把文案切成一句句字幕，写成 subtitle.srt。",
     "字幕要和配音对得上，所以要等配音完成才能做。",
     "用 Edge 时间戳只要 1–2 秒；改用 whisper 识别要几十秒到几分钟（还要先下载模型）。",
     "选了 whisper 但没装可选组件或没下载模型；配音没有返回时间点时字幕可能为空。"),
    ("materials", "找素材或画卡片",
     "库存视频来源：用关键词去 Pexels / Pixabay 搜索并下载视频片段；「知识讲解卡片」：按字幕时间把文案分成若干页，由程序画出讲解页。",
     "画面要覆盖整段配音的时长。",
     "下载素材 30 秒到几分钟（看网速和片段数，已下过的会用缓存）；画知识卡片几秒到十几秒。",
     "素材钥匙没填或填错（401、400）、搜不到素材、网络或代理问题（可在「设置 → 素材来源」填素材下载代理）；"
     "卡片很少出错，出错多半是磁盘满或字体文件缺失。"),
    ("clips", "处理片段",
     "把每段素材裁成统一的画面比例和分辨率、截到合适长度、按需要变速，各自重新编码成小片段。",
     "素材来自不同作者，尺寸、帧率、编码都不一样，统一以后才能接起来。",
     "每段 1–5 秒，片段越多越久。知识卡片走快速通道时没有这一步。",
     "素材文件损坏或格式特殊（failed to process clip）、磁盘空间不足。"),
    ("concat", "拼接",
     "用 FFmpeg 把处理好的片段按顺序接成一整条画面（combined-1.mp4）。",
     "最终渲染要的是一整条和配音一样长的画面。",
     "几秒到一分钟。知识卡片走快速通道时没有这一步。",
     "磁盘空间不足、片段文件被占用或已被删除。"),
    ("render", "最终渲染",
     "把画面、配音、背景音乐合在一起，把字幕一帧帧画上去，再编码成成片 final-1.mp4。"
     "知识卡片快速通道：直接把讲解页（字幕已画进页面）交给 FFmpeg，一次编码出成片。",
     "这一步才产出能发布的视频。上游用 MoviePy 逐帧合成，是整个流程里最慢的一步。",
     "库存视频：大约是成片时长的 2–6 倍（50 秒的竖屏视频常要 2–5 分钟，取决于电脑）；知识卡片快速通道：大约是成片时长的 0.3–1 倍。"
     "「still running」那几行只是「还在跑」的报平安，不是出错。",
     "字幕字体缺失或不支持中文、硬件编码器不可用（会自动退回软件编码）、内存不足、磁盘空间不足、任务被取消。"),
]
STEP_INDEX = {k: i for i, (k, *_rest) in enumerate(STEPS)}

# 每条规则：(正则, 步骤, 动作, 说明模板)。动作：start 开始这一步、done 这一步完成、skip 跳过、note 只加一条说明。
RULES = [
    (r"## generating video script", "script", "start", "开始写文案"),
    (r"generating video script: subject=", "script", "note", "把主题发给大模型写文案"),
    (r"failed to generate (?:video )?script", "script", "note", "大模型这次没写成功，上游会自动重试"),
    (r"## generating video terms", "terms", "start", "开始提取搜索关键词"),
    (r"failed to generate video terms", "terms", "note", "这次没拿到关键词，上游会自动重试"),
    (r"## generating audio", "audio", "start", "开始配音"),
    (r"start, voice name: (?P<v>\S+), try: (?P<n>\d+)", "audio", "note", "用声音 {v} 合成配音（第 {n} 次尝试）"),
    (r"no custom audio file provided, using TTS", "audio", "note", "没有上传自己的配音，用在线朗读合成"),
    (r"## generating subtitle", "subtitle", "start", "配音完成，开始做字幕"),
    (r"subtitle file created", "subtitle", "done", "字幕文件 subtitle.srt 写好了"),
    (r"## (?:downloading videos from (?P<src>\S+)|preprocess local materials)", "materials", "start", "开始准备画面素材（{src}）"),
    (r"searching videos on (?P<src>\w+): term='?(?P<t>[^'\n]*)", "materials", "note", "在 {src} 搜索「{t}」"),
    (r"found (?P<n>\d+) videos for '(?P<t>[^']*)'", "materials", "note", "「{t}」搜到 {n} 个视频"),
    (r"downloaded material (?P<a>\d+)/(?P<b>\d+)", "materials", "note", "下载素材 {a}/{b}"),
    (r"downloaded (?P<n>\d+) (?:ordered )?videos", "materials", "done", "素材下载完成，共 {n} 段"),
    (r"知识卡片：(?P<n>\d+) 页", "materials", "start", "知识讲解卡片：文案分成 {n} 页，开始画讲解页"),
    (r"知识卡片视频已生成", "materials", "done", "讲解页视频画好了"),
    (r"知识卡片快速通道：讲解页已画好", "materials", "done", "讲解页画好了（快速通道，下一步直接出成片）"),
    (r"## combining video", "clips", "start", "开始处理片段"),
    (r"audio duration: (?P<d>[\d.]+) seconds", "clips", "note", "配音时长 {d} 秒，画面要至少这么长"),
    (r"processed clip (?P<n>\d+): (?P<a>[\d.]+) of (?P<b>[\d.]+)s covered", "clips", "note", "第 {n} 段处理完，画面已覆盖 {a} / {b} 秒"),
    (r"failed to process clip", "clips", "note", "有一段素材处理失败（会跳过它继续）"),
    (r"starting clip merging process|concatenating (?P<n>\d+) clips with ffmpeg", "concat", "start", "开始拼接片段"),
    (r"ffmpeg concat still running: elapsed=(?P<s>\d+)s", "concat", "note", "仍在拼接，已用 {s} 秒"),
    (r"video combining completed", "concat", "done", "拼接完成"),
    (r"## generating video: ", "render", "start", "开始最终渲染：合成画面、配音、背景音乐和字幕"),
    (r"## 知识卡片快速通道", "render", "start", "知识卡片快速通道：一次合成画面、配音、背景音乐和字幕"),
    (r"知识卡片快速通道：编码器 (?P<c>\S+)", "render", "note", "视频编码器：{c}"),
    (r"(?P<what>final video render|知识卡片快速通道) still running: elapsed=(?P<s>\d+)s", "render", "note", "仍在渲染，已用 {s} 秒"),
    (r"知识卡片快速通道：成片", "render", "done", "成片写好了"),
    (r"(?:目标时长 \d+ 秒|实际配音 [\d.]+ 秒)", "audio", "note", "{_raw}"),
]
RULES = [(re.compile(p), step, act, tpl) for p, step, act, tpl in RULES]
FINISH_RE = re.compile(r"task \S+ finished, generated (\d+) videos")
FAIL_RE = re.compile(r"task failed, task_id: \S+, stage: (?P<stage>\w+), error: (?P<err>.*)", re.S)
CANCEL_RE = re.compile(r"用户取消了任务|已取消（在任务管理里")
STAGE_TO_STEP = {"script": "script", "terms": "terms", "audio": "audio", "subtitle": "subtitle", "materials": "materials",
                 "video": "render", "preflight": "script", "pipeline": None, "webui_worker": None, "cancelled": None}
STAGE_LABEL = {"preflight": "开工前检查", "pipeline": "流水线", "webui_worker": "后台线程"}


def _ts(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S").timestamp()
    except (TypeError, ValueError):
        return None


def parse(records: list[str]) -> list[dict]:
    """[(起始行号, 时间, 级别, 消息)]：行号和界面上原始日志（记录用换行连起来、带行号显示）一致。"""
    out = []
    line = 1
    for rec in records:
        text = str(rec).rstrip()
        m = LINE_RE.match(text.lstrip("\n"))
        if m:
            out.append({"line": line, "time": m.group(1), "level": m.group(2), "msg": m.group(3).strip()})
        else:
            out.append({"line": line, "time": "", "level": "", "msg": text.strip()})
        line += text.count("\n") + 1
    return out


def explain(records: list[str], extra: dict | None = None) -> dict:
    """返回 {"steps": [Step...], "current": 步骤 key 或 None, "state": running/done/failed/cancelled, "summary", "warnings": [...]}。
    extra 可带：source（视频来源）、user_script（是否用自己的文案）、target_seconds / actual_seconds 等。"""
    extra = extra or {}
    steps = [Step(*s) for s in STEPS]
    by = {s.key: s for s in steps}
    entries = parse(records)
    state, current, fail = "running", None, None
    warnings = []
    last_time = ""

    def start(key, e, text):
        nonlocal current
        s = by[key]
        idx = STEP_INDEX[key]
        for prev in steps[:idx]:          # 之前没出现的步骤：跳过了（如知识卡片不提取关键词）
            if prev.status == "pending":
                prev.status = "skipped"
            elif prev.status == "running":
                prev.status, prev.end_time = "done", e["time"]
        if s.status in ("pending", "skipped"):
            s.status, s.start_line, s.start_time = "running", e["line"], e["time"]
        if not s.notes or s.notes[-1][2] != text:
            s.notes.append((e["line"], e["time"], text))
        current = key

    for e in entries:
        msg = e["msg"]
        if e["time"]:
            last_time = e["time"]
        matched = False
        for rx, key, act, tpl in RULES:
            m = rx.search(msg)
            if not m:
                continue
            matched = True
            vals = {k: (v if v is not None else "") for k, v in m.groupdict().items()}
            vals["_raw"] = msg.splitlines()[0][:200]
            text = tpl.format(**vals).replace("（）", "")
            if act == "start":
                start(key, e, text)
            elif act == "done":
                if by[key].status == "pending":
                    start(key, e, text)
                else:
                    by[key].notes.append((e["line"], e["time"], text))
                by[key].status, by[key].end_time = "done", e["time"]
            else:
                if by[key].status == "pending" and key != "audio":
                    start(key, e, text)
                else:
                    by[key].notes.append((e["line"], e["time"], text))
            break
        if FINISH_RE.search(msg):
            state = "done"
            for s in steps:
                if s.status == "running":
                    s.status, s.end_time = "done", e["time"]
                elif s.status == "pending":
                    s.status = "skipped"
            current = None
            continue
        fm = FAIL_RE.search(msg)
        if fm:
            fail = (fm.group("stage"), fm.group("err").strip().splitlines()[0][:300], e)
            continue
        if CANCEL_RE.search(msg):
            state = "cancelled"
            if current:
                by[current].notes.append((e["line"], e["time"], "用户取消了任务，停在这一步"))
            continue
        if not matched and e["level"] in ("WARNING", "ERROR", "CRITICAL"):
            warnings.append((e["line"], e["time"], e["level"], msg.splitlines()[0][:300]))
            if current:
                by[current].notes.append((e["line"], e["time"], ("警告：" if e["level"] == "WARNING" else "出错：")
                                          + msg.splitlines()[0][:200]))

    # 用自己写的文案时，上游也会打 "## generating video script"，但不调用大模型
    if extra.get("user_script") and by["script"].status != "pending":
        by["script"].notes.append((by["script"].start_line, by["script"].start_time, "用的是你自己写的文案，没有调用大模型"))

    if fail and state != "cancelled":
        stage, err, e = fail
        key = STAGE_TO_STEP.get(stage) or current
        state = "failed"
        if key:
            s = by[key]
            if s.status == "pending":
                s.start_line, s.start_time = e["line"], e["time"]
            s.status, s.end_time = "failed", e["time"]
            s.notes.append((e["line"], e["time"], f"失败：{err}"))
        current = key
    elif state == "cancelled" and current:
        by[current].status, by[current].end_time = "failed", last_time
    if state == "running" and current:
        cur = by[current]
        cur.end_time = ""
    summary = _summary(state, by.get(current) if current else None, last_time, fail, extra)
    return {"steps": steps, "current": current, "state": state, "summary": summary, "warnings": warnings,
            "last_time": last_time, "entries": len(entries)}


def _summary(state, cur: Step | None, last_time: str, fail, extra: dict) -> str:
    if state == "done":
        return "已完成：成片已生成。"
    if state == "cancelled":
        return "已取消：停在「" + (cur.title if cur else "当前步骤") + "」。"
    if state == "failed":
        stage = fail[0] if fail else ""
        where = cur.title if cur else STAGE_LABEL.get(stage, stage)
        return f"在「{where}」这一步失败了：{fail[1] if fail else ''}。常见原因见下面这一步的说明。"
    if cur:
        el = cur.elapsed(last_time)
        return f"现在：{cur.title}" + (f"（这一步已用 {el:.0f} 秒）" if el is not None else "") + f"。{cur.what}"
    return "正在准备…"


def duration_line(extra: dict) -> str:
    """「目标多少秒、实际多少秒」。"""
    t, a = extra.get("target_seconds"), extra.get("actual_seconds")
    parts = []
    if t:
        parts.append(f"目标时长 {int(t)} 秒" + (f"（按约 {extra['chars_per_second']:.1f} 字/秒，请大模型写约 {extra['target_chars']} 字）"
                                              if extra.get("target_chars") and extra.get("chars_per_second") else ""))
    if a:
        s = f"实际配音 {float(a):.1f} 秒"
        if extra.get("script_chars"):
            s += f"（文案 {extra['script_chars']} 字，约 {extra['script_chars'] / max(float(a), 0.1):.1f} 字/秒）"
        if t:
            diff = float(a) - float(t)
            s += f"，比目标{'长' if diff > 0 else '短'} {abs(diff):.0f} 秒"
        parts.append(s)
    return "；".join(parts)
