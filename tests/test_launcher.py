"""启动入口 run.py 的窗口/退出流程测试：用假的 pywebview 模块代替真窗口。

- 关闭窗口（webview.start 返回）→ 进程自己退出，端口释放
- 窗口启动失败 → 写日志并改用浏览器；页面「退出」→ 进程退出
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FAKE_CLOSE = '''
import time
class _Ev:
    def __init__(self): self.fns = []
    def __iadd__(self, f): self.fns.append(f); return self
class _W:
    def __init__(self): self.events = type("E", (), {"closed": _Ev()})()
    def destroy(self): pass
_w = []
def create_window(*a, **k):
    _w.append(_W()); return _w[-1]
def start(**k):
    time.sleep(1.5)  # 模拟用户 1.5 秒后关掉窗口
    for f in _w[-1].events.closed.fns: f()
'''

FAKE_FAIL = '''
class _Ev:
    def __iadd__(self, f): return self
class _W:
    events = type("E", (), {"closed": _Ev()})()
    def destroy(self): pass
def create_window(*a, **k): return _W()
def start(**k): raise RuntimeError("fake: no GUI")
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_open(port):
    s = socket.socket()
    s.settimeout(1)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


class LauncherTests(unittest.TestCase):
    def launch(self, fake_src):
        tmp = Path(tempfile.mkdtemp(prefix="studio-launch-"))
        (tmp / "fake").mkdir()
        (tmp / "fake" / "webview.py").write_text(fake_src, encoding="utf-8")
        port = free_port()
        env = dict(os.environ, PYTHONPATH=str(tmp / "fake"), STUDIO_DATA_DIR=str(tmp / "data"),
                   STUDIO_URL_FILE=str(tmp / "url.txt"), BROWSER="true" if os.name != "nt" else "")
        proc = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--port", str(port)], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        return proc, port, tmp

    def test_window_close_exits(self):
        proc, port, tmp = self.launch(FAKE_CLOSE)
        proc.wait(30)
        self.assertEqual(proc.returncode, 0)
        self.assertFalse(port_open(port))
        log = (tmp / "data" / "studio.log").read_text(encoding="utf-8")
        self.assertIn("窗口已关闭", log)
        self.assertIn("已退出", log)

    def test_window_failure_falls_back_and_quit(self):
        proc, port, tmp = self.launch(FAKE_FAIL)
        for _ in range(100):
            if (tmp / "url.txt").exists() and "窗口启动失败" in (tmp / "data" / "studio.log").read_text(encoding="utf-8", errors="replace"):
                break
            time.sleep(0.1)
        token = (tmp / "url.txt").read_text(encoding="utf-8").split("t=")[-1]
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status/processes", headers={"X-Studio-Token": token})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(json.loads(r.read())["mode"], "browser")
        req = urllib.request.Request(f"http://127.0.0.1:{port}/api/shutdown", method="POST", headers={"X-Studio-Token": token})
        urllib.request.urlopen(req, timeout=10).read()
        proc.wait(30)
        self.assertFalse(port_open(port))
        log = (tmp / "data" / "studio.log").read_text(encoding="utf-8")
        self.assertIn("窗口启动失败，改用浏览器", log)
        self.assertIn("已退出", log)


if __name__ == "__main__":
    unittest.main()
