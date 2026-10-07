"""把资讯匹配到监控对象：名称、别名、代码；支持排除词。"""

from __future__ import annotations

import re


def _term_pattern(term: str):
    if re.fullmatch(r"\d{4,6}", term):
        return re.compile(r"(?<!\d)" + re.escape(term) + r"(?!\d)")
    if re.fullmatch(r"[A-Za-z0-9 .&'-]+", term):
        # 英文名/美股代码按单词边界匹配；全大写的短代码区分大小写，避免 "ON"、"AI" 之类误伤
        flags = 0 if (term.isupper() and len(term) <= 4) else re.I
        return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", flags)
    return re.compile(re.escape(term), re.I)


class Matcher:
    def __init__(self, entities):
        self.rules = []
        for e in entities:
            if not e.get("enabled", 1):
                continue
            terms = [e["name"]] + [a for a in (e.get("aliases") or "").split(",") if a]
            code = (e.get("code") or "").strip()
            if code and (len(code) >= 4 or not code.isalpha()):
                terms.append(code)
            elif code and e.get("market") == "US":
                terms.append(code)  # 美股短代码如 "F"、"GM" 也允许，区分大小写
            excl = [x for x in (e.get("exclude") or "").split(",") if x]
            self.rules.append((e["id"], [_term_pattern(t) for t in dict.fromkeys(terms) if t], excl))

    def match(self, text: str) -> list[int]:
        out = []
        for eid, pats, excl in self.rules:
            if excl and any(x in text for x in excl):
                continue
            if any(p.search(text) for p in pats):
                out.append(eid)
        return out
