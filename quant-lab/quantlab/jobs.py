"""后台任务：同一时间只跑一个（导入 / 实验 / 因子检验），带进度、日志和取消。"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from typing import Any, Callable

log = logging.getLogger("quantlab")


class Cancelled(Exception):
    pass


class Jobs:
    def __init__(self):
        self.lock = threading.Lock()
        self.job: dict | None = None
        self._cancel = threading.Event()
        self.history: list[dict] = []

    def busy(self) -> bool:
        with self.lock:
            return bool(self.job and self.job["status"] == "running")

    def start(self, kind: str, title: str, fn: Callable[[Callable[[float, str], None]], Any]) -> dict:
        with self.lock:
            if self.job and self.job["status"] == "running":
                raise RuntimeError(f"正在运行：{self.job['title']}，请等它结束或取消")
            self._cancel.clear()
            job = {"id": uuid.uuid4().hex[:10], "kind": kind, "title": title, "status": "running",
                   "progress": 0.0, "message": "开始", "logs": [], "result": None, "error": None,
                   "started": time.time(), "finished": None}
            self.job = job

        def progress(p: float, msg: str):
            if self._cancel.is_set():
                raise Cancelled()
            with self.lock:
                job["progress"] = round(max(job["progress"], min(float(p), 1.0)), 4)
                if msg and msg != job["message"]:
                    job["message"] = msg
                    job["logs"].append(f"{time.strftime('%H:%M:%S')} {msg}")
                    del job["logs"][:-200]

        def target():
            try:
                result = fn(progress)
                with self.lock:
                    job.update(status="done", result=result, progress=1.0)
            except Cancelled:
                with self.lock:
                    job.update(status="cancelled", message="已取消")
            except Exception as exc:  # noqa: BLE001
                log.error("任务失败 %s：%s\n%s", title, exc, traceback.format_exc())
                with self.lock:
                    job.update(status="error", error=str(exc) or exc.__class__.__name__, message="失败")
            finally:
                with self.lock:
                    job["finished"] = time.time()
                    self.history = ([{k: job[k] for k in ("id", "kind", "title", "status", "error", "started", "finished")}]
                                    + self.history)[:30]

        threading.Thread(target=target, name=f"job-{kind}", daemon=True).start()
        return self.snapshot()

    def cancel(self) -> bool:
        if self.busy():
            self._cancel.set()
            return True
        return False

    def snapshot(self) -> dict | None:
        with self.lock:
            if not self.job:
                return None
            j = dict(self.job)
            j["logs"] = list(j["logs"][-60:])
            j["elapsed"] = round((j["finished"] or time.time()) - j["started"], 1)
            return j
