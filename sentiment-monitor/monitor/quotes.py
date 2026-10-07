"""行情：腾讯财经公开行情接口 qt.gtimg.cn（A 股 / 港股 / 美股），只用来在监控对象旁显示涨跌。"""

from __future__ import annotations

import re
import time

from . import netutil


def tencent_code(e: dict) -> str:
    code = (e.get("code") or "").strip().upper()
    market = (e.get("market") or "").upper()
    if not code:
        return ""
    if market == "A" and code.isdigit() and len(code) == 6:
        if code.startswith(("6", "5", "9")):
            return "sh" + code
        if code.startswith(("4", "8")) or code.startswith("92"):
            return "bj" + code
        return "sz" + code
    if market == "HK" and code.isdigit():
        return "hk" + code.zfill(5)
    if market == "US" and re.fullmatch(r"[A-Z.]+", code):
        return "us" + code
    return ""


def parse(text: str) -> dict:
    out = {}
    for m in re.finditer(r'v_(\w+)="([^"]*)"', text):
        f = m.group(2).split("~")
        if len(f) < 5:
            continue
        try:
            price, prev = float(f[3]), float(f[4])
        except ValueError:
            continue
        chg = price - prev if prev else 0.0
        out[m.group(1)] = {
            "name": f[1], "price": price, "prev_close": prev, "change": round(chg, 4),
            "pct": round(chg / prev * 100, 2) if prev else 0.0, "time": f[30] if len(f) > 30 else "",
        }
    return out


class QuoteCache:
    def __init__(self):
        self.data = {}
        self.ts = 0
        self.error = ""

    def get(self, entities, max_age=30):
        codes = {e["id"]: tencent_code(e) for e in entities}
        codes = {k: v for k, v in codes.items() if v}
        if not codes:
            return {}
        if time.time() - self.ts > max_age or not set(codes.values()) <= set(self.data):
            try:
                text = netutil.fetch("https://qt.gtimg.cn/q=" + ",".join(sorted(set(codes.values()))), timeout=8)
                self.data = parse(text)
                self.error = ""
            except Exception as e:  # noqa: BLE001
                self.error = str(e)
            self.ts = time.time()
        return {eid: self.data[c] for eid, c in codes.items() if c in self.data}
