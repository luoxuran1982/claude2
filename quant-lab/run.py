#!/usr/bin/env python3
"""QuantLab 量化学习实验室：python run.py（独立窗口），python run.py --browser（浏览器），python run.py cli ...（命令行）。"""
import os
import sys

if sys.version_info < (3, 10):
    sys.exit("需要 Python 3.10 或更高版本")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from quantlab.app import main  # noqa: E402

if __name__ == "__main__":
    main()
