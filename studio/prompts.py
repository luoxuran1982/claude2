"""提示词。系统提示词固定不变（放在最前面才能命中服务商的前缀缓存），变化的内容都放在用户消息里。"""

from .textutil import CHARS_PER_MINUTE

SYSTEM = """你是知识类视频的编剧兼分镜师。你的输出会被程序直接解析：只输出一个 JSON 对象，不要输出解释、不要 Markdown。

## 画面类型 type 与 data 字段
- title：标题卡，用于开场和章节开头。data: {"subtitle": "副标题"}
- bullets：要点列表，随口播逐条出现。data: {"items": ["要点", ...]}，2–5 条，每条不超过 16 字
- keyword：关键词卡，用于抽象概念。data: {"keyword": "不超过 8 字", "note": "不超过 24 字的补充"}
- stat：一个关键数字。data: {"value": "73%", "label": "不超过 16 字的说明"}
- code：代码片段。data: {"language": "python", "code": "不超过 12 行", "highlight": [要强调的行号]}
- flow：流程或因果链。data: {"nodes": ["步骤", ...]}，2–6 个节点，每个不超过 8 字
- chart：数据图。data: {"chart": "bar 或 line", "labels": [...], "values": [数字, ...], "unit": "单位", "source": "数据来源"}
- compare：两栏对比。data: {"left_title": "", "left": [...], "right_title": "", "right": [...]}
- table：表格。data: {"headers": [...], "rows": [[...], ...]}，不超过 4 列 5 行
- quote：引语或作者观点。data: {"text": "不超过 40 字", "source": "出处"}
- image：配图。只在 category 为 concrete 时使用。data: {"caption": "不超过 16 字"}

## category：这一镜讲的是什么
- concrete：看得见、拍得到的具体事物（芯片、机房、工厂流水线）。只有它可以用 image，并且必须填写 image_query
- abstract：抽象概念 → 用 keyword、bullets、flow、compare
- data：数字和统计 → 用 stat、chart、table
- entity：人名、机构、产品名 → 用 keyword，不配图（避免肖像和商标问题）

## 写作规则
1. narration 是口播原文，口语化，每镜 1–4 句，每句不超过 30 字。画面上的字是提纲，不要和口播逐字重复。
2. 第一镜用 title，口播是一句抓人的开场钩子；最后一镜收尾，总结一句并引出下一期。
3. 作者观点必须用第一人称融入，至少有一镜用 quote 呈现作者观点。这是平台判断“有作者原创见解”的依据。
4. 只使用参考资料里的数字和事实；资料没有、你又不确定的数字，不要写，也不要做成 chart。
5. 相邻镜头不要连续 3 次使用同一种 type，整体类型要有变化。
6. 英文缩写第一次出现时给一句中文解释。
7. 合规：不对具体股票、基金、币种给出买卖建议、目标价或涨跌预测；不承诺收益；不引导加群、开户、私信；不提供疾病诊断和用药建议；不贬损具体企业和个人。
8. image_query：只有 concrete 镜头填写，用 8–12 个英文单词描述画面（例如 "close-up of a computer motherboard with CPU socket"）；其他镜头填空字符串。
9. short：为 60–90 秒的竖版精简版挑选镜头，选中的镜头口播合计约 280–420 字，并且连起来能独立看懂。

## 输出 JSON 结构
{
  "title": "视频标题，不超过 24 字",
  "cover_titles": ["5 个候选封面标题，每个不超过 14 字，不夸张、不标题党"],
  "description": "视频简介 80–150 字",
  "tags": ["5–8 个标签"],
  "scenes": [
    {"type": "...", "category": "...", "heading": "画面标题，不超过 14 字", "narration": "口播", "data": {}, "image_query": "", "short": false}
  ],
  "claims": [{"text": "稿中的一条事实性陈述", "confidence": "high 或 medium 或 low"}]
}
claims 要列出稿中所有具体事实（数字、年份、人名、定义、因果结论），把握不足的标 low。"""


