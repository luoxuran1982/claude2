"""界面检查（在 Linux 容器里用 Playwright + 无头 Chromium 跑，不进 CI）。

    python tests/ui_check.py --python <装好依赖的 Python> --shots <截图目录> [--chromium <chromium 路径>] [--generate]

做什么（对应交付要求「交付前的验收」）：
1. 用服务模式启动应用（全新的数据目录），等生成界面后台就绪。
2. 缩放：11 档逐个验证；下拉和「−」「＋」显示一致、每次 ±5、上下限禁用、恢复 100%、切换栏目和刷新后保留；
   外壳和嵌入的 Streamlit 页面字号都跟着变；Streamlit 下拉菜单弹出位置正确；没有横向溢出；在 70/95/100/120% 截图。
3. 使用指南：逐篇打开 README、UPSTREAM 和 docs/ 全部文档，内容完整、图片能显示、站内链接能跳转、附件能下载；
   源码清单、外壳和上游源码只读查看、源码 zip 下载；窄窗口下入口仍可见。
4. 运行状态：表里的 PID 与 ps 抽样一致（含内存），重启后台后列表更新、旧 PID 消失。
5. （--generate）走通一条主要操作：本地素材 + 无配音（不需要网络和 Key）生成一条视频；生成中 FFmpeg 子进程出现在运行状态里，结束后消失；
   再用「知识讲解卡片」+ 无配音生成一条（1.0.2）。
5b. 1.0.2：视频来源下拉的「程序生成」组、卡片配色、声音和文案语言不一致提醒、钥匙填错栏位提示、素材下载代理输入框。
5c. 1.0.3（--generate 时）：目标时长控件；界面上「生成文案」（假的 OpenAI 兼容接口）→ 连点三次「生成视频」只起一个任务、按钮变灰「生成中…」；
   日志解读、大模型调用记录（含完整提示词、token，没有钥匙）、原始日志行号；任务管理里取消 → 「已取消」→ 能删除；
   「打开目录」失败时显示完整路径和「复制路径」（容器里没有 xdg-open，正好走失败分支）。截图写进 docs/assets。
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
sys.path.insert(0, str(ROOT))
from svf import __version__  # noqa: E402
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
    """关掉上游的新手引导（driver.js 遮罩会挡住点击）。"""
    for _ in range(6):
        try:
            frame.wait_for_timeout(800)
            if frame.locator(".driver-overlay, .driver-popover").count() == 0:
                return
            btn = frame.locator(".driver-popover-close-btn")
            if btn.count():
                btn.first.click(timeout=3000, force=True)
            else:
                frame.page.keyboard.press("Escape")
        except Exception:  # noqa: BLE001
            frame.page.keyboard.press("Escape")
    # 仍关不掉（引导指向的元素不在视野里时）就直接移除遮罩，只影响这次检查
    frame.evaluate("document.querySelectorAll('.driver-overlay, .driver-popover').forEach(e => e.remove());"
                   "document.body.classList.remove('driver-active', 'driver-fade')")


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
    dismiss_tour(frame)
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
        # 图片是懒加载的：逐张滚到可视范围，等它载入
        for i in range(page.locator(".md img").count()):
            page.locator(".md img").nth(i).scroll_into_view_if_needed()
            page.wait_for_timeout(500)
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


def finals_of(data: Path) -> set:
    tasks = data / "storage" / "tasks"
    return set(tasks.glob("*/final-1.mp4")) if tasks.exists() else set()


def wait_generation(page, frame, data: Path, before: set, base: str = "", timeout: int = 600):
    """等这次生成结束：出现新的成片（不在 before 里）且页面显示完成，或页面显示失败。返回 (成功, 新成片, 期间见过的 FFmpeg PID)。"""
    t0 = time.time()
    saw_ffmpeg = set()
    new = []
    while time.time() - t0 < timeout:
        if base:
            procs = page.request.get(base + "/api/processes").json()["processes"]
            saw_ffmpeg |= {p["pid"] for p in procs if "ffmpeg" in p["name"].lower()}
        new = sorted(finals_of(data) - before)
        body = frame.inner_text("body")
        if (new and "视频生成完成" in body) or ("视频生成失败" in body and time.time() - t0 > 5):
            break
        time.sleep(1.5)
    return bool(new) and "视频生成完成" in frame.inner_text("body"), new, saw_ffmpeg


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
    before = finals_of(data)
    t0 = time.time()
    frame.locator("button").filter(has_text="生成视频").last.click()
    done, finals, saw_ffmpeg = wait_generation(page, frame, data, before, base)
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


def voice_select(frame, text):
    """在「配音声音」下拉里输入并选中一个声音。"""
    box = frame.locator("[data-testid='stSelectbox']").filter(has_text="配音声音").first
    box.locator("input").first.click()
    frame.page.keyboard.press("Control+a")
    frame.page.keyboard.press("Backspace")
    frame.page.keyboard.type(text)
    frame.wait_for_timeout(800)
    frame.page.keyboard.press("Enter")
    frame.wait_for_timeout(2500)


def run_cards_ui(page, shots: Path, data: Path):
    """1.0.2：程序生成来源、卡片配色、声音语言提醒、钥匙格式提示、素材下载代理。"""
    page.click("#tab-studio")
    frame = wait_studio(page)
    dismiss_tour(frame)
    sel = frame.locator(".mpt-grouped-select select").first
    groups = sel.evaluate("s => [...s.querySelectorAll('optgroup')].map(g => [g.label, [...g.querySelectorAll('option')].map(o => o.value)])")
    prog = dict((g[0], g[1]) for g in groups).get("程序生成", [])
    expect(prog == ["knowledge_cards", "hybrid_pixabay", "hybrid_pexels"], "视频来源下拉单列「程序生成」组（知识讲解卡片、两种混合）", str(groups))
    sel.select_option(value="knowledge_cards")
    frame.wait_for_timeout(3000)
    body = frame.inner_text("body")
    expect("卡片配色" in body and "不需要素材钥匙" in body, "选「知识讲解卡片」后显示说明和「卡片配色」")
    page.screenshot(path=str(shots / "cards-source-100.png"))
    frame.locator("textarea[aria-label*='视频主题']").first.fill("逻辑门是怎样工作的")
    frame.locator("textarea[aria-label*='视频主题']").first.press("Tab")
    frame.wait_for_timeout(1500)
    voice_select(frame, "af-ZA-Adri")
    frame.wait_for_timeout(1500)
    body = frame.inner_text("body")
    expect("不是中文" in body and "No audio was received" in body, "中文主题选了 af-ZA 声音时提醒语言不一致")
    warn = frame.locator("[data-testid='stAlert']").filter(has_text="不是中文").first
    if warn.count():
        warn.scroll_into_view_if_needed()
        page.screenshot(path=str(shots / "voice-language-warning.png"))
    voice_select(frame, "zh-CN-Xiaoxiao")
    expect("不是中文" not in frame.inner_text("body"), "换回 zh-CN 声音后提醒消失")
    frame.locator("button").filter(has_text="配置素材来源").first.click()
    frame.wait_for_selector("[role='dialog']", timeout=15000)
    dlg = frame.locator("[role='dialog']").first
    pex = dlg.locator("input[aria-label^='Pexels API Key']").first
    pex.fill("12345678-0123456789abcdef012345678")
    pex.press("Enter")
    frame.wait_for_timeout(2500)
    expect("这像是 Pixabay 的钥匙" in dlg.inner_text(), "Pixabay 格式的钥匙填进 Pexels 栏时提示")
    proxy = dlg.locator("input[aria-label*='素材下载代理']").first
    expect(proxy.count() == 1, "素材来源设置里有「素材下载代理」输入框")
    proxy.fill("http://127.0.0.1:7897")
    proxy.press("Enter")
    frame.wait_for_timeout(2500)
    expect("素材搜索和下载走代理" in dlg.inner_text(), "填了素材下载代理后显示生效说明")
    proxy.scroll_into_view_if_needed()
    page.screenshot(path=str(shots / "material-settings-proxy.png"))
    pex.fill("")
    pex.press("Enter")
    proxy.fill("")
    proxy.press("Enter")
    frame.wait_for_timeout(2500)
    expect("这像是" not in dlg.inner_text(), "清空后钥匙提示消失")
    page.keyboard.press("Escape")
    frame.wait_for_timeout(1500)


def run_generate_cards(page, base: str, data: Path):
    """知识讲解卡片 + 无配音：不需要网络和任何钥匙，走通一条出片。"""
    page.click("#tab-studio")
    frame = wait_studio(page)
    dismiss_tour(frame)
    frame.locator(".mpt-grouped-select select").first.select_option(value="knowledge_cards")
    frame.wait_for_timeout(2500)
    frame.locator("textarea[aria-label*='视频主题']").first.fill("逻辑门")
    frame.locator("textarea[aria-label*='视频文案']").first.fill(
        "逻辑门是数字电路的基本单元。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。非门则把输入取反。"
        "计算机先把信号转成二进制，再交给逻辑门运算，最后输出结果。")
    frame.wait_for_timeout(800)
    frame.locator("button").filter(has_text="无配音").first.click()
    frame.wait_for_timeout(2000)
    before = finals_of(data)
    t0 = time.time()
    frame.locator("button").filter(has_text="生成视频").last.click()
    done, finals, _ = wait_generation(page, frame, data, before)
    stills = list(finals[0].parent.glob("knowledge-cards/page-*.png")) if finals else []
    page.screenshot(path=str(Path(REPORT["shots"]) / "generate-cards-done.png"))
    expect(done and finals and stills, "主要操作：知识讲解卡片 + 无配音生成一条视频（离线、无钥匙）",
           f"用时 {time.time() - t0:.0f} 秒，成片 {finals[0] if finals else '无'}，讲解页 {len(stills)} 张")
    if finals:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_name,width,height",
                              "-of", "compact", str(finals[0])], capture_output=True, text=True).stdout
        ok("知识卡片成片信息", out.replace("\n", " "))
        for k, f in enumerate(sorted(stills)[:3]):
            shutil.copy(f, Path(REPORT["shots"]) / f"cards-page-{k + 1}.png")


FAKE_KEY = "sk-uicheck-NOTAREALKEY1234567890"
UI_SCRIPT = ("逻辑门是数字电路的基本单元，也是计算机里最小的“判断员”。与门只有两个输入都为1时才输出1。或门只要有一个输入为1就输出1。"
             "非门则把输入取反。学习逻辑门有三个要点：第一，记住真值表。第二，会画电路符号。第三，能把门组合成加法器。"
             "计算机处理数据，先把信号转成二进制，再交给逻辑门运算，最后输出结果。CPU里有数十亿个晶体管，而早期的计算机只有几千个电子管。"
             "常见的门电路包括与门、或门、非门和异或门。把这些门组合起来，就能做出整个CPU。")


def start_fake_llm() -> int:
    """假的 OpenAI 兼容接口：写文案返回 UI_SCRIPT，关键词返回 JSON 数组，带 token 用量。"""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            prompt = body["messages"][-1]["content"]
            text = '["logic gate", "circuit"]' if "search terms" in prompt else UI_SCRIPT * 3   # 长一点，留出取消的时间
            data = json.dumps({"id": "chatcmpl-ui", "object": "chat.completion", "created": 1, "model": body.get("model"),
                               "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 512, "completion_tokens": 260, "total_tokens": 772}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_port


def preset_config(data: Path, llm_port: int) -> None:
    """首启前写好配置：大模型指向假接口（只有 1.0.3 的检查用到）。"""
    from svf import userdata
    data.mkdir(parents=True, exist_ok=True)
    text = userdata.initial_config_text((ROOT / "upstream" / "config.example.toml").read_text(encoding="utf-8"))
    text = re.sub(r'(?m)^llm_provider = ".*"$', 'llm_provider = "openai"', text, count=1)
    text = re.sub(r'(?m)^openai_api_key = ".*"$', f'openai_api_key = "{FAKE_KEY}"', text, count=1)
    text = re.sub(r'(?m)^openai_base_url = ".*"$', f'openai_base_url = "http://127.0.0.1:{llm_port}/v1"', text, count=1)
    text = re.sub(r'(?m)^openai_model_name = ".*"$', 'openai_model_name = "fake-model"', text, count=1)
    (data / "config.toml").write_text(text, encoding="utf-8")


def task_dirs(data: Path) -> set:
    t = data / "storage" / "tasks"
    return {d.name for d in t.iterdir() if d.is_dir()} if t.exists() else set()


def run_103(page, base: str, data: Path, assets: Path):
    """1.0.3：目标时长、生成文案（假接口）、连点只起一个、按钮置灰、日志解读、调用记录、取消、删除、打开目录失败的兜底。"""
    page.click("#tab-studio")
    frame = wait_studio(page)
    dismiss_tour(frame)
    frame.locator(".mpt-grouped-select select").first.select_option(value="knowledge_cards")
    frame.wait_for_timeout(2500)
    box = frame.locator("[data-testid='stSelectbox']").filter(has_text="目标时长").first
    expect(box.count() == 1, "文案设置里有「目标时长（秒）」")
    box.click()
    frame.wait_for_timeout(800)
    frame.locator("[role='option']").filter(has_text="60 秒").first.click()
    frame.wait_for_timeout(2500)
    expect("约 270 字" in frame.inner_text("body"), "选 60 秒后显示换算的字数（约 270 字）")
    frame.locator("textarea[aria-label*='视频主题']").first.fill("逻辑门是怎样工作的")
    frame.locator("textarea[aria-label*='视频主题']").first.press("Tab")
    frame.wait_for_timeout(1200)
    frame.locator("button").filter(has_text="点击使用AI生成视频文案和关键词").first.click()
    for _ in range(40):
        frame.wait_for_timeout(1000)
        if "逻辑门是数字电路的基本单元" in frame.locator("textarea[aria-label*='视频文案']").first.input_value():
            break
    expect("逻辑门是数字电路的基本单元" in frame.locator("textarea[aria-label*='视频文案']").first.input_value(),
           "界面上「生成文案」经假的 OpenAI 兼容接口写进了文案框")
    frame.locator("button").filter(has_text="无配音").first.click()
    frame.wait_for_timeout(2000)
    before = task_dirs(data)
    btn = frame.locator(".st-key-generate_video_button button").first
    for _ in range(3):
        try:
            btn.click(timeout=1500)
        except Exception:  # noqa: BLE001  按钮已经变灰，点不了也算对
            pass
    frame.wait_for_timeout(8000)
    new = task_dirs(data) - before
    expect(len(new) == 1, "连点三次「生成视频」只起了一个任务", f"新任务 {sorted(new)}")
    btn = frame.locator(".st-key-generate_video_button button").first
    expect(btn.is_disabled() and "生成中" in btn.inner_text(), "生成中时按钮变灰、显示「生成中…」", btn.inner_text())
    tid = sorted(new)[0] if new else ""
    exp = frame.locator("[data-testid='stExpander']").filter(has=frame.locator("summary", has_text="日志解读")).first
    exp.scroll_into_view_if_needed()
    frame.wait_for_timeout(1500)
    text = exp.inner_text()
    expect("最终渲染" in text and "原始日志第" in text and "在做什么" in text, "日志解读按步骤显示，带原始日志行号")
    page.screenshot(path=str(assets / "screen-log-explain-100.png"))
    llm = frame.locator("[data-testid='stExpander']").filter(has=frame.locator("summary", has_text="大模型调用记录")).last
    llm.locator("summary").first.click()
    frame.wait_for_timeout(2000)
    t2 = llm.inner_text()
    expect("写文案" in t2 and "Length Requirement" in t2 and "772" in t2 and FAKE_KEY not in t2 and "在界面上" in t2,
           "大模型调用记录：界面上生成文案的那次被复制进任务，有完整提示词（含目标时长）、token，没有钥匙")
    llm.scroll_into_view_if_needed()
    page.screenshot(path=str(assets / "screen-llm-calls-100.png"))
    rec_file = data / "storage" / "tasks" / tid / "llm_calls.jsonl"
    expect(rec_file.exists() and FAKE_KEY not in rec_file.read_text(encoding="utf-8"), "任务目录有 llm_calls.jsonl，里面没有钥匙")
    expect(frame.locator("[data-testid='stCode'] .linenumber, [data-testid='stCode'] [class*='linenumber']").count() > 0
           or "1\n" in frame.locator("[data-testid='stCode']").last.inner_text()[:6], "原始日志带行号")
    # 任务管理：取消、删除、打开目录
    for b in frame.locator("button").filter(has_text="知道了").all():   # 上一个任务完成时自动打开目录失败的提示，先关掉
        try:
            b.click(timeout=2000)
        except Exception:  # noqa: BLE001
            pass
    frame.locator("button").filter(has_text="任务管理").first.click()
    cancel = frame.locator(f".st-key-cancel_task_all_{tid} button")
    try:
        cancel.first.wait_for(timeout=15000)
    except Exception:  # noqa: BLE001
        pass
    frame.wait_for_timeout(1500)
    note = frame.get_by_text("不能直接删除").count()
    expect(cancel.count() >= 1 and note > 0, "任务管理：生成中的任务有「取消」，并说明为什么暂时不能删",
           f"取消按钮 {cancel.count()} 个，说明 {note} 处")
    page.screenshot(path=str(assets / "screen-task-cancel-100.png"))
    cancel.first.click()
    frame.wait_for_timeout(5000)
    expect("已取消" in frame.inner_text("body"), "点「取消」后状态变成「已取消」")
    delete = frame.locator(f".st-key-delete_task_all_{tid} button")
    for _ in range(20):
        if delete.count() >= 1 and delete.first.is_enabled():
            break
        frame.wait_for_timeout(500)
    expect(delete.count() >= 1 and delete.first.is_enabled(), "取消后删除按钮可以点", f"删除按钮 {delete.count()} 个")
    frame.locator(f".st-key-open_task_all_{tid} button").first.click()
    for _ in range(20):
        frame.wait_for_timeout(500)
        if "没能打开文件夹" in frame.inner_text("body") or "已请系统打开文件夹" in frame.inner_text("body"):
            break
    body = frame.inner_text("body")
    ok_open = "已请系统打开文件夹" in body
    fail_ui = "没能打开文件夹" in body and str(data / "storage" / "tasks" / tid) in body
    expect(ok_open or fail_ui, "「打开目录」经启动器打开；失败时显示原因和完整路径", "打开了" if ok_open else "走了失败兜底")
    if fail_ui:
        cp = frame.locator("button.svf-copy-path").first
        expect(cp.count() == 1, "失败时有「复制路径」按钮")
        page.screenshot(path=str(assets / "screen-open-fail-100.png"))
        cp.click()
        frame.wait_for_timeout(600)
        expect("已复制" in cp.inner_text() or "复制失败" in cp.inner_text(), "点「复制路径」有反馈", cp.inner_text())
    delete.first.click()
    frame.wait_for_timeout(4000)
    expect(tid not in task_dirs(data), "取消后的任务能删除")
    page.keyboard.press("Escape")
    frame.wait_for_timeout(1000)


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
    ap.add_argument("--assets", default="", help="1.0.3 检查的截图另存到这里（如 docs/assets）")
    ap.add_argument("--only-103", action="store_true", help="只跑 1.0.3 新增的检查（调试用）")
    a = ap.parse_args()
    shots = Path(a.shots)
    shots.mkdir(parents=True, exist_ok=True)
    REPORT["shots"] = str(shots)
    data = Path(tempfile.mkdtemp(prefix="svf-ui-"))
    preset_config(data, start_fake_llm())
    os.environ.setdefault("DISPLAY", ":99")   # 让上游认为有桌面，「打开目录」走 1.0.3 的经启动器打开（容器里没有 xdg-open → 失败兜底）
    proc, rec = start_app(a.python, data, a.port)
    base = f"http://127.0.0.1:{rec['shell_port']}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(executable_path=a.chromium)
            ctx = browser.new_context(viewport={"width": 1440, "height": 900}, accept_downloads=True)
            page = ctx.new_page()
            page.goto(base + "/")
            expect(page.inner_text("#app-version") == "v" + __version__, "顶栏显示版本号", page.inner_text("#app-version"))
            t0 = time.time()
            frame = wait_studio(page)
            ok("生成界面经反向代理载入", f"{time.time() - t0:.0f} 秒")
            dismiss_tour(frame)
            page.screenshot(path=str(shots / "studio-100.png"))
            if not a.only_103:
                run_zoom(page, shots)
                run_guide(page, shots, base)
                run_narrow(page)
                rec = json.loads((data / "run.json").read_text(encoding="utf-8"))
                run_status(page, rec, shots, base)
                run_cards_ui(page, shots, data)
            if a.only_103:
                run_103(page, base, data, Path(a.assets) if a.assets else shots)
            elif a.generate:
                media = Path(a.media) if a.media else Path(tempfile.mkdtemp(prefix="svf-media-"))
                for i, src in enumerate(("testsrc2", "mandelbrot"), 1):
                    if not (media / f"clip{i}.mp4").exists():
                        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"{src}=size=1080x1920:rate=25",
                                        "-t", "8", "-pix_fmt", "yuv420p", "-c:v", "libx264", str(media / f"clip{i}.mp4")], check=True)
                run_generate(page, base, data, media)
                run_generate_cards(page, base, data)
                run_103(page, base, data, Path(a.assets) if a.assets else shots)
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
