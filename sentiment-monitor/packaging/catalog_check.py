"""批量实测订阅目录，结果写进 monitor/catalog_tested.json（应用里显示为「实测 N 条 · 日期」）。

只测「不填参数就能订阅」的条目（直连源 + 免必填参数、免 Cookie、免内置浏览器的 RSSHub 路由）。
需要本机 RSSHub 在运行。会真实访问很多网站，请低并发、偶尔跑一次。

    python packaging/catalog_check.py                       # 只测直连源
    python packaging/catalog_check.py finance social-media  # 再测这些 RSSHub 分类
    python packaging/catalog_check.py --zh-only finance     # 只测中文网站
    python packaging/catalog_check.py --force finance       # 已测过的也重测

已测过的条目默认跳过，可以分几次跑完。结果格式：{订阅地址: {"n": 条数, "latest": 最新一条的日期, "at": 测试日期}}；
失败的不写入（只记录成功的，避免把一时的网络问题固化成“不可用”）。
"""

import argparse
import concurrent.futures as cf
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from monitor import catalog, sources  # noqa: E402


def check(item):
    feed = catalog.default_feed(item)
    try:
        got = sources.get(feed["type"]).fetch(feed["params"], [])
    except Exception as e:  # noqa: BLE001
        return item, feed, -1, "", str(e)[:100]
    latest = max((g["published_at"] for g in got), default=0)
    return item, feed, len(got), time.strftime("%Y-%m-%d", time.localtime(latest)) if latest else "", ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("categories", nargs="*", help="要测的 RSSHub 分类 id，如 finance、social-media")
    ap.add_argument("--zh-only", action="store_true", help="只测中文网站")
    ap.add_argument("--force", action="store_true", help="已测过的也重测")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()

    result = catalog.tested()
    items, err = catalog.all_items()
    if err:
        print("提示：", err)
    todo = []
    for it in items:
        feed = catalog.default_feed(it)
        if not feed or (set(it["needs"]) & {"config", "browser"}):
            continue
        if not it.get("direct") and not (set(it["categories"]) & set(args.categories)):
            continue
        if args.zh_only and not str(it.get("lang", "")).startswith("zh"):
            continue
        if not args.force and catalog.feed_key(feed["type"], feed["params"]) in result:
            continue
        todo.append(it)
    print(f"待测 {len(todo)} 条")
    today = time.strftime("%Y-%m-%d")
    ok = 0
    with cf.ThreadPoolExecutor(args.workers) as ex:
        for i, (item, feed, n, latest, error) in enumerate(ex.map(check, todo), 1):
            key = catalog.feed_key(feed["type"], feed["params"])
            print(f"[{i}/{len(todo)}] {n:>4} {latest:10} {item['site']}·{item['name']} {error}", flush=True)
            if n > 0:
                ok += 1
                result[key] = {"n": n, "latest": latest, "at": today}
            if i % 20 == 0:
                _save(result)
    _save(result)
    print(f"完成：成功 {ok} 条；记录总数 {len(result)}，写入 {catalog.TESTED_FILE}")


def _save(result):
    catalog.TESTED_FILE.write_text(json.dumps(dict(sorted(result.items())), ensure_ascii=False, indent=0) + "\n", "utf-8")


if __name__ == "__main__":
    main()
