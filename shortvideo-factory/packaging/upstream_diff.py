"""生成 upstream-patches.diff：本项目对上游 MoneyPrinterTurbo 源码做的全部改动（统一 diff 格式）。

    git clone https://github.com/harry0703/MoneyPrinterTurbo /tmp/mpt && git -C /tmp/mpt checkout bbe7eb9
    python packaging/upstream_diff.py /tmp/mpt

升级上游时：先用这个文件看清我们改了什么，换成新版源码后按 UPSTREAM.md 逐条重新打补丁，再重新生成本文件。
不比较按版权要求删掉的文件（商业字体、来源不明的音乐），它们在 UPSTREAM.md 里单独列出。
"""
from __future__ import annotations

import difflib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OURS = ROOT / "upstream"
REMOVED_ON_PURPOSE = ("resource/fonts/MicrosoftYaHei", "resource/fonts/STHeiti", "resource/fonts/UTM Kabel", "resource/songs/")


def main(pristine: Path) -> None:
    out = []
    names = sorted({p.relative_to(pristine).as_posix() for p in pristine.rglob("*") if p.is_file() and ".git" not in p.parts}
                   | {p.relative_to(OURS).as_posix() for p in OURS.rglob("*") if p.is_file() and "__pycache__" not in p.parts})
    changed = []
    for rel in names:
        if rel.startswith(REMOVED_ON_PURPOSE):
            continue
        a, b = pristine / rel, OURS / rel
        try:
            ta = a.read_text(encoding="utf-8").splitlines(keepends=True) if a.exists() else []
            tb = b.read_text(encoding="utf-8").splitlines(keepends=True) if b.exists() else []
        except UnicodeDecodeError:
            if a.exists() != b.exists() or (a.exists() and a.read_bytes() != b.read_bytes()):
                changed.append(rel + "（二进制）")
            continue
        if ta != tb:
            changed.append(rel)
            out += difflib.unified_diff(ta, tb, f"a/{rel}", f"b/{rel}")
    header = ["# 本项目对上游 MoneyPrinterTurbo（commit bbe7eb9）的改动，由 packaging/upstream_diff.py 生成。\n",
              "# 改动的文件：" + "、".join(changed) + "\n"]
    (ROOT / "upstream-patches.diff").write_text("".join(header + out), encoding="utf-8")
    print("改动的文件：", changed)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
