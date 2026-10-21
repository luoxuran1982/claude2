"""运行状态：列出本次启动涉及的每个应用进程（职责、PID、内存、采样时间）。

口径：macOS / Linux 用常驻内存 RSS，Windows 用工作集（Working Set），单位 MiB（1 MiB = 1024² 字节）。
有 psutil 时用 psutil；没有时 Linux 读 /proc，macOS 调 ps，Windows 调 psapi。读不到的值返回 None，
页面显示「暂不可用」，不拿旧值或估算值冒充。
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

try:
    import psutil  # type: ignore
except Exception:  # noqa: BLE001
    psutil = None

MIB = 1024 * 1024
IS_WIN = sys.platform.startswith("win")
IS_MAC = sys.platform == "darwin"


def metric_name() -> str:
    return "工作集（Working Set）" if IS_WIN else "常驻内存 RSS"


def _alive(pid: int) -> bool:
    if psutil:
        return psutil.pid_exists(pid)
    if IS_WIN:
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def mem_bytes(pid: int):
    """返回进程的 RSS / 工作集（字节）；读不到返回 None。"""
    try:
        if psutil:
            return int(psutil.Process(pid).memory_info().rss)  # Windows 上 rss 即 WorkingSetSize
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/status", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
            return None
        if IS_MAC:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
            return int(out) * 1024 if out else None
        if IS_WIN:
            return _win_working_set(pid)
    except Exception:  # noqa: BLE001
        return None
    return None


def _win_working_set(pid: int):
    import ctypes
    from ctypes import wintypes

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

    h = ctypes.windll.kernel32.OpenProcess(0x1000 | 0x0010, False, pid)  # QUERY_LIMITED_INFORMATION | VM_READ
    if not h:
        return None
    try:
        c = PMC()
        c.cb = ctypes.sizeof(PMC)
        if ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(c), c.cb):
            return int(c.WorkingSetSize)
        return None
    finally:
        ctypes.windll.kernel32.CloseHandle(h)


def children(pid: int):
    """子孙进程 PID 列表；读不到返回 None。"""
    try:
        if psutil:
            return [c.pid for c in psutil.Process(pid).children(recursive=True)]
        pairs = []
        if sys.platform.startswith("linux"):
            for d in os.listdir("/proc"):
                if d.isdigit():
                    try:
                        with open(f"/proc/{d}/stat", encoding="utf-8", errors="replace") as f:
                            ppid = int(f.read().rsplit(")", 1)[1].split()[1])
                        pairs.append((int(d), ppid))
                    except (OSError, ValueError, IndexError):
                        continue
        elif IS_MAC:
            out = subprocess.run(["ps", "-A", "-o", "pid=,ppid="], capture_output=True, text=True, timeout=3).stdout
            pairs = [tuple(map(int, ln.split())) for ln in out.splitlines() if ln.strip()]
        else:
            return None
        res, frontier = [], {pid}
        while frontier:
            nxt = {p for p, pp in pairs if pp in frontier and p not in res}
            res.extend(sorted(nxt))
            frontier = nxt
        return res
    except Exception:  # noqa: BLE001
        return None


def _name(pid: int) -> str:
    try:
        if psutil:
            return psutil.Process(pid).name()
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/comm", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _row(role: str, pid: int, kind: str):
    b = mem_bytes(pid)
    return {"role": role, "kind": kind, "pid": pid, "name": _name(pid),
            "mem_mib": round(b / MIB, 1) if b is not None else None, "sampled_at": time.time()}


def snapshot(browser=None, browser_mode: str = "", native: bool = False) -> dict:
    """browser：lifecycle.BrowserWindow；browser_mode：open() 的返回值（edge/chrome/default/none）；
    native：v0.4 原生窗口模式（窗口和后台是同一个进程）。"""
    if native:
        rows = [_row("应用进程（原生窗口 + HTTP 接口 + 采集调度，同一个进程）", os.getpid(), "backend")]
        notes = ["原生窗口模式：窗口由本进程直接创建，没有单独的窗口进程。"
                 "macOS 的网页内容由系统的 WebKit 辅助进程（com.apple.WebKit.WebContent 等）渲染，"
                 "它们由系统管理、不是本进程的子进程，这里不列出、不计入合计。"]
        vals = [r["mem_mib"] for r in rows if r["mem_mib"] is not None]
        return _result(rows, notes, vals)
    rows = [_row("后台服务（HTTP 接口 + 采集调度；采集任务是本进程内的线程）", os.getpid(), "backend")]
    notes = []
    proc = getattr(browser, "proc", None)
    if proc is not None and proc.poll() is None:
        rows.append(_row("应用窗口（浏览器 --app 主进程）", proc.pid, "window"))
        kids = children(proc.pid)
        if kids is None:
            notes.append("应用窗口的子进程：暂不可用（未安装 psutil，当前系统无法枚举子进程）")
        else:
            for k in kids:
                if _alive(k):
                    rows.append(_row("应用窗口子进程（渲染/GPU/网络等）", k, "window-child"))
    elif browser_mode == "default":
        notes.append("页面在系统默认浏览器里打开：浏览器是外部程序，不属于本应用，不计入合计。")
    elif browser_mode in ("", "none"):
        notes.append("本次没有由本应用打开的应用窗口（无窗口/服务模式，或页面由外部浏览器打开）。")
    else:
        notes.append("应用窗口进程已结束或交给了已在运行的浏览器，不再列出。")
    vals = [r["mem_mib"] for r in rows if r["mem_mib"] is not None]
    return _result(rows, notes, vals)


def _result(rows, notes, vals) -> dict:
    return {
        "sampled_at": time.time(),
        "platform": sys.platform,
        "metric": metric_name(),
        "unit": "MiB",
        "reader": "psutil" if psutil else "系统接口（/proc、ps 或 psapi）",
        "processes": rows,
        "total_mib": round(sum(vals), 1) if vals else None,
        "workers": "采集任务在后台服务进程内以线程运行（collector 调度线程 + 最多 12 个 fetch 线程），线程共享后台的 PID，没有独立的工作进程。",
        "notes": notes,
    }
