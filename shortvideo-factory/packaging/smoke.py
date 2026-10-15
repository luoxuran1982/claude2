"""CI 冒烟测试：直接运行打好的安装包里的程序，检查能用、能干净退出。只用标准库 + psutil。

    python packaging/smoke.py

检查项：
1. 包里的东西：随包 Python、上游源码、开源字体、使用指南的文档和源码；没有商业字体和来源不明的音乐。
2. 随包 Python 能导入上游的主要模块，FFmpeg 可执行；随包 edge-tts 走通上游配音调用（假服务端）；
   用「知识讲解卡片」来源离线跑通一个短任务（固定文案 + 假配音），产出 mp4（1.0.3 起走快速通道，并报告选用的编码器）；
   1.0.3：防重复提交 / 编码中取消 / 残留任务标「已中断」（tests/tasks_check.py），大模型调用记录和目标时长（tests/llm_check.py，假接口）。
3. 服务模式启动：生成界面后台就绪；外壳页面、接口、使用指南（文档/源码/zip）、运行状态可用；
   1.0.3 的「打开目录」接口 /api/open-path：数据目录之外 403、没有令牌 403、目录之内（只检查不打开）200；
   经反向代理的 Streamlit 页面插入了外壳脚本，静态文件和 WebSocket 握手正常。
4. 发「退出」指令后：启动器、生成界面后台及其子进程全部结束，两个端口都不再监听，运行记录已删除。
5. 异常收尾：强制结束启动器（模拟崩溃）后，生成界面后台在 20 秒内自行结束（Windows 靠作业对象，macOS 靠看门狗）。
6. 窗口模式：能创建应用窗口（Windows WebView2 / macOS WKWebView），再用「退出」指令收尾。CI 上窗口建不起来时只警告。
"""
from __future__ import annotations

import base64
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

import psutil

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
WIN, MAC = sys.platform == "win32", sys.platform == "darwin"
APP_NAME = "AI 短视频工厂"
READY_TIMEOUT = 300

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def exe_and_payload() -> tuple[Path, Path]:
    if WIN:
        base = DIST / "ShortVideoFactory"
        return base / "ShortVideoFactory.exe", base / "payload"
    if MAC:
        app = DIST / f"{APP_NAME}.app"
        return app / "Contents" / "MacOS" / "ShortVideoFactory", app / "Contents" / "Resources" / "payload"
    base = DIST / "ShortVideoFactory"
    return base / "ShortVideoFactory", base / "payload"


def ok(msg):
    print("✔", msg, flush=True)


def fail(msg):
    print("✘", msg, flush=True)
    raise SystemExit(1)


def http(url, data=None, headers=None, timeout=15, raw=False):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return (r.status, dict(r.headers), body) if raw else body


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        return True
    except OSError:
        return False


def check_payload(payload: Path) -> Path:
    py = payload / "runtime" / ("python.exe" if WIN else "bin/python3")
    need = [py, payload / "upstream" / "webui" / "Main.py", payload / "upstream" / "LICENSE",
            payload / "child" / "svf_child.py", payload / "child" / "svf_ext" / "__init__.py", payload / "child" / "svf_ext" / "pipeline.py",
            payload / "fonts" / "NotoSansSC-Bold.ttf", payload / "fonts" / "OFL-NotoSansSC.txt",
            payload / "project" / "README.md", payload / "project" / "UPSTREAM.md", payload / "project" / "svf" / "app.py"]
    need += [payload / "project" / "docs" / n for n in ("00 项目阅读入口.md", "01 使用指南.md", "02 软件框架与系统架构.md",
                                                       "03 源码阅读与重建指南.md", "04 与AI知识视频工坊的对比.md")]
    missing = [str(p) for p in need if not p.exists()]
    if missing:
        fail(f"包里缺少：{missing}")
    bad = [str(p) for p in payload.rglob("*") if re.search(r"MicrosoftYaHei|STHeiti|UTM Kabel", p.name, re.I)
           or (p.suffix.lower() == ".mp3" and "songs" in p.parts)]
    if bad:
        fail(f"包里有不能分发的文件：{bad}")
    ok("包内文件齐全，没有商业字体和来源不明的音乐")
    return py


