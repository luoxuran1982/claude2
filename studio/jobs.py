"""任务管理：同一时间只跑一个生成/渲染任务，避免同一项目一边改一边渲染导致版本错乱。"""

import threading
import time
import traceback
import uuid

from .llm import LLMError
from .media import Cancelled, ToolError
from .pipeline import Ctx


class JobBusy(RuntimeError):
    pass


class JobManager:
    def __init__(self):
        self._lock = threading.Lock()
        self.current = None
        self.history = []

    def start(self, kind, project_id, fn, *args, **kwargs):
        with self._lock:
            if self.current and self.current["state"] == "running":
                raise JobBusy(f"已有任务在运行：{self.current['label']}")
            cancel = threading.Event()
            job = {
                "id": uuid.uuid4().hex[:10],
                "kind": kind,
                "label": kwargs.pop("label", kind),
                "project": project_id,
                "state": "running",
                "progress": 0.0,
                "message": "准备中",
                "log": [],
                "started": time.time(),
                "ended": None,
                "result": None,
                "error": "",
                "_cancel": cancel,
            }
            self.current = job

        def progress(p, m):
            job["progress"] = round(p, 4)
            if m:
                job["message"] = m

        def log(m):
            job["log"].append(time.strftime("%H:%M:%S ") + m)
            job["log"] = job["log"][-200:]

        def run():
            ctx = Ctx(cancel, progress, log)
            job["_ctx"] = ctx
            try:
                job["result"] = fn(*args, ctx=ctx, **kwargs)
                job["state"] = "done"
                job["progress"] = 1.0
            except Cancelled:
                job["state"] = "cancelled"
                job["message"] = "已取消"
            except (ToolError, LLMError, ValueError, FileNotFoundError) as e:
                job["state"] = "failed"
                job["error"] = str(e)
                job["message"] = "失败：" + str(e)
                log(job["message"])
            except Exception as e:  # 未预料的错误，保留堆栈方便排查
                job["state"] = "failed"
                job["error"] = f"{type(e).__name__}: {e}"
                job["message"] = "失败：" + job["error"]
                log(traceback.format_exc())
            finally:
                job["ended"] = time.time()
                with self._lock:
                    self.history.append(self.public(job))
                    self.history = self.history[-30:]

        threading.Thread(target=run, daemon=True, name=f"job-{kind}").start()
        return self.public(job)

    def cancel(self):
        job = self.current
        if job and job["state"] == "running":
            job["_cancel"].set()
            ctx = job.get("_ctx")
            if ctx:
                ctx.runner.kill()
            job["message"] = "正在取消…"
            return True
        return False

    def busy(self):
        return bool(self.current and self.current["state"] == "running")

    @staticmethod
    def public(job):
        if not job:
            return None
        out = {k: v for k, v in job.items() if not k.startswith("_")}
        out["elapsed"] = round((job["ended"] or time.time()) - job["started"], 1)
        return out

    def status(self):
        return self.public(self.current)
