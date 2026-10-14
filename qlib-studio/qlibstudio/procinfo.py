"""运行状态：本次启动涉及的每个进程的 PID 与内存。

口径：macOS / Linux 用常驻内存 RSS，Windows 用工作集（Working Set），单位 MiB（1 MiB = 1024² 字节）。
每次请求现读现算，不缓存；读不到的值返回 None，界面显示「暂不可用」，不用旧值或估算值顶替。
列出的进程：主进程（窗口 + 本机服务）、它启动的全部子进程（数据/实验工作进程、Windows 上的 WebView2 子进程）。
外部依赖单独列出、不计入合计：富途 / moomoo OpenD（用户自己启动的独立程序，本应用只通过 127.0.0.1:11111 连接它）。
"""
from __future__ import annotations

import os
import sys
import threading
from datetime import datetime, timezone

MIB = 1024 * 1024


def metric_name(platform: str | None = None) -> str:
    platform = platform or sys.platform
    return "工作集（Working Set）" if platform == "win32" else "常驻内存 RSS"


def _role(name: str, cmdline: list[str], is_main: bool, workers: dict[int, str], pid: int) -> str:
    if is_main:
        return "主进程：应用窗口 + 本机服务（127.0.0.1）+ 任务管理"
    if pid in workers:
        return workers[pid]
    n = name.lower()
    joined = " ".join(cmdline).lower()
    if "--worker" in joined:
        return "工作进程（运行上游 Qlib：导入数据 / 训练 / 回测）"
    if "msedgewebview2" in n:
        return "WebView2 子进程（窗口网页渲染，由主进程启动）"
    if "webkit" in n:
        return "WebKit 网页进程"
    return "本应用启动的子进程"


def _fallback_rss(pid: int) -> float | None:
    try:
        with open(f"/proc/{pid}/status", encoding="ascii") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024 / MIB
    except OSError:
        return None
    return None


OPEND_NAMES = ("futuopend", "moomooopend", "opend")


def _external(psutil) -> list[dict]:
    """富途/moomoo OpenD：按进程名找（不是本应用的子进程，单独列出）。"""
    found = []
    if psutil:
        for p in psutil.process_iter(["pid", "name"]):
            try:
                n = (p.info["name"] or "").lower().replace(".exe", "")
                if n in OPEND_NAMES or n.startswith("futuopend") or n.startswith("moomooopend"):
                    found.append({"name": f"富途/moomoo OpenD（{p.info['name']}）", "pid": p.info["pid"],
                                  "mem_mib": round(p.memory_info().rss / MIB, 1), "note": "用户自己启动的独立程序，不计入本应用"})
            except psutil.Error:
                continue
    if not found:
        found.append({"name": "富途/moomoo OpenD", "pid": None, "mem_mib": None,
                      "note": "本机没有发现 OpenD 进程；只有用「富途 OpenD 下载」时才需要它"})
    return found


def snapshot(workers: dict[int, str] | None = None, job_running: bool = False) -> dict:
    workers = workers or {}
    sampled = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    names = [t.name for t in threading.enumerate()]
    req = sum("process_request_thread" in n or n.startswith("Thread-") for n in names)
    threads = sorted(n for n in names if "process_request_thread" not in n and not n.startswith("Thread-"))
    if req:
        threads.append(f"HTTP 请求处理线程 × {req}")
    processes, note = [], None
    try:
        import psutil
    except ImportError:
        psutil = None
    if psutil:
        me = psutil.Process(os.getpid())
        try:
            children = me.children(recursive=True)
        except psutil.Error:
            children = []
        for i, p in enumerate([me] + children):
            try:
                with p.oneshot():
                    name = p.name()
                    try:
                        cmd = p.cmdline()
                    except psutil.Error:
                        cmd = []
                    mem = p.memory_info().rss / MIB  # psutil 在 Windows 上的 rss 就是工作集
                    processes.append({"pid": p.pid, "name": name, "role": _role(name, cmd, i == 0, workers, p.pid),
                                      "mem_mib": round(mem, 1), "parent": p.ppid()})
            except psutil.Error:
                continue  # 采样瞬间刚好结束的进程不再列出，不保留旧值
    else:
        mem = _fallback_rss(os.getpid())
        processes.append({"pid": os.getpid(), "name": os.path.basename(sys.executable),
                          "role": _role("", [], True, workers, os.getpid()),
                          "mem_mib": None if mem is None else round(mem, 1), "parent": os.getppid()})
        note = "没有 psutil，只能列出主进程；子进程暂不可用"
    if sys.platform == "darwin":
        note = (note + "；" if note else "") + ("macOS 的 WKWebView 网页内容进程由系统托管（活动监视器里名为 com.apple.WebKit.*），"
                                                 "不是本应用的子进程，这里读不到，暂不可用")
    known = [p["mem_mib"] for p in processes if p["mem_mib"] is not None]
    worker_listed = any(p["pid"] in workers for p in processes)
    return {
        "sampled_at": sampled,
        "platform": sys.platform,
        "metric": metric_name(),
        "unit": "MiB",
        "processes": processes,
        "total_mib": round(sum(known), 1) if known else None,
        "total_note": "合计是各进程数值相加，进程间共享的内存（例如同一程序文件的代码段）可能被重复计算" if len(known) > 1 else None,
        "backend_note": ("无独立后台：本机服务和任务管理是主进程里的线程（共用主进程 PID）；"
                         "导入数据、下载、训练和回测在需要时才启动一个独立的工作进程，任务结束进程就退出。"
                         + ("" if worker_listed else "当前没有正在运行的工作进程。")),
        "job_running": job_running,
        "external": _external(psutil),
        "threads": threads,
        "note": note,
    }
