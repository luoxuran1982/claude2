"""运行状态：列出本应用的进程（职责、PID、内存、采样时间）。

口径：macOS 用常驻内存 RSS，Windows 用工作集（Working Set），Linux 用 RSS；单位 MiB（1 MiB = 1024² 字节）。
优先用 psutil；没有 psutil 时在 Linux 上读 /proc，其他系统显示「暂不可用」，不估算、不沿用旧值。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

MIB = 1024 * 1024

try:  # pragma: no cover - 取决于环境
    import psutil
except Exception:  # noqa: BLE001
    psutil = None

BACKEND_NOTE = "无独立后台：本机 HTTP 服务和计算任务（导入、实验、因子检验）都是主进程里的线程，不单独占 PID"


def metric_name(platform: str | None = None) -> str:
    platform = sys.platform if platform is None else platform
    return "工作集（Working Set）" if platform == "win32" else "常驻内存 RSS"


def child_role(name: str) -> str:
    n = (name or "").lower()
    if "msedgewebview2" in n:
        return "窗口渲染子进程（WebView2）"
    if "webkit" in n:
        return "窗口渲染子进程（WebKit）"
    if n.startswith(("python", "quantlab")):
        return "计算子进程"
    if n in ("explorer.exe", "open", "xdg-open"):
        return "打开文件夹（系统程序，短暂存在）"
    return "子进程"


def _mib(b):
    return None if b is None else round(b / MIB, 1)


def _rss(p) -> int | None:
    try:
        return p.memory_info().rss  # Windows 上 psutil 的 rss 就是工作集
    except Exception:  # noqa: BLE001
        return None


def _proc_rss_linux(pid: int) -> int | None:
    try:
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def _children_linux(pid: int) -> list[int]:
    out: list[int] = []
    try:
        for task in Path(f"/proc/{pid}/task").iterdir():
            try:
                out += [int(x) for x in (task / "children").read_text().split()]
            except OSError:
                pass
    except OSError:
        pass
    for c in list(out):
        out += _children_linux(c)
    return out


def _name_linux(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/comm").read_text().strip()
    except OSError:
        return ""


def processes(main_role: str = "主进程", mode: str = "") -> dict:
    """返回本应用当前的进程列表。读不到的数值为 None（界面显示「暂不可用」）。"""
    me = os.getpid()
    rows: list[dict] = []
    source = "psutil" if psutil else ("/proc" if Path("/proc/self/status").exists() else None)
    if psutil:
        try:
            p = psutil.Process(me)
            rows.append({"role": main_role, "name": p.name(), "pid": me, "mem_mib": _mib(_rss(p)), "threads": p.num_threads()})
            for c in p.children(recursive=True):
                try:
                    name = c.name()
                    rows.append({"role": child_role(name), "name": name, "pid": c.pid, "mem_mib": _mib(_rss(c)),
                                 "threads": c.num_threads()})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        except Exception:  # noqa: BLE001
            rows = [{"role": main_role, "name": "", "pid": me, "mem_mib": None, "threads": None}]
    elif source == "/proc":
        rows.append({"role": main_role, "name": _name_linux(me), "pid": me, "mem_mib": _mib(_proc_rss_linux(me)),
                     "threads": None})
        for c in _children_linux(me):
            rss = _proc_rss_linux(c)
            if rss is None:      # 已结束的进程不列出旧值
                continue
            name = _name_linux(c)
            rows.append({"role": child_role(name), "name": name, "pid": c, "mem_mib": _mib(rss), "threads": None})
    else:
        rows.append({"role": main_role, "name": "", "pid": me, "mem_mib": None, "threads": None})
    known = [r["mem_mib"] for r in rows if r["mem_mib"] is not None]
    return {
        "sampled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": sys.platform,
        "metric": metric_name(),
        "unit": "MiB",
        "source": source or "暂不可用",
        "mode": mode,
        "processes": rows,
        "total_mib": round(sum(known), 1) if known and len(known) == len(rows) else None,
        "backend": BACKEND_NOTE,
        "external": [],
    }


def external_opend() -> list[dict]:
    """富途/moomoo OpenD：应用外部的独立服务（用户自己打开，不随本应用退出），单独列出、不计入本应用合计。"""
    if not psutil:
        return []
    out = []
    try:
        for p in psutil.process_iter(["name", "pid"]):
            name = (p.info.get("name") or "")
            if "opend" in name.lower().replace(" ", "").replace("_", ""):
                try:
                    out.append({"name": name, "pid": p.pid, "mem_mib": _mib(_rss(p)),
                                "note": "富途/moomoo OpenD（外部独立服务，本应用只连接它，退出时不会关闭它）"})
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    out.append({"name": name, "pid": p.pid, "mem_mib": None, "note": "富途/moomoo OpenD（外部独立服务）"})
    except Exception:  # noqa: BLE001
        return out
    return out


def stop_children(timeout: float = 3.0) -> int:
    """退出时结束本进程启动的全部子进程（先 terminate，超时再 kill）。返回处理的数量。"""
    if not psutil:
        return 0
    try:
        kids = psutil.Process(os.getpid()).children(recursive=True)
    except Exception:  # noqa: BLE001
        return 0
    for c in kids:
        try:
            c.terminate()
        except Exception:  # noqa: BLE001
            pass
    try:
        _, alive = psutil.wait_procs(kids, timeout=timeout)
    except Exception:  # noqa: BLE001
        alive = kids
    for c in alive:
        try:
            c.kill()
        except Exception:  # noqa: BLE001
            pass
    return len(kids)
