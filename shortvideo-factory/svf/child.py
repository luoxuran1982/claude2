"""Streamlit 子进程入口：在随包 Python 里运行上游 WebUI（upstream/webui/Main.py）。

由启动器（svf/supervisor.py）这样调用：

    <runtime python> svf_child.py --parent-pid 1234 --port 51234 --upstream <dir> [--extra-path <dir>]

本文件只用标准库，安装包里原样复制为 payload/child/svf_child.py，不依赖 svf 包。

除了启动 Streamlit，这里还有一个「看门狗」线程：每 2 秒检查一次启动器（父进程）是否还在。
启动器崩溃或被强制结束、来不及收尾时，看门狗结束本进程和它启动的 FFmpeg 等子进程，
所以不会长期遗留后台。Windows 上启动器另外把本进程放进了「关闭即结束」的作业对象（Job Object），双保险。
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

WATCH_INTERVAL = 2.0


def parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        handle = kernel32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
        if not handle:
            # 87 = ERROR_INVALID_PARAMETER：进程已不存在；其他错误（如权限）按「还在」处理，避免误杀
            return ctypes.get_last_error() != 87
        try:
            return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT：还在运行
        finally:
            kernel32.CloseHandle(handle)
    # POSIX：父进程退出后本进程会被过继给 init/launchd，getppid() 随之改变
    if os.getppid() != pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def kill_own_tree() -> None:
    """结束本进程及其全部子进程（FFmpeg 等）。"""
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(os.getpid())],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000)  # CREATE_NO_WINDOW
        os._exit(3)
    # 启动器用 start_new_session 启动本进程，本进程是进程组组长：整组发信号即可带上所有子进程
    try:
        os.killpg(os.getpgrp(), signal.SIGTERM)
    except OSError:
        pass
    time.sleep(1.5)
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    except OSError:
        pass
    os._exit(3)


def watchdog(parent_pid: int) -> None:
    while True:
        time.sleep(WATCH_INTERVAL)
        if not parent_alive(parent_pid):
            print(f"[svf-child] 启动器（PID {parent_pid}）已不在，结束生成界面后台及其子进程", flush=True)
            kill_own_tree()


def streamlit_argv(script: str, port: int, base_path: str) -> list[str]:
    return [
        "streamlit", "run", script,
        "--server.address=127.0.0.1",
        f"--server.port={port}",
        f"--server.baseUrlPath={base_path}",
        "--server.headless=true",
        "--server.fileWatcherType=none",
        "--server.runOnSave=false",
        "--server.showEmailPrompt=false",
        "--server.enableCORS=true",
        "--server.maxUploadSize=1024",
        "--browser.gatherUsageStats=false",
        "--browser.serverAddress=127.0.0.1",
        "--global.developmentMode=false",
        "--client.toolbarMode=minimal",
        "--client.showErrorDetails=true",
        "--logger.hideWelcomeMessage=true",
    ]


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description="AI 短视频工厂：Streamlit 子进程")
    ap.add_argument("--parent-pid", type=int, required=True)
    ap.add_argument("--port", type=int, required=True)
    ap.add_argument("--upstream", required=True, help="上游 MoneyPrinterTurbo 目录")
    ap.add_argument("--base-path", default="app")
    ap.add_argument("--extra-path", default="", help="可选依赖目录（如 faster-whisper），存在时加入 sys.path")
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    threading.Thread(target=watchdog, args=(args.parent_pid,), name="svf-watchdog", daemon=True).start()

    upstream = os.path.abspath(args.upstream)
    if args.extra_path and os.path.isdir(args.extra_path):
        sys.path.append(args.extra_path)
    sys.path.insert(0, upstream)
    os.environ["PYTHONPATH"] = upstream + os.pathsep + os.environ.get("PYTHONPATH", "")
    os.chdir(upstream)

    sys.argv = streamlit_argv(os.path.join(upstream, "webui", "Main.py"), args.port, args.base_path)
    print(f"[svf-child] PID {os.getpid()} 启动 Streamlit：127.0.0.1:{args.port}/{args.base_path}/", flush=True)
    from streamlit.web import cli as stcli

    sys.exit(stcli.main())


if __name__ == "__main__":
    main()
