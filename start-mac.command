#!/bin/bash
# macOS：双击运行。第一次会自动建虚拟环境并安装依赖（约 1 分钟）。
cd "$(dirname "$0")" || exit 1
PY="$(command -v python3)"
if [ -z "$PY" ]; then
  echo "没有找到 Python 3。请先安装：https://www.python.org/downloads/ 或 brew install python"
  read -n 1 -s -r -p "按任意键退出"; exit 1
fi
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || { read -n 1 -s -r -p "创建虚拟环境失败，按任意键退出"; exit 1; }
fi
if ! cmp -s requirements.txt .venv/req-installed.txt; then
  echo "正在安装依赖…"
  .venv/bin/python -m pip install -q --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt && cp requirements.txt .venv/req-installed.txt \
    || { read -n 1 -s -r -p "安装依赖失败，按任意键退出"; exit 1; }
fi
exec .venv/bin/python run.py "$@"
