"""「使用指南」后端：列出并读取随应用打包的文档（README.md + docs/）和手写源码。

安全：只按白名单返回文件。文档限定在 README.md 和 docs/ 目录内；源码只返回
SOURCE_PATTERNS 匹配到的本应用手写文件，并检查解析后的真实路径仍在根目录内，防止路径穿越。
"""

from __future__ import annotations

import io
import time
import zipfile
from pathlib import Path

from . import paths

DOC_ASSET_EXT = {".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".pdf", ".csv", ".json", ".txt", ".xlsx", ".zip"}

# 本应用的全部手写源码（不含第三方库、生成文件和运行数据）
SOURCE_PATTERNS = [
    "run.py", "requirements.txt", "start-mac-linux.sh", "start-windows.bat", "start-windows-debug.bat",
    "monitor/*.py", "monitor/sources/*.py",
    "monitor/web/*.html", "monitor/web/*.js", "monitor/web/*.css",
    "packaging/*.py", "packaging/*.iss", "packaging/*.sh",
    "tests/*.py",
]

SOURCE_ROLES = {
    "run.py": "启动入口：参数解析、单实例检测、启动后台和应用窗口、退出收尾",
    "requirements.txt": "可选依赖（psutil：读取进程内存）",
    "monitor/__init__.py": "版本号和应用名",
    "monitor/server.py": "本机 HTTP 服务：静态页面、JSON 接口、SSE 推送、退出流程",
    "monitor/lifecycle.py": "前后台一起开一起关：打开应用窗口、看门狗自动退出",
    "monitor/collector.py": "采集调度：按间隔抓取、去重、情感分析、入库、预警",
    "monitor/db.py": "SQLite 存储：监控对象、数据源、资讯、预警、设置",
    "monitor/sentiment.py": "中文/英文情感打分和风险词识别",
    "monitor/matcher.py": "资讯与监控对象的匹配",
    "monitor/quotes.py": "行情快照（30 秒缓存）",
    "monitor/netutil.py": "网络请求：代理、超时、编码处理",
    "monitor/events.py": "SSE 事件总线，记录页面连接数",
    "monitor/demo.py": "演示数据",
    "monitor/paths.py": "数据目录、打包资源目录",
    "monitor/guide.py": "「使用指南」：文档和源码的白名单读取、源码 zip",
    "monitor/procinfo.py": "运行状态：本应用各进程的 PID 和内存",
    "monitor/sources/__init__.py": "数据源注册表",
    "monitor/sources/base.py": "数据源基类、时间解析",
    "monitor/sources/cn_finance.py": "国内财经快讯和搜索数据源",
    "monitor/sources/rss.py": "RSS/Atom、Google 新闻、Bing、Yahoo 数据源",
    "monitor/web/index.html": "页面骨架：侧栏导航、顶部栏、缩放控件",
    "monitor/web/app.js": "前端全部逻辑：路由、各页面、图表、缩放、使用指南、运行状态",
    "monitor/web/markdown.js": "随应用打包的小型 Markdown 渲染器（不依赖 CDN）",
    "monitor/web/style.css": "样式（浅色/深色主题）",
    "packaging/build.py": "PyInstaller 打包脚本（把文档和源码一起打进安装包）",
    "tests/test_monitor.py": "离线单元测试和端到端退出测试",
}


def _inside(root: Path, f: Path) -> bool:
    try:
        f.relative_to(root)
        return True
    except ValueError:
        return False


# ------------------------------------------------------------------ 文档
def list_docs():
    """README.md 和 docs/ 下全部 .md。docs 按文件名排序在前，README 在后。"""
    root = paths.guide_root().resolve()
    out = []
    docs = root / "docs"
    if docs.is_dir():
        for f in sorted(docs.rglob("*.md"), key=lambda p: p.relative_to(docs).as_posix()):
            out.append({"path": f.relative_to(root).as_posix(), "title": f.stem, "size": f.stat().st_size})
    readme = root / "README.md"
    if readme.is_file():
        out.append({"path": "README.md", "title": "项目说明（README）", "size": readme.stat().st_size})
    return out


def list_doc_assets():
    root = paths.guide_root().resolve()
    docs = root / "docs"
    if not docs.is_dir():
        return []
    return [{"path": f.relative_to(root).as_posix(), "size": f.stat().st_size}
            for f in sorted(docs.rglob("*")) if f.is_file() and f.suffix.lower() in DOC_ASSET_EXT]


def resolve_doc_file(rel: str):
    """返回文档或附件的真实路径；不在白名单内返回 None。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    root = paths.guide_root().resolve()
    f = (root / rel).resolve()
    if not f.is_file() or not _inside(root, f):
        return None
    r = f.relative_to(root).as_posix()
    if r == "README.md":
        return f
    if r.startswith("docs/") and (f.suffix.lower() == ".md" or f.suffix.lower() in DOC_ASSET_EXT):
        return f
    return None


# ------------------------------------------------------------------ 源码
def list_sources():
    root = paths.source_root().resolve()
    seen = {}
    for pat in SOURCE_PATTERNS:
        for f in sorted(root.glob(pat)):
            if not f.is_file() or "__pycache__" in f.parts:
                continue
            rel = f.relative_to(root).as_posix()
            if rel in seen:
                continue
            try:
                lines = f.read_text(encoding="utf-8").count("\n") + 1
            except (OSError, UnicodeDecodeError):
                continue
            seen[rel] = {"path": rel, "size": f.stat().st_size, "lines": lines, "role": SOURCE_ROLES.get(rel, "")}
    return sorted(seen.values(), key=lambda d: d["path"])


def read_source(rel: str):
    """只返回白名单内的源码文本；其他路径（含 ../ 穿越）一律返回 None。"""
    rel = (rel or "").replace("\\", "/").lstrip("/")
    if rel not in {d["path"] for d in list_sources()}:
        return None
    root = paths.source_root().resolve()
    f = (root / rel).resolve()
    if not _inside(root, f) or not f.is_file():
        return None
    return f.read_text(encoding="utf-8")


def source_zip(version: str) -> bytes:
    """全部手写源码 + 文档打成 zip（内存中生成）。"""
    root = paths.source_root().resolve()
    groot = paths.guide_root().resolve()
    buf = io.BytesIO()
    prefix = f"sentiment-monitor-{version}-source/"
    srcs = list_sources()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for d in srcs:
            z.write(root / d["path"], prefix + d["path"])
        for d in list_docs() + list_doc_assets():
            z.write(groot / d["path"], prefix + d["path"])
        z.writestr(prefix + "MANIFEST.txt", f"舆情监控系统 v{version} 手写源码与文档\n生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
                   + "\n".join(d["path"] for d in srcs) + "\n")
    return buf.getvalue()
