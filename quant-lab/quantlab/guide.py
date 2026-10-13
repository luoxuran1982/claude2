"""应用内「使用指南」的资料来源：项目文档（README + docs/）和本应用的手写源码。

源码运行时直接读项目目录；打包后读 PyInstaller 带进来的 projectdocs/ 与 projectsrc/QuantLab-source.zip。
所有读取都按白名单进行（先列出允许的文件，再只接受清单里的路径），防止路径穿越。
"""
from __future__ import annotations

import io
import mimetypes
import sys
import zipfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent          # 源码运行时的 quant-lab/
SRC_ZIP_NAME = "QuantLab-source.zip"
# 手写源码：按模式收录；排除第三方（vendor）、生成文件和运行产物
SRC_PATTERNS = ("run.py", "requirements.txt", "start-windows.bat", "quantlab/**/*.py", "quantlab/web/*.html",
                "quantlab/web/*.js", "quantlab/web/*.css", "quantlab/web/*.svg", "packaging/*.py", "packaging/*.nsi", "packaging/*.sh",
                "tests/*.py")
SRC_EXCLUDE = ("vendor/", "__pycache__/", "build/", "dist/")
LANG = {".py": "Python", ".js": "JavaScript", ".html": "HTML", ".css": "CSS", ".svg": "SVG", ".nsi": "NSIS",
        ".txt": "文本", ".bat": "批处理"}


def _bundle() -> Path | None:
    base = getattr(sys, "_MEIPASS", None)
    return Path(base) if base else None


def docs_root() -> Path:
    b = _bundle()
    return b / "projectdocs" if b else PROJECT


# ------------------------------------------------------------------ 文档
def list_docs(root: Path | None = None) -> list[dict]:
    """全部文档：docs/ 里以数字开头的（阅读顺序）→ README → docs/ 里其余的。"""
    root = docs_root() if root is None else root
    files = sorted((root / "docs").rglob("*.md")) if (root / "docs").is_dir() else []
    numbered = [p for p in files if p.name[:1].isdigit()]
    others = [p for p in files if p not in numbered]
    ordered = numbered + ([root / "README.md"] if (root / "README.md").is_file() else []) + others
    out = []
    for p in ordered:
        rel = p.relative_to(root).as_posix()
        title = "README（项目说明）" if rel == "README.md" else p.stem
        out.append({"path": rel, "title": title, "size": p.stat().st_size})
    return out


def read_doc(rel: str, root: Path | None = None) -> dict:
    root = docs_root() if root is None else root
    allowed = {d["path"]: d for d in list_docs(root)}
    if rel not in allowed:
        raise LookupError("没有这篇文档")
    return {**allowed[rel], "text": (root / rel).read_text(encoding="utf-8", errors="replace")}


def list_assets(root: Path | None = None) -> list[dict]:
    root = docs_root() if root is None else root
    d = root / "docs"
    if not d.is_dir():
        return []
    return [{"path": p.relative_to(root).as_posix(), "size": p.stat().st_size}
            for p in sorted(d.rglob("*")) if p.is_file() and p.suffix.lower() != ".md"]


def doc_asset(rel: str, root: Path | None = None) -> tuple[bytes, str]:
    """文档引用的图片/附件：只允许 docs/ 下列出的非 .md 文件。"""
    root = docs_root() if root is None else root
    if rel not in {a["path"] for a in list_assets(root)}:
        raise LookupError("没有这个附件")
    target = root / rel
    return target.read_bytes(), mimetypes.guess_type(target.name)[0] or "application/octet-stream"


# ------------------------------------------------------------------ 源码
def source_files(project: Path = PROJECT) -> list[str]:
    seen: list[str] = []
    for pat in SRC_PATTERNS:
        for p in sorted(project.glob(pat)):
            rel = p.relative_to(project).as_posix()
            if p.is_file() and not any(x in rel + "/" for x in SRC_EXCLUDE) and rel not in seen:
                seen.append(rel)
    return seen


def build_source_zip(project: Path = PROJECT) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in source_files(project):
            z.write(project / rel, "quant-lab/" + rel)
    return buf.getvalue()


def _lines(b: bytes) -> int:
    return b.count(b"\n") + (1 if b and not b.endswith(b"\n") else 0)


class Sources:
    """统一的源码读取：打包版读内置 zip，源码运行时读项目目录。"""

    def __init__(self, project: Path = PROJECT, bundle: Path | None = None):
        bundle = _bundle() if bundle is None else bundle
        zp = bundle / "projectsrc" / SRC_ZIP_NAME if bundle else None
        self.zip_path = zp if zp and zp.is_file() else None
        self.project = project

    def _files(self) -> dict[str, bytes]:
        if self.zip_path:
            with zipfile.ZipFile(self.zip_path) as z:
                return {n[len("quant-lab/"):]: z.read(n) for n in z.namelist()
                        if n.startswith("quant-lab/") and not n.endswith("/")}
        return {rel: (self.project / rel).read_bytes() for rel in source_files(self.project)}

    def list(self) -> list[dict]:
        return [{"path": k, "size": len(v), "lines": _lines(v), "lang": LANG.get(Path(k).suffix.lower(), "文本")}
                for k, v in self._files().items()]

    def read(self, rel: str) -> dict:
        files = self._files()
        if rel not in files:
            raise LookupError("不在源码清单里")
        v = files[rel]
        return {"path": rel, "size": len(v), "lines": _lines(v), "lang": LANG.get(Path(rel).suffix.lower(), "文本"),
                "text": v.decode("utf-8", errors="replace")}

    def zip_bytes(self) -> bytes:
        return self.zip_path.read_bytes() if self.zip_path else build_source_zip(self.project)
