"""命令行：批量/脚本化使用，和界面共用同一个数据目录。

    python run.py demo --stocks 300 --years 8
    python run.py import D:\\new_tdx\\vipdoc --title 通达信全市场
    python run.py list
    python run.py run my_config.json          # 配置格式见 docs/使用说明.md
    python run.py factors --dataset demo
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import experiment
from .data import demo
from .data.store import DataStore
from .paths import data_dir


def _progress():
    last = [0.0]

    def p(frac, msg):
        if time.time() - last[0] > 1 or frac >= 1:
            print(f"[{frac * 100:5.1f}%] {msg}", flush=True)
            last[0] = time.time()
    return p


def main(argv=None) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser(prog="quantlab")
    ap.add_argument("--data", help="数据目录（默认与界面相同）")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo", help="生成模拟演示数据")
    d.add_argument("--stocks", type=int, default=300)
    d.add_argument("--years", type=float, default=8)
    i = sub.add_parser("import", help="导入 K 线文件夹/文件")
    i.add_argument("path")
    i.add_argument("--title", default="")
    i.add_argument("--merge-into", default=None, help="合并进已有数据集（增量更新）")
    i.add_argument("--no-adjust", action="store_true", help="不做除权估算")
    i.add_argument("--with-funds", action="store_true", help="同时导入基金/ETF")
    sub.add_parser("list", help="列出数据集和实验")
    r = sub.add_parser("run", help="按 JSON 配置运行实验")
    r.add_argument("config")
    f = sub.add_parser("factors", help="单因子检验")
    f.add_argument("--dataset", required=True)
    f.add_argument("--horizon", type=int, default=5)
    args = ap.parse_args(argv)

    root = data_dir() if not args.data else __import__("pathlib").Path(args.data)
    store, runs = DataStore(root), experiment.Runs(root)
    if args.cmd == "demo":
        bars, names = demo.generate(args.stocks, args.years)
        meta = store.save_frame(bars, f"演示数据（模拟 {args.stocks} 只）", names, "demo", demo=True)
        print(json.dumps(meta, ensure_ascii=False, indent=1))
    elif args.cmd == "import":
        kinds = ("stock", "index", "fund") if args.with_funds else ("stock", "index")
        meta = store.import_path(args.path, args.title, args.merge_into, kinds=kinds,
                                 auto_adjust=not args.no_adjust, merge=bool(args.merge_into), progress=_progress())
        print(json.dumps(meta, ensure_ascii=False, indent=1))
    elif args.cmd == "list":
        print("数据集：")
        for m in store.list():
            print(f"  {m['id']:24s} {m['title']}  {m['n_symbols']} 只  {m['start']}~{m['end']}")
        print("实验：")
        for r_ in runs.list():
            p = r_.get("perf") or {}
            print(f"  {r_['id']:24s} {r_['name']}  年化 {p.get('cagr')}  夏普 {p.get('sharpe')}  回撤 {p.get('max_drawdown')}")
    elif args.cmd == "run":
        with open(args.config, encoding="utf-8") as fh:
            cfg = json.load(fh)
        rid = experiment.run(store, runs, cfg, _progress())
        s = runs.summary(rid)
        print(f"实验 {rid} 完成")
        print(json.dumps({"perf": s["perf"], "ic": s["ic"], "trade_stats": s["trade_stats"]}, ensure_ascii=False, indent=1))
        print("结果目录：", runs.path(rid))
    elif args.cmd == "factors":
        res = experiment.factor_study(store, {"dataset": args.dataset, "label": {"horizon": args.horizon}}, _progress())
        print(f"{'因子':18s} {'RankIC':>8s} {'ICIR':>7s} {'胜率':>6s}  说明")
        for r_ in res["factors"]:
            print(f"{r_['name']:18s} {r_['rank_ic'] or 0:8.4f} {r_['icir'] or 0:7.3f} {r_['positive'] or 0:6.2f}  {r_['desc']}")


if __name__ == "__main__":
    main()
