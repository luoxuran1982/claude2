"""随包 edge-tts 的版本，以及上游配音调用在这个版本下能否走通（通信层用假服务端，不连网）。

需要装好 requirements-runtime.txt（CI 的 test 任务会装）；没装时跳过。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:
    import edge_tts
except ImportError:  # 只装了外壳依赖
    edge_tts = None


def pinned(name: str) -> str:
    text = (ROOT / "requirements-runtime.txt").read_text(encoding="utf-8")
    return re.search(rf"^{re.escape(name)}==([^\s;]+)", text, re.M).group(1)


class TestEdgeTtsPin(unittest.TestCase):
    def test_runtime_pins_fixed_edge_tts(self):
        # 7.2.7 在 2026-10 被微软接口拒绝（403），至少 7.3.1
        ver = tuple(int(x) for x in pinned("edge-tts").split("."))
        self.assertGreaterEqual(ver, (7, 3, 1))

    def test_override_documented_in_generator(self):
        sys.path.insert(0, str(ROOT / "packaging"))
        try:
            import make_runtime_requirements as m
        finally:
            sys.path.pop(0)
        self.assertEqual(m.OVERRIDES["edge-tts"][0], pinned("edge-tts"))
        self.assertIn("403", m.OVERRIDES["edge-tts"][1])
        pkgs = {"edge-tts": {"line": "edge-tts==7.2.7 ; python_version >= '3'", "via": set()}}
        self.assertEqual(m.apply_overrides(pkgs, {"edge-tts": ("9.9.9", "x")}), ["edge-tts 7.2.7 → 9.9.9"])
        self.assertEqual(pkgs["edge-tts"]["line"], "edge-tts==9.9.9 ; python_version >= '3'")


@unittest.skipIf(edge_tts is None, "没装 requirements-runtime.txt")
class TestEdgeTtsCompat(unittest.TestCase):
    def test_installed_version_matches_pin(self):
        self.assertEqual(edge_tts.__version__, pinned("edge-tts"))

    def test_upstream_azure_tts_v1_through_fake_service(self):
        tmp = tempfile.mkdtemp(prefix="svf-edge-cfg-")
        env = dict(os.environ, MPT_CONFIG_FILE=os.path.join(tmp, "config.toml"), PYTHONDONTWRITEBYTECODE="1",
                   PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, str(ROOT / "tests" / "edge_tts_compat_check.py")], cwd=ROOT / "upstream",
                           env=env, capture_output=True, text=True, encoding="utf-8", timeout=120)
        line = next((l for l in p.stdout.splitlines() if l.startswith("RESULT ")), None)
        self.assertIsNotNone(line, p.stdout[-2000:] + p.stderr[-4000:])
        r = json.loads(line[7:])
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["audio_same"], r)
        self.assertEqual(r["cues"], ["你好", "世界"], r)
        self.assertIn("你好", r["srt"])
        self.assertTrue(r["gec_version"].startswith("2-"), r)      # 7.3.x 的 v2 令牌
        self.assertTrue(r["gec_token_v2"], r)
        self.assertTrue(r["voice_in_ssml"], r)
        self.assertTrue(r["refused_returns_none"], r)              # 403 时上游重试 3 次后返回 None（界面显示配音失败）
        self.assertTrue(r["refused_no_file"], r)
        self.assertEqual(r["refused_calls"], 6, r)                  # 每次尝试：edge-tts 按服务器时间校正后再连一次


if __name__ == "__main__":
    unittest.main()
