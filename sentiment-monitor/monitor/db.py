"""SQLite 存储层：监控对象、数据源、资讯、预警、设置。单连接 + 锁，WAL 模式。"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    market TEXT NOT NULL DEFAULT '',      -- A / HK / US / 空
    kind TEXT NOT NULL DEFAULT 'stock',   -- stock / topic / company / person
    aliases TEXT NOT NULL DEFAULT '',     -- 逗号分隔
    exclude TEXT NOT NULL DEFAULT '',     -- 排除词，逗号分隔
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    name TEXT NOT NULL,
    params TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    interval_min REAL NOT NULL DEFAULT 10,
    last_run INTEGER NOT NULL DEFAULT 0,
    last_ok INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    last_count INTEGER NOT NULL DEFAULT 0,
    last_new INTEGER NOT NULL DEFAULT 0,
    total INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    source_id INTEGER,
    source_name TEXT NOT NULL DEFAULT '',
    media TEXT NOT NULL DEFAULT '',
    published_at INTEGER NOT NULL,
    fetched_at INTEGER NOT NULL,
    score REAL NOT NULL DEFAULT 0,
    label TEXT NOT NULL DEFAULT 'neu',    -- pos / neu / neg
    risk TEXT NOT NULL DEFAULT '',        -- 风险标签，逗号分隔
    hits TEXT NOT NULL DEFAULT '',        -- 命中的情感词，便于解释打分
    title_hash TEXT NOT NULL UNIQUE,
    url_hash TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_articles_pub ON articles(published_at DESC);
CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source_id);
CREATE TABLE IF NOT EXISTS article_entities (
    article_id INTEGER NOT NULL,
    entity_id INTEGER NOT NULL,
    PRIMARY KEY (article_id, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_ae_entity ON article_entities(entity_id);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id INTEGER,
    article_id INTEGER,
    kind TEXT NOT NULL,                   -- negative / risk / spike
    level TEXT NOT NULL,                  -- warning / danger
    message TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    is_read INTEGER NOT NULL DEFAULT 0,
    UNIQUE (entity_id, article_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_alerts_created ON alerts(created_at DESC);
"""

DEFAULT_SETTINGS = {
    "auto_exit": True,          # 关闭页面后自动退出后台
    "exit_grace_sec": 10,       # 页面全部断开多少秒后退出（留给刷新页面的时间）
    "browser": "auto",          # auto / edge / chrome / default / none
    "app_window": True,         # Edge/Chrome 用独立应用窗口打开
    "proxy": "",                # 例：http://127.0.0.1:7890，访问 Google 等需要
    "timeout": 15,
    "retention_days": 30,
    "store_unmatched": True,    # 没命中监控对象的快讯也保存（作为市场快讯）
    "pos_threshold": 0.15,
    "neg_threshold": -0.15,
    "alert_negative": True,
    "alert_negative_score": -0.4,
    "alert_risk": True,
    "alert_spike": True,
    "spike_min": 5,
    "spike_ratio": 3.0,
    "color_scheme": "cn",       # cn 红涨绿跌 / us 绿涨红跌
    "notify_desktop": True,
    "notify_sound": False,
    "quote_refresh_sec": 30,
}

DEFAULT_SOURCES = [
    # type, name, params, enabled, interval_min
    ("sina_roll", "新浪财经·滚动新闻", {"lid": "2516"}, 1, 5),
    ("eastmoney_724", "东方财富·7×24 快讯", {}, 1, 3),
    ("cls_telegraph", "财联社·电报", {}, 1, 3),
    ("eastmoney_search", "东方财富·资讯搜索（按监控对象）", {}, 1, 20),
    ("bing_news", "必应新闻 RSS（按监控对象）", {"mkt": "zh-CN"}, 1, 30),
    ("google_news", "Google 新闻 RSS（按监控对象，需能访问 Google）", {"hl": "zh-CN", "gl": "CN"}, 0, 30),
    ("yahoo_finance", "Yahoo Finance RSS（美股代码）", {}, 1, 30),
    ("rss", "36氪", {"url": "https://36kr.com/feed"}, 1, 30),
    ("rss", "RSSHub 示例·雪球个股讨论（建议自建 RSSHub）", {"url": "https://rsshub.app/xueqiu/stock_comments/SH600519"}, 0, 60),
]

