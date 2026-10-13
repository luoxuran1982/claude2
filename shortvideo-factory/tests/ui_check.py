"""界面检查（在 Linux 容器里用 Playwright + 无头 Chromium 跑，不进 CI）。

    python tests/ui_check.py --python <装好依赖的 Python> --shots <截图目录> [--chromium <chromium 路径>] [--generate]

做什么（对应交付要求「交付前的验收」）：
1. 用服务模式启动应用（全新的数据目录），等生成界面后台就绪。
2. 缩放：11 档逐个验证；下拉和「−」「＋」显示一致、每次 ±5、上下限禁用、恢复 100%、切换栏目和刷新后保留；
   外壳和嵌入的 Streamlit 页面字号都跟着变；Streamlit 下拉菜单弹出位置正确；没有横向溢出；在 70/95/100/120% 截图。
3. 使用指南：逐篇打开 README、UPSTREAM 和 docs/ 全部文档，内容完整、图片能显示、站内链接能跳转、附件能下载；
   源码清单、外壳和上游源码只读查看、源码 zip 下载；窄窗口下入口仍可见。
4. 运行状态：表里的 PID 与 ps 抽样一致（含内存），重启后台后列表更新、旧 PID 消失。
5. （--generate）走通一条主要操作：本地素材 + 无配音（不需要网络和 Key）生成一条视频；生成中 FFmpeg 子进程出现在运行状态里，结束后消失。
6. 扫一遍 Streamlit 页面里剩下的英文。
7. 点「退出」：启动器、后台全部结束，端口释放。
结果写到 <截图目录>/ui_check_report.json。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path

import psutil
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
STEPS = [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120]
REPORT: dict = {"checks": [], "problems": []}


def ok(name, detail=""):
    REPORT["checks"].append({"name": name, "detail": detail})
    print("✔", name, detail, flush=True)


def problem(name, detail=""):
    REPORT["problems"].append({"name": name, "detail": detail})
    print("✘", name, detail, flush=True)


def expect(cond, name, detail=""):
    (ok if cond else problem)(name, detail)
    return cond


def port_open(port):
    try:
        socket.create_connection(("127.0.0.1", port), 0.5).close()
        return True
    except OSError:
        return False


def ps_tree(root_pid: int) -> dict[int, float]:
    """用 ps 命令（不是 psutil）抽样本应用进程树：PID → RSS MiB。"""
    out = subprocess.run(["ps", "-eo", "pid=,ppid=,rss="], capture_output=True, text=True).stdout
    rows = [tuple(int(x) for x in line.split()) for line in out.splitlines() if line.strip()]
    children: dict[int, list] = {}
    rss = {}
    for pid, ppid, kb in rows:
        children.setdefault(ppid, []).append(pid)
        rss[pid] = kb / 1024
    tree, stack = {}, [root_pid]
    while stack:
        p = stack.pop()
        if p in rss:
            tree[p] = rss[p]
            stack += children.get(p, [])
    return tree


def start_app(python: str, data: Path, port: int) -> tuple[subprocess.Popen, dict]:
    env = dict(os.environ, SVF_DATA_DIR=str(data), PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.Popen([python, str(ROOT / "run.py"), "--serve-only", "--port", str(port)], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(120):
        try:
            rec = json.loads((data / "run.json").read_text(encoding="utf-8"))
            return proc, rec
        except (OSError, ValueError):
            time.sleep(0.5)
    raise SystemExit("应用没有启动")


def frame_of(page):
    for f in page.frames:
        if "/app/" in f.url:
            return f
    return None


def wait_studio(page, timeout=180):
    page.wait_for_selector("#studio-frame:not([hidden])", timeout=timeout * 1000)
    end = time.time() + timeout
    while time.time() < end:
        f = frame_of(page)
        if f:
            try:
                f.wait_for_selector("[data-testid='stSelectbox']", timeout=5000)
                return f
            except Exception:  # noqa: BLE001
                pass
        time.sleep(1)
    raise SystemExit("生成界面没有载入")


def dismiss_tour(frame):
    try:
        close = frame.locator("button:has-text('×')")
        if close.count():
            close.first.click(timeout=3000)
            frame.wait_for_timeout(500)
    except Exception:  # noqa: BLE001
        pass


def zoom_state(page, frame):
    shell_fs = page.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)")
    frame_fs = frame.evaluate("parseFloat(getComputedStyle(document.documentElement).fontSize)") if frame else None
    return {
        "select": int(page.input_value("#zoom-select")),
        "minus_disabled": page.is_disabled("#zoom-out"),
        "plus_disabled": page.is_disabled("#zoom-in"),
        "reset_disabled": page.is_disabled("#zoom-reset"),
        "shell_fs": round(shell_fs, 2),
        "frame_fs": round(frame_fs, 2) if frame_fs else None,
        "overflow_x": page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1"),
    }


def check_zoom_state(page, frame, pct, label):
    page.wait_for_timeout(350)
    s = zoom_state(page, frame)
    good = (s["select"] == pct and abs(s["shell_fs"] - 15 * pct / 100) < 0.05
            and (s["frame_fs"] is None or abs(s["frame_fs"] - 16 * pct / 100) < 0.05)
            and s["minus_disabled"] == (pct == 70) and s["plus_disabled"] == (pct == 120)
            and s["reset_disabled"] == (pct == 100) and not s["overflow_x"])
    # 顶栏关键控件都在可视范围内、可点
    for sel in ("#zoom-out", "#zoom-select", "#zoom-in", "#zoom-reset", "#quit-btn", "#tab-guide", "#tab-status"):
        box = page.locator(sel).bounding_box()
        vw = page.viewport_size["width"]
        if not box or box["x"] < 0 or box["x"] + box["width"] > vw + 1:
            good = False
            s[f"offscreen {sel}"] = box
    expect(good, f"缩放 {label}", json.dumps(s, ensure_ascii=False))
    return s


def check_popover(frame, pct):
    """Streamlit 下拉菜单弹出位置：紧贴输入框下方，左边对齐。"""
    box = frame.locator("[data-testid='stSelectbox']").nth(1)
    box.scroll_into_view_if_needed()
    inp = box.locator("input[role='combobox']")
    ib = inp.bounding_box()
    inp.click()
    frame.wait_for_timeout(500)
    lb = frame.locator("[role='listbox']").first.bounding_box()
    frame.page.keyboard.press("Escape")
    frame.wait_for_timeout(300)
    if not lb or not ib:
        return problem(f"下拉菜单 {pct}%", "没找到")
    gap = lb["y"] - (ib["y"] + ib["height"])
    expect(-30 < gap < 30 and abs(lb["x"] - ib["x"]) < 40, f"Streamlit 下拉菜单位置正确（{pct}%）",
           f"输入框底 {ib['y'] + ib['height']:.0f}，菜单顶 {lb['y']:.0f}，左边差 {lb['x'] - ib['x']:.0f}")


def run_zoom(page, shots: Path):
    frame = frame_of(page)
    opts = page.eval_on_selector_all("#zoom-select option", "els => els.map(e => +e.value)")
    expect(opts == STEPS, "缩放下拉共 11 档 70%–120%", str(opts))
    page.click("#zoom-reset") if not page.is_disabled("#zoom-reset") else None
    check_zoom_state(page, frame, 100, "初始 100%")
    # − 从 100 一路到 70，每次 −5
    pct = 100
    while pct > 70:
        page.click("#zoom-out")
        pct -= 5
        check_zoom_state(page, frame, pct, f"−→{pct}%")
    # ＋ 从 70 一路到 120
    while pct < 120:
        page.click("#zoom-in")
        pct += 5
        check_zoom_state(page, frame, pct, f"＋→{pct}%")
    # 下拉逐档选
    for p in STEPS:
        page.select_option("#zoom-select", str(p))
        check_zoom_state(page, frame, p, f"下拉选 {p}%")
    for p in (70, 120):
        page.select_option("#zoom-select", str(p))
        page.wait_for_timeout(400)
        check_popover(frame, p)
    # 截图
    for p in (70, 95, 100, 120):
        page.select_option("#zoom-select", str(p))
        page.wait_for_timeout(800)
        page.screenshot(path=str(shots / f"studio-zoom-{p}.png"))
    ok("截图：视频生成页 70% / 95% / 100% / 120%")
    # 切换栏目、刷新后保留，恢复 100%
    page.select_option("#zoom-select", "115")
    page.wait_for_timeout(500)
    page.click("#tab-guide")
    page.wait_for_timeout(500)
    expect(int(page.input_value("#zoom-select")) == 115, "切换栏目后档位保留（115%）")
    page.click("#tab-studio")
    page.reload()
    frame = wait_studio(page)
    page.wait_for_timeout(1000)
    s = zoom_state(page, frame)
    expect(s["select"] == 115 and abs(s["frame_fs"] - 18.4) < 0.05, "刷新后档位保留（115%），生成界面同步", str(s))
    page.click("#zoom-reset")
    check_zoom_state(page, frame, 100, "「100%」按钮恢复")
    # 快捷键
    page.keyboard.press("Control+=")
    check_zoom_state(page, frame, 105, "快捷键 Ctrl+=")
    page.keyboard.press("Control+0")
    check_zoom_state(page, frame, 100, "快捷键 Ctrl+0")


def run_guide(page, shots: Path, base: str):
    page.click("#tab-guide")
    page.wait_for_selector("#guide-nav .gn", timeout=15000)
    idx = page.request.get(base + "/api/guide").json()
    keys = page.eval_on_selector_all("#guide-nav button.gn[data-key^='doc:']", "els => els.map(e => e.dataset.key)")
    expect(len(keys) == len(idx["docs"]) >= 7, "使用指南目录列出全部文档", f"{len(keys)} 篇")
    for key in keys:
        page.click(f"#guide-nav button[data-key=\"{key}\"]")
        page.wait_for_selector(".md", timeout=10000)
        page.wait_for_timeout(400)
        path = key[4:]
        src = page.request.get(base + "/api/guide/doc?path=" + path).text()
        text_len = page.evaluate("document.querySelector('.md').innerText.length")
        imgs = page.evaluate("[...document.querySelectorAll('.md img')].map(i => [i.getAttribute('src'), i.complete && i.naturalWidth > 0])")
        headings = page.evaluate("document.querySelectorAll('.md h2, .md h3').length")
        bad_imgs = [i for i in imgs if not i[1]]
        expect(text_len > len(src) * 0.6 and not bad_imgs, f"文档完整显示：{path}",
               f"正文 {text_len} 字（源文件 {len(src)} 字），小标题 {headings} 个，图片 {len(imgs)} 张" + (f"，未显示 {bad_imgs}" if bad_imgs else ""))
        if path == "docs/01 使用指南.md":
            page.screenshot(path=str(shots / "guide-100.png"))
    # 站内链接：00 → 01
    page.click("#guide-nav button[data-key=\"doc:docs/00 项目阅读入口.md\"]")
    page.wait_for_selector(".md a[data-doc]")
    page.click(".md a[data-doc=\"docs/01 使用指南.md\"]")
    page.wait_for_timeout(600)
    expect(page.locator(".md[data-doc=\"docs/01 使用指南.md\"]").count() == 1, "文档里的站内链接能跳转（00 → 01）")
    for a in idx["assets"]:
        r = page.request.get(base + "/api/guide/file?path=" + a["path"])
        expect(r.status == 200 and len(r.body()) == a["size"], f"附件可打开：{a['path']}", a["kind"])
    # 源码
    page.click("#guide-nav button[data-key='sources']")
    page.wait_for_selector(".src-list")
    rows = page.locator(".src-list tr").count()
    expect(rows == len(idx["sources"]), "源码清单完整", f"{rows} 个文件（外壳 {idx['source_stats']['shell']['files']}，上游 {idx['source_stats']['upstream']['files']}）")
    for path in ("svf/app.py", "upstream/webui/Main.py"):
        page.click(f"tr[data-key=\"src:{path}\"]")
        page.wait_for_selector(".src-view")
        n = page.locator(".src-view tr").count()
        want = next(s["lines"] for s in idx["sources"] if s["path"] == path)
        expect(abs(n - want) <= 1, f"源码只读查看：{path}", f"{n} 行")
        if path == "svf/app.py":
            page.screenshot(path=str(shots / "guide-source-100.png"))
        page.click(".src-head button[data-key='sources']")
        page.wait_for_selector(".src-list")
    with page.expect_download() as dl:
        page.click(".src-head button[data-zip]")
    path = dl.value.path()
    z = zipfile.ZipFile(path)
    expect("shortvideo-factory/svf/app.py" in z.namelist(), "下载全部源码（zip）", f"{len(z.namelist())} 个文件，{os.path.getsize(path) // 1024} KB")


def run_narrow(page):
    page.set_viewport_size({"width": 760, "height": 760})
    page.wait_for_timeout(600)
    vis = all(page.locator(s).is_visible() for s in ("#tab-guide", "#tab-status", "#zoom-select", "#zoom-out", "#zoom-in", "#quit-btn"))
    page.click("#tab-guide")
    page.wait_for_timeout(500)
    expect(vis and page.locator("#guide-nav .gn").first.is_visible(), "窄窗口（760px）下栏目、缩放、退出和使用指南目录都可见可点")
    page.set_viewport_size({"width": 1440, "height": 900})
    page.wait_for_timeout(400)


def table_pids(page) -> dict[int, float]:
    rows = page.eval_on_selector_all("#proc-table tbody tr", "trs => trs.map(t => [...t.children].map(c => c.innerText))")
    return {int(r[2]): (float(r[4]) if re.match(r"^[\d.]+$", r[4]) else None) for r in rows}


def run_status(page, rec: dict, shots: Path, base: str):
    page.click("#tab-status")
    page.wait_for_selector("#proc-table")
    page.wait_for_timeout(1500)
    tbl = table_pids(page)
    ps = ps_tree(rec["launcher_pid"])
    same = set(tbl) == set(ps)
    mem_ok = all(tbl[p] is not None and abs(tbl[p] - ps[p]) <= max(8.0, ps[p] * 0.15) for p in tbl if p in ps)
    expect(same and mem_ok, "运行状态的 PID 与 ps 抽样一致（含内存）",
           "页面 " + json.dumps({p: tbl[p] for p in sorted(tbl)}) + " / ps " + json.dumps({p: round(ps[p], 1) for p in sorted(ps)}))
    page.screenshot(path=str(shots / "status-100.png"))
    # 重启后台：旧 PID 消失，新 PID 出现
    old = page.request.get(base + "/api/status").json()["webui"]["pid"]
    page.click("#webui-restart")
    end = time.time() + 120
    new = None
    while time.time() < end:
        st = page.request.get(base + "/api/status").json()["webui"]
        if st["state"] == "ready" and st["pid"] and st["pid"] != old:
            new = st["pid"]
            break
        time.sleep(1)
    page.click("#tab-status")
    page.wait_for_timeout(4000)
    tbl2 = table_pids(page)
    expect(new is not None and new in tbl2 and old not in tbl2 and not psutil.pid_exists(old),
           "重启生成界面后台后，列表换成新 PID，旧 PID 不再显示", f"旧 {old} → 新 {new}；表中 {sorted(tbl2)}")


def run_generate(page, base: str, data: Path, media: Path):
    page.click("#tab-studio")
    frame = wait_studio(page)
    dismiss_tour(frame)
    frame.locator(".mpt-grouped-select select").first.select_option(value="local")
    frame.wait_for_timeout(2500)
    frame.locator("[data-testid='stFileUploader'] input[type='file']").first.set_input_files(
        [str(media / "clip1.mp4"), str(media / "clip2.mp4")])
    frame.wait_for_timeout(4000)
    frame.locator("textarea[aria-label*='视频文案']").first.fill(
        "夏天容易犯困，是因为气温升高后血管扩张，大脑供血相对减少。午后小睡二十分钟，可以让下午更有精神。")
    frame.locator("textarea[aria-label*='视频关键词']").first.fill("summer, sleepy")
    frame.wait_for_timeout(800)
    frame.locator("button").filter(has_text="无配音").first.click()
    frame.wait_for_timeout(2000)
    frame.locator("button").filter(has_text="生成视频").last.click()
    t0 = time.time()
    saw_ffmpeg = set()
    while time.time() - t0 < 600:
        procs = page.request.get(base + "/api/processes").json()["processes"]
        saw_ffmpeg |= {p["pid"] for p in procs if "ffmpeg" in p["name"].lower()}
        body = frame.inner_text("body")
        if "视频生成完成" in body or "视频生成失败" in body:
            break
        time.sleep(1.5)
    done = "视频生成完成" in frame.inner_text("body")
    finals = list((data / "storage" / "tasks").glob("*/final-1.mp4"))
    page.screenshot(path=str(Path(REPORT["shots"]) / "generate-done.png"))
    expect(done and finals, "主要操作：本地素材 + 无配音生成一条视频（离线）",
           f"用时 {time.time() - t0:.0f} 秒，成片 {finals[0] if finals else '无'}")
    time.sleep(3)
    procs = page.request.get(base + "/api/processes").json()["processes"]
    left = [p for p in procs if p["pid"] in saw_ffmpeg]
    expect(saw_ffmpeg and not left, "生成中 FFmpeg 子进程出现在运行状态里，结束后从列表消失", f"出现过 {sorted(saw_ffmpeg)}，结束后残留 {left}")
    if finals:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                              "-of", "compact", str(finals[0])], capture_output=True, text=True).stdout
        ok("成片信息", out.replace("\n", " "))


ICONS = {"expand_more", "settings", "keyboard_arrow_right", "keyboard_arrow_down", "auto_awesome", "restart_alt", "preview",
         "graphic_eq", "article", "download", "upload", "close", "check", "delete", "folder_open", "refresh", "add", "remove"}


def scan_english(page):
    frame = frame_of(page)
    texts = frame.evaluate(r"""() => { const out = new Set();
      const tw = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
      for (let n = tw.nextNode(); n; n = tw.nextNode()) { const t = n.nodeValue.trim(); const el = n.parentElement;
        if (!t || !/[A-Za-z]{3,}/.test(t) || /[一-鿿]/.test(t)) continue;
        if (getComputedStyle(el).fontFamily.includes('Material') || el.closest('textarea, input, code, pre, noscript')) continue;
        out.add(t.slice(0, 60)); }
      document.querySelectorAll('[placeholder],[aria-label],[title]').forEach(e => { for (const a of ['placeholder','aria-label','title']) {
        const v = e.getAttribute(a); if (v && /[A-Za-z]{3,}/.test(v) && !/[一-鿿]/.test(v) && !/^https?:/.test(v)) out.add(a + '=' + v.slice(0, 60)); } });
      return [...out]; }""")
    texts = [t for t in texts if t not in ICONS]
    REPORT["remaining_english"] = texts
    ok("生成界面里剩余的英文（多为产品名、模型名、网址等专有名词）", f"{len(texts)} 处：" + "；".join(texts[:40]))


def run_quit(page, rec: dict, proc):
    pids = list(ps_tree(rec["launcher_pid"]))
    page.click("#tab-studio")
    page.once("dialog", lambda d: d.accept())
    page.click("#quit-btn")
    try:
        proc.wait(40)
    except subprocess.TimeoutExpired:
        return problem("点「退出」后 40 秒应用仍未退出")
    time.sleep(1)
    left = [p for p in pids if psutil.pid_exists(p) and psutil.Process(p).status() != psutil.STATUS_ZOMBIE]
    webui_port = rec.get("webui_port")
    expect(not left and not port_open(rec["shell_port"]), "点「退出」后全部进程结束、端口释放",
           f"退出前 {len(pids)} 个进程 {sorted(pids)}，残留 {left}；端口 {rec['shell_port']} 已关闭")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", default=sys.executable, help="运行应用的 Python（需装好 requirements*.txt）")
    ap.add_argument("--shots", required=True)
    ap.add_argument("--chromium", default=os.environ.get("SVF_CHROMIUM", "/opt/pw-browsers/chromium"))
    ap.add_argument("--port", type=int, default=18777)
    ap.add_argument("--generate", action="store_true", help="走通一条离线出片（本地素材 + 无配音）")
    ap.add_argument("--media", default="", help="--generate 用的两个测试视频所在目录（clip1.mp4、clip2.mp4）")
    a = ap.parse_args()
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    REPORT["shots"] = str(shots)
    data = Path(tempfile.mkdtemp(prefix="svf-ui-"))
    proc, rec = start_app(a.python, data, a.port)
    base = f"http://127.0.0.1:{rec['shell_port']}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=a.chromium)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            page = ctx.new_page()
            page.goto(base + "/")
            expect(page.inner_text("#app-version") == "v1.0.0", "顶栏显示版本号", page.inner_text("#app-version"))
            t0 = time.time()
            frame = wait_studio(page)
            ok("生成界面经反向代理载入", f"{time.time() - t0:.0f} 秒")
            dismiss_tour(frame)
            page.screenshot(path=str(shots / "studio-100.png"))
            run_zoom(page, shots)
            run_guide(page, shots, base)
            run_narrow(page)
            rec = json.loads((data / "run.json").read_text(encoding="utf-8"))
            run_status(page, rec, shots, base)
            if a.generate:
                media = Path(a.media) if a.media else Path(tempfile.mkdtemp(prefix="svf-media-"))
                for i, src in enumerate(("testsrc2", "mandelbrot"), 1):
                    if not (media / f"clip{i}.mp4").exists():
                        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{src}=size=1080x1920:rate=25",
                                        "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(media / f"clip{i}.mp4")], check=True)
                run_generate(page, base, data, media)
            page.click("#tab-studio")
            wait_studio(page)
            scan_english(page)
            rec = json.loads((data / "run.json").read_text(encoding="utf-8"))
            run_quit(page, rec, proc)
            browser.close()
    finally:
        if proc.poll() is None:
            proc.terminate()
        (shots / "ui_check_report.json").write_text(json.dumps(REPORT, ensure_ascii=False, indent=2), encoding="utf-8")
        shutil.rmtree(data, ignore_errors=True)
    print(f"\n通过 {len(REPORT['checks'])} 项，问题 {len(REPORT['problems'])} 项。报告：{shots / 'ui_check_report.json'}")
    return 1 if REPORT["problems"] else 0


if __name__ == "__main__":
    sys.exit(main())
