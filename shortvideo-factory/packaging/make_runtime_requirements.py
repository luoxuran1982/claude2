"""从上游的 uv.lock 生成随包 Python 运行时的依赖清单 requirements-runtime.txt（全部精确版本）。

为什么不直接用上游 requirements.txt：它只钉了直接依赖，间接依赖（如 starlette）会解析到最新版。
实测 starlette 1.8.0 + streamlit 1.59.1 时 Streamlit 自带的图标字体请求会报 500，界面图标变成英文单词；
上游 uv.lock 锁定的 starlette 1.2.1 没有这个问题。所以以上游锁文件为准。

去掉的可选依赖：faster-whisper（Whisper 字幕模式才用，还要另下约 3 GB 模型）以及只被它用到的包
（ctranslate2、onnxruntime、av 等）。字幕默认用 Edge TTS 自带的时间戳，不需要它们。

覆盖上游锁定版本（OVERRIDES）：上游锁定的版本已知在用户那里不能用、而上游还没跟进时，在这里改成指定版本，
每条都写明原因；上游锁文件跟上以后就删掉对应条目。覆盖只换版本号，不会自动解析新版本的依赖，所以加条目前
要核对新版本的依赖要求（wheel 里 METADATA 的 Requires-Dist），锁文件里已有的版本满足就不用动，不满足的一并写进来。

    python packaging/make_runtime_requirements.py      # 需要 uv；升级上游后重新生成并提交
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DROP = {"faster-whisper"}
# 包名 → (版本, 原因)
OVERRIDES: dict[str, tuple[str, str]] = {
    "edge-tts": ("7.3.1",
                 "上游锁定 7.2.7；2026-10 微软 Edge 在线朗读接口改了验证，7.2.7 连接时被拒（403 Invalid response status），"
                 "配音全部失败。7.3.0 起改用 Sec-MS-GEC v2 令牌，7.3.1 是当前最新版。7.3.1 的依赖 aiohttp>=3.8,<4、"
                 "certifi>=2023.11.17、tabulate>=0.4.4,<1、typing-extensions>=4.1,<5，锁文件里的版本都满足，无连带升级。"),
}
HEADER = """# 生成界面后台（上游 MoneyPrinterTurbo WebUI）的依赖，装进随包的独立 Python 运行时。
# 由 packaging/make_runtime_requirements.py 根据 upstream/uv.lock（上游 commit bbe7eb9）生成，不要手改。
# 已去掉可选的 faster-whisper 及只被它用到的包：{dropped}
# 覆盖了上游锁定的版本（原因见 make_runtime_requirements.py 的 OVERRIDES）：{overridden}
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


def apply_overrides(pkgs: dict[str, dict], overrides: dict[str, tuple[str, str]]) -> list[str]:
    """把锁文件里的版本换成 OVERRIDES 指定的版本，保留环境标记（`; python_version ...`）。返回改动说明。"""
    notes = []
    for name, (version, _why) in overrides.items():
        info = pkgs.get(name.lower())
        if info is None:
            raise SystemExit(f"OVERRIDES 里的 {name} 不在上游锁文件里，请删掉这条或检查包名")
        m = re.match(r"^([A-Za-z0-9_.\-]+)==([^\s;]+)(.*)$", info["line"])
        old = m.group(2)
        info["line"] = f"{m.group(1)}=={version}{m.group(3)}"
        notes.append(f"{m.group(1)} {old} → {version}")
    return notes


def main() -> None:
    text = subprocess.run(["uv", "export", "--frozen", "--no-dev", "--no-emit-project", "--no-hashes",
                           "--format", "requirements-txt"], cwd=ROOT / "upstream", check=True,
                          capture_output=True, text=True).stdout
    pkgs = parse(text)
    removed = prune(pkgs, DROP)
    overridden = apply_overrides(pkgs, OVERRIDES)
    lines = [HEADER.format(dropped="、".join(sorted(removed)), overridden="、".join(overridden) or "无")]
    lines += [info["line"] for name, info in sorted(pkgs.items()) if name not in removed]
    out = ROOT / "requirements-runtime.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{out}: {len(pkgs) - len(removed)} 个包，去掉 {sorted(removed)}，覆盖 {overridden}")


if __name__ == "__main__":
    sys.exit(main())
