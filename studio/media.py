"""FFmpeg 定位、可取消的子进程、WAV 读写。"""

import os
import shutil
import ssl
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from . import paths

SAMPLE_RATE = 24000
CREATE_NO_WINDOW = 0x08000000 if paths.IS_WIN else 0


class Cancelled(Exception):
    pass


class ToolError(RuntimeError):
    pass


_ffmpeg_cache = {}


def find_ffmpeg(override=""):
    if override and Path(override).is_file():
        return override
    if "path" in _ffmpeg_cache:
        return _ffmpeg_cache["path"]
    exe = "ffmpeg.exe" if paths.IS_WIN else "ffmpeg"
    candidates = [os.environ.get("STUDIO_FFMPEG", ""), str(paths.bundled_bin_dir() / exe), shutil.which("ffmpeg") or ""]
    if paths.IS_MAC:
        # 从访达双击启动时 PATH 里没有 Homebrew
        candidates += ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    found = next((c for c in candidates if c and Path(c).is_file()), "")
    if not found:
        try:
            import imageio_ffmpeg  # 随 pip 安装附带各平台的 ffmpeg

            found = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            found = ""
    if found:
        _ffmpeg_cache["path"] = found
    return found


def require_ffmpeg(override=""):
    f = find_ffmpeg(override)
    if not f:
        raise ToolError("找不到 FFmpeg。请运行 pip install imageio-ffmpeg，或在设置里填写 ffmpeg 路径。")
    return f


class Runner:
    """在任务线程中跑外部命令；取消时杀掉当前子进程。"""

    def __init__(self, cancel_event=None):
        self.cancel_event = cancel_event or threading.Event()
        self._proc = None
        self._lock = threading.Lock()

    def check(self):
        if self.cancel_event.is_set():
            raise Cancelled()

    def kill(self):
        with self._lock:
            p = self._proc
        if p and p.poll() is None:
            try:
                p.kill()
            except OSError:
                pass

    def run(self, args, timeout=None, on_line=None, input_bytes=None):
        self.check()
        kwargs = {"stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "stdin": subprocess.PIPE if input_bytes else subprocess.DEVNULL}
        if CREATE_NO_WINDOW:
            kwargs["creationflags"] = CREATE_NO_WINDOW
        try:
            proc = subprocess.Popen([str(a) for a in args], **kwargs)
        except FileNotFoundError as e:
            raise ToolError(f"无法启动 {args[0]}：{e}") from e
        with self._lock:
            self._proc = proc
        out_chunks, err_chunks = [], []

        def pump(stream, sink, cb):
            for line in iter(stream.readline, b""):
                sink.append(line)
                if cb:
                    try:
                        cb(line.decode("utf-8", "replace").strip())
                    except Exception:
                        pass
            stream.close()

        t1 = threading.Thread(target=pump, args=(proc.stdout, out_chunks, on_line), daemon=True)
        t2 = threading.Thread(target=pump, args=(proc.stderr, err_chunks, None), daemon=True)
        t1.start()
        t2.start()
        if input_bytes:
            try:
                proc.stdin.write(input_bytes)
                proc.stdin.close()
            except OSError:
                pass
        start = time.time()
        while proc.poll() is None:
            if self.cancel_event.is_set():
                proc.kill()
                proc.wait()
                raise Cancelled()
            if timeout and time.time() - start > timeout:
                proc.kill()
                proc.wait()
                raise ToolError(f"{Path(str(args[0])).name} 超时")
            time.sleep(0.05)
        t1.join(2)
        t2.join(2)
        with self._lock:
            self._proc = None
        if proc.returncode != 0:
            err = b"".join(err_chunks).decode("utf-8", "replace").strip().splitlines()[-6:]
            raise ToolError(f"{Path(str(args[0])).name} 失败（{proc.returncode}）：" + " / ".join(err))
        return b"".join(out_chunks)


def normalize_audio(runner, ffmpeg, src, dst):
    """任何格式的配音统一转成 24kHz 单声道 16 位 WAV，后面只按采样数算时长。"""
    tmp = Path(str(dst) + ".part.wav")
    runner.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", src, "-ac", "1", "-ar", str(SAMPLE_RATE), "-sample_fmt", "s16", "-f", "wav", tmp], timeout=120)
    os.replace(tmp, dst)


def wav_frames(path):
    with wave.open(str(path), "rb") as w:
        return w.getnframes()


def read_pcm(path):
    with wave.open(str(path), "rb") as w:
        if w.getframerate() != SAMPLE_RATE or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ToolError(f"音频格式不一致：{path}")
        return w.readframes(w.getnframes())


def silence(seconds):
    return b"\x00\x00" * int(round(max(0.0, seconds) * SAMPLE_RATE))


def write_wav(path, pcm):
    path = Path(path)
    tmp = path.with_name(".tmp-" + path.name)
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm)
    os.replace(tmp, path)


def open_in_file_manager(path, reveal=False):
    path = str(path)
    if paths.IS_WIN:
        if reveal:
            subprocess.Popen(f'explorer /select,"{path}"')
        else:
            os.startfile(path)  # type: ignore[attr-defined]
    elif paths.IS_MAC:
        subprocess.Popen(["open", "-R", path] if reveal else ["open", path])
    else:
        subprocess.Popen(["xdg-open", os.path.dirname(path) if reveal else path])


_ssl_ctx = None


def ssl_context():
    """python.org 安装的 macOS Python 默认没有根证书，优先用 certifi。"""
    global _ssl_ctx
    if _ssl_ctx is None:
        try:
            import certifi

            _ssl_ctx = ssl.create_default_context(cafile=certifi.where()) if paths.IS_MAC else ssl.create_default_context()
        except Exception:
            _ssl_ctx = ssl.create_default_context()
    return _ssl_ctx


def urlopen(req, timeout):
    import urllib.request

    url = req.full_url if hasattr(req, "full_url") else str(req)
    if url.startswith("https://"):
        return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())
    return urllib.request.urlopen(req, timeout=timeout)


def python_info():
    return f"Python {sys.version.split()[0]}"
