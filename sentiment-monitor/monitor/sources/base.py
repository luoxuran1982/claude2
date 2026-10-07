"""数据源基类与通用解析工具。"""

from __future__ import annotations

import html
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

CN_TZ_OFFSET = 8 * 3600  # 国内接口返回的 "YYYY-MM-DD HH:MM:SS" 都是北京时间


class Source:
    type = ""
    label = ""            # 类型名称（界面显示）
    desc = ""             # 说明
    per_entity = False    # True：按每个监控对象分别查询
    params_spec = []      # [{"key","label","placeholder","default"}]，界面据此生成表单

    def fetch(self, params: dict, entities: list[dict]) -> list[dict]:
        """返回 [{title, summary, url, media, published_at, entity_ids?}]"""
        raise NotImplementedError

    @classmethod
    def info(cls):
        return {"type": cls.type, "label": cls.label, "desc": cls.desc, "per_entity": cls.per_entity, "params": cls.params_spec}


def strip_html(s: str) -> str:
    s = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", s or "", flags=re.S | re.I)
    s = re.sub(r"<br\s*/?>|</p>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v　]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def parse_time(v) -> int:
    """把各种时间格式转成 Unix 秒。解析失败返回当前时间。"""
    now = int(time.time())
    if v is None or v == "":
        return now
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
        t = int(float(v))
        if t > 10**12:  # 毫秒
            t //= 1000
        return t if 946684800 < t < now + 86400 else now
    s = str(v).strip()
    try:
        return int(parsedate_to_datetime(s).timestamp())  # RFC 822（RSS）
    except (TypeError, ValueError, IndexError):
        pass
    try:
        iso = s.replace("Z", "+00:00")
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}(:\d{2})?(\.\d+)?", iso):
            # 无时区：按北京时间
            dt = datetime.fromisoformat(iso.replace(" ", "T")).replace(tzinfo=timezone.utc)
            return int(dt.timestamp()) - CN_TZ_OFFSET
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp()) - CN_TZ_OFFSET
        return int(dt.timestamp())
    except ValueError:
        pass
    m = re.match(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?", s)
    if m:
        y, mo, d, hh, mm, ss = (int(x or 0) for x in m.groups())
        return int(datetime(y, mo, d, hh, mm, ss, tzinfo=timezone.utc).timestamp()) - CN_TZ_OFFSET
    return now


def entity_query(e: dict) -> str:
    return (e.get("name") or "").strip()
