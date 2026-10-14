"""把各分支里各项目的文档汇总成一个 Obsidian 文件夹，由 obsidian-docs 工作流推送到 obsidian 分支。

用法：python publish.py <输出目录>

- 项目 = 仓库根目录，以及每个含 README.md 或 docs/ 的顶层目录；「交付规范/」整个原样放在顶层。
- 同一个项目出现在多个分支时，取最近一次改动其文档的那个分支。
- 文件夹名取 docs/00 项目阅读入口.md 的 `project:`，没有就取 README 标题（去掉版本号和括号）。
- 指向源码等未复制文件的相对链接，改写成 GitHub 链接，在 Obsidian 里也能点开。
"""
import posixpath
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote, unquote

REPO = "luoxuran1982/claude2"
SPEC_DIR = "交付规范"
ROOT_KEY = ""
# 同一项目在多个分支的文档一模一样（时间相同）时，优先认默认分支
DEFAULT_REF = "origin/claude/local-api-video-gen-6ooxk8"


def git(*args: str, binary: bool = False):
    out = subprocess.run(["git", "-c", "core.quotepath=off", *args], check=True, capture_output=True).stdout
    return out if binary else out.decode("utf-8")


def ls(ref: str, path: str = "") -> list[str]:
    """列出 ref 中 path 目录下的全部文件（递归），返回仓库内路径。"""
    try:
        out = git("ls-tree", "-r", "-z", "--name-only", ref, "--", path) if path else ""
    except subprocess.CalledProcessError:
        return []
    return [p for p in out.split("\0") if p]


def show(ref: str, path: str) -> str | None:
    try:
        return git("show", f"{ref}:{path}")
    except subprocess.CalledProcessError:
        return None


def doc_files(ref: str, key: str) -> list[str]:
    if key == SPEC_DIR:
        return ls(ref, SPEC_DIR)
    base = f"{key}/" if key else ""
    files = [f for f in ls(ref, f"{base}docs") if f.startswith(f"{base}docs/")]
    if show(ref, f"{base}README.md") is not None:
        files.insert(0, f"{base}README.md")
    return files


def project_name(ref: str, key: str) -> str:
    if key == SPEC_DIR:
        return SPEC_DIR
    base = f"{key}/" if key else ""
    entry = show(ref, f"{base}docs/00 项目阅读入口.md") or ""
    m = re.match(r"---\n(.*?)\n---", entry, re.S)
    name = ""
    if m:
        pm = re.search(r"^project:\s*(.+?)\s*$", m.group(1), re.M)
        name = pm.group(1).strip("'\"") if pm else ""
    if not name:
        readme = show(ref, f"{base}README.md") or ""
        hm = re.search(r"^#\s+(.+?)\s*$", readme, re.M)
        if hm:
            name = re.sub(r"[（(][^）)]*[）)]", "", hm.group(1))
            name = re.sub(r"\s+v?\d+(?:\.\d+)*\s*$", "", name.strip())
    name = name or key or "仓库根目录"
    return re.sub(r'[\\/:*?"<>|#^\[\]]', "-", name).strip()


LINK = re.compile(r"(!?\[[^\]]*\]\()(<[^>]+>|[^)\s]+)((?:\s+\"[^\"]*\")?\))")


def rewrite_links(text: str, src: str, ref: str, copied: set[str]) -> str:
    """相对链接指向没复制过来的文件时，改成 GitHub 上该分支的链接。"""
    branch = ref.removeprefix("origin/")

    def fix(m: re.Match) -> str:
        raw = m.group(2)
        target = raw[1:-1] if raw.startswith("<") else raw
        if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I) or target.startswith("#"):
            return m.group(0)
        path, _, anchor = target.partition("#")
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(src), unquote(path)))
        if resolved.startswith("..") or resolved in copied or any(c.startswith(resolved + "/") for c in copied):
            return m.group(0)
        url = f"https://github.com/{REPO}/blob/{quote(branch)}/{quote(resolved)}" + (f"#{anchor}" if anchor else "")
        return f"{m.group(1)}{url}{m.group(3)}"

    out, fenced = [], False
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced:
            # 行内代码 `...` 里的示例链接不改；链接文字本身是代码（[`run.py`](../run.py)）的照常改
            spans = [m.span() for m in re.finditer(r"`[^`]*`", line)]
            line = LINK.sub(lambda m: m.group(0) if any(a <= m.start() < b for a, b in spans) else fix(m), line)
        out.append(line)
    return "\n".join(out)


def main(dest: Path) -> None:
    refs = [r for r in git("for-each-ref", "--format=%(refname:short)", "refs/remotes/origin/claude/").split() if r]
    best: dict[str, tuple[int, str, list[str]]] = {}
    for ref in refs:
        dirs = [d for d in git("ls-tree", "-d", "--name-only", ref).split("\n") if d and not d.startswith(".")]
        for key in [ROOT_KEY, *dirs]:
            files = doc_files(ref, key)
            if not files:
                continue
            ts = int(git("log", "-1", "--format=%ct", ref, "--", *files).strip() or 0)
            if key not in best or ts > best[key][0] or (ts == best[key][0] and ref == DEFAULT_REF):
                best[key] = (ts, ref, files)

    rows, used = [], set()
    for key, (ts, ref, files) in sorted(best.items(), key=lambda kv: (kv[0] != SPEC_DIR, kv[0])):
        name = project_name(ref, key)
        if name in used:
            name = f"{name}（{key or '根目录'}）"
        used.add(name)
        strip = f"{SPEC_DIR}/" if key == SPEC_DIR else (f"{key}/" if key else "")
        copied = set(files)
        for f in files:
            out = dest / name / f.removeprefix(strip)
            out.parent.mkdir(parents=True, exist_ok=True)
            if f.endswith(".md"):
                out.write_text(rewrite_links(show(ref, f), f, ref, copied), encoding="utf-8")
            else:
                out.write_bytes(git("show", f"{ref}:{f}", binary=True))
        entry = next((f for f in files if f.endswith(("docs/00 项目阅读入口.md", f"{SPEC_DIR}/交付要求.md"))), files[0])
        rel = f"{name}/{entry.removeprefix(strip)}"
        day = time.strftime("%Y-%m-%d %H:%M", time.gmtime(ts + 8 * 3600))
        rows.append(f"| [{name}](<{rel}>) | `{ref.removeprefix('origin/')}` | {day} |")

    (dest / "00 目录.md").write_text(
        "---\nauthor: Claude\n---\n\n"
        "本文件夹由 GitHub 自动生成，在这里修改的内容会在下次同步时被覆盖。要改文档，请告诉 Claude 在仓库里改。\n\n"
        "| 项目 | 来源分支 | 文档最后更新（北京时间） |\n|---|---|---|\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )
    print("\n".join(rows))


if __name__ == "__main__":
    main(Path(sys.argv[1]))
