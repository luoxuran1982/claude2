"""情感打分：金融领域词典 + 否定词 + 程度副词 + 风险标签。

不依赖任何模型，速度快、可解释（每条资讯都记下命中的词）。
打分范围 [-1, 1]；标题权重是正文摘要的 2 倍。
想换成大模型或 SnowNLP，只要实现同样的 analyze(title, summary) -> dict 即可。
"""

from __future__ import annotations

import math
import re

# ---------------------------------------------------------------- 中文词典（词: 权重）
POS_ZH = {
    # 业绩
    "扭亏为盈": 2.5, "扭亏": 2, "超预期": 2, "大超预期": 2.5, "净利润增长": 2, "营收增长": 1.5, "业绩增长": 1.8,
    "同比增长": 1, "环比增长": 0.8, "创新高": 2, "创历史新高": 2.5, "历史新高": 2, "高增长": 1.8, "增长": 0.6,
    "预增": 2, "盈利": 1, "利润提升": 1.5, "毛利率提升": 1.5, "现金流改善": 1.5, "业绩亮眼": 2, "稳健": 0.8,
    # 行情
    "涨停": 2.5, "大涨": 2, "暴涨": 2.2, "飙升": 2, "走高": 1, "上涨": 1, "拉升": 1.2, "反弹": 1, "领涨": 1.5,
    "收涨": 1, "高开": 1, "突破": 1, "放量上涨": 1.8, "资金流入": 1.2, "主力净流入": 1.5, "北向资金买入": 1.5,
    "净买入": 1.2, "增持": 1.8, "回购": 1.5, "大手笔回购": 2, "举牌": 1, "抄底": 0.8, "强势": 1, "走强": 1, "翻倍": 1.5,
    # 评级/政策/经营
    "买入评级": 2, "上调评级": 2, "上调目标价": 2, "增持评级": 1.5, "推荐": 1, "看好": 1.5, "看涨": 1.5, "利好": 2,
    "重大利好": 2.5, "获批": 1.5, "中标": 1.5, "签约": 1, "大单": 1.2, "订单饱满": 1.8, "合作": 0.6, "战略合作": 1,
    "分红": 1.2, "高分红": 1.8, "派息": 1, "送转": 1, "降本增效": 1, "扩产": 1, "投产": 1, "量产": 1.2, "突破性": 1.5,
    "领先": 1, "龙头": 0.8, "市占率提升": 1.5, "供不应求": 1.8, "景气": 1.2, "复苏": 1.2, "回暖": 1.2, "提振": 1.2,
    "支持": 0.5, "鼓励": 0.8, "降准": 1.2, "降息": 1, "减税": 1.2, "补贴": 0.8, "护盘": 1, "纳入指数": 1.2,
    "解除": 0.5, "和解": 1, "胜诉": 1.5, "澄清": 0.5, "创新": 0.6, "首发": 0.8, "认可": 0.8, "受益": 1.2, "向好": 1.5,
    "改善": 1, "辟谣": 1.5, "加速": 0.6, "火爆": 1.2, "热销": 1.5, "畅销": 1.2,
}

NEG_ZH = {
    # 业绩
    "亏损": 2, "巨亏": 2.5, "首亏": 2.2, "预亏": 2.2, "续亏": 2, "净利润下降": 2, "业绩下滑": 2, "营收下滑": 1.8,
    "同比下降": 1, "同比下滑": 1.2, "下滑": 1, "下降": 0.7, "不及预期": 2, "低于预期": 1.8, "爆雷": 3, "暴雷": 3,
    "商誉减值": 2, "计提减值": 1.5, "资产减值": 1.5, "毛利率下降": 1.5, "现金流紧张": 2, "资金链断裂": 3, "资金链": 1,
    "债务逾期": 2.8, "逾期": 1.8, "违约": 2.5, "债务危机": 2.8, "破产": 3, "重整": 1.8, "清算": 2.5,
    # 行情
    "跌停": 2.5, "大跌": 2, "暴跌": 2.5, "闪崩": 2.5, "重挫": 2.2, "跳水": 2, "下跌": 1, "走低": 1, "收跌": 1,
    "低开": 1, "破发": 1.8, "跌破": 1.5, "新低": 1.8, "创新低": 2, "资金流出": 1.2, "主力净流出": 1.5, "净卖出": 1.2,
    "抛售": 1.8, "减持": 2, "清仓": 1.8, "质押": 1, "平仓": 2, "爆仓": 2.5, "走弱": 1, "承压": 1.2, "腰斩": 2.2, "缩水": 1.5,
    # 监管/法律
    "立案调查": 3, "立案": 2.2, "被调查": 2.5, "行政处罚": 2.5, "处罚": 2, "罚款": 2, "警示函": 2, "问询函": 1.5,
    "关注函": 1.2, "监管函": 1.8, "通报批评": 2, "公开谴责": 2.5, "造假": 3, "财务造假": 3, "欺诈": 3, "违规": 2,
    "违法": 2.5, "内幕交易": 2.5, "操纵": 2.2, "诉讼": 1.5, "起诉": 1.5, "仲裁": 1.2, "冻结": 2, "被执行": 2,
    "失信": 2.5, "退市": 3, "暂停上市": 3, "终止上市": 3, "风险警示": 2.2, "留置": 2.5, "被查": 2.5, "带走": 1.5,
    # 经营
    "停产": 2, "停工": 1.8, "裁员": 1.8, "召回": 2, "事故": 2, "爆炸": 2.5, "火灾": 2, "泄露": 1.5, "污染": 1.8,
    "投诉": 1.2, "维权": 1.5, "质量问题": 2, "暴露": 0.8, "丑闻": 2.5, "辞职": 1, "离职": 0.8, "失联": 2.5,
    "制裁": 2, "禁令": 1.8, "限制": 0.8, "加征关税": 1.8, "关税": 0.8, "风险": 0.8, "隐患": 1.2, "危机": 2,
    "下调评级": 2, "下调目标价": 2, "卖出评级": 2, "减持评级": 1.5, "看空": 1.5, "做空": 1.8, "利空": 2, "重大利空": 2.5,
    "低迷": 1.5, "疲软": 1.5, "萎缩": 1.5, "过剩": 1.2, "价格战": 1.5, "降价": 0.8, "恶化": 2, "担忧": 1.2, "恐慌": 1.8,
    "下行": 1, "衰退": 1.8, "缩量": 0.6, "加息": 1, "收紧": 1, "流失": 1.2, "失败": 1.5, "终止": 1.2, "取消": 1,
    "延期": 0.8, "否决": 1.5, "驳回": 1.2, "警告": 1.5, "严重": 0.8,
}

