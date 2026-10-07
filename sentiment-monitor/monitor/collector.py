"""采集调度：后台线程按各数据源的间隔抓取 → 去重 → 情感分析 → 匹配监控对象 → 入库 → 预警 → 推送页面。"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from . import netutil, sentiment, sources
from .matcher import Matcher

log = logging.getLogger("monitor.collector")


class Collector:
    def __init__(self, store, bus):
        self.store = store
        self.bus = bus
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.running: set[int] = set()
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="fetch")
        self.thread = None
        self.last_cleanup = 0
        self.paused = False

    # ------------------------------------------------------------ 生命周期
    def start(self):
        self.apply_settings()
        self.thread = threading.Thread(target=self._loop, name="collector", daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        self.pool.shutdown(wait=False, cancel_futures=True)

    def apply_settings(self):
        cfg = self.store.settings()
        netutil.options["proxy"] = cfg["proxy"]
        netutil.options["timeout"] = cfg["timeout"]

    def run_now(self, source_id=None):
        """立即抓取：指定数据源，或全部启用的数据源。"""
        if source_id:
            self._submit(int(source_id))
        else:
            for s in self.store.sources():
                if s["enabled"]:
                    self._submit(s["id"])

    def _loop(self):
        time.sleep(1.5)
        while not self.stop_event.is_set():
            try:
                if not self.paused:
                    now = time.time()
                    for s in self.store.sources():
                        if s["enabled"] and now - s["last_run"] >= s["interval_min"] * 60:
                            self._submit(s["id"])
                    if now - self.last_cleanup > 3600:
                        self.last_cleanup = now
                        n = self.store.cleanup(self.store.settings()["retention_days"])
                        if n:
                            log.info("清理过期资讯 %d 条", n)
            except Exception:  # noqa: BLE001
                log.exception("调度出错")
            self.wake.wait(10)
            self.wake.clear()

    def _submit(self, sid):
        with self.lock:
            if sid in self.running:
                return
            self.running.add(sid)
        self.bus.publish("source", {"id": sid, "running": True})
        try:
            self.pool.submit(self._run_safe, sid)
        except RuntimeError:  # 线程池已关闭
            with self.lock:
                self.running.discard(sid)

    def _run_safe(self, sid):
        try:
            self.run_source(sid)
        finally:
            with self.lock:
                self.running.discard(sid)

    # ------------------------------------------------------------ 抓取一个数据源
    def run_source(self, sid) -> dict:
        s = self.store.source(sid)
        if not s:
            return {"ok": False, "error": "数据源不存在"}
        entities = self.store.entities(enabled_only=True)
        errors, items = [], []
        try:
            src = sources.get(s["type"])
            if src.per_entity:
                for e in entities:
                    if self.stop_event.is_set():
                        break
                    try:
                        items += src.fetch(s["params"], [e])
                    except Exception as ex:  # noqa: BLE001
                        errors.append(f"{e['name']}：{ex}")
                if errors and not items:
                    raise RuntimeError(_summarize(errors, len(entities)))
            else:
                items = src.fetch(s["params"], entities)
        except Exception as ex:  # noqa: BLE001
            msg = str(ex) or type(ex).__name__
            log.warning("数据源 %s 失败：%s", s["name"], msg)
            self.store.source_status(sid, False, msg)
            self.bus.publish("source", {"id": sid, "running": False, "ok": False, "error": msg})
            return {"ok": False, "error": msg}
        new = self.ingest(items, s)
        self.store.source_status(sid, True, count=len(items), new=len(new))
        if errors:
            self.store._x("UPDATE sources SET last_error=? WHERE id=?", ("部分失败：" + _summarize(errors, len(entities)), sid))
        self.bus.publish("source", {"id": sid, "running": False, "ok": True, "count": len(items), "new": len(new)})
        if new:
            self.bus.publish("articles", {"source": s["name"], "count": len(new), "items": new[:20]})
        log.info("数据源 %s：获取 %d 条，新增 %d 条", s["name"], len(items), len(new))
        return {"ok": True, "count": len(items), "new": len(new), "warning": _summarize(errors, len(entities)) if errors else ""}

    def ingest(self, items, source) -> list[dict]:
        """处理一批原始资讯，返回新增入库的（附带分析结果）。"""
        cfg = self.store.settings()
        entities = self.store.entities(enabled_only=True)
        matcher = Matcher(entities)
        names = {e["id"]: e["name"] for e in entities}
        now = int(time.time())
        new = []
        for it in items:
            title = (it.get("title") or "").strip()
            if not title:
                continue
            text = f"{title}\n{it.get('summary') or ''}"
            eids = list(dict.fromkeys((it.get("entity_ids") or []) + matcher.match(text)))
            eids = [e for e in eids if e in names]
            if not eids and not cfg["store_unmatched"]:
                continue
            res = sentiment.analyze(title, it.get("summary") or "", cfg["pos_threshold"], cfg["neg_threshold"])
            art = {
                **it, **res, "title": title, "source_id": source.get("id"), "source_name": source.get("name", ""),
                "published_at": min(int(it.get("published_at") or now), now), "fetched_at": now, "entity_ids": eids,
            }
            aid = self.store.add_article(art)
            if not aid:
                # 已有同标题资讯：补充关联对象（例如先从快讯抓到，后来搜索源又命中）
                old = self.store.find_article_id(title)
                if old and eids:
                    self.store.link_entities(old, eids)
                continue
            art["id"] = aid
            art["entities"] = [{"id": e, "name": names[e]} for e in eids]
            art.pop("entity_ids", None)
            new.append(art)
            self._alerts_for(art, cfg)
        if new:
            self._spike_check(cfg, {e["id"] for a in new for e in a["entities"]}, names)
        return new

    # ------------------------------------------------------------ 预警
    def _alerts_for(self, art, cfg):
        for ent in art["entities"]:
            if cfg["alert_risk"] and art["risk"]:
                self._alert(ent["id"], art["id"], "risk", "danger", f"【{ent['name']}】风险信号（{art['risk']}）：{art['title']}")
            elif cfg["alert_negative"] and art["score"] <= cfg["alert_negative_score"]:
                self._alert(ent["id"], art["id"], "negative", "warning", f"【{ent['name']}】负面舆情：{art['title']}")

    def _spike_check(self, cfg, eids, names):
        if not cfg["alert_spike"]:
            return
        now = int(time.time())
        for eid in eids:
            # 按发布时间统计，首次抓到的旧资讯不会被当成“刚刚爆发”
            n1 = self.store.entity_counts(eid, now - 3600, now + 1)
            base = self.store.entity_counts(eid, now - 86400, now - 3600) / 23.0
            if n1 >= cfg["spike_min"] and n1 >= cfg["spike_ratio"] * max(base, 0.5):
                if now - self.store.last_alert_time(eid, "spike") > 2 * 3600:
                    self._alert(eid, None, "spike", "warning", f"【{names[eid]}】热度异动：近 1 小时 {n1} 条资讯（前 23 小时平均每小时 {base:.1f} 条）")

    def _alert(self, eid, aid, kind, level, msg):
        alert_id = self.store.add_alert(eid, aid, kind, level, msg)
        if alert_id:
            self.bus.publish("alert", self.store.alert(alert_id))


def _summarize(errors: list[str], n_entities: int) -> str:
    """把 “对象：错误” 列表压缩成一句：相同错误合并。"""
    by = {}
    for e in errors:
        name, _, msg = e.partition("：")
        by.setdefault(msg, []).append(name)
    parts = []
    for msg, names in by.items():
        who = "全部对象" if len(names) == n_entities and n_entities > 1 else "、".join(names[:3]) + ("等" if len(names) > 3 else "")
        parts.append(f"{who}：{msg}")
    return "；".join(parts[:3])
