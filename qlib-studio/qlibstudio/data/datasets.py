"""数据集登记：<数据目录>/datasets/<编号>/ 是一个完整的 Qlib 数据目录，外加我们自己的说明文件。

    calendars/day.txt          交易日历（Qlib 格式）
    instruments/*.txt          股票池：all（全部代码，含指数）、stocks（只含股票）、main/chinext/star/bse（按板块），
                               社区数据集另外自带 csi300/csi500 等
    features/<代码>/*.day.bin   每个字段一个二进制文件（Qlib 格式）
    meta.json                  标题、来源、区间、股票数、复权方式、可选基准
    names.json                 代码 → 名称
    quality.json               数据质量检查结果
"""
from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
SOURCES = {"demo": "演示数据（模拟 A 股）", "tdx": "通达信 vipdoc", "csv": "CSV / 文本 / Excel 文件",
           "futu": "富途 / moomoo OpenD", "community": "社区数据集 chenditc/investment_data"}
POOL_NAMES = {"all": "全部代码（含指数）", "stocks": "全部股票", "main": "沪深主板", "chinext": "创业板", "star": "科创板",
              "bse": "北交所", "csi300": "沪深 300 成分", "csi500": "中证 500 成分", "csi800": "中证 800 成分",
              "csi1000": "中证 1000 成分", "csiall": "中证全指成分"}


def root(data_root: Path) -> Path:
    p = Path(data_root) / "datasets"
    p.mkdir(parents=True, exist_ok=True)
    return p


def new_id(prefix: str = "d") -> str:
    return time.strftime(f"{prefix}%Y%m%d_%H%M%S") + f"_{int(time.time() * 1000) % 1000:03d}"


def path(data_root: Path, ds_id: str) -> Path:
    if not ID_RE.match(ds_id or ""):
        raise ValueError("数据集编号无效")
    p = root(data_root) / ds_id
    if not (p / "meta.json").is_file():
        raise LookupError("数据集不存在")
    return p


def read_json(p: Path, default=None):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p: Path, value) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    tmp.replace(p)


def list_all(data_root: Path) -> list[dict]:
    out = []
    for d in sorted(root(data_root).iterdir()):
        m = read_json(d / "meta.json")
        if isinstance(m, dict):
            q = read_json(d / "quality.json", {}) or {}
            m["quality_summary"] = q.get("summary")
            m["size_mib"] = round(sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) / 1024 / 1024, 1)
            out.append(m)
    return sorted(out, key=lambda m: m.get("created", ""), reverse=True)


def meta(data_root: Path, ds_id: str) -> dict:
    return read_json(path(data_root, ds_id) / "meta.json", {})


def quality(data_root: Path, ds_id: str) -> dict:
    return read_json(path(data_root, ds_id) / "quality.json", {}) or {}


def names(data_root: Path, ds_id: str) -> dict:
    return read_json(path(data_root, ds_id) / "names.json", {}) or {}


def delete(data_root: Path, ds_id: str) -> None:
    shutil.rmtree(path(data_root, ds_id))


def pools(ds_dir: Path) -> list[dict]:
    """数据目录里实际有的股票池（instruments/*.txt），带股票数。"""
    out = []
    inst = Path(ds_dir) / "instruments"
    if inst.is_dir():
        for f in sorted(inst.glob("*.txt")):
            try:
                n = sum(1 for line in f.read_text(encoding="utf-8").splitlines() if line.strip())
            except OSError:
                n = 0
            out.append({"id": f.stem, "name": POOL_NAMES.get(f.stem, f.stem), "count": n})
    order = list(POOL_NAMES)
    out.sort(key=lambda p: (order.index(p["id"]) if p["id"] in order else 99, p["id"]))
    return out


def calendar(ds_dir: Path) -> list[str]:
    f = Path(ds_dir) / "calendars" / "day.txt"
    if not f.is_file():
        return []
    return [x.strip()[:10] for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
