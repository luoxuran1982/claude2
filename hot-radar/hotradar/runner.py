"""任务管理：启动/停止抓取工作进程、读日志算进度、记录每次运行；窗口开着时的定时抓取；MCP 服务进程。

同一时间只跑一个抓取类任务（抓取、离线演示、推送测试、体检）。每次运行的完整输出存在数据目录 logs/ 下，
运行记录（时间、结果、成功/失败平台、报告路径）存在 runs.json，保留最近 200 条。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import configio, paths

log = logging.getLogger("hotradar")

TASK_LABELS = {"crawl": "立即抓取", "demo": "离线演示", "test-notify": "推送测试", "doctor": "环境体检", "mcp": "MCP 服务"}
WS_OF = {"crawl": "main", "demo": "demo", "test-notify": "main", "doctor": "main", "mcp": "main"}
MAX_RUNS = 200
MAX_LOGS = 60
INTERVALS = (15, 30, 60, 120, 180, 360, 720)  # 定时抓取可选间隔（分钟）


def worker_command(task: str, workspace: Path, log_path: Path, port: int = 0) -> list[str]:
    if paths.frozen():
        base = [sys.executable]
    else:
        base = [sys.executable, str(paths.bundle_root() / "run.py")]
    cmd = base + ["--worker", task, "--workspace", str(workspace), "--log", str(log_path), "--parent", str(os.getpid())]
    if port:
        cmd += ["--port", str(port)]
    return cmd


def _popen(cmd: list[str], cwd: Path, env: dict) -> subprocess.Popen:
    kw = {}
    if sys.platform == "win32":
        kw["creationflags"] = 0x08000000  # CREATE_NO_WINDOW：不弹黑色命令行窗口
    else:
        kw["start_new_session"] = True  # 自成进程组，结束时连同它的子进程一起结束
    return subprocess.Popen(cmd, cwd=str(cwd), env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)


def stop_process(proc: subprocess.Popen | None, timeout: float = 5.0) -> None:
    """先请它退出，超时再强制结束；连同它启动的子进程（上游偶尔会起子进程）一起结束。"""
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


class Progress:
    """从上游的输出逐行推算进度。上游的打印格式见 trendradar/__main__.py、crawler/fetcher.py、crawler/rss/fetcher.py。"""

    RE_PLATFORMS = re.compile(r"^监控平台数量: (\d+)")
    RE_OK = re.compile(r"^获取 (\S+) 成功")
    RE_FAIL = re.compile(r"^请求 (\S+) 失败: (.*)$")
    RE_SUMMARY = re.compile(r"^成功: \[(.*)\], 失败: \[(.*)\]")
    RE_RSS_START = re.compile(r"^\[RSS\] 开始抓取 (\d+) 个 RSS 源")
    RE_RSS_ONE = re.compile(r"^\[RSS\] (.+?): (获取 \d+ 条|请求超时.*|请求失败.*|解析失败.*|未知错误.*)$")
    RE_RSS_DONE = re.compile(r"^\[RSS\] 抓取完成: (\d+) 个源成功, (\d+) 个失败, 共 (\d+) 条")
    RE_HTML = re.compile(r"^HTML报告已生成: (.+)$")
    RE_RSS_HTML = re.compile(r"^\[RSS\] HTML 报告已生成: (.+)$")

    def __init__(self, task: str):
        self.task = task
        self.stage = "准备中"
        self.platforms_total = 0
        self.platforms_done: list[str] = []
        self.platforms_ok: list[str] = []
        self.platforms_failed: dict[str, str] = {}
        self.rss_total = 0
        self.rss_done = 0
        self.rss_summary = ""
        self.report = ""
        self.rss_report = ""
        self.errors: list[str] = []
        self.notify: list[str] = []
        self.lines = 0

    def feed(self, line: str) -> None:
        self.lines += 1
        s = line.strip()
        if not s:
            return
        if m := self.RE_PLATFORMS.match(s):
            self.platforms_total = int(m.group(1))
            self.stage = "读取配置"
        elif s.startswith("开始爬取数据"):
            self.stage = "抓取热榜"
        elif m := self.RE_OK.match(s):
            self._done(m.group(1), ok=True)
        elif m := self.RE_FAIL.match(s):
            if "秒后重试" not in m.group(2):
                self._done(m.group(1), ok=False, why=m.group(2))
        elif m := self.RE_SUMMARY.match(s):
            fails = [x.strip(" '\"") for x in m.group(2).split(",") if x.strip()]
            for f in fails:
                self.platforms_failed.setdefault(f, "未通过（见日志）")
            self.stage = "抓取 RSS"
        elif m := self.RE_RSS_START.match(s):
            self.rss_total = int(m.group(1))
            self.stage = "抓取 RSS"
        elif self.RE_RSS_DONE.match(s):
            self.rss_summary = s.replace("[RSS] ", "")
            self.rss_done = self.rss_total
            self.stage = "筛选与分析"
        elif self.RE_RSS_ONE.match(s):
            self.rss_done += 1
        elif s.startswith("[AI]") or s.startswith("[AI筛选]") or s.startswith("[翻译]"):
            self.stage = "AI 处理"
        elif m := self.RE_HTML.match(s):
            self.report = m.group(1).strip()
            self.stage = "生成报告"
        elif m := self.RE_RSS_HTML.match(s):
            self.rss_report = m.group(1).strip()
        elif "推送" in s or "发送" in s:
            if any(k in s for k in ("成功", "失败", "跳过")):
                self.notify.append(s[:200])
            self.stage = "推送" if self.stage in ("生成报告", "AI 处理", "筛选与分析") else self.stage
        if s.startswith("❌") or "Traceback" in s or s.startswith("[热点雷达] 工作进程出错"):
            self.errors.append(s[:300])

    def _done(self, pid: str, ok: bool, why: str = "") -> None:
        if pid in self.platforms_done:
            return
        self.platforms_done.append(pid)
        (self.platforms_ok.append(pid) if ok else self.platforms_failed.__setitem__(pid, why[:200]))
        self.stage = "抓取热榜"

    def percent(self, finished: bool) -> int:
        if finished:
            return 100
        if self.task not in ("crawl", "demo"):
            return min(90, 10 + self.lines * 3)
        p = 5.0
        if self.platforms_total:
            p += 65 * len(self.platforms_done) / self.platforms_total
        if self.rss_total:
            p += 15 * min(1, self.rss_done / self.rss_total)
        elif self.stage not in ("准备中", "读取配置", "抓取热榜"):
            p += 15
        if self.stage in ("AI 处理",):
            p = max(p, 85)
        if self.stage in ("生成报告", "推送"):
            p = max(p, 92)
        return int(min(p, 99))

    def view(self, finished: bool) -> dict:
        return {
            "stage": self.stage, "percent": self.percent(finished),
            "platforms_total": self.platforms_total, "platforms_done": len(self.platforms_done),
            "platforms_ok": self.platforms_ok, "platforms_failed": self.platforms_failed,
            "rss_total": self.rss_total, "rss_done": self.rss_done, "rss_summary": self.rss_summary,
            "report": self.report, "rss_report": self.rss_report, "errors": self.errors[-5:], "notify": self.notify[-10:],
        }


class Runner:
    def __init__(self, root: Path):
        self.root = root
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.job: dict | None = None
        self.progress: Progress | None = None
        self.stop_event = threading.Event()
        self.logs_dir = root / "logs"
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.runs_file = root / "runs.json"
        self.settings_file = root / "settings.json"
        self.settings = self._load_settings()
        self.next_auto = None if not self.settings["auto_enabled"] else time.time() + 60
        self.mcp_proc: subprocess.Popen | None = None
        self.mcp_info: dict = {}
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------ 设置
    def _load_settings(self) -> dict:
        s = {"auto_enabled": False, "auto_interval_min": 60, "mcp_port": 3333}
        try:
            s.update({k: v for k, v in json.loads(self.settings_file.read_text(encoding="utf-8")).items() if k in s})
        except (OSError, ValueError):
            pass
        if s["auto_interval_min"] not in INTERVALS:
            s["auto_interval_min"] = 60
        return s

    def _save_settings(self) -> None:
        configio.atomic_write(self.settings_file, json.dumps(self.settings, ensure_ascii=False, indent=1).encode("utf-8"))

    def set_schedule(self, enabled: bool, interval: int) -> dict:
        if interval not in INTERVALS:
            raise ValueError("间隔只能是 " + "、".join(f"{i} 分钟" for i in INTERVALS))
        with self.lock:
            self.settings.update(auto_enabled=bool(enabled), auto_interval_min=int(interval))
            self._save_settings()
            if enabled:
                last = self._last_crawl_start()
                due = (last + interval * 60) if last else time.time() + 5
                self.next_auto = max(due, time.time() + 5)
            else:
                self.next_auto = None
        log.info("定时抓取：%s，每 %s 分钟", "开" if enabled else "关", interval)
        return self.schedule_view()

    def schedule_view(self) -> dict:
        return {"enabled": self.settings["auto_enabled"], "interval_min": self.settings["auto_interval_min"],
                "intervals": list(INTERVALS),
                "next_at": datetime.fromtimestamp(self.next_auto).astimezone().isoformat(timespec="seconds") if self.next_auto else None,
                "note": "只在应用开着时运行；关闭窗口或退出应用后定时抓取随之停止，不会在后台继续"}

    # ------------------------------------------------------------ 运行记录
    def runs(self) -> list[dict]:
        try:
            data = json.loads(self.runs_file.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save_run(self, rec: dict) -> None:
        runs = [r for r in self.runs() if r.get("id") != rec["id"]]
        runs.insert(0, rec)
        configio.atomic_write(self.runs_file, json.dumps(runs[:MAX_RUNS], ensure_ascii=False, indent=1).encode("utf-8"))

    def _last_crawl_start(self) -> float | None:
        for r in self.runs():
            if r.get("task") == "crawl":
                try:
                    return datetime.fromisoformat(r["started_at"]).timestamp()
                except (KeyError, ValueError):
                    return None
        return None

    def _prune_logs(self) -> None:
        logs = sorted(self.logs_dir.glob("run-*.log"))
        for p in logs[:-MAX_LOGS]:
            try:
                p.unlink()
            except OSError:
                pass

    def read_log(self, run_id: str, tail: int = 400) -> str | None:
        if not re.fullmatch(r"run-\d{8}-\d{6}(-\d+)?", run_id or ""):
            return None
        p = self.logs_dir / f"{run_id}.log"
        if not p.is_file():
            return None
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        return "\n".join(lines[-tail:])

    # ------------------------------------------------------------ 启动/停止
    def busy(self) -> bool:
        with self.lock:
            return self.proc is not None and self.proc.poll() is None

    def start(self, task: str, trigger: str = "手动") -> dict:
        if task not in ("crawl", "demo", "test-notify", "doctor"):
            raise ValueError("未知任务")
        with self.lock:
            if self.busy():
                return {"started": False, "reason": f"正在{TASK_LABELS[self.job['task']]}，请等它结束或先停止"}
            ws_name = WS_OF[task]
            ws = paths.workspace_dir(ws_name, self.root)
            configio.ensure_workspace(ws)
            if task == "demo":
                configio.prepare_demo_workspace(paths.workspace_dir("main", self.root), ws)
            now = datetime.now().astimezone()
            run_id = now.strftime("run-%Y%m%d-%H%M%S")
            n = 1
            while (self.logs_dir / f"{run_id}.log").exists():
                n += 1
                run_id = now.strftime("run-%Y%m%d-%H%M%S") + f"-{n}"
            log_path = self.logs_dir / f"{run_id}.log"
            log_path.write_text("", encoding="utf-8")
            env = dict(os.environ)
            env.pop("GITHUB_ACTIONS", None)
            env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
            if task != "demo":
                env.update(configio.secrets_env(self.root))
            cmd = worker_command(task, ws, log_path)
            self.proc = _popen(cmd, ws, env)
            self.progress = Progress(task)
            self.job = {"id": run_id, "task": task, "label": TASK_LABELS[task], "workspace": ws_name, "trigger": trigger,
                        "pid": self.proc.pid, "started_at": now.isoformat(timespec="seconds"), "log": log_path.name,
                        "cancelled": False}
            if task == "crawl":
                self.next_auto = (time.time() + self.settings["auto_interval_min"] * 60) if self.settings["auto_enabled"] else None
            t = threading.Thread(target=self._watch, args=(self.proc, self.job, self.progress, log_path),
                                 name=f"watch-{task}", daemon=True)
            t.start()
            log.info("开始%s（%s），工作进程 PID %s", TASK_LABELS[task], trigger, self.proc.pid)
            return {"started": True, "job": self.job_view()}

    def cancel(self) -> bool:
        with self.lock:
            if not self.busy():
                return False
            self.job["cancelled"] = True
            proc = self.proc
        stop_process(proc)
        return True

    def _watch(self, proc: subprocess.Popen, job: dict, progress: Progress, log_path: Path) -> None:
        pos = 0
        buf = ""

        def pump():
            nonlocal pos, buf
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
            except OSError:
                return
            buf += chunk
            *lines, buf = buf.split("\n")
            for line in lines:
                progress.feed(line)

        while proc.poll() is None:
            pump()
            if self.stop_event.wait(0.5):
                break
        pump()
        if buf:
            progress.feed(buf)
        code = proc.poll()
        ended = datetime.now().astimezone()
        view = progress.view(True)
        if job["cancelled"]:
            status, summary = "cancelled", "已手动停止"
        elif code is None:
            status, summary = "cancelled", "应用退出时停止"
        elif job["task"] in ("crawl", "demo"):
            ok_n, fail_n = len(view["platforms_ok"]), len(view["platforms_failed"])
            if code == 0 and view["report"] and not view["errors"]:
                status = "ok" if fail_n == 0 else "partial"
            elif code == 0 and ok_n and not view["errors"]:
                status = "partial"
            else:
                status = "failed"
            summary = f"热榜 {ok_n} 个平台成功、{fail_n} 个失败" + (f"；RSS {view['rss_summary']}" if view["rss_summary"] else "")
            if status == "failed" and not ok_n and fail_n:
                summary += "。全部平台都没取到：多半是网络连不上 newsnow 接口（见日志）"
        else:
            status = "ok" if code == 0 else "failed"
            summary = "完成" if code == 0 else f"未通过（退出码 {code}），详见日志"
        rec = {**job, **view, "status": status, "exit_code": code, "summary": summary,
               "ended_at": ended.isoformat(timespec="seconds"),
               "duration_sec": round(ended.timestamp() - datetime.fromisoformat(job["started_at"]).timestamp(), 1)}
        rec.pop("stage", None)
        rec.pop("percent", None)
        try:
            self._save_run(rec)
            self._prune_logs()
        except OSError:
            log.exception("保存运行记录失败")
        log.info("%s结束：%s（%s）", job["label"], status, summary)
        with self.lock:
            if self.proc is proc:
                self.proc = None
                self.job = None
                self.progress = None

    def job_view(self) -> dict | None:
        with self.lock:
            if not self.job:
                return None
            log_tail = self.read_log(self.job["id"], tail=60) or ""
            return {**self.job, **self.progress.view(False), "log_tail": log_tail}

    # ------------------------------------------------------------ 定时抓取（窗口开着时）
    def scheduler_loop(self) -> None:
        while not self.stop_event.wait(5):
            with self.lock:
                due = self.settings["auto_enabled"] and self.next_auto and time.time() >= self.next_auto
            if due:
                if self.busy():
                    with self.lock:
                        self.next_auto = time.time() + 60  # 正在运行别的任务，一分钟后再看
                    continue
                try:
                    self.start("crawl", trigger="定时")
                except Exception:  # noqa: BLE001
                    log.exception("定时抓取启动失败")
                    with self.lock:
                        self.next_auto = time.time() + self.settings["auto_interval_min"] * 60

    def start_scheduler(self) -> None:
        t = threading.Thread(target=self.scheduler_loop, name="scheduler", daemon=True)
        t.start()
        self._threads.append(t)

    # ------------------------------------------------------------ MCP 服务（可选，默认关）
    def mcp_view(self) -> dict:
        with self.lock:
            running = self.mcp_proc is not None and self.mcp_proc.poll() is None
            info = dict(self.mcp_info)
            if not running and self.mcp_proc is not None:
                info["exited"] = self.mcp_proc.poll()
            port = self.settings["mcp_port"]
        return {"running": running, "port": port, "url": f"http://127.0.0.1:{port}/mcp", "pid": info.get("pid") if running else None,
                "started_at": info.get("started_at") if running else None, "log": info.get("log"),
                "exit_code": info.get("exited"),
                "note": "只监听本机 127.0.0.1；关闭窗口或退出应用时一起停止"}

    def set_mcp(self, enabled: bool, port: int | None = None) -> dict:
        with self.lock:
            if port is not None:
                if not 1024 <= int(port) <= 65535:
                    raise ValueError("端口应在 1024–65535 之间")
                self.settings["mcp_port"] = int(port)
                self._save_settings()
            running = self.mcp_proc is not None and self.mcp_proc.poll() is None
            if enabled and not running:
                ws = paths.workspace_dir("main", self.root)
                configio.ensure_workspace(ws)
                log_path = self.logs_dir / "mcp.log"
                log_path.write_text("", encoding="utf-8")
                env = dict(os.environ)
                env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1", LITELLM_LOCAL_MODEL_COST_MAP="True")
                env.update(configio.secrets_env(self.root))
                self.mcp_proc = _popen(worker_command("mcp", ws, log_path, self.settings["mcp_port"]), ws, env)
                self.mcp_info = {"pid": self.mcp_proc.pid, "log": log_path.name,
                                 "started_at": datetime.now().astimezone().isoformat(timespec="seconds")}
                log.info("MCP 服务已启动，PID %s，端口 %s", self.mcp_proc.pid, self.settings["mcp_port"])
                proc = None
            elif not enabled and running:
                proc = self.mcp_proc
            else:
                proc = None
        if proc is not None:
            stop_process(proc)
            log.info("MCP 服务已停止")
        return self.mcp_view()

    def mcp_log(self) -> str:
        p = self.logs_dir / "mcp.log"
        try:
            return "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-200:])
        except OSError:
            return ""

    # ------------------------------------------------------------ 退出
    def worker_pids(self) -> dict[int, str]:
        with self.lock:
            out = {}
            if self.proc is not None and self.proc.poll() is None:
                out[self.proc.pid] = f"抓取工作进程（{TASK_LABELS[self.job['task']]}，运行上游 TrendRadar）"
            if self.mcp_proc is not None and self.mcp_proc.poll() is None:
                out[self.mcp_proc.pid] = f"MCP 服务进程（上游 mcp_server，端口 {self.settings['mcp_port']}）"
            return out

    def shutdown(self) -> None:
        """退出时：停定时线程 → 结束正在运行的抓取 → 停 MCP 服务，并确认进程都已退出。"""
        self.stop_event.set()
        with self.lock:
            procs = [p for p in (self.proc, self.mcp_proc) if p is not None]
            if self.job:
                self.job["cancelled"] = True
        for p in procs:
            stop_process(p)
        for p in procs:
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                log.warning("工作进程 %s 没有按时退出", p.pid)
        log.info("后台任务已全部停止")