NEGATIONS = ["不", "未", "没有", "没", "无", "非", "并非", "并未", "尚未", "不会", "否认", "难以", "未能", "免于", "排除", "不存在", "暂无", "未见"]
DEGREE = {
    "大幅": 1.5, "显著": 1.4, "明显": 1.3, "急剧": 1.6, "严重": 1.4, "持续": 1.2, "再度": 1.2, "继续": 1.1, "全面": 1.3,
    "巨额": 1.5, "大额": 1.3, "罕见": 1.4, "史上最": 1.6, "极度": 1.6, "超": 1.2,
    "小幅": 0.6, "略": 0.6, "微": 0.5, "稍": 0.6, "轻微": 0.5, "有望": 0.7, "或将": 0.7, "可能": 0.7, "传闻": 0.6,
}

# 风险标签：命中即在资讯上打标签，并触发风险预警（被否定时不打）
RISK_TAGS = {
    "监管处罚": ["立案调查", "立案", "被调查", "行政处罚", "处罚", "罚款", "警示函", "问询函", "监管函", "关注函", "通报批评", "公开谴责", "留置", "被查"],
    "财务风险": ["爆雷", "暴雷", "财务造假", "造假", "商誉减值", "巨亏", "首亏", "预亏", "债务逾期", "违约", "资金链断裂", "破产", "重整", "清算"],
    "股东减持": ["减持", "清仓", "平仓", "爆仓", "质押", "冻结"],
    "经营风险": ["停产", "停工", "召回", "事故", "爆炸", "裁员", "质量问题", "丑闻", "失联", "制裁", "禁令"],
    "法律诉讼": ["诉讼", "起诉", "仲裁", "被执行", "失信", "内幕交易", "操纵", "欺诈"],
    "退市风险": ["退市", "暂停上市", "终止上市", "风险警示", "*ST"],
}

# ---------------------------------------------------------------- 英文
POS_EN = {
    "surge": 2, "surges": 2, "soar": 2, "soars": 2, "jump": 1.5, "jumps": 1.5, "rally": 1.5, "rallies": 1.5, "gain": 1, "gains": 1,
    "rise": 1, "rises": 1, "beat": 1.8, "beats": 1.8, "upgrade": 2, "upgraded": 2, "outperform": 1.8, "bullish": 1.8,
    "record": 1.2, "growth": 1, "profit": 1, "profits": 1, "strong": 1, "boost": 1.2, "boosts": 1.2, "buyback": 1.5,
    "dividend": 1, "approval": 1.5, "approved": 1.5, "wins": 1.2, "win": 1, "partnership": 0.8, "expands": 0.8, "breakthrough": 1.8,
    "optimistic": 1.5, "upbeat": 1.5, "rebound": 1.2, "recovers": 1.2, "higher": 0.6, "top": 0.6, "tops": 1,
}
NEG_EN = {
    "plunge": 2.2, "plunges": 2.2, "plummet": 2.2, "tumble": 2, "tumbles": 2, "slump": 2, "slumps": 2, "crash": 2.5,
    "fall": 1, "falls": 1, "drop": 1, "drops": 1, "decline": 1, "declines": 1, "slide": 1.2, "slides": 1.2, "sink": 1.5, "sinks": 1.5,
    "miss": 1.8, "misses": 1.8, "downgrade": 2, "downgraded": 2, "underperform": 1.8, "bearish": 1.8, "loss": 1.5, "losses": 1.5,
    "lawsuit": 1.8, "sued": 1.8, "probe": 2, "investigation": 2, "fraud": 3, "fine": 1.5, "fined": 2, "penalty": 1.8,
    "bankruptcy": 3, "default": 2.5, "recall": 2, "layoff": 1.8, "layoffs": 1.8, "cuts": 1, "warning": 1.5, "warns": 1.5,
    "weak": 1.2, "concern": 1, "concerns": 1, "fears": 1.2, "risk": 0.6, "ban": 1.8, "sanctions": 2, "tariff": 1, "tariffs": 1,
    "selloff": 2, "sell-off": 2, "lower": 0.6, "delisting": 3, "scandal": 2.5, "halt": 1.5, "halts": 1.5,
}
NEG_WORDS_EN = {"not", "no", "never", "without", "denies", "deny", "didn't", "doesn't", "won't", "isn't", "fails"}