DEFAULT_ENTITIES = [
    # name, code, market, kind, aliases
    ("贵州茅台", "600519", "A", "stock", "茅台"),
    ("宁德时代", "300750", "A", "stock", "宁王,CATL"),
    ("腾讯控股", "00700", "HK", "stock", "腾讯,Tencent"),
    ("英伟达", "NVDA", "US", "stock", "NVIDIA,Nvidia,辉达"),
    ("人工智能", "", "", "topic", "大模型,AIGC,算力"),
]


def norm_title(title: str) -> str:
    t = re.sub(r"\s+-\s+[^-]{1,30}$", "", title or "")  # 去掉聚合源加的 " - 媒体名"
    return re.sub(r"[\W_]+", "", t).lower()


def h(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()


def norm_url(url: str) -> str:
    u = (url or "").strip()
    u = re.sub(r"#.*$", "", u)
    u = re.sub(r"[?&](utm_[a-z]+|spm|from|share_token)=[^&]*", "", u)
    return u.rstrip("/").lower()


class Store:
    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        with self.lock:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._seed()

    def close(self):
        with self.lock:
            self.conn.close()

    # ------------------------------------------------------------ 工具
    def _q(self, sql, args=()):
        with self.lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    def _one(self, sql, args=()):
        rows = self._q(sql, args)
        return rows[0] if rows else None

    def _x(self, sql, args=()):
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def _seed(self):
        if self.kv_get("seeded"):
            return
        now = int(time.time())
        with self.lock:
            for t, name, params, en, iv in DEFAULT_SOURCES:
                self.conn.execute(
                    "INSERT INTO sources(type,name,params,enabled,interval_min) VALUES(?,?,?,?,?)",
                    (t, name, json.dumps(params, ensure_ascii=False), en, iv),
                )
            for name, code, market, kind, aliases in DEFAULT_ENTITIES:
                self.conn.execute(
                    "INSERT INTO entities(name,code,market,kind,aliases,created_at) VALUES(?,?,?,?,?,?)",
                    (name, code, market, kind, aliases, now),
                )
            self.conn.commit()
        self.kv_set("seeded", "1")

    # ------------------------------------------------------------ 设置
    def kv_get(self, key, default=None):
        r = self._one("SELECT value FROM kv WHERE key=?", (key,))
        return r["value"] if r else default

    def kv_set(self, key, value):
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def settings(self) -> dict:
        cfg = dict(DEFAULT_SETTINGS)
        try:
            saved = json.loads(self.kv_get("settings", "{}"))
        except json.JSONDecodeError:
            saved = {}
        cfg.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
        return cfg

    def save_settings(self, patch: dict) -> dict:
        cfg = self.settings()
        for k, v in (patch or {}).items():
            if k not in DEFAULT_SETTINGS:
                continue
            d = DEFAULT_SETTINGS[k]
            try:
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, int):
                    v = int(v)
                elif isinstance(d, float):
                    v = float(v)
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            cfg[k] = v
        self.kv_set("settings", json.dumps(cfg, ensure_ascii=False))
        return cfg

    # ------------------------------------------------------------ 监控对象
    def entities(self, enabled_only=False):
        sql = "SELECT * FROM entities" + (" WHERE enabled=1" if enabled_only else "") + " ORDER BY id"
        return self._q(sql)

    def save_entity(self, e: dict) -> dict:
        name = (e.get("name") or "").strip()
        if not name:
            raise ValueError("名称不能为空")
        vals = (
            name,
            (e.get("code") or "").strip().upper(),
            (e.get("market") or "").strip().upper(),
            (e.get("kind") or "stock").strip(),
            _clean_list(e.get("aliases")),
            _clean_list(e.get("exclude")),
            1 if e.get("enabled", True) else 0,
        )
        if e.get("id"):
            self._x("UPDATE entities SET name=?,code=?,market=?,kind=?,aliases=?,exclude=?,enabled=? WHERE id=?", vals + (int(e["id"]),))
            eid = int(e["id"])
        else:
            eid = self._x(
                "INSERT INTO entities(name,code,market,kind,aliases,exclude,enabled,created_at) VALUES(?,?,?,?,?,?,?,?)",
                vals + (int(time.time()),),
            ).lastrowid
        return self._one("SELECT * FROM entities WHERE id=?", (eid,))

    def delete_entity(self, eid: int):
        with self.lock:
            self.conn.execute("DELETE FROM entities WHERE id=?", (eid,))
            self.conn.execute("DELETE FROM article_entities WHERE entity_id=?", (eid,))
            self.conn.execute("DELETE FROM alerts WHERE entity_id=?", (eid,))
            self.conn.commit()

    # ------------------------------------------------------------ 数据源
    def sources(self):
        rows = self._q("SELECT * FROM sources ORDER BY id")
        for r in rows:
            r["params"] = json.loads(r["params"] or "{}")
        return rows

    def source(self, sid: int):
        r = self._one("SELECT * FROM sources WHERE id=?", (sid,))
        if r:
            r["params"] = json.loads(r["params"] or "{}")
        return r

    def save_source(self, s: dict) -> dict:
        name = (s.get("name") or "").strip()
        if not name or not s.get("type"):
            raise ValueError("名称和类型不能为空")
        params = s.get("params") or {}
        if isinstance(params, str):
            params = json.loads(params or "{}")
        vals = (s["type"], name, json.dumps(params, ensure_ascii=False), 1 if s.get("enabled", True) else 0, max(1.0, float(s.get("interval_min") or 10)))
        if s.get("id"):
            self._x("UPDATE sources SET type=?,name=?,params=?,enabled=?,interval_min=? WHERE id=?", vals + (int(s["id"]),))
            sid = int(s["id"])
        else:
            sid = self._x("INSERT INTO sources(type,name,params,enabled,interval_min) VALUES(?,?,?,?,?)", vals).lastrowid
        return self.source(sid)

    def delete_source(self, sid: int):
        self._x("DELETE FROM sources WHERE id=?", (sid,))

    def source_status(self, sid, ok: bool, error="", count=0, new=0):
        now = int(time.time())
        if ok:
            self._x(
                "UPDATE sources SET last_run=?,last_ok=?,last_error='',last_count=?,last_new=?,total=total+? WHERE id=?",
                (now, now, count, new, new, sid),
            )
        else:
            self._x("UPDATE sources SET last_run=?,last_error=?,last_count=0,last_new=0 WHERE id=?", (now, error[:500], sid))

    # ------------------------------------------------------------ 资讯
    def add_article(self, a: dict) -> int | None:
        """新文章返回 id；标题或链接重复返回 None。"""
        th = h(norm_title(a["title"]))
        uh = h(norm_url(a.get("url") or "") or "t:" + th)
        with self.lock:
            cur = self.conn.execute(
                """INSERT OR IGNORE INTO articles(title,summary,url,source_id,source_name,media,published_at,fetched_at,
                   score,label,risk,hits,title_hash,url_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    a["title"], a.get("summary", ""), a.get("url", ""), a.get("source_id"), a.get("source_name", ""),
                    a.get("media", ""), int(a["published_at"]), int(a.get("fetched_at") or time.time()),
                    float(a.get("score", 0)), a.get("label", "neu"), a.get("risk", ""), a.get("hits", ""), th, uh,
                ),
            )
            aid = cur.lastrowid if cur.rowcount else None
            if aid:
                for eid in a.get("entity_ids") or []:
                    self.conn.execute("INSERT OR IGNORE INTO article_entities(article_id,entity_id) VALUES(?,?)", (aid, eid))
            self.conn.commit()
        return aid

    def find_article_id(self, title: str, url: str = "") -> int | None:
        th = h(norm_title(title))
        r = self._one("SELECT id FROM articles WHERE title_hash=?", (th,))
        return r["id"] if r else None

    def link_entities(self, aid: int, eids):
        with self.lock:
            for eid in eids:
                self.conn.execute("INSERT OR IGNORE INTO article_entities(article_id,entity_id) VALUES(?,?)", (aid, eid))
            self.conn.commit()

    def articles(self, entity_id=None, label=None, source_id=None, q=None, since=None, until=None,
                 risk_only=False, matched_only=False, limit=50, offset=0):
        where, args = [], []
        if entity_id:
            where.append("a.id IN (SELECT article_id FROM article_entities WHERE entity_id=?)")
            args.append(int(entity_id))
        if matched_only:
            where.append("a.id IN (SELECT article_id FROM article_entities)")
        if label in ("pos", "neu", "neg"):
            where.append("a.label=?")
            args.append(label)
        if source_id:
            where.append("a.source_id=?")
            args.append(int(source_id))
        if q:
            where.append("(a.title LIKE ? OR a.summary LIKE ?)")
            args += [f"%{q}%", f"%{q}%"]
        if since:
            where.append("a.published_at>=?")
            args.append(int(since))
        if until:
            where.append("a.published_at<?")
            args.append(int(until))
        if risk_only:
            where.append("a.risk<>''")
        w = (" WHERE " + " AND ".join(where)) if where else ""
        total = self._one(f"SELECT COUNT(*) n FROM articles a{w}", args)["n"]
        rows = self._q(
            f"SELECT a.* FROM articles a{w} ORDER BY a.published_at DESC, a.id DESC LIMIT ? OFFSET ?",
            args + [int(limit), int(offset)],
        )
        self._attach_entities(rows)
        return {"total": total, "items": rows}

    def _attach_entities(self, rows):
        if not rows:
            return
        ids = [r["id"] for r in rows]
        marks = ",".join("?" * len(ids))
        links = self._q(
            f"SELECT ae.article_id, e.id, e.name FROM article_entities ae JOIN entities e ON e.id=ae.entity_id WHERE ae.article_id IN ({marks})",
            ids,
        )
        by = {}
        for l in links:
            by.setdefault(l["article_id"], []).append({"id": l["id"], "name": l["name"]})
        for r in rows:
            r["entities"] = by.get(r["id"], [])

    def article(self, aid):
        r = self._one("SELECT * FROM articles WHERE id=?", (aid,))
        if r:
            self._attach_entities([r])
        return r

    # ------------------------------------------------------------ 统计
    def overview(self, now=None):
        now = int(now or time.time())
        d1 = now - 86400
        r = self._one(
            """SELECT COUNT(*) n,
                      SUM(label='pos') pos, SUM(label='neg') neg, SUM(label='neu') neu,
                      AVG(score) avg, SUM(risk<>'') risk
               FROM articles WHERE published_at>=?""",
            (d1,),
        )
        prev = self._one("SELECT COUNT(*) n, AVG(score) avg FROM articles WHERE published_at>=? AND published_at<?", (d1 - 86400, d1))
        total = self._one("SELECT COUNT(*) n FROM articles")["n"]
        unread = self._one("SELECT COUNT(*) n FROM alerts WHERE is_read=0")["n"]
        return {
            "count_24h": r["n"] or 0, "pos_24h": r["pos"] or 0, "neg_24h": r["neg"] or 0, "neu_24h": r["neu"] or 0,
            "avg_24h": round(r["avg"] or 0, 3), "risk_24h": r["risk"] or 0,
            "count_prev": prev["n"] or 0, "avg_prev": round(prev["avg"] or 0, 3),
            "total": total, "unread_alerts": unread,
        }

    def trend(self, days=14, entity_id=None, tz_offset_min=0, now=None):
        """按天汇总（按浏览器时区分天）。返回 [{day, pos, neu, neg, avg}]。"""
        now = int(now or time.time())
        off = int(tz_offset_min) * 60
        start_local = ((now + off) // 86400 - (days - 1)) * 86400
        start = start_local - off
        ew = ""
        args = [off, start]
        if entity_id:
            ew = " AND id IN (SELECT article_id FROM article_entities WHERE entity_id=?)"
            args.append(int(entity_id))
        rows = self._q(
            f"""SELECT (published_at+?)/86400 d, SUM(label='pos') pos, SUM(label='neu') neu, SUM(label='neg') neg, AVG(score) avg
                FROM articles WHERE published_at>=?{ew} GROUP BY d""",
            args,
        )
        by = {r["d"]: r for r in rows}
        out = []
        for i in range(days):
            d = start_local // 86400 + i
            r = by.get(d, {})
            out.append({
                "day": time.strftime("%Y-%m-%d", time.gmtime(d * 86400)),
                "pos": r.get("pos") or 0, "neu": r.get("neu") or 0, "neg": r.get("neg") or 0,
                "avg": round(r["avg"], 3) if r.get("avg") is not None else None,
            })
        return out

    def source_dist(self, since):
        return self._q(
            "SELECT source_name name, COUNT(*) n FROM articles WHERE published_at>=? GROUP BY source_name ORDER BY n DESC LIMIT 12",
            (int(since),),
        )

    def entity_board(self, now=None):
        now = int(now or time.time())
        d1 = now - 86400
        rows = self._q(
            """SELECT e.*,
                 (SELECT COUNT(*) FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=e.id AND a.published_at>=?) n24,
                 (SELECT COUNT(*) FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=e.id AND a.published_at>=? AND a.published_at<?) nprev,
                 (SELECT AVG(a.score) FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=e.id AND a.published_at>=?) avg24,
                 (SELECT SUM(a.label='neg') FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=e.id AND a.published_at>=?) neg24,
                 (SELECT SUM(a.label='pos') FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=e.id AND a.published_at>=?) pos24,
                 (SELECT COUNT(*) FROM article_entities ae WHERE ae.entity_id=e.id) total
               FROM entities e ORDER BY e.id""",
            (d1, d1 - 86400, d1, d1, d1, d1),
        )
        for r in rows:
            r["avg24"] = round(r["avg24"], 3) if r["avg24"] is not None else None
            r["neg24"] = r["neg24"] or 0
            r["pos24"] = r["pos24"] or 0
        return rows

    def entity_counts(self, eid, since, until):
        r = self._one(
            "SELECT COUNT(*) n FROM article_entities ae JOIN articles a ON a.id=ae.article_id WHERE ae.entity_id=? AND a.published_at>=? AND a.published_at<?",
            (eid, int(since), int(until)),
        )
        return r["n"]

    # ------------------------------------------------------------ 预警
    def add_alert(self, entity_id, article_id, kind, level, message) -> int | None:
        with self.lock:
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO alerts(entity_id,article_id,kind,level,message,created_at) VALUES(?,?,?,?,?,?)",
                (entity_id, article_id, kind, level, message, int(time.time())),
            )
            self.conn.commit()
            return cur.lastrowid if cur.rowcount else None

    def last_alert_time(self, entity_id, kind):
        r = self._one("SELECT MAX(created_at) t FROM alerts WHERE entity_id=? AND kind=?", (entity_id, kind))
        return r["t"] or 0

    def alerts(self, limit=100, unread_only=False):
        w = " WHERE al.is_read=0" if unread_only else ""
        return self._q(
            f"""SELECT al.*, e.name entity_name, a.title, a.url, a.source_name, a.published_at, a.score
                FROM alerts al LEFT JOIN entities e ON e.id=al.entity_id LEFT JOIN articles a ON a.id=al.article_id
                {w} ORDER BY al.created_at DESC, al.id DESC LIMIT ?""",
            (int(limit),),
        )

    def alert(self, alert_id):
        rows = self._q(
            """SELECT al.*, e.name entity_name, a.title, a.url, a.source_name, a.published_at, a.score
               FROM alerts al LEFT JOIN entities e ON e.id=al.entity_id LEFT JOIN articles a ON a.id=al.article_id WHERE al.id=?""",
            (alert_id,),
        )
        return rows[0] if rows else None

    def mark_alerts_read(self, ids=None):
        if ids:
            marks = ",".join("?" * len(ids))
            self._x(f"UPDATE alerts SET is_read=1 WHERE id IN ({marks})", [int(i) for i in ids])
        else:
            self._x("UPDATE alerts SET is_read=1")

    # ------------------------------------------------------------ 维护
    def cleanup(self, retention_days: int):
        cutoff = int(time.time()) - max(1, int(retention_days)) * 86400
        with self.lock:
            self.conn.execute("DELETE FROM article_entities WHERE article_id IN (SELECT id FROM articles WHERE fetched_at<?)", (cutoff,))
            self.conn.execute("DELETE FROM alerts WHERE created_at<?", (cutoff,))
            n = self.conn.execute("DELETE FROM articles WHERE fetched_at<?", (cutoff,)).rowcount
            self.conn.commit()
        return n

    def clear_articles(self):
        with self.lock:
            self.conn.execute("DELETE FROM article_entities")
            self.conn.execute("DELETE FROM alerts")
            self.conn.execute("DELETE FROM articles")
            self.conn.execute("UPDATE sources SET total=0,last_new=0,last_count=0")
            self.conn.commit()


def _clean_list(v) -> str:
    if isinstance(v, (list, tuple)):
        items = v
    else:
        items = re.split(r"[,，;；\n]", v or "")
    return ",".join(dict.fromkeys(i.strip() for i in items if i.strip()))
