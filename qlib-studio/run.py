#!/usr/bin/env python3
"""Qlib 量化研究台入口。

    python run.py                 独立窗口（pywebview）；没有窗口组件时自动改用浏览器
    python run.py --browser       用默认浏览器打开
    python run.py --serve-only    只启动本机服务（开发、测试、CI 冒烟用）
    python run.py --worker <job.json> --parent <PID>   内部用：工作进程（由主进程启动，见 qlibstudio/worker.py）
"""
import multiprocessing
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10 或更高版本（推荐 3.12，与安装包相同）")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        from qlibstudio.worker import main as worker_main
        worker_main(sys.argv[2:])
        return
    from qlibstudio.app import main as app_main
    app_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()  # 打包后的程序里，上游万一启动多进程时不会把整个应用再开一遍
    main()
