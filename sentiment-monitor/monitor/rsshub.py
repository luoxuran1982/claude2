"""和本机 RSSHub 打交道：地址、运行状态、一键启动、读取它的全部路由目录。

RSSHub 是独立的开源程序（AGPL-3.0），本应用只通过 HTTP 调用它：
    舆情监控 → GET http://127.0.0.1:1200/cls/telegraph → RSSHub 运行对应抓取脚本 → 返回 RSS
它不属于本应用的进程：本应用退出时不会停止 RSSHub（它可能也在给别的程序用）。
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import netutil

log = logging.getLogger("monitor.rsshub")

FETCH_TIMEOUT = 45          # RSSHub 要现场去抓目标网站，比普通请求慢
CATALOG_TTL = 6 * 3600      # 路由目录缓存 6 小时

# RSSHub 自己的分类（routes 的 categories 字段），只翻译名称，不改分法
CATEGORY_NAMES = {
    "popular": "热门", "social-media": "社交媒体", "new-media": "新媒体", "traditional-media": "传统媒体",
    "finance": "金融财经", "government": "政务消息", "bbs": "论坛", "blog": "博客", "programming": "编程",
    "design": "设计", "live": "直播", "multimedia": "音视频", "picture": "图片", "anime": "二次元",
    "program-update": "程序更新", "university": "大学通知", "forecast": "预报预警", "travel": "出行旅游",
    "shopping": "购物", "game": "游戏", "reading": "阅读", "study": "学习", "journal": "科学期刊",
    "sport": "体育", "other": "其他",
}
CATEGORY_ORDER = list(CATEGORY_NAMES)

_cache = {"at": 0, "routes": None, "error": ""}
_cache_lock = threading.Lock()
_start_lock = threading.Lock()
_started_at = 0.0


def base_url() -> str:
    return (netutil.options.get("rsshub_url") or "http://127.0.0.1:1200").rstrip("/")


def url_for(path: str) -> str:
    path = path.strip()
    if path.startswith(("http://", "https://")):
        return path
    return base_url() + "/" + path.lstrip("/")


def is_local() -> bool:
    return any(h in base_url() for h in ("127.0.0.1", "localhost", "[::1]"))


def explain_error(msg: str) -> str:
    """把 RSSHub 的报错翻成人话。"""
    if "网络连接失败" in msg and is_local():
        return f"RSSHub 没有运行（{base_url()} 连不上）。到「订阅目录」点「启动 RSSHub」，或在设置里改地址。原始错误：{msg}"
    if "HTTP 503" in msg:
        return "RSSHub 抓取失败（503）：目标网站可能需要 Cookie/登录、有反爬验证，或暂时打不开。原始错误：" + msg
    if "HTTP 404" in msg:
        return "RSSHub 没有这个路由（404），可能填错了路径或参数，或本机 RSSHub 版本太旧。"
    return msg


def status() -> dict:
    """RSSHub 是否在运行（访问首页，2.5 秒超时）。"""
    t0 = time.time()
    try:
        netutil.fetch(base_url() + "/", timeout=2.5)
        return {"ok": True, "url": base_url(), "ms": int((time.time() - t0) * 1000), "can_start": bool(start_command())}
    except netutil.FetchError as e:
        starting = time.time() - _started_at < 90
        return {"ok": False, "url": base_url(), "error": str(e), "can_start": bool(start_command()), "starting": starting}


def start_command() -> list[str] | None:
    """本机怎样启动 RSSHub。优先用设置里的命令；否则找用户机器上现成的一键启动脚本。"""
    custom = (netutil.options.get("rsshub_start_cmd") or "").strip()
    if custom:
        return ["/bin/sh", "-c", custom] if os.name != "nt" else ["cmd", "/c", custom]
    if not is_local():
        return None
    script = Path.home() / "Projects" / "第三方开源" / "启动.sh"
    if sys.platform == "darwin" and script.exists() and shutil.which("zsh"):
        return ["zsh", str(script), "rsshub"]
    return None


def start() -> dict:
    """在后台启动 RSSHub（独立进程，本应用退出时不会停止它）。"""
    global _started_at
    cmd = start_command()
    if not cmd:
        return {"ok": False, "error": "没有找到启动方式。请在「设置 → RSSHub 启动命令」填写，或手动启动 RSSHub。"}
    with _start_lock:
        if status()["ok"]:
            return {"ok": True, "already": True}
        if time.time() - _started_at < 90:
            return {"ok": True, "starting": True}
        _started_at = time.time()
        log.info("启动 RSSHub：%s", " ".join(cmd))
        kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x00000008}  # DETACHED_PROCESS
        subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    return {"ok": True, "starting": True}


# ------------------------------------------------------------------ 路由目录
def _flatten(namespaces: dict) -> list[dict]:
    """把 RSSHub /api/namespace 的嵌套结构拍平成一条条可订阅的路由。"""
    out = []
    for ns, info in namespaces.items():
        for path, r in (info.get("routes") or {}).items():
            feats = r.get("features") or {}
            cats = r.get("categories") or info.get("categories") or ["other"]
            params = r.get("parameters") or {}
            segs = [s for s in path.strip("/").split("/") if s]
            required = [s[1:].split("{")[0] for s in segs if s.startswith(":") and not s.rstrip("}").endswith("?") and "?" not in s]
            needs = []
            if feats.get("requireConfig"):
                needs.append("config")
            if feats.get("requirePuppeteer"):
                needs.append("browser")
            if feats.get("antiCrawler"):
                needs.append("anti")
            example = r.get("example") or ""
            if example and not example.startswith(f"/{ns}/") and example != f"/{ns}":
                example = f"/{ns}{example}"
            out.append({
                "key": f"/{ns}{path}",
                "ns": ns,
                "site": info.get("name") or ns,
                "site_url": r.get("url") or info.get("url") or "",
                "lang": info.get("lang") or "",
                "name": r.get("name") or path,
                "path": f"/{ns}{path}",
                "example": example,
                "params": {k: (v if isinstance(v, str) else (v or {}).get("description", "")) for k, v in params.items()},
                "required": required,
                "needs": needs,
                "categories": [c for c in cats if c],
                "desc": (r.get("description") or "")[:600],
            })
    return out


def catalog(force=False) -> tuple[list[dict] | None, str]:
    """返回 (路由列表, 错误信息)。读不到时返回上次成功的缓存。"""
    with _cache_lock:
        fresh = _cache["routes"] is not None and time.time() - _cache["at"] < CATALOG_TTL
        if fresh and not force:
            return _cache["routes"], ""
    try:
        text = netutil.fetch(base_url() + "/api/namespace", timeout=30)
        routes = _flatten(json.loads(text))
    except (netutil.FetchError, json.JSONDecodeError, AttributeError) as e:
        with _cache_lock:
            _cache["error"] = explain_error(str(e))
            return _cache["routes"], _cache["error"]
    with _cache_lock:
        _cache.update(at=time.time(), routes=routes, error="")
    log.info("读取 RSSHub 路由目录：%d 条", len(routes))
    return routes, ""
