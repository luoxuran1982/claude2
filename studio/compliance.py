"""本地合规检查：规则匹配，不调用模型。只能提示风险，不能替代人工审片和法律意见。

规则来源：调研报告的“合规红线”和补充二、三。
"""

import difflib
import re

from . import project as proj
from .textutil import review_terms

# (类别, 严重度, 正则, 说明)
RULES = [
    ("投资", "high", r"(?<!\d)(?:00|30|60|68)\d{4}(?!\d)|\b\d{4,5}\.HK\b|(?<![A-Za-z])\$[A-Z]{1,5}\b|[（(]\d{6}[)）]",
     "出现具体证券代码。没有投资咨询牌照不要分析具体标的；你持有的标的公开评价还会触及“抢帽子”。"),
    ("投资", "high", r"买入|卖出|加仓|减仓|满仓|抄底|逃顶|止盈|目标价|建仓|清仓|上车|梭哈|必涨|稳赚|翻倍股|涨停板|牛股|金股|荐股|代客理财",
     "出现买卖或荐股措辞。可以讲方法和原理，不要对具体标的给操作建议。"),
    ("投资", "high", r"保本|保收益|稳赚不赔|无风险|零风险|年化\s*\d+\s*%以上|收益率?高达",
     "承诺收益或回避风险的说法。"),
    ("引流", "high", r"加群|进群|微信群|私信我|加我微信|vx|VX|wx号|扫码|开户链接|返佣|邀请码",
     "引导到私域、开户或返佣，财经内容平台从严处置。"),
    ("加密货币", "high", r"币价|交易所|合约交易|杠杆交易|空投|挖矿收益|USDT|比特币.*(买|卖|涨|跌)",
     "虚拟货币相关业务境内一律禁止；讲技术原理可以，不讲币价和怎么买。"),
    ("医疗", "high", r"治愈|根治|疗效|药方|偏方|剂量|处方|包治|抗癌|降血压|降血糖",
     "医疗健康需要资质；不要给诊断、用药和疗效说法。"),
    ("广告法", "medium", r"最好的|最佳|第一品牌|全网第一|国家级|顶级|绝对|史上最|100%有效|万能",
     "绝对化用语，商业内容中容易违规。"),
    ("涉企", "medium", r"倒闭|暴雷|跑路|骗局|割韭菜|造假|财务舞弊",
     "点评具体企业时事实错误或贬损措辞会构成名誉侵权；涉企信息正在专项整治，事实逐条对照公告原文。"),
    ("时效", "low", r"今天|昨天|刚刚|最新消息|本周|这周",
     "时效性表述，视频长期挂着会过时；如必要请写明具体日期。"),
]


def check(project, recent_projects=None):
    issues = []
    for i, sc in enumerate(project.get("scenes", [])):
        text = " ".join(
            [sc.get("heading") or "", sc.get("narration") or ""]
            + [str(v) for v in (sc.get("data") or {}).values() if isinstance(v, (str, int, float))]
            + [str(x) for v in (sc.get("data") or {}).values() if isinstance(v, list) for x in v]
        )
        for cat, sev, pattern, msg in RULES:
            for m in re.finditer(pattern, text):
                issues.append({"scene": sc["id"], "index": i + 1, "category": cat, "severity": sev, "match": m.group(0), "message": msg})
                break
    issues += project_level(project, recent_projects or [])
    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: (order.get(x["severity"], 3), x.get("index", 0)))
    return issues


def project_level(p, recent_projects):
    out = []
    scenes = p.get("scenes", [])
    if not (p.get("brief", {}).get("views") or "").strip():
        out.append({"scene": "", "index": 0, "category": "原创性", "severity": "high", "match": "",
                    "message": "没有填写作者观点。YouTube 不让“没有作者见解的模板化 AI 内容”变现，国内平台原创审核也看这一点。请在创作页写 3–5 句你自己的看法或例子。"})
    elif not any(s["type"] == "quote" for s in scenes) and scenes:
        out.append({"scene": "", "index": 0, "category": "原创性", "severity": "low", "match": "",
                    "message": "没有 quote 镜头。建议至少一镜用引语卡呈现你的观点，观众听得出、看得到。"})
    if scenes:
        kinds = [s["type"] for s in scenes]
        top = max(set(kinds), key=kinds.count)
        if len(scenes) >= 6 and kinds.count(top) / len(kinds) > 0.5:
            out.append({"scene": "", "index": 0, "category": "重复内容", "severity": "medium", "match": top,
                        "message": f"超过一半镜头都是「{proj.SCENE_TYPES.get(top, top)}」，画面单一，容易被判模板化。"})
        seq = "".join(k[0] for k in kinds)
        for other in recent_projects:
            oseq = "".join(s.get("type", "k")[0] for s in other.get("scenes", []))
            if other.get("id") != p.get("id") and len(oseq) >= 6 and difflib.SequenceMatcher(None, seq, oseq).ratio() > 0.85:
                out.append({"scene": "", "index": 0, "category": "重复内容", "severity": "medium", "match": other.get("title", ""),
                            "message": f"镜头结构和《{other.get('title', '')}》高度相似。连着看几条结构一样，会被判“重复内容”。换一下镜头组合或主题版式。"})
                break
    low = [c for c in p.get("claims", []) if c.get("confidence") == "low" and not c.get("checked")]
    if low:
        out.append({"scene": "", "index": 0, "category": "事实核查", "severity": "high", "match": str(len(low)),
                    "message": f"有 {len(low)} 条低把握度的事实还没核对。知识类账号被指出硬伤，掉粉比娱乐号严重得多。"})
    if not p.get("ai_declared"):
        out.append({"scene": "", "index": 0, "category": "AI 标识", "severity": "medium", "match": "",
                    "message": "《人工智能生成合成内容标识办法》要求发布时主动声明 AI 内容。请在审片页勾选“发布时声明 AI 内容”，并在各平台发布页勾选 AI 声明。"})
    return out


def pronunciation_checklist(project, mapping):
    terms, nums = set(), set()
    for sc in project.get("scenes", []):
        a, b = review_terms(sc.get("narration"))
        terms.update(a)
        nums.update(b)
    return {
        "terms": [{"term": t, "mapped": mapping.get(t, "")} for t in sorted(terms)],
        "numbers": sorted(nums),
    }
