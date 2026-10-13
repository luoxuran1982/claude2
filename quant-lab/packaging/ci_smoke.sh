#!/usr/bin/env bash
# CI 冒烟测试（Windows / macOS 打包版共用）：
# 等服务起来 → 主要接口 → 使用指南文档与源码 → 路径穿越被拦截 → 发退出指令 → 确认主进程结束、端口不再监听。
# 用法：bash packaging/ci_smoke.sh <端口> <url 文件>
set -euo pipefail
PORT=$1
URL_FILE=$2
BASE="http://127.0.0.1:$PORT"

ok=
for i in $(seq 60); do
  if [ -s "$URL_FILE" ] && curl -sf "$BASE/api/status" >/dev/null; then ok=1; break; fi
  sleep 2
done
[ -n "$ok" ] || { cat "$QUANTLAB_DATA/quantlab.log" || true; exit 1; }
TOKEN=$(sed 's/.*?t=//' "$URL_FILE" | tr -d '\r\n')

curl -sf "$BASE/" | grep -q "QuantLab"
curl -sf "$BASE/vendor/echarts.min.js" -o /dev/null
curl -sf "$BASE/markdown.js" -o /dev/null
curl -sf "$BASE/api/catalog" | grep -q lgbm
curl -sf "$BASE/api/processes" > ps.json
curl -sf "$BASE/api/docs" > docs.json
curl -sf "$BASE/api/sources" > srcs.json
curl -sf "$BASE/api/source.zip" -o src.zip

python - <<'EOF'
import json, zipfile
ps = json.load(open("ps.json", encoding="utf-8"))
print("进程：", [(p["role"], p["name"], p["pid"], p["mem_mib"]) for p in ps["processes"]], ps["metric"], ps["unit"])
assert ps["processes"] and ps["processes"][0]["mem_mib"], ps
open("pid.txt", "w").write(str(ps["processes"][0]["pid"]))
paths = [d["path"] for d in json.load(open("docs.json", encoding="utf-8"))["docs"]]
print("文档：", paths)
assert "docs/00 项目阅读入口.md" in paths and "docs/01 使用指南.md" in paths and "README.md" in paths and len(paths) >= 7, paths
srcs = [f["path"] for f in json.load(open("srcs.json", encoding="utf-8"))["files"]]
assert "quantlab/app.py" in srcs and not any("vendor/" in s for s in srcs), srcs
assert "quant-lab/quantlab/app.py" in zipfile.ZipFile("src.zip").namelist()
print("源码文件：", len(srcs))
EOF
PID=$(cat pid.txt)

code=$(curl -s -o /dev/null -w "%{http_code}" "$BASE/api/source?path=..%2F..%2Fetc%2Fpasswd")
[ "$code" = 404 ] || { echo "路径穿越没有被拦截：$code"; exit 1; }

curl -sf -X POST -H "X-QuantLab-Token: $TOKEN" -H "Content-Type: application/json" -d '{}' "$BASE/api/quit"
gone=
for i in $(seq 30); do
  if ! curl -s -o /dev/null "$BASE/api/status"; then gone=1; break; fi
  sleep 1
done
[ -n "$gone" ] || { echo "退出后端口仍在监听"; exit 1; }
sleep 3
if [ "${OS:-}" = "Windows_NT" ]; then
  if tasklist //FI "PID eq $PID" | grep -q "$PID"; then echo "退出后主进程 $PID 仍在"; tasklist; exit 1; fi
else
  if ps -p "$PID" >/dev/null; then echo "退出后主进程 $PID 仍在"; ps -p "$PID"; exit 1; fi
fi
rm -f ps.json docs.json srcs.json src.zip pid.txt
echo "冒烟测试通过：接口可用，退出后主进程 $PID 已结束、端口 $PORT 已释放"
