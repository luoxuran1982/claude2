#!/usr/bin/env python3
"""热点雷达入口。

    python run.py                 独立窗口（pywebview）；没有窗口组件时自动改用浏览器
    python run.py --browser       用默认浏览器打开
    python run.py --serve-only    只启动本机服务（开发、测试、CI 冒烟用）
    python run.py --worker …      内部用：抓取/MCP 工作进程（由主进程启动，见 hotradar/worker.py）
"""
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10 或更高版本（上游 TrendRadar 要求 3.12）")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        from hotradar.worker import main as worker_main
        worker_main(sys.argv[2:])
        return
    from hotradar.app import main as app_main
    app_main()


if __name__ == "__main__":
    main()