def check_runtime(py: Path, payload: Path, tmp: Path):
    env = dict(os.environ, MPT_CONFIG_FILE=str(tmp / "rt-config.toml"), MPT_STORAGE_DIR=str(tmp / "rt-storage"),
               MPT_FONT_DIR=str(payload / "fonts"), MPT_SONG_DIR=str(tmp / "rt-songs"), PYTHONDONTWRITEBYTECODE="1")
    for k in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)
    code = ("import sys; sys.path.insert(0, sys.argv[1]);"
            "from app.services import task, video, voice, llm, material, subtitle;"
            "from app.utils import utils;"
            "import imageio_ffmpeg, subprocess; exe = imageio_ffmpeg.get_ffmpeg_exe();"
            "subprocess.run([exe, '-version'], check=True, capture_output=True);"
            "print('fonts:', sorted(f for f in __import__('os').listdir(utils.font_dir()) if f.endswith('.ttf')));"
            "print('ffmpeg ok:', exe)")
    r = subprocess.run([str(py), "-c", code, str(payload / "upstream")], env=env, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=300, cwd=str(payload / "upstream"))
    print(r.stdout[-1500:], r.stderr[-3000:])
    if r.returncode != 0:
        fail("随包 Python 导入上游模块失败")
    ok("随包 Python 能导入上游主要模块，FFmpeg 可执行")
    # 随包 edge-tts 能走通上游配音调用（通信层换成假服务端，不连微软；真实配音要在用户电脑上验收）
    r = subprocess.run([str(py), str(ROOT / "tests" / "edge_tts_compat_check.py")], env=env, capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=300, cwd=str(payload / "upstream"))
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
    print(line or (r.stdout[-1500:] + r.stderr[-3000:]))
    res = json.loads(line[7:]) if line else {}
    if not (res.get("ok") and res.get("cues") == ["你好", "世界"] and str(res.get("gec_version", "")).startswith("2-")
            and res.get("refused_returns_none")):
        fail("随包 edge-tts 走不通上游配音调用")
    ok(f"随包 edge-tts {res['edge_tts']} 走通上游配音调用（假服务端，Sec-MS-GEC v2）")
    # 知识讲解卡片：用固定文案、假配音（本地生成的音频 + 逐字时间），离线跑通上游完整流水线，产出 mp4
    env2 = dict(env, MPT_STORAGE_DIR=str(tmp / "cards-storage"), MPT_CONFIG_FILE=str(tmp / "cards-config.toml"))
    t0 = time.time()
    r = subprocess.run([str(py), str(ROOT / "tests" / "cards_task_check.py"), str(payload / "child"), "knowledge_cards"], env=env2,
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=1200, cwd=str(payload / "upstream"))
    line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
    print(line or (r.stdout[-3000:] + r.stderr[-3000:]))
    res = json.loads(line[7:]) if line else {}
    if not (res.get("ok") and res.get("size") == [1080, 1920] and res.get("subtitle") and res.get("pages")):
        print(r.stdout[-4000:], r.stderr[-4000:])
        fail("知识讲解卡片离线任务没有产出可用的 mp4")
    codec = re.search(r"知识卡片快速通道：编码器 (\S+)", r.stdout + r.stderr)
    ok(f"知识讲解卡片：离线跑通短任务，{len(res['pages'])} 页（{'、'.join(res['pages'])}），"
       f"成片 {res['duration']} 秒 {res['size'][0]}×{res['size'][1]}，用时 {time.time() - t0:.0f} 秒，"
       f"快速通道编码器 {codec.group(1) if codec else '（没走快速通道）'}")
    if not codec:
        fail("知识讲解卡片没有走快速通道")
    # 1.0.3：防重复提交、取消、残留任务；大模型调用记录和目标时长
    for script, label in (("tasks_check.py", "防重复提交、编码中取消（结束 FFmpeg、状态不被写回）、残留任务标「已中断」"),
                          ("llm_check.py", "大模型调用记录（完整提示词、原始输出、token，没有钥匙）、提示词里的目标时长")):
        env3 = dict(env, MPT_STORAGE_DIR=str(tmp / f"{script}-storage"), MPT_CONFIG_FILE=str(tmp / f"{script}-config.toml"))
        r = subprocess.run([str(py), str(ROOT / "tests" / script), str(payload / "child")], env=env3, capture_output=True,
                           text=True, encoding="utf-8", errors="replace", timeout=900, cwd=str(payload / "upstream"))
        line = next((l for l in r.stdout.splitlines() if l.startswith("RESULT ")), "")
        print(line[:1500] or (r.stdout[-3000:] + r.stderr[-3000:]))
        res = json.loads(line[7:]) if line else {}
        if not res.get("ok"):
            print(r.stdout[-4000:], r.stderr[-4000:])
            fail(f"1.0.3 检查没通过：{label}")
        ok(f"1.0.3：{label}")