RISK_EN = {
    "probe": "监管处罚", "investigation": "监管处罚", "fined": "监管处罚", "penalty": "监管处罚",
    "fraud": "财务风险", "bankruptcy": "财务风险", "default": "财务风险",
    "recall": "经营风险", "layoffs": "经营风险", "sanctions": "经营风险", "ban": "经营风险",
    "lawsuit": "法律诉讼", "sued": "法律诉讼", "delisting": "退市风险",
}

_LEX = {}
for _w, _v in POS_ZH.items():
    _LEX[_w] = _v
for _w, _v in NEG_ZH.items():
    _LEX[_w] = -_v
_MAXLEN = max(len(w) for w in _LEX)
_RISK_INDEX = {w: tag for tag, words in RISK_TAGS.items() for w in words}
assert all(w in _LEX for w in _RISK_INDEX if w != "*ST"), "风险词必须也在情感词典里"


def _scan_zh(text: str):
    """正向最大匹配找情感词，检查前面 6 个字以内的否定词和程度词。返回 [(词, 分值)]。"""
    hits = []
    i, n = 0, len(text)
    while i < n:
        for L in range(min(_MAXLEN, n - i), 0, -1):
            w = text[i:i + L]
            if w in _LEX:
                v = _LEX[w]
                ctx = text[max(0, i - 6):i]
                # 遇到标点就截断上下文，否定词不跨句
                ctx = re.split(r"[，,。；;！!？?、\s]", ctx)[-1]
                neg = any(ctx.endswith(x) or x in ctx[-4:] for x in NEGATIONS)
                for d, m in DEGREE.items():
                    if d in ctx:
                        v *= m
                        break
                if neg:
                    v = -v * 0.6
                hits.append((w, v, neg))
                i += L
                break
        else:
            i += 1
    return hits


def _scan_en(text: str):
    hits = []
    toks = re.findall(r"[a-z][a-z'-]*", text.lower())
    for k, t in enumerate(toks):
        v = POS_EN.get(t) or (-NEG_EN[t] if t in NEG_EN else None)
        if v is None:
            continue
        neg = any(p in NEG_WORDS_EN for p in toks[max(0, k - 3):k])
        if neg:
            v = -v * 0.6
        hits.append((t, v, neg))
    return hits


def _scan(text: str):
    if not text:
        return []
    return _scan_zh(text) + _scan_en(text)


def risk_tags(text: str, hits=None) -> list[str]:
    """风险标签只来自没被否定的命中词（所有风险词都在词典里），外加 *ST 这类代码标记。"""
    tags = []
    if re.search(r"\*ST|（ST|\bST[\u4e00-\u9fa5]", text or ""):
        tags.append("退市风险")
    for w, _, neg in hits or []:
        tag = _RISK_INDEX.get(w) or RISK_EN.get(w)
        if tag and not neg and tag not in tags:
            tags.append(tag)
    return tags


def analyze(title: str, summary: str = "", pos_th=0.15, neg_th=-0.15) -> dict:
    th = _scan(title or "")
    sh = _scan((summary or "")[:600])
    raw = 2 * sum(v for _, v, _ in th) + sum(v for _, v, _ in sh)
    score = math.tanh(raw / 4)
    label = "pos" if score >= pos_th else "neg" if score <= neg_th else "neu"
    allhits = th + sh
    words = []
    for w, v, neg in allhits:
        s = f"{'否定·' if neg else ''}{w}{'+' if v > 0 else '-'}"
        if s not in words:
            words.append(s)
    return {
        "score": round(score, 3),
        "label": label,
        "risk": ",".join(risk_tags(f"{title} {summary or ''}", allhits)),
        "hits": " ".join(words[:12]),
    }