def brief_block(brief):
    minutes = float(brief.get("minutes") or 3)
    chars = int(minutes * CHARS_PER_MINUTE)
    scenes = max(4, int(round(minutes * 4.5)))
    parts = [
        f"主题：{brief.get('topic') or '（未填写）'}",
        f"受众：{brief.get('audience') or '对该主题感兴趣的普通观众'}",
        f"目标时长：约 {minutes:g} 分钟，口播总字数约 {chars} 字，分镜约 {scenes} 个。",
    ]
    if brief.get("series"):
        parts.append(f"系列/栏目：{brief['series']}")
    if brief.get("style"):
        parts.append(f"风格要求：{brief['style']}")
    parts.append("作者观点（必须用第一人称融入稿件）：\n" + (brief.get("views") or "（作者没有提供观点：请在结尾写一句中立的思考，并在 claims 里不要编造作者经历）"))
    if brief.get("materials"):
        parts.append("参考资料（事实以此为准）：\n" + brief["materials"][:40000])
    else:
        parts.append("参考资料：无。只写你有把握的通用知识，不要编造具体数字。")
    return "\n\n".join(parts)


def single_call(brief):
    return [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": brief_block(brief) + "\n\n请输出完整的 JSON。"},
    ]


def outline_call(brief, n_chapters):
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": brief_block(brief)
            + f"""

这是长视频，分两步生成。第一步只输出大纲 JSON，结构如下，scenes 和 claims 先不写：
{{"title": "", "cover_titles": [], "description": "", "tags": [],
  "chapters": [{{"heading": "章节标题", "points": ["本章要讲的要点"], "chars": 本章口播字数}}]}}
分 {n_chapters} 章，各章字数之和等于总字数。""",
        },
    ]


def chapter_call(brief, outline, index):
    ch = outline["chapters"][index]
    first = index == 0
    last = index == len(outline["chapters"]) - 1
    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": brief_block(brief)
            + "\n\n全片大纲：\n"
            + "\n".join(f"{i + 1}. {c.get('heading')}：{'；'.join(c.get('points') or [])}" for i, c in enumerate(outline["chapters"]))
            + f"""

现在只写第 {index + 1} 章「{ch.get('heading')}」，口播约 {ch.get('chars')} 字。
{'这是第一章，第一镜用 title 做全片开场。' if first else '本章第一镜用 title 作为章节标题卡。'}{'这是最后一章，最后一镜收尾。' if last else '不要写全片总结。'}
只输出：{{"scenes": [...], "claims": [...]}}""",
        },
    ]


def rewrite_scene_call(brief, title, scene, prev_text, next_text, instruction):
    import json

    return [
        {"role": "system", "content": SYSTEM},
        {
            "role": "user",
            "content": f"""视频：{title}
主题：{brief.get('topic')}
上一镜口播：{prev_text or '（无）'}
下一镜口播：{next_text or '（无）'}

当前分镜：
{json.dumps({k: scene[k] for k in ('type', 'category', 'heading', 'narration', 'data', 'image_query', 'short')}, ensure_ascii=False)}

修改要求：{instruction or '让口播更口语化、更准确，画面更贴合口播'}
只输出修改后的这一个分镜 JSON 对象（结构同 scenes 里的元素）。""",
        },
    ]


def fact_check_call(title, scenes, materials):
    script = "\n".join(f"[{i + 1}] {s['narration']}" for i, s in enumerate(scenes))
    return [
        {
            "role": "system",
            "content": "你是严谨的事实核查编辑。只输出 JSON。",
        },
        {
            "role": "user",
            "content": f"""逐条列出下面视频口播中的事实性陈述（数字、年份、人名、定义、因果结论），判断把握度，指出可能的错误。
输出：{{"claims": [{{"text": "陈述（注明镜头号）", "confidence": "high/medium/low", "note": "疑点或更正建议"}}]}}

标题：{title}

口播：
{script}

参考资料（如有）：
{(materials or '无')[:20000]}""",
        },
    ]