def start(exe: Path, data: Path, *extra) -> subprocess.Popen:
    env = dict(os.environ, SVF_DATA_DIR=str(data))
    for k in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(k, None)
    return subprocess.Popen([str(exe), *extra], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_record(data: Path, proc, timeout=60) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            dump_logs(data)
            fail(f"程序提前退出，返回码 {proc.returncode}")
        try:
            rec = json.loads((data / "run.json").read_text(encoding="utf-8"))
            if rec.get("shell_port") and rec.get("launcher_pid") == proc.pid:   # 不要读到上次留下的旧记录
                return rec
        except (OSError, ValueError):
            pass
        time.sleep(0.5)
    dump_logs(data)
    fail("等不到运行记录 run.json")


def wait_ready(base: str, data: Path, proc) -> dict:
    deadline = time.time() + READY_TIMEOUT
    st = {}
    while time.time() < deadline:
        if proc.poll() is not None:
            dump_logs(data)
            fail("程序提前退出")
        try:
            st = json.loads(http(base + "/api/status"))
            if st["webui"]["state"] == "ready":
                return st
            if st["webui"]["state"] == "failed":
                dump_logs(data)
                fail("生成界面后台启动失败：" + st["webui"]["error"][-2000:])
        except OSError:
            pass
        time.sleep(1)
    dump_logs(data)
    fail(f"{READY_TIMEOUT} 秒内生成界面后台没有就绪：{st}")


def dump_logs(data: Path):
    for name in ("launcher.log", "webui.log"):
        p = data / "logs" / name
        if p.exists():
            print(f"----- {name} -----")
            print(p.read_text(encoding="utf-8", errors="replace")[-6000:])


def ws_handshake(port: int, path: str) -> int:
    key = base64.b64encode(os.urandom(16)).decode()
    s = socket.create_connection(("127.0.0.1", port), 10)
    s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Origin: http://127.0.0.1:{port}\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
               f"Sec-WebSocket-Protocol: streamlit\r\n\r\n").encode())
    resp = s.recv(1024).decode("latin-1")
    s.close()
    return int(resp.split()[1]) if resp.startswith("HTTP/") else 0


def tree(pid: int) -> list[int]:
    try:
        p = psutil.Process(pid)
        return [pid] + [c.pid for c in p.children(recursive=True)]
    except psutil.Error:
        return []


def alive(pids) -> list[int]:
    out = []
    for p in pids:
        try:
            if psutil.Process(p).status() != psutil.STATUS_ZOMBIE:
                out.append(p)
        except psutil.Error:
            pass
    return out


