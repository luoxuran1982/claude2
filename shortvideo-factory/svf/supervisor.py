"""启动、监视和停止「生成界面后台」：上游 MoneyPrinterTurbo 的 Streamlit WebUI 子进程。

进程关系（本应用自用的进程都在这棵树里）：

    启动器 ShortVideoFactory（窗口 + 本机服务 + 反向代理）
    └─ 生成界面后台：随包 Python 运行 Streamlit（上游 WebUI，含 Edge TTS、素材下载等线程）
       └─ FFmpeg（生成视频时由 MoviePy / pydub 启动，任务结束即退出）

停止顺序：先礼后兵。POSIX 上整组发 SIGTERM，Windows 上逐个结束；最多等 6 秒，仍在的再强制结束，
最后用 psutil 逐个确认都已退出。异常收尾：
- 子进程里的看门狗每 2 秒检查启动器是否还在，不在就结束整棵子树（见 svf/child.py）；
- Windows 上子进程放进「关闭即结束」的作业对象，启动器无论怎么退出，系统都会结束作业里的全部进程；
- 启动时读取上次留下的运行记录 run.json，若发现上次的后台还活着（PID 与创建时间都对得上）就先结束它。
"""
from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

log = logging.getLogger("svf")

BASE_PATH = "app"          # Streamlit 的 baseUrlPath，代理把 /app/ 原样转发
READY_TIMEOUT = 240        # 首次启动要导入 pandas/pyarrow/moviepy 等，慢的电脑可能要一两分钟


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return s.getsockname()[1]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# ---------------- Windows 作业对象：启动器退出（含崩溃）时系统自动结束子进程树 ----------------

