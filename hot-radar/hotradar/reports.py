"""上游生成的 HTML 报告：列出历史、找最新一份、按白名单读取文件。

上游的报告位置（trendradar/report/generator.py、__main__.py）：
    output/html/<YYYY-MM-DD>/<HH-MM>.html       每次运行的热榜报告快照
    output/html/<YYYY-MM-DD>/rss_<HH-MM>.html   RSS 报告快照（开启 RSS 时）
    output/html/latest/<模式>.html              各报告模式（current/daily/incremental）最新一份
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from . import paths

MODE_LABELS = {"current": "当前榜单", "daily": "全天汇总", "incremental": "增量监控"}
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FILE = re.compile(r"^(rss_)?(\d{2}-\d{2})(?:-\d+)?\.html$")


def html_root(ws_name: str, root: Path | None = None) -> Path:
    return paths.workspace_dir(ws_name, root) / "output" / "html"


def list_reports(ws_name: str, root: Path | None = None, limit: int = 500) -> list[dict]:
    base = html_root(ws_name, root)
    out = []
    if not base.is_dir():
        return out
    for day in sorted((d for d in base.iterdir() if d.is_dir() and _DATE.match(d.name)), reverse=True):
        for f in sorted(day.iterdir(), reverse=True):
            m = _FILE.match(f.name)
            if not f.is_file() or not m:
                continue
            st = f.stat()
            out.append({"path": f"{day.name}/{f.name}", "date": day.name, "time": m.group(2).replace("-", ":"),
                        "kind": "rss" if m.group(1) else "hotlist", "size": st.st_size,
                        "mtime": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
            if len(out) >= limit:
                return out
    return out


def latest(ws_name: str, root: Path | None = None) -> dict | None:
    base = html_root(ws_name, root) / "latest"
    if not base.is_dir():
        return None
    files = [f for f in base.glob("*.html") if f.stem in MODE_LABELS]
    if not files:
        return None
    f = max(files, key=lambda p: p.stat().st_mtime)
    return {"path": f"latest/{f.name}", "mode": f.stem, "mode_label": MODE_LABELS[f.stem],
            "mtime": datetime.fromtimestamp(f.stat().st_mtime).astimezone().isoformat(timespec="seconds")}


def resolve(ws_name: str, rel: str, root: Path | None = None) -> Path | None:
    """只允许 output/html/ 下的 .html 文件；路径必须规整（不含 .. 等），且解析后仍在目录内。"""
    if ws_name not in paths.WORKSPACES or not isinstance(rel, str):
        return None
    if not re.fullmatch(r"(latest/(current|daily|incremental)|\d{4}-\d{2}-\d{2}/(rss_)?\d{2}-\d{2}(-\d+)?)\.html", rel):
        return None
    base = html_root(ws_name, root).resolve()
    p = (base / rel).resolve()
    try:
        p.relative_to(base)
    except ValueError:
        return None
    return p if p.is_file() else None


def stats(ws_name: str, root: Path | None = None) -> dict:
    """数据库与报告的数量和占用空间（给「首页」的数据概况）。"""
    out_dir = paths.workspace_dir(ws_name, root) / "output"
    def size_of(d: Path, pattern: str) -> tuple[int, int]:
        files = [f for f in d.glob(pattern) if f.is_file()] if d.is_dir() else []
        return len(files), sum(f.stat().st_size for f in files)
    news_n, news_b = size_of(out_dir / "news", "*.db")
    rss_n, rss_b = size_of(out_dir / "rss", "*.db")
    html_n, html_b = size_of(out_dir / "html", "*/*.html")
    days = sorted(f.stem for f in (out_dir / "news").glob("*.db")) if (out_dir / "news").is_dir() else []
    return {"news_db_days": news_n, "news_db_bytes": news_b, "rss_db_days": rss_n, "rss_db_bytes": rss_b,
            "html_files": html_n, "html_bytes": html_b, "first_day": days[0] if days else None,
            "last_day": days[-1] if days else None}