def check_service(base: str, rec: dict, st: dict):
    page = http(base + "/").decode("utf-8")
    assert "AI 短视频工厂" in page and rec["token"] in page, "外壳页面不对"
    http(base + "/ui/app.js")
    http(base + "/ui/inject.js")
    g = json.loads(http(base + "/api/guide"))
    docs = {d["path"] for d in g["docs"]}
    need = {"README.md", "UPSTREAM.md", "docs/00 项目阅读入口.md", "docs/01 使用指南.md", "docs/02 软件框架与系统架构.md",
            "docs/03 源码阅读与重建指南.md", "docs/04 与AI知识视频工坊的对比.md"}
    assert need <= docs, docs
    assert any(s["path"] == "svf/app.py" for s in g["sources"]) and any(s["path"] == "upstream/webui/Main.py" for s in g["sources"])
    for d in sorted(docs):
        text = http(base + "/api/guide/doc?path=" + urllib.parse.quote(d)).decode("utf-8")
        assert len(text) > 200, d
    guide01 = http(base + "/api/guide/doc?path=" + urllib.parse.quote("docs/01 使用指南.md")).decode("utf-8")
    assert "怎样缩放界面" in guide01 and "Pexels" in guide01
    assert "def main" in http(base + "/api/guide/source?path=svf/app.py").decode("utf-8")
    z = zipfile.ZipFile(__import__("io").BytesIO(http(base + "/api/guide/source.zip", timeout=60)))
    assert "shortvideo-factory/svf/app.py" in z.namelist() and "shortvideo-factory/docs/01 使用指南.md" in z.namelist()
    ok(f"使用指南：{len(g['docs'])} 篇文档、{len(g['sources'])} 个源码文件、{len(g['assets'])} 个附件，源码 zip {len(z.namelist())} 个文件")
    check_open_path(base, rec)
    procs = json.loads(http(base + "/api/processes"))
    pids = {p["pid"] for p in procs["processes"]}
    assert rec["launcher_pid"] in pids and st["webui"]["pid"] in pids, procs
    assert all(p["mem_mib"] is not None for p in procs["processes"]), procs
    ok(f"运行状态：{procs['count']} 个进程，合计 {procs['total_mib']} MiB（{procs['metric']}）")
    html = http(base + "/app/").decode("utf-8")
    assert "/ui/inject.js" in html, "Streamlit 页面没有插入外壳脚本"
    assert http(base + "/app/_stcore/health").strip() == b"ok"
    css = re.search(r'static/css/[^"]+\.css', html)
    if css:
        text = http(base + "/app/" + css.group(0)).decode("utf-8", "replace")
        font = re.search(r"media/(MaterialSymbols[^)\"']+\.woff2)", text)
        if font:
            status, _, body = http(base + "/app/static/media/" + font.group(1), raw=True)
            assert status == 200 and len(body) > 10000, "Streamlit 图标字体加载失败"
    code = ws_handshake(rec["shell_port"], "/app/_stcore/stream")
    assert code == 101, f"WebSocket 握手返回 {code}"
    ok("Streamlit 页面经反向代理可用：插入了外壳脚本，静态文件、图标字体、WebSocket 握手正常")


def check_open_path(base: str, rec: dict):
    """1.0.3：生成界面后台请启动器打开文件夹的接口。CI 上不真的打开窗口（check_only），只验证路径限制。"""
    st = json.loads(http(base + "/api/status"))
    data = Path(st["paths"]["data"])
    hdr = {"X-SVF-Token": rec["token"], "Content-Type": "application/json"}

    def call(payload, headers=hdr):
        try:
            return 200, json.loads(http(base + "/api/open-path", data=json.dumps(payload).encode(), headers=headers))
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read() or b"{}")
            except ValueError:
                return e.code, {}
    code, res = call({"path": str(data / "storage"), "check_only": True})
    assert code == 200 and res.get("ok"), (code, res)
    code, res = call({"path": str(data.parent), "check_only": True})
    assert code == 403 and "数据目录" in res.get("error", ""), (code, res)
    code, res = call({"path": str(data / "storage" / ".." / ".." / "outside"), "check_only": True})
    assert code == 403, (code, res)
    code, res = call({"path": str(data / "storage" / "no-such-dir"), "check_only": True})
    assert code == 404 and res.get("path"), (code, res)
    code, _ = call({"path": str(data / "storage"), "check_only": True}, headers={"Content-Type": "application/json"})
    assert code == 403, code
    ok("打开目录接口 /api/open-path：数据目录之内放行（只检查），目录外、.. 越界、没有令牌都被拒绝，不存在的路径给出完整路径")


def quit_and_verify(base: str, rec: dict, proc, data: Path):
    pids = tree(rec["launcher_pid"])
    webui_port = json.loads(http(base + "/api/status"))["webui"]["port"]
    http(base + "/api/quit", data=b"{}", headers={"X-SVF-Token": rec["token"], "Content-Type": "application/json"})
    try:
        proc.wait(40)
    except subprocess.TimeoutExpired:
        dump_logs(data)
        fail("发出退出指令 40 秒后启动器仍未退出")
    time.sleep(1)
    left = alive(pids)
    if left:
        dump_logs(data)
        fail(f"退出后仍有进程：{left}")
    if port_open(rec["shell_port"]) or (webui_port and port_open(webui_port)):
        fail("退出后端口仍在监听")
    if (data / "run.json").exists():
        fail("退出后运行记录 run.json 没删")
    ok(f"退出指令：{len(pids)} 个进程 {pids} 全部结束，端口 {rec['shell_port']}、{webui_port} 已释放")