class _WinJob:
    def __init__(self):
        import ctypes
        from ctypes import wintypes

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                        ("SchedulingClass", wintypes.DWORD)]

        class EXTENDED(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                        ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                        ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k32.CreateJobObjectW.restype = wintypes.HANDLE
        self.k32.OpenProcess.restype = wintypes.HANDLE
        self.handle = self.k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject 失败")
        info = EXTENDED()
        info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.k32.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject 失败")

    def assign(self, pid: int) -> None:
        import ctypes
        h = self.k32.OpenProcess(0x0100 | 0x0001, False, pid)  # PROCESS_SET_QUOTA | PROCESS_TERMINATE
        if not h:
            raise OSError(ctypes.get_last_error(), "OpenProcess 失败")
        try:
            if not self.k32.AssignProcessToJobObject(self.handle, h):
                raise OSError(ctypes.get_last_error(), "AssignProcessToJobObject 失败")
        finally:
            self.k32.CloseHandle(h)


# ---------------- 运行记录：用于下次启动时清理崩溃遗留 ----------------

def _create_time(pid: int) -> float | None:
    try:
        import psutil
        return psutil.Process(pid).create_time()
    except Exception:
        return None


def read_run_record(directory: Path) -> dict | None:
    try:
        return json.loads((directory / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_run_record(directory: Path, record: dict) -> None:
    tmp = directory / "run.json.tmp"
    tmp.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, directory / "run.json")


def clear_run_record(directory: Path) -> None:
    try:
        (directory / "run.json").unlink()
    except OSError:
        pass


def same_process(pid: int | None, created: float | None) -> bool:
    """PID 会被系统复用，必须连同创建时间一起比对，才能认定是同一个进程。"""
    if not pid or created is None:
        return False
    ct = _create_time(pid)
    return ct is not None and abs(ct - created) < 1.0


def kill_tree(pid: int, timeout: float = 6.0) -> list[int]:
    """结束 pid 及其所有子孙进程，返回结束前看到的 PID 列表。"""
    import psutil
    try:
        root = psutil.Process(pid)
        procs = [root] + root.children(recursive=True)
    except psutil.Error:
        return []
    pids = [p.pid for p in procs]
    for p in reversed(procs):
        try:
            p.terminate()
        except psutil.Error:
            pass
    _, alive = psutil.wait_procs(procs, timeout=timeout)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    psutil.wait_procs(alive, timeout=3)
    return pids


def cleanup_stale(directory: Path) -> str | None:
    """上次运行若异常退出、后台还活着，就结束它。返回给日志的说明。"""
    rec = read_run_record(directory)
    if not rec:
        return None
    if same_process(rec.get("launcher_pid"), rec.get("launcher_started")):
        return "running"  # 另一个实例正在运行
    msg = "上次运行没有正常收尾"
    if same_process(rec.get("webui_pid"), rec.get("webui_started")):
        killed = kill_tree(rec["webui_pid"])
        msg += f"，已结束遗留的生成界面后台及子进程 {killed}"
    clear_run_record(directory)
    return msg


# ---------------- 生成界面后台 ----------------

class WebUI:
    """一个 Streamlit 子进程的生命周期：start → ready → stop。线程安全。"""

    def __init__(self, python: Path, child_script: Path, upstream: Path, data: Path, fonts_default: str,
                 port: int | None = None):
        self.python, self.child_script, self.upstream, self.data = Path(python), Path(child_script), Path(upstream), Path(data)
        self.fonts_default = fonts_default
        self.fixed_port = port
        self.port: int | None = None
        self.proc: subprocess.Popen | None = None
        self.started_at: float | None = None
        self.state = "stopped"     # stopped / starting / ready / failed / stopping
        self.error = ""
        self.restarts = 0
        self._lock = threading.RLock()
        self._job = None
        self._log_file = None

    # 子进程环境：上游的配置、任务、字体、音乐目录都指向用户数据目录（见 UPSTREAM.md 补丁 P1/P2/P3）
    def env(self) -> dict:
        env = dict(os.environ)
        for k in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP", "VIRTUAL_ENV", "PYINSTALLER_RESET_ENVIRONMENT"):
            env.pop(k, None)
        if getattr(sys, "frozen", False):
            # PyInstaller 启动器会改 DYLD/LD 库路径，别让它传给随包 Python
            for k in ("LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH", "TCL_LIBRARY", "TK_LIBRARY"):
                env.pop(k, None)
                if env.get(k + "_ORIG"):
                    env[k] = env.pop(k + "_ORIG")
        env.update({
            "MPT_CONFIG_FILE": str(self.data / "config.toml"),
            "MPT_STORAGE_DIR": str(self.data / "storage"),
            "MPT_FONT_DIR": str(self.data / "fonts"),
            "MPT_SONG_DIR": str(self.data / "songs"),
            "MPT_DEFAULT_FONT": self.fonts_default,
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "SVF_CHILD": "1",
        })
        if getattr(sys, "frozen", False):
            env["PYTHONDONTWRITEBYTECODE"] = "1"   # 安装目录只读，.pyc 已在打包时预编译
        return env

    def command(self, port: int) -> list[str]:
        return [str(self.python), "-X", "utf8", str(self.child_script), "--parent-pid", str(os.getpid()),
                "--port", str(port), "--upstream", str(self.upstream), "--base-path", BASE_PATH,
                "--extra-path", str(self.data / "python-packages")]

    def start(self) -> None:
        with self._lock:
            if self.proc and self.proc.poll() is None:
                return
            self.port = self.fixed_port or free_port()
            self.state, self.error = "starting", ""
            logs = self.data / "logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / "webui.log"
            if log_path.exists() and log_path.stat().st_size > 5_000_000:
                log_path.replace(logs / "webui.old.log")
            self._log_file = open(log_path, "ab")
            self._log_file.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动生成界面后台 =====\n".encode("utf-8"))
            self._log_file.flush()
            kwargs: dict = {"cwd": str(self.upstream), "env": self.env(), "stdin": subprocess.DEVNULL,
                            "stdout": self._log_file, "stderr": subprocess.STDOUT}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000 | 0x00000200  # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            else:
                kwargs["start_new_session"] = True  # 自成进程组，停止时整组结束（含 FFmpeg）
            cmd = self.command(self.port)
            log.info("启动生成界面后台：%s", " ".join(cmd))
            self.proc = subprocess.Popen(cmd, **kwargs)
            self.started_at = time.time()
            if os.name == "nt":
                try:
                    self._job = self._job or _WinJob()
                    self._job.assign(self.proc.pid)
                except OSError as exc:
                    log.warning("无法把后台放进作业对象（%s），崩溃时依靠看门狗收尾", exc)
        threading.Thread(target=self._wait_ready, name="webui-ready", daemon=True).start()

    def _wait_ready(self) -> None:
        proc, port = self.proc, self.port
        deadline = time.time() + READY_TIMEOUT
        url = f"http://127.0.0.1:{port}/{BASE_PATH}/_stcore/health"
        while time.time() < deadline:
            if proc.poll() is not None:
                with self._lock:
                    if self.proc is proc and self.state != "stopping":
                        self.state = "failed"
                        self.error = f"生成界面后台启动后退出（返回码 {proc.returncode}）。\n" + self.log_tail()
                log.error("生成界面后台退出，返回码 %s", proc.returncode)
                return
            try:
                with urllib.request.urlopen(url, timeout=2) as r:
                    if r.status == 200:
                        with self._lock:
                            if self.proc is proc and self.state == "starting":
                                self.state = "ready"
                        log.info("生成界面后台就绪：PID %s，端口 %s，用时 %.1f 秒", proc.pid, port, time.time() - self.started_at)
                        threading.Thread(target=self._watch_exit, args=(proc,), name="webui-exit", daemon=True).start()
                        return
            except Exception:
                pass
            time.sleep(0.5)
        with self._lock:
            if self.proc is proc and self.state == "starting":
                self.state = "failed"
                self.error = f"等了 {READY_TIMEOUT} 秒生成界面仍未就绪。\n" + self.log_tail()

    def _watch_exit(self, proc) -> None:
        proc.wait()
        with self._lock:
            if self.proc is proc and self.state == "ready":
                self.state = "failed"
                self.error = f"生成界面后台意外退出（返回码 {proc.returncode}）。\n" + self.log_tail()
                log.error("生成界面后台意外退出，返回码 %s", proc.returncode)

    def log_tail(self, lines: int = 30) -> str:
        try:
            data = (self.data / "logs" / "webui.log").read_bytes()[-12000:]
            return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])
        except OSError:
            return ""

    def pid(self) -> int | None:
        p = self.proc
        return p.pid if p and p.poll() is None else None

    def stop(self, timeout: float = 6.0) -> dict:
        """停止后台及其子进程，并确认它们都已退出、端口已释放。返回停止结果（写进日志）。"""
        with self._lock:
            proc, port = self.proc, self.port
            if not proc:
                self.state = "stopped"
                return {"stopped": [], "alive": [], "port_released": True}
            self.state = "stopping"
        result = {"stopped": [], "alive": [], "port_released": True}
        try:
            import psutil
            try:
                root = psutil.Process(proc.pid)
                procs = [root] + root.children(recursive=True)
            except psutil.Error:
                procs = []
            result["stopped"] = [p.pid for p in procs]
            if os.name != "nt" and proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)   # 整组：Streamlit + FFmpeg
                except OSError:
                    pass
            else:
                for p in reversed(procs):
                    try:
                        p.terminate()
                    except psutil.Error:
                        pass
            _, alive = psutil.wait_procs(procs, timeout=timeout)
            for p in alive:
                log.warning("进程 %s 未在 %s 秒内退出，强制结束", p.pid, timeout)
                try:
                    p.kill()
                except psutil.Error:
                    pass
            _, alive = psutil.wait_procs(alive, timeout=3)
            result["alive"] = [p.pid for p in alive]
        except ImportError:
            proc.terminate()
            try:
                proc.wait(timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            proc.wait(3)
        except subprocess.TimeoutExpired:
            pass
        if port:
            for _ in range(20):
                if not port_open(port):
                    break
                time.sleep(0.2)
            result["port_released"] = not port_open(port)
        with self._lock:
            if self._log_file:
                try:
                    self._log_file.close()
                except OSError:
                    pass
                self._log_file = None
            self.state = "stopped"
        log.info("生成界面后台已停止：%s", result)
        return result

    def restart(self) -> None:
        self.stop()
        with self._lock:
            self.restarts += 1
        self.start()

    def status(self) -> dict:
        return {"state": self.state, "pid": self.pid(), "port": self.port, "error": self.error,
                "started_at": self.started_at, "restarts": self.restarts}
