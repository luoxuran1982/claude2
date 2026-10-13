"""运行状态：列出本应用的进程（主进程、自己启动的 FFmpeg/配音子进程、窗口子进程）及其内存。

口径：macOS/Linux 用常驻内存 RSS，Windows 用工作集（Working Set），单位 MiB（1024² 字节）。
读不到就返回 None，界面显示「暂不可用」，不拿旧值或估算值冒充。

子进程登记在 CHILDREN 里：正常退出时逐个结束；主进程崩溃时
- Windows：子进程放进「作业对象」（Job Object，关闭即结束全部成员），主进程一没，系统就结束它们；
- macOS / Linux：第一次启动子进程时顺带启动一个很小的守护进程（/bin/sh 循环），
  每 2 秒检查主进程是否还在；主进程不在了，就结束登记过的子进程，然后自己退出。
"""

import atexit
import os
import subprocess
import sys
import threading
import time

from . import paths

try:
    import psutil  # 可选：有它就能列出所有后代进程（包括 WebView2 子进程）
except Exception:  # pragma: no cover
    psutil = None

MIB = 1024 * 1024
_lock = threading.Lock()
CHILDREN = {}  # pid -> {"role", "started", "proc"}
MODE = {"value": "browser"}  # browser / window，由 run.py 设置
_watchdog = {"proc": None, "file": None}
_job = {"handle": None}


def set_mode(mode):
    MODE["value"] = mode


# ------------------------------------------------------------------ 登记子进程
def register(proc, role):
    with _lock:
        CHILDREN[proc.pid] = {"role": role, "started": time.time(), "proc": proc}
    _assign_job(proc)
    _ensure_watchdog()
    _write_pid_file()


def unregister(proc):
    with _lock:
        CHILDREN.pop(proc.pid, None)
    _write_pid_file()


def kill_children():
    """结束本应用启动的全部子进程，返回结束的个数。"""
    with _lock:
        items = list(CHILDREN.values())
        CHILDREN.clear()
    n = 0
    for c in items:
        p = c["proc"]
        if p.poll() is None:
            try:
                p.kill()
                p.wait(5)
                n += 1
            except Exception:
                pass
    _write_pid_file()
    return n


def stop_watchdog():
    w = _watchdog.get("proc")
    if w and w.poll() is None:
        try:
            w.kill()
            w.wait(2)
        except Exception:
            pass
    f = _watchdog.get("file")
    if f:
        try:
            os.remove(f)
        except OSError:
            pass


def shutdown():
    kill_children()
    stop_watchdog()


atexit.register(shutdown)


def _write_pid_file():
    f = _watchdog.get("file")
    if not f:
        return
    with _lock:
        pids = " ".join(str(p) for p in CHILDREN)
    try:
        with open(f + ".tmp", "w") as fh:
            fh.write(pids)
        os.replace(f + ".tmp", f)
    except OSError:
        pass


def _ensure_watchdog():
    if paths.IS_WIN or (_watchdog["proc"] and _watchdog["proc"].poll() is None):
        return
    f = str(paths.sub("run") / f"children-{os.getpid()}.pids")
    _watchdog["file"] = f
    script = (
        'P=$1; F=$2; while kill -0 "$P" 2>/dev/null; do sleep 2; done; '
        '[ -f "$F" ] && for c in $(cat "$F"); do kill -9 "$c" 2>/dev/null; done; rm -f "$F"'
    )
    try:
        _watchdog["proc"] = subprocess.Popen(["/bin/sh", "-c", script, "studio-watchdog", str(os.getpid()), f],
                                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                             start_new_session=True)
    except OSError:
        _watchdog["proc"] = None


