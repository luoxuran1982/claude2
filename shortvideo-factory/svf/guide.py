"""应用内「使用指南」：文档清单、附件、源码清单（外壳手写源码 + 上游源码）与打包下载。

只按白名单提供文件：
- 文档 = README.md、UPSTREAM.md、docs/ 下全部 .md；
- 附件 = docs/assets/ 下的图片等，以及 fonts/ 下的字体许可证；
- 源码 = SHELL_GLOBS（我们写的外壳）+ UPSTREAM_GLOBS（上游 MoneyPrinterTurbo 的主要源码，MIT）。
  不含：上游测试与示例素材、字体和音乐二进制、随包 Python 运行时与第三方库、构建产物。
请求里的路径必须精确等于清单里的某一项，所以 ../ 之类的路径穿越不会命中任何文件。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from .paths import guide_root

SHELL_GLOBS = (
    "run.py", "requirements.txt", "requirements-runtime.txt", "start-mac.command", "start-windows.bat",
    "upstream-patches.diff",
    "svf/*.py", "svf/web/*.html", "svf/web/*.js", "svf/web/*.css", "svf/web/*.svg", "svf_ext/*.py",
    "packaging/*.py", "packaging/*.nsi", "tests/*.py", "tests/*.js",
)
UPSTREAM_GLOBS = (
    "upstream/LICENSE", "upstream/README.md", "upstream/requirements.txt", "upstream/pyproject.toml",
    "upstream/config.example.toml", "upstream/main.py", "upstream/cli.py",
    "upstream/app/*.py", "upstream/app/*/*.py", "upstream/app/*/*/*.py",
    "upstream/webui/Main.py", "upstream/webui/kimi_login.py", "upstream/webui/styles.css",
    "upstream/webui/i18n/zh.json", "upstream/webui/i18n/en.json",
)
DOC_FILES = ("README.md", "UPSTREAM.md")
ASSET_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".pdf": "application/pdf", ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8", ".zip": "application/zip",
}
TEXT_TYPES = {".py": "Python", ".js": "JavaScript", ".html": "HTML", ".css": "CSS", ".svg": "SVG", ".json": "JSON",
              ".toml": "TOML", ".md": "Markdown", ".txt": "文本", ".nsi": "NSIS", ".bat": "批处理", ".command": "Shell",
              ".diff": "补丁"}
DOC_NAMES = {"README.md": "项目说明（README）", "UPSTREAM.md": "上游来源与补丁（UPSTREAM）"}


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def list_docs(root: Path | None = None) -> list[dict]:
    root = root or guide_root()
    files = [root / n for n in DOC_FILES if (root / n).is_file()]
    docs = root / "docs"
    if docs.is_dir():
        files += sorted(p for p in docs.rglob("*.md") if p.is_file())
    return [{"path": _rel(p, root), "name": DOC_NAMES.get(_rel(p, root), p.stem), "size": p.stat().st_size} for p in files]


def list_assets(root: Path | None = None) -> list[dict]:
    root = root or guide_root()
    out = []
    docs = root / "docs"
    if docs.is_dir():
        out += [{"path": _rel(p, root), "name": p.name, "size": p.stat().st_size, "kind": "文档附件"}
                for p in sorted(docs.rglob("*")) if p.is_file() and p.suffix.lower() in ASSET_TYPES]
    fonts = root / "fonts"
    if fonts.is_dir():
        out += [{"path": _rel(p, root), "name": p.name, "size": p.stat().st_size, "kind": "字体许可证"}
                for p in sorted(fonts.glob("*.txt")) if p.is_file()]
    return out


def list_sources(root: Path | None = None) -> list[dict]:
    root = root or guide_root()
    seen: dict[str, tuple[Path, str]] = {}
    for group, patterns in (("shell", SHELL_GLOBS), ("upstream", UPSTREAM_GLOBS)):
        for pattern in patterns:
            for p in sorted(root.glob(pattern)):
                if p.is_file() and "__pycache__" not in p.parts:
                    seen.setdefault(_rel(p, root), (p, group))
    out = []
    for rel, (p, group) in seen.items():
        data = p.read_bytes()
        lines = data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)
        out.append({"path": rel, "group": group, "size": len(data), "lines": lines,
                    "lang": TEXT_TYPES.get(p.suffix.lower(), "文本") if p.suffix else "文本"})
    return out


def _read_text(root: Path, rel: str, allowed: set[str]) -> str | None:
    if rel not in allowed:
        return None
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def read_doc(rel: str, root: Path | None = None) -> str | None:
    root = root or guide_root()
    return _read_text(root, rel, {d["path"] for d in list_docs(root)})


def read_source(rel: str, root: Path | None = None) -> str | None:
    root = root or guide_root()
    return _read_text(root, rel, {s["path"] for s in list_sources(root)})


def read_asset(rel: str, root: Path | None = None) -> tuple[bytes, str] | None:
    root = root or guide_root()
    if rel not in {a["path"] for a in list_assets(root)}:
        return None
    return (root / rel).read_bytes(), ASSET_TYPES[Path(rel).suffix.lower()]


def source_zip(root: Path | None = None) -> bytes:
    """外壳源码 + 上游主要源码 + 全部文档和附件，目录结构与仓库里的 shortvideo-factory/ 相同。"""
    root = root or guide_root()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for item in list_sources(root) + list_docs(root) + list_assets(root):
            z.write(root / item["path"], "shortvideo-factory/" + item["path"])
    return buf.getvalue()


def index(root: Path | None = None) -> dict:
    from . import UPSTREAM_COMMIT, UPSTREAM_VERSION, __version__
    root = root or guide_root()
    sources = list_sources(root)
    return {"version": __version__, "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
            "docs": list_docs(root), "assets": list_assets(root), "sources": sources,
            "source_stats": {g: {"files": sum(1 for s in sources if s["group"] == g),
                                 "lines": sum(s["lines"] for s in sources if s["group"] == g)} for g in ("shell", "upstream")}}
