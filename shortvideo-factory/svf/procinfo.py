"""运行状态：本次启动涉及的每个进程的职责、PID、内存和采样时间。

口径：macOS / Linux 用常驻内存 RSS，Windows 用工作集（Working Set），单位 MiB（1 MiB = 1024² 字节）。
每次请求现读现算，不缓存；读不到的值返回 None，界面显示「暂不可用」，不拿旧值或估算值顶替。
已结束的进程不会出现在列表里（每次都从启动器重新枚举子进程树）。
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


def role_of(name: str, pid: int, main_pid: int, webui_pid: int | None, cmdline: list[str] | None = None) -> str:
    n = (name or "").lower()
    if pid == main_pid:
        return "启动器：窗口 + 本机服务 + 反向代理（主进程）"
    if webui_pid and pid == webui_pid:
        return "生成界面后台：Streamlit + MoneyPrinterTurbo（文案、配音、素材、合成都在这里）"
    if "ffmpeg" in n or "ffprobe" in n:
        return "FFmpeg：视频/音频处理（生成任务运行时才有）"
    if "msedgewebview2" in n:
        return "WebView2 子进程（窗口网页渲染，由主进程启动）"
    if "webkit" in n:
        return "WebKit 网页进程"
    if n.startswith("python"):
        return "Python 子进程（由生成界面后台启动）"
    return "本应用启动的子进程"


def snapshot(webui_pid: int | None = None, webui_state: str = "") -> dict:
    sampled = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    threads = sorted({t.name for t in threading.enumerate()})
    processes, note = [], None
    try:
        import psutil
    except ImportError:
        psutil = None
    main_pid = os.getpid()
    if psutil:
        me = psutil.Process(main_pid)
        try:
            children = me.children(recursive=True)
        except psutil.Error:
            children = []
        for p in [me] + children:
            try:
                with p.oneshot():
                    name = p.name()
                    mem = p.memory_info().rss / MIB   # psutil 在 Windows 上的 rss 就是工作集
                    ppid = p.ppid()
                processes.append({"pid": p.pid, "name": name, "role": role_of(name, p.pid, main_pid, webui_pid),
                                  "mem_mib": round(mem, 1), "parent": ppid})
            except psutil.NoSuchProcess:
                continue   # 枚举之后刚好结束的进程：不列出，避免显示已结束进程
            except psutil.Error:
                processes.append({"pid": p.pid, "name": "?", "role": role_of("", p.pid, main_pid, webui_pid),
                                  "mem_mib": None, "parent": None})
    else:
        processes.append({"pid": main_pid, "name": os.path.basename(sys.executable),
                          "role": role_of("", main_pid, main_pid, webui_pid), "mem_mib": None, "parent": os.getppid()})
        note = "缺少 psutil，只能列出主进程，内存暂不可用"
    if sys.platform == "darwin":
        note = (note + "；" if note else "") + ("macOS 的 WKWebView 网页内容进程由系统托管（活动监视器里名为「AI 短视频工厂 网页内容」"
                                               "或 com.apple.WebKit.*），不是本应用的子进程，这里读不到，暂不可用")
    known = [p["mem_mib"] for p in processes if p["mem_mib"] is not None]
    webui_listed = bool(webui_pid) and any(p["pid"] == webui_pid for p in processes)
    if webui_pid and webui_listed:
        backend = f"有独立后台：生成界面后台 PID {webui_pid}（状态：{webui_state or '未知'}），随窗口关闭/退出而停止"
    elif webui_state in ("starting",):
        backend = "生成界面后台正在启动"
    else:
        backend = f"生成界面后台当前未运行（状态：{webui_state or '未启动'}）"
    return {
        "sampled_at": sampled,
        "platform": sys.platform,
        "metric": metric_name(),
        "unit": "MiB",
        "processes": processes,
        "count": len(processes),
        "total_mib": round(sum(known), 1) if known else None,
        "total_note": "合计是各进程数值直接相加，进程之间共享的内存（系统库等）可能被重复计算" if len(known) > 1 else None,
        "backend_note": backend,
        "external": [
            {"name": "大模型、素材网站、配音等云端服务", "note": "通过网络调用，不在本机运行，不计入本应用合计"},
            {"name": "本机 Ollama / Chatterbox / Kokoro 等（如果你自己装了）", "note": "独立的外部服务，不随本应用启停，也不计入合计"},
        ],
        "threads": threads,
        "note": note,
    }
