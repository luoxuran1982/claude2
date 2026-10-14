"""应用内「使用指南」：文档清单、文档附件、全部源码的只读查看和打包下载。

只按白名单提供文件：
- 文档 = README.md、UPSTREAM.md 和 docs/ 下的全部 .md（应用内渲染显示）；
- 附件 = docs/assets/ 下的图片等、samples/ 下的样例文件、LICENSE；
- 源码分两组：「研究台外壳」= 我们手写的文件（SHELL_GLOBS），「上游 Qlib」= upstream/ 下的全部文本文件。
  不列入：web/vendor/echarts.min.js（第三方压缩库，Apache-2.0，许可证见同目录）、编译产物（.so/.pyd/.cpp）。
请求里的路径必须精确等于清单里的某一项，所以 ../ 之类的路径穿越不会命中任何文件。
源码 zip = 上面全部文件，目录结构与仓库 qlib-studio/ 相同。
"""
from __future__ import annotations

import io
import re
import zipfile
from functools import lru_cache
from pathlib import Path

from . import paths

SHELL_GLOBS = (
    "run.py", "requirements.txt",
    "qlibstudio/*.py", "qlibstudio/data/*.py", "qlibstudio/web/*.html", "qlibstudio/web/*.js", "qlibstudio/web/*.css",
    "qlibstudio/web/*.svg", "packaging/*.py", "packaging/*.nsi", "tests/*.py", "tests/*.js", "tests/fixtures/*", "ci/*.yml",
)
CI_NAME = "qlib-studio.yml"
ASSET_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp",
    ".svg": "image/svg+xml", ".pdf": "application/pdf", ".json": "application/json; charset=utf-8",
    ".csv": "text/csv; charset=utf-8", ".txt": "text/plain; charset=utf-8", "": "text/plain; charset=utf-8",
    ".zip": "application/zip",
}
TEXT_SUFFIXES = {".py", ".js", ".html", ".css", ".svg", ".md", ".txt", ".yaml", ".yml", ".toml", ".sql", ".json",
                 ".sh", ".bat", ".command", ".nsi", ".lock", ".env", ".pyx", ".rst", ".cfg", ".ini", ".in", ".ipynb", ""}
SKIP_PARTS = {"__pycache__", "build", "dist", ".git", ".pytest_cache"}


def project_dir() -> Path:
    return paths.project_dir()


