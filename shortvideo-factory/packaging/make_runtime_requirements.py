"""从上游的 uv.lock 生成随包 Python 运行时的依赖清单 requirements-runtime.txt（全部精确版本）。

为什么不直接用上游 requirements.txt：它只钉了直接依赖，间接依赖（如 starlette）会解析到最新版。
实测 starlette 1.8.0 + streamlit 1.59.1 时 Streamlit 自带的图标字体请求会报 500，界面图标变成英文单词；
上游 uv.lock 锁定的 starlette 1.2.1 没有这个问题。所以以上游锁文件为准。

去掉的可选依赖：faster-whisper（Whisper 字幕模式才用，还要另下约 3 GB 模型）以及只被它用到的包
（ctranslate2、onnxruntime、av 等）。字幕默认用 Edge TTS 自带的时间戳，不需要它们。

    python packaging/make_runtime_requirements.py      # 需要 uv；升级上游后重新生成并提交
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROP = {"faster-whisper"}
HEADER = """# 生成界面后台（上游 MoneyPrinterTurbo WebUI）的依赖，装进随包的独立 Python 运行时。
# 由 packaging/make_runtime_requirements.py 根据 upstream/uv.lock（上游 commit bbe7eb9）生成，不要手改。
# 已去掉可选的 faster-whisper 及只被它用到的包：{dropped}
"""


def parse(text: str) -> dict[str, dict]:
    pkgs: dict[str, dict] = {}
    cur = None
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9_.\-]+)==", line)
        if m:
            cur = m.group(1).lower()
            pkgs[cur] = {"line": line, "via": set()}
            continue
        if cur is None:
            continue
        v = re.match(r"^\s+#\s+(?:via\s+)?([A-Za-z0-9_.\-]+)\s*$", line)
        if v and v.group(1) != "via":
            pkgs[cur]["via"].add(v.group(1).lower())
    return pkgs


def prune(pkgs: dict[str, dict], drop: set[str]) -> set[str]:
    removed = set(d.lower() for d in drop)
    changed = True
    while changed:
        changed = False
        for name, info in pkgs.items():
            if name in removed or not info["via"]:
                continue
            if info["via"] <= removed:
                removed.add(name)
                changed = True
    return removed


def main() -> None:
    text = subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                           "--format", "requirements-txt"], cwd=ROOT / "upstream", check=True,
                          capture_output=True, text=True).stdout
    pkgs = parse(text)
    removed = prune(pkgs, DROP)
    lines = [HEADER.format(dropped="、".join(sorted(removed)))]
    lines += [info["line"] for name, info in sorted(pkgs.items()) if name not in removed]
    out = ROOT / "requirements-runtime.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(pkgs) - len(removed)} 个包，去掉 {sorted(removed)}")


if __name__ == "__main__":
    sys.exit(main())