def crash_and_verify(exe: Path, data: Path):
    proc = start(exe, data, "--serve-only")
    rec = wait_record(data, proc)
    base = f"http://127.0.0.1:{rec['shell_port']}"
    st = wait_ready(base, data, proc)
    webui_pid, webui_port = st["webui"]["pid"], st["webui"]["port"]
    descendants = tree(webui_pid)
    proc.kill()   # 模拟崩溃 / 任务管理器强制结束
    proc.wait(10)
    deadline = time.time() + 20
    while time.time() < deadline and alive(descendants):
        time.sleep(0.5)
    left = alive(descendants)
    if left:
        dump_logs(data)
        for p in left:
            try:
                psutil.Process(p).kill()
            except psutil.Error:
                pass
        fail(f"启动器被强制结束后，生成界面后台 {left} 没有自行退出")
    if port_open(webui_port):
        fail("启动器被强制结束后，生成界面端口仍在监听")
    ok(f"异常收尾：强制结束启动器后，生成界面后台 {descendants} 已自行结束，端口 {webui_port} 已释放")
    # 下次启动应能识别并清掉残留的运行记录
    proc = start(exe, data, "--serve-only", "--no-webui")
    rec2 = wait_record(data, proc)
    assert rec2["launcher_pid"] == proc.pid
    quit_and_verify(f"http://127.0.0.1:{rec2['shell_port']}", rec2, proc, data)


def window_check(exe: Path, data: Path):
    proc = start(exe, data)
    rec = wait_record(data, proc)
    base = f"http://127.0.0.1:{rec['shell_port']}"
    log = data / "logs" / "launcher.log"
    created = False
    for _ in range(60):
        text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
        if "窗口已创建并载入页面" in text:
            created = True
            break
        if "原生窗口启动失败" in text:
            break
        time.sleep(1)
    st = json.loads(http(base + "/api/status"))
    print("显示方式：", st["window"])
    if created:
        ok("窗口模式：应用窗口已创建并载入外壳页面")
    else:
        print("⚠ 窗口模式：CI 上没有确认到窗口创建（应用会自动改用浏览器），日志如下")
        dump_logs(data)
    quit_and_verify(base, rec, proc, data)


def main():
    exe, payload = exe_and_payload()
    if not exe.exists():
        fail(f"找不到程序：{exe}")
    py = check_payload(payload)
    tmp = Path(tempfile.mkdtemp(prefix="svf-smoke-"))
    check_runtime(py, payload, tmp)

    data = tmp / "data"
    proc = start(exe, data, "--serve-only")
    rec = wait_record(data, proc)
    base = f"http://127.0.0.1:{rec['shell_port']}"
    t0 = time.time()
    st = wait_ready(base, data, proc)
    ok(f"服务模式启动：生成界面后台 PID {st['webui']['pid']} 就绪，用时 {time.time() - t0:.0f} 秒")
    cfg = (data / "config.toml").read_text(encoding="utf-8")
    assert 'language = "zh"' in cfg and "NotoSansSC-Bold.ttf" in cfg, "首启配置没有设成中文/开源字体"
    assert (data / "fonts" / "NotoSansSC-Bold.ttf").exists()
    assert 'voice_name = "zh-CN-XiaoxiaoNeural-Female"' in cfg, "首启配置没有设默认中文配音声音"
    ok("首启：用户数据目录里生成了中文配置（含默认中文配音声音），复制了开源字体")
    check_service(base, rec, st)
    quit_and_verify(base, rec, proc, data)

    crash_and_verify(exe, tmp / "data-crash")
    if WIN or MAC:
        window_check(exe, tmp / "data-window")
    print("冒烟测试全部通过", flush=True)


if __name__ == "__main__":
    main()
