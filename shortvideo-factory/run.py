"""AI 短视频工厂：源码运行入口（开发用）。安装包里的可执行文件也是从这里打包的。

    pip install -r requirements.txt -r requirements-runtime.txt
    python run.py              # 独立窗口（不可用时自动改用浏览器）
    python run.py --browser    # 用默认浏览器打开
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from svf.app import main  # noqa: E402

if __name__ == "__main__":
    main()