def _assign_job(proc):
    if not paths.IS_WIN:
        return
    try:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        if _job["handle"] is None:
            h = k32.CreateJobObjectW(None, None)

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

            class IOC(ctypes.Structure):
                _fields_ = [(n, ctypes.c_uint64) for n in ("r", "w", "o", "rb", "wb", "ob")]

            class EXT(ctypes.Structure):
                _fields_ = [("Basic", BASIC), ("Io", IOC), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                            ("PeakJobMemoryUsed", ctypes.c_size_t)]

            info = EXT()
            info.Basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info))  # JobObjectExtendedLimitInformation
            _job["handle"] = h
        k32.AssignProcessToJobObject(_job["handle"], int(proc._handle))
    except Exception:
        pass


# ------------------------------------------------------------------ 内存读取
def _rss_bytes(pid):
    if psutil:
        try:
            return psutil.Process(pid).memory_info().rss  # Windows 上即工作集
        except Exception:
            return None
    if sys.platform.startswith("linux"):
        try:
            with open(f"/proc/{pid}/status") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) * 1024
        except OSError:
            return None
    if paths.IS_MAC:
        try:
            out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout.strip()
            return int(out) * 1024 if out else None
        except Exception:
            return None
    return None


def _name(pid):
    if psutil:
        try:
            return psutil.Process(pid).name()
        except Exception:
            return ""
    return ""


def _role_for(name):
    n = name.lower()
    if "msedgewebview2" in n:
        return "窗口子进程（WebView2）"
    if "ffmpeg" in n:
        return "FFmpeg 子进程"
    if n in ("say", "espeak", "espeak-ng") or "powershell" in n:
        return "系统配音子进程"
    if n in ("sh", "bash") or "watchdog" in n:
        return "守护进程（主进程崩溃后清理子进程）"
    return "子进程"


def snapshot():
    me = os.getpid()
    now = time.time()
    mode = MODE["value"]
    rows = [{
        "pid": me,
        "role": "主进程：后台服务（127.0.0.1 HTTP）" + ("+ 应用窗口" if mode == "window" else ""),
        "name": _name(me) or os.path.basename(sys.executable),
        "kind": "main",
    }]
    known = set([me])
    with _lock:
        registered = {pid: dict(c) for pid, c in CHILDREN.items() if c["proc"].poll() is None}
    w = _watchdog.get("proc")
    if w and w.poll() is None:
        registered.setdefault(w.pid, {"role": "守护进程（主进程崩溃后清理子进程）", "started": None})
    for pid, c in registered.items():
        rows.append({"pid": pid, "role": c["role"], "name": _name(pid), "kind": "child"})
        known.add(pid)
    if psutil:  # 其他后代进程，例如 Windows 上 WebView2 启动的进程
        try:
            for ch in psutil.Process(me).children(recursive=True):
                if ch.pid not in known:
                    nm = ch.name()
                    rows.append({"pid": ch.pid, "role": _role_for(nm), "name": nm, "kind": "child"})
                    known.add(ch.pid)
        except Exception:
            pass
    total = 0
    ok = True
    for r in rows:
        b = _rss_bytes(r["pid"])
        r["mib"] = round(b / MIB, 1) if b is not None else None
        r["sampled"] = time.strftime("%H:%M:%S", time.localtime(now))
        if b is None:
            ok = False
        else:
            total += b
    metric = "工作集（Working Set）" if paths.IS_WIN else "常驻内存 RSS"
    return {
        "processes": rows,
        "total_mib": round(total / MIB, 1) if ok else None,
        "metric": metric,
        "unit": "MiB（1 MiB = 1024² 字节）",
        "mode": mode,
        "backend": "无独立后台：后台服务是主进程里的线程" if mode == "window" else "无独立后台：后台服务就是主进程",
        "external": "浏览器（外部程序，不计入本应用）" if mode == "browser" else "",
        "source": "psutil" if psutil else ("/proc" if sys.platform.startswith("linux") else "ps" if paths.IS_MAC else "暂不可用"),
        "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "note": "合计是各进程内存相加，进程间共享的内存可能被重复计算。",
    }
