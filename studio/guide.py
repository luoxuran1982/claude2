"""应用内「使用指南」：提供项目文档（README.md 和 docs/）与本应用手写源码的只读访问。

源码和文档只按白名单提供：先列出允许的文件，再按相对路径精确匹配，
不拼接用户给的路径，所以 ../ 之类的路径穿越不会生效。
"""

import io
import mimetypes
import re
import sys
import time
import zipfile
from pathlib import Path

from . import paths

# 本应用的手写源码（相对源码根目录）。不含第三方库、虚拟环境和生成文件。
SOURCE_PATTERNS = [
    "run.py",
    "requirements.txt",
    "start-mac.command",
    "start-windows.bat",
    "studio/*.py",
    "studio/web/*.html",
    "studio/web/*.js",
    "studio/web/*.css",
    "packaging/*.py",
    "tests/*.py",
    ".github/workflows/build.yml",
]
SOURCE_EXCLUDE_DIRS = {"__pycache__", "vendor", "node_modules", ".venv", "build", "dist"}
ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".txt", ".json", ".csv"}


def bundle_root() -> Path:
    """打包版是 PyInstaller 的解包目录；源码运行时是仓库根目录。"""
    return Path(getattr(sys, "_MEIPASS", paths.package_dir().parent))


def docs_root() -> Path:
    return bundle_root()


def source_root() -> Path:
    """打包时 build.py 把手写源码原样复制到 source/ 下（打包版默认只有 .pyc）。"""
    if getattr(sys, "frozen", False):
        return bundle_root() / "source"
    return paths.package_dir().parent


# ------------------------------------------------------------------ 文档
def _title_of(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return path.stem
    body = _strip_front_matter(text)
    m = re.search(r"^#\s+(.+)$", body, re.M)
    if path.name == "README.md":
        return "README（项目说明）"
    # 文件名本身就是标题时（如「00 项目阅读入口」），用文件名
    if re.match(r"^\d{2}\s", path.stem) or not m:
        return path.stem
    return m.group(1).strip()


def _strip_front_matter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip("\n")
    return text


def list_docs():
    root = docs_root()
    items = []
    readme = root / "README.md"
    docs = sorted((p for p in (root / "docs").rglob("*.md") if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    for p in docs:
        rel = p.relative_to(root).as_posix()
        items.append({"path": rel, "title": _title_of(p), "bytes": p.stat().st_size})
    if readme.is_file():
        items.append({"path": "README.md", "title": _title_of(readme), "bytes": readme.stat().st_size})
    return items


def read_doc(rel: str) -> str:
    allowed = {d["path"]: d for d in list_docs()}
    if rel not in allowed:
        raise FileNotFoundError(rel)
    return (docs_root() / rel).read_text(encoding="utf-8", errors="replace")


def list_assets():
    root = docs_root() / "docs"
    if not root.is_dir():
        return []
    return [p.relative_to(docs_root()).as_posix() for p in sorted(root.rglob("*")) if p.is_file() and p.suffix.lower() in ASSET_EXTS]


def read_asset(rel: str):
    if rel not in set(list_assets()):
        raise FileNotFoundError(rel)
    f = docs_root() / rel
    return f.read_bytes(), mimetypes.guess_type(str(f))[0] or "application/octet-stream"


# ------------------------------------------------------------------ 源码
def list_sources():
    root = source_root()
    seen = {}
    for pat in SOURCE_PATTERNS:
        for p in sorted(root.glob(pat)):
            if not p.is_file() or SOURCE_EXCLUDE_DIRS & set(p.relative_to(root).parts):
                continue
            rel = p.relative_to(root).as_posix()
            if rel in seen:
                continue
            try:
                lines = p.read_text(encoding="utf-8", errors="replace").count("\n") + 1
            except OSError:
                continue
            seen[rel] = {"path": rel, "bytes": p.stat().st_size, "lines": lines}
    return list(seen.values())


def read_source(rel: str) -> str:
    if rel not in {f["path"] for f in list_sources()}:
        raise FileNotFoundError(rel)
    return (source_root() / rel).read_text(encoding="utf-8", errors="replace")


def source_zip(version: str) -> bytes:
    buf = io.BytesIO()
    prefix = f"AIVideoStudio-{version}-source/"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in list_sources():
            z.write(source_root() / f["path"], prefix + f["path"])
        for d in list_docs():
            z.write(docs_root() / d["path"], prefix + d["path"])
        for a in list_assets():
            z.write(docs_root() / a, prefix + a)
        z.writestr(prefix + "MANIFEST.txt", f"AI 知识视频工坊 {version} 手写源码与文档\n生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                   "不含第三方库、虚拟环境、打包产物和用户数据。\n")
    return buf.getvalue()