def _rel(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _ok(p: Path, root: Path) -> bool:
    return p.is_file() and not (set(p.relative_to(root).parts) & SKIP_PARTS) and p.suffix != ".pyc"


def ci_workflow(root: Path) -> Path | None:
    """CI 工作流在仓库根目录 .github/workflows/qlib-studio.yml；安装包里由 build.py 复制成 ci/qlib-studio.yml。"""
    staged = root / "ci" / CI_NAME
    if staged.is_file():
        return staged
    repo = root.parent / ".github" / "workflows" / CI_NAME
    return repo if repo.is_file() else None


DOC_NAMES = {"README.md": "项目说明（README）", "UPSTREAM.md": "上游来源、许可证与补丁（UPSTREAM）"}
ASSET_NOTES = {"docs/assets": "文档插图", "samples": "样例文件（对比交换格式样例等）"}


def list_docs(root: Path | None = None) -> list[dict]:
    root = root or project_dir()
    files = [root / n for n in DOC_NAMES if (root / n).is_file()]
    docs = root / "docs"
    if docs.is_dir():
        files += sorted(p for p in docs.rglob("*.md") if p.is_file())
    return [{"path": _rel(p, root), "name": DOC_NAMES.get(_rel(p, root), p.stem), "size": p.stat().st_size} for p in files]


def list_assets(root: Path | None = None) -> list[dict]:
    root = root or project_dir()
    out = []
    for base, note in ((root / "docs" / "assets", ASSET_NOTES["docs/assets"]), (root / "samples", ASSET_NOTES["samples"])):
        if base.is_dir():
            for p in sorted(base.rglob("*")):
                if _ok(p, root) and p.suffix.lower() in ASSET_TYPES:
                    out.append({"path": _rel(p, root), "name": p.name, "size": p.stat().st_size, "note": note})
    if (root / "LICENSE").is_file():
        out.append({"path": "LICENSE", "name": "LICENSE（MIT 许可证全文）", "size": (root / "LICENSE").stat().st_size,
                    "note": "许可证"})
    return out


def _source_entry(p: Path, rel: str, group: str) -> dict:
    data = p.read_bytes()
    return {"path": rel, "group": group, "size": len(data),
            "lines": data.count(b"\n") + (0 if data.endswith(b"\n") or not data else 1)}


def list_sources(root: Path | None = None) -> list[dict]:
    root = root or project_dir()
    seen: dict[str, dict] = {}
    for pattern in SHELL_GLOBS:
        for p in sorted(root.glob(pattern)):
            if _ok(p, root):
                seen.setdefault(_rel(p, root), _source_entry(p, _rel(p, root), "shell"))
    ci = ci_workflow(root)
    if ci is not None and f"ci/{CI_NAME}" not in seen:
        seen[f"ci/{CI_NAME}"] = _source_entry(ci, f"ci/{CI_NAME}", "shell")
    up = root / "upstream"
    if up.is_dir():
        for p in sorted(up.rglob("*")):
            if _ok(p, root) and p.suffix.lower() in TEXT_SUFFIXES:  # 无后缀的 LICENSE、Dockerfile、version、.gitkeep 也算
                rel = _rel(p, root)
                seen.setdefault(rel, _source_entry(p, rel, "upstream"))
    return list(seen.values())


def _path_of(rel: str, root: Path) -> Path:
    if rel == f"ci/{CI_NAME}":
        return ci_workflow(root) or root / rel
    return root / rel


def read_doc(rel: str, root: Path | None = None) -> str | None:
    root = root or project_dir()
    if rel not in {d["path"] for d in list_docs(root)}:
        return None
    return (root / rel).read_text(encoding="utf-8", errors="replace")


def read_asset(rel: str, root: Path | None = None) -> tuple[bytes, str] | None:
    root = root or project_dir()
    if rel not in {a["path"] for a in list_assets(root)}:
        return None
    return (root / rel).read_bytes(), ASSET_TYPES.get(Path(rel).suffix.lower(), "application/octet-stream")


def read_source(rel: str, root: Path | None = None) -> str | None:
    root = root or project_dir()
    if rel not in {s["path"] for s in list_sources(root)}:
        return None
    return _path_of(rel, root).read_text(encoding="utf-8", errors="replace")


def source_zip(root: Path | None = None) -> bytes:
    """全部源码 + 文档 + 附件打成一个 zip，目录结构与仓库 qlib-studio/ 相同（CI 工作流放在 ci/qlib-studio.yml）。"""
    root = root or project_dir()
    buf = io.BytesIO()
    names = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for item in list_sources(root) + list_docs(root) + list_assets(root):
            if item["path"] in names:
                continue
            names.add(item["path"])
            z.write(_path_of(item["path"], root), "qlib-studio/" + item["path"])
    return buf.getvalue()


def _title(text: str, fallback: str) -> str:
    m = re.search(r"^#\s+(.+)$", text, re.M)
    return m.group(1).strip() if m else fallback


@lru_cache(maxsize=4)
def _index_cached(root: str) -> dict:
    from . import UPSTREAM_COMMIT, UPSTREAM_VERSION, __version__
    r = Path(root)
    sources = list_sources(r)
    return {"version": __version__, "upstream_version": UPSTREAM_VERSION, "upstream_commit": UPSTREAM_COMMIT,
            "docs": list_docs(r), "assets": list_assets(r), "sources": sources,
            "source_totals": {g: {"files": sum(1 for s in sources if s["group"] == g),
                                  "lines": sum(s["lines"] for s in sources if s["group"] == g)} for g in ("shell", "upstream")}}


def index(root: Path | None = None) -> dict:
    root = root or project_dir()
    if paths.frozen():  # 安装包里文件不会变，缓存清单
        return _index_cached(str(root))
    _index_cached.cache_clear()
    return _index_cached(str(root))
