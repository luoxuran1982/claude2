"""使用指南、源码白名单、运行状态接口、退出释放端口：python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from quantlab import guide, sysinfo  # noqa: E402


class GuideTest(unittest.TestCase):
    def test_docs_cover_all_markdown(self):
        listed = {d["path"] for d in guide.list_docs(ROOT)}
        expected = {"README.md"} | {p.relative_to(ROOT).as_posix() for p in (ROOT / "docs").rglob("*.md")}
        self.assertEqual(listed, expected)
        for name in ("docs/00 项目阅读入口.md", "docs/01 使用指南.md", "docs/02 软件框架与系统架构.md"):
            self.assertIn(name, listed)
        self.assertEqual(guide.list_docs(ROOT)[0]["path"], "docs/00 项目阅读入口.md")

    def test_entry_yaml(self):
        text = (ROOT / "docs" / "00 项目阅读入口.md").read_text(encoding="utf-8")
        head = text.split("---")[1]
        self.assertIn("project: QuantLab 量化学习实验室", head)
        self.assertIn("author: Claude", head)

    def test_guide_covers_eight_topics(self):
        text = (ROOT / "docs" / "01 使用指南.md").read_text(encoding="utf-8")
        for k in ("怎样开始和结束", "主要任务", "数据从哪来", "结果如何理解", "出了问题", "软件框架", "进程与内存", "缩放界面",
                  "macOS", "Windows", "MiB", "70%", "120%"):
            self.assertIn(k, text)

    def test_doc_traversal_blocked(self):
        for bad in ("../README.md", "/etc/passwd", "docs/../../x.md", "quantlab/app.py"):
            with self.assertRaises(LookupError):
                guide.read_doc(bad, ROOT)
        with self.assertRaises(LookupError):
            guide.doc_asset("docs/../quantlab/app.py", ROOT)

    def test_sources_whitelist_and_zip(self):
        files = guide.source_files(ROOT)
        for must in ("run.py", "quantlab/app.py", "quantlab/web/app.js", "quantlab/web/markdown.js", "packaging/build.py",
                     "tests/test_core.py"):
            self.assertIn(must, files)
        self.assertFalse([f for f in files if "vendor/" in f or "__pycache__" in f])
        src = guide.Sources(ROOT, bundle=Path("/nonexistent"))
        self.assertIsNone(src.zip_path)
        self.assertEqual(src.read("quantlab/app.py")["text"], (ROOT / "quantlab/app.py").read_text(encoding="utf-8"))
        for bad in ("../README.md", "quantlab/web/vendor/echarts.min.js", "/etc/passwd", "quantlab/../run.py"):
            with self.assertRaises(LookupError):
                src.read(bad)
        z = zipfile.ZipFile(io.BytesIO(src.zip_bytes()))
        self.assertEqual(sorted(n[len("quant-lab/"):] for n in z.namelist()), sorted(files))

    def test_bundled_sources(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "projectsrc").mkdir()
            (Path(tmp) / "projectsrc" / guide.SRC_ZIP_NAME).write_bytes(guide.build_source_zip(ROOT))
            src = guide.Sources(ROOT, bundle=Path(tmp))
            self.assertIsNotNone(src.zip_path)
            self.assertEqual(len(src.list()), len(guide.source_files(ROOT)))
            self.assertIn("def main", src.read("quantlab/app.py")["text"])

    def test_processes(self):
        info = sysinfo.processes("主进程")
        self.assertEqual(info["unit"], "MiB")
        self.assertEqual(info["processes"][0]["pid"], os.getpid())
        self.assertIn("无独立后台", info["backend"])
        if info["source"] != "暂不可用":
            self.assertGreater(info["processes"][0]["mem_mib"], 1)
        self.assertEqual(sysinfo.metric_name("win32"), "工作集（Working Set）")
        self.assertEqual(sysinfo.metric_name("darwin"), "常驻内存 RSS")


def _port_open(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


class ServeQuitTest(unittest.TestCase):
    """启动服务模式 → 接口可用 → 子进程出现在进程列表 → /api/quit → 进程退出、端口释放。"""

    def test_quit_releases_port(self):
        with tempfile.TemporaryDirectory() as tmp:
            url_file = Path(tmp) / "url.txt"
            env = dict(os.environ, QUANTLAB_DATA=str(Path(tmp) / "data"))
            p = subprocess.Popen([sys.executable, str(ROOT / "run.py"), "--serve-only", "--url-file", str(url_file)],
                                 env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                for _ in range(100):
                    if url_file.exists() and url_file.read_text():
                        break
                    time.sleep(0.1)
                url = url_file.read_text().strip()
                base, token = url.split("/?t=")
                port = int(base.rsplit(":", 1)[1])
                info = json.loads(urllib.request.urlopen(base + "/api/processes", timeout=10).read())
                self.assertEqual(info["processes"][0]["pid"], p.pid)
                docs = json.loads(urllib.request.urlopen(base + "/api/docs", timeout=10).read())
                self.assertGreaterEqual(len(docs["docs"]), 4)
                with self.assertRaises(urllib.error.HTTPError):
                    urllib.request.urlopen(base + "/api/source?path=..%2FREADME.md", timeout=10)
                req = urllib.request.Request(base + "/api/quit", data=b"{}", method="POST",
                                             headers={"X-QuantLab-Token": token, "Content-Type": "application/json"})
                urllib.request.urlopen(req, timeout=10).read()
                p.wait(timeout=20)
                self.assertFalse(_port_open(port))
            finally:
                if p.poll() is None:
                    p.kill()


if __name__ == "__main__":
    unittest.main()
