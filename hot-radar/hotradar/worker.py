"""工作进程：在独立进程里运行上游 TrendRadar（一次抓取、推送测试、体检）或它的 MCP 服务。

由主进程用同一个程序启动：HotRadar --worker <任务> --workspace <工作区> --log <日志> --parent <主进程 PID>
（源码运行时是 python run.py --worker …）。为什么用独立进程：
- 上游按「当前目录下的 config/ 和 output/」工作，独立进程才能切换当前目录而不影响主进程；
- 上游会加载 LiteLLM 等大依赖，抓取完进程退出就把内存还给系统；
- 抓取卡住时主进程可以直接结束它。

异常收尾：工作进程每 2 秒检查一次主进程是否还在（PID + 启动时间，防止 PID 被复用），
主进程崩溃或被强制结束后，工作进程在 2 秒内自行退出，不会长期遗留。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback

from . import paths

TASKS = ("crawl", "demo", "test-notify", "doctor", "mcp")


def _parent_alive_checker(parent_pid: int):
    try:
        import psutil
        proc = psutil.Process(parent_pid)
        created = proc.create_time()

        def alive() -> bool:
            try:
                return proc.is_running() and proc.create_time() == created
            except psutil.Error:
                return False
        return alive
    except Exception:  # noqa: BLE001  没有 psutil 或主进程已不在
        if sys.platform == "win32":
            return lambda: True  # 没有 psutil 时 Windows 无法安全探测，只能依赖主进程退出时结束本进程
        def alive_posix() -> bool:
            try:
                os.kill(parent_pid, 0)
                return os.getppid() == parent_pid
            except OSError:
                return False
        return alive_posix


def start_watchdog(parent_pid: int, interval: float = 2.0) -> None:
    if parent_pid <= 0:
        return
    alive = _parent_alive_checker(parent_pid)

    def loop():
        while True:
            time.sleep(interval)
            if not alive():
                print(f"[热点雷达] 主进程（PID {parent_pid}）已不在，工作进程自行退出", flush=True)
                os._exit(3)
    threading.Thread(target=loop, name="parent-watchdog", daemon=True).start()


def _redirect_output(log_path: str) -> None:
    """输出一律写进日志文件（行缓冲）。主进程读这个文件显示进度，比管道更可靠（Windows 无控制台程序也一样）。"""
    f = open(log_path, "a", encoding="utf-8", errors="replace", buffering=1)  # noqa: SIM115
    sys.stdout = f
    sys.stderr = f


def _patch_upstream(task: str, workspace: str) -> None:
    """运行时替换（不改上游文件），详见 UPSTREAM.md「运行时替换」。"""
    import webbrowser
    webbrowser.open = lambda *a, **k: False  # 上游生成报告后会打开系统浏览器；桌面版在窗口里看

    import trendradar.__main__ as tm

    def no_version_check(*_a, **_k):
        print("[热点雷达] 桌面版不联网检查上游版本：上游代码随热点雷达安装包一起更新")
        return False, None
    tm.check_all_versions = no_version_check

    if task == "demo":
        _patch_demo_fetcher(workspace)


def _patch_demo_fetcher(workspace: str) -> None:
    """离线演示：热榜不访问 newsnow，改读安装包里的样例（上游样例库 2025-12-27 的真实抓取结果）。
    每次演示轮流用 3 次抓取中的下一次，这样连续演示能看到「新增」标记的变化。"""
    from trendradar.crawler.fetcher import DataFetcher

    sample = json.loads((paths.samples_dir() / "newsnow-sample-2025-12-27.json").read_text(encoding="utf-8"))
    counter = os.path.join(workspace, "demo-counter.txt")
    try:
        n = int(open(counter, encoding="ascii").read().strip() or 0)
    except (OSError, ValueError):
        n = 0
    snap = sample["snapshots"][n % len(sample["snapshots"])]
    with open(counter, "w", encoding="ascii") as f:
        f.write(str(n + 1))
    print(f"[热点雷达] 离线演示：使用样例数据（上游仓库 {snap['crawl_time']} 的抓取结果），不访问 newsnow")

    def fetch_data(self, id_info, max_retries=2, min_retry_wait=3, max_retry_wait=5):
        id_value, alias = (id_info if isinstance(id_info, tuple) else (id_info, id_info))
        items = snap["platforms"].get(id_value)
        if items is None:
            print(f"请求 {id_value} 失败: 样例数据里没有这个平台")
            return None, id_value, alias
        print(f"获取 {id_value} 成功（样例数据）")
        return json.dumps({"status": "success", "id": id_value, "items": items}, ensure_ascii=False), id_value, alias

    DataFetcher.fetch_data = fetch_data


def run(task: str, workspace: str, log_path: str, parent_pid: int, port: int = 0) -> int:
    _redirect_output(log_path)
    start_watchdog(parent_pid)
    os.chdir(workspace)
    os.environ.pop("GITHUB_ACTIONS", None)  # CI 里也按本机模式运行（本地 SQLite 存储）
    os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")  # LiteLLM 启动时不联网下载价格表
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    print(f"[热点雷达] 工作进程 PID {os.getpid()} 开始：{task}，工作目录 {workspace}", flush=True)
    paths.ensure_upstream_importable()
    try:
        if task == "mcp":
            from mcp_server.server import run_server
            run_server(project_root=workspace, transport="http", host="127.0.0.1", port=port or 3333)
            return 0
        _patch_upstream(task, workspace)
        import trendradar.__main__ as tm
        sys.argv = ["trendradar"] + {"test-notify": ["--test-notification"], "doctor": ["--doctor"]}.get(task, [])
        tm.main()
        return 0
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
        return code
    except BaseException:  # noqa: BLE001
        print("[热点雷达] 工作进程出错：", flush=True)
        traceback.print_exc()
        return 2
    finally:
        print(f"[热点雷达] 工作进程结束：{task}", flush=True)
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]) -> None:
    import argparse
    ap = argparse.ArgumentParser(prog="HotRadar --worker")
    ap.add_argument("task", choices=TASKS)
    ap.add_argument("--workspace", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--parent", type=int, default=0)
    ap.add_argument("--port", type=int, default=0)
    a = ap.parse_args(argv)
    code = run(a.task, a.workspace, a.log, a.parent, a.port)
    os._exit(code)  # 不等上游可能残留的非守护线程
