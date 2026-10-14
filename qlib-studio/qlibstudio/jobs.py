"""任务管理（主进程里）：每个耗时任务都在独立的工作进程里跑，同一时间只跑一个。

    主进程 ──Popen──▶ 工作进程（同一个程序加 --worker <任务文件>）
                       ├─ 写 jobs/<编号>/progress.json（进度 0–1 和一句说明，每步覆盖）
                       ├─ 写 jobs/<编号>/log.txt（上游 Qlib 的输出、错误堆栈）
                       └─ 写 jobs/<编号>/result.json（成功的结果或中文错误原因），然后退出
主进程每次界面查询时读这几个文件，不用管道（Windows 无控制台程序也一样可靠）。

为什么用独立进程：Qlib 训练和回测会占几百 MB 到几 GB 内存，跑完进程退出就全部还给系统；
「取消」可以直接结束进程，不会卡住界面；上游代码出错也不会拖垮主进程。
异常收尾：工作进程每 2 秒检查主进程（PID + 启动时间），主进程不在了就自行退出（worker.start_watchdog）。
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import paths
from .data import datasets

log = logging.getLogger("qlibstudio")
KINDS = {
    "demo": "生成演示数据",
    "import": "导入通达信 / CSV",
    "futu-check": "连接富途 OpenD、查询额度",
    "futu-download": "富途 OpenD 下载日 K",
    "community-download": "下载社区数据集",
    "community-import": "导入社区数据集文件",
    "quality": "数据质量检查",
    "experiment": "运行实验",
}
MAX_JOBS_KEPT = 80


def worker_command(job_file: Path) -> list[str]:
    base = [sys.executable] if paths.frozen() else [sys.executable, str(paths.bundle_root() / "run.py")]
    return base + ["--worker", str(job_file), "--parent", str(os.getpid())]


def _popen(cmd: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：不弹黑色命令行窗口
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def stop_process(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """先请它退出，超时再强制结束；连同它启动的子进程一起结束。"""
    if proc is None or proc.poll() is not None:
        return
    children = []
    try:
        import psutil
        children = psutil.Process(proc.pid).children(recursive=True)
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.terminate()
        proc.wait(timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            pass
    except OSError:
        pass
    for c in children:
        try:
            c.kill()
        except Exception:  # noqa: BLE001
            pass


class Jobs:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.dir = self.root / "jobs"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.current: dict | None = None
        self.extra_env: dict[str, str] = {}

    # ------------------------------------------------------------ 启动 / 取消
    def busy(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self, kind: str, params: dict) -> dict:
        if kind not in KINDS:
            raise ValueError(f"未知任务：{kind}")
        with self.lock:
            if self.proc is not None and self.proc.poll() is None:
                raise RuntimeError(f"「{self.current['title']}」还在运行，等它结束或先取消")
            job_id = datasets.new_id("j")
            jd = self.dir / job_id
            jd.mkdir(parents=True)
            job = {"id": job_id, "kind": kind, "title": KINDS[kind], "params": params, "data_root": str(self.root),
                   "created": time.strftime("%Y-%m-%d %H:%M:%S")}
            (jd / "job.json").write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")
            env = dict(os.environ, PYTHONIOENCODING="utf-8", **self.extra_env)
            env.pop("PYTHONPATH", None)
            self.proc = _popen(worker_command(jd / "job.json"), jd, env)
            job["pid"] = self.proc.pid
            job["started_at"] = time.time()
            (jd / "pid.txt").write_text(str(self.proc.pid), encoding="ascii")
            self.current = job
            log.info("任务 %s（%s）已启动，工作进程 PID %s", job_id, kind, self.proc.pid)
        self._prune()
        return self.view(job_id)

    def cancel(self) -> bool:
        with self.lock:
            proc, job = self.proc, self.current
        if proc is None or proc.poll() is not None or job is None:
            return False
        stop_process(proc)
        jd = self.dir / job["id"]
        if not (jd / "result.json").exists():
            datasets.write_json(jd / "result.json", {"ok": False, "cancelled": True, "error": "已取消"})
        log.info("任务 %s 已取消", job["id"])
        return True

    def shutdown(self) -> None:
        """退出应用时：结束正在运行的工作进程并确认已退出。"""
        with self.lock:
            proc = self.proc
        if proc is not None and proc.poll() is None:
            log.info("退出：结束工作进程 PID %s", proc.pid)
            stop_process(proc)
            if self.current and not (self.dir / self.current["id"] / "result.json").exists():
                datasets.write_json(self.dir / self.current["id"] / "result.json",
                                    {"ok": False, "cancelled": True, "error": "应用退出时结束了这个任务"})

    # ------------------------------------------------------------ 查询
    def worker_pids(self) -> dict[int, str]:
        with self.lock:
            if self.proc is not None and self.proc.poll() is None and self.current:
                return {self.proc.pid: f"工作进程：{self.current['title']}（任务 {self.current['id']}）"}
        return {}

    def view(self, job_id: str) -> dict | None:
        jd = self.dir / job_id
        job = datasets.read_json(jd / "job.json")
        if not isinstance(job, dict):
            return None
        prog = datasets.read_json(jd / "progress.json", {}) or {}
        res = datasets.read_json(jd / "result.json")
        running = False
        with self.lock:
            if self.current and self.current["id"] == job_id and self.proc is not None:
                code = self.proc.poll()
                running = code is None
                if not running and res is None:
                    res = {"ok": False, "error": f"工作进程意外退出（退出码 {code}），详情见任务日志"}
                    datasets.write_json(jd / "result.json", res)
        if res is None and not running:
            res = {"ok": False, "error": "任务没有结束记录（应用上次可能被强制关闭）"}
        status = "运行中" if running else ("已取消" if res.get("cancelled") else ("完成" if res.get("ok") else "失败"))
        pid = None
        try:
            pid = int((jd / "pid.txt").read_text(encoding="ascii"))
        except (OSError, ValueError):
            pass
        return {"id": job_id, "kind": job["kind"], "title": job["title"], "created": job["created"], "pid": pid,
                "running": running, "status": status, "progress": prog.get("p", 1.0 if res and res.get("ok") else 0.0),
                "message": prog.get("msg", ""), "steps": prog.get("steps", [])[-12:], "result": res,
                "params": job.get("params")}

    def current_view(self) -> dict | None:
        with self.lock:
            cur = self.current
        return self.view(cur["id"]) if cur else None

    def list(self, limit: int = 30) -> list[dict]:
        out = []
        for d in sorted(self.dir.iterdir(), reverse=True)[:limit]:
            v = self.view(d.name) if d.is_dir() else None
            if v:
                v.pop("steps", None)
                if isinstance(v.get("result"), dict):
                    v["result"] = {k: v["result"].get(k) for k in ("ok", "error", "cancelled", "summary")}
                out.append(v)
        return out

    def read_log(self, job_id: str, tail: int = 400) -> str | None:
        if not datasets.ID_RE.match(job_id or ""):
            return None
        f = self.dir / job_id / "log.txt"
        if not f.is_file():
            return None
        lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    def _prune(self) -> None:
        dirs = sorted(d for d in self.dir.iterdir() if d.is_dir())
        for d in dirs[:-MAX_JOBS_KEPT]:
            shutil.rmtree(d, ignore_errors=True)
