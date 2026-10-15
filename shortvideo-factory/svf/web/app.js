"use strict";
// 外壳主逻辑：顶栏（栏目、缩放、退出）、「视频生成」里嵌入上游 Streamlit 界面、「运行状态」。
(function () {
  const BOOT = window.SVF_BOOT || { version: "开发", token: "", settings: { zoom: 100 }, upstream: {} };
  const Z = window.SVFZoom;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // 给嵌入的 Streamlit 页面（inject.js）用的接口：同源，可直接读写
  const SVF = window.SVF = {
    zoom: Z.clamp(BOOT.settings.zoom || 100),
    inWindow: false,
    zoomStep: dir => setZoom(Z.step(SVF.zoom, dir)),
    openExternal: url => post("/api/open", { url }).catch(e => toast("打开失败：" + e.message)),
  };
  // pywebview 会注入 window.pywebview；有它说明在独立窗口里（外部链接交给系统浏览器）
  const detectWindow = () => { SVF.inWindow = !!window.pywebview; };
  window.addEventListener("pywebviewready", detectWindow);
  detectWindow();

  async function api(url) {
    const res = await fetch(url, { cache: "no-store" });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  async function post(url, body) {
    const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json", "X-SVF-Token": BOOT.token },
      body: JSON.stringify(body || {}) });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || res.statusText);
    return data;
  }
  let toastTimer;
  function toast(msg) {
    const t = $("toast");
    t.textContent = msg; t.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.hidden = true; }, 3200);
  }

  // ---------- 缩放：70%–120%，每 5% 一档；外壳用 rem（根字号），Streamlit 页面用 CSS zoom ----------
  function applyZoom() {
    document.documentElement.style.setProperty("--zoom", SVF.zoom / 100);
    const b = Z.buttons(SVF.zoom);
    $("zoom-select").value = String(SVF.zoom);
    $("zoom-out").disabled = b.minusDisabled;
    $("zoom-in").disabled = b.plusDisabled;
    $("zoom-reset").disabled = b.resetDisabled;
    const f = $("studio-frame");
    try { f.contentWindow && f.contentWindow.postMessage({ type: "svf-zoom", value: SVF.zoom }, location.origin); } catch { /* 还没载入 */ }
  }
  function setZoom(pct) {
    SVF.zoom = Z.clamp(pct);
    applyZoom();
    post("/api/settings", { zoom: SVF.zoom }).catch(() => toast("缩放档位没保存上（下次打开会回到之前的档位）"));
  }
  $("zoom-select").innerHTML = Z.STEPS.map(z => `<option value="${z}">${z}%</option>`).join("");
  $("zoom-select").onchange = e => setZoom(+e.target.value);
  $("zoom-out").onclick = () => SVF.zoomStep(-1);
  $("zoom-in").onclick = () => SVF.zoomStep(1);
  $("zoom-reset").onclick = () => SVF.zoomStep(0);
  window.addEventListener("keydown", e => {
    if (!(e.metaKey || e.ctrlKey)) return;
    const dir = e.key === "=" || e.key === "+" ? 1 : e.key === "-" ? -1 : e.key === "0" ? 0 : null;
    if (dir === null) return;
    e.preventDefault(); SVF.zoomStep(dir);
  });

  // ---------- 栏目 ----------
  const TABS = ["studio", "review", "calls", "guide", "status"];
  let tab = TABS.includes(BOOT.settings.tab) ? BOOT.settings.tab : "studio";
  function switchTab(name, opts) {
    tab = TABS.includes(name) ? name : "studio";
    document.querySelectorAll(".tabs button").forEach(b => { b.classList.toggle("on", b.dataset.tab === tab); b.setAttribute("aria-selected", b.dataset.tab === tab); });
    document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.dataset.view === tab));
    post("/api/settings", { tab }).catch(() => {});
    if (tab === "guide") guide.renderGuide(opts && opts.page);
    if (tab === "review") review.render(opts || {});
    if (tab === "calls") calls.render(opts || {});
    if (tab === "status") renderStatus();
  }
  document.querySelectorAll(".tabs button").forEach(b => { b.onclick = () => switchTab(b.dataset.tab); });

  // ---------- 视频生成：等生成界面后台就绪后载入 /app/ ----------
  let frameLoaded = false, studioTimer = null;
  async function pollStudio() {
    clearTimeout(studioTimer);
    let st;
    try { st = await api("/api/status"); }
    catch (e) { setOverlay("failed", "连不上本机服务", e.message); return; }
    const w = st.webui;
    if (w.state === "ready") {
      if (!frameLoaded) {
        const f = $("studio-frame");
        f.src = BOOT.base_path || "/app/";
        f.hidden = false;
        frameLoaded = true;
      }
      $("studio-overlay").hidden = true;
      studioTimer = setTimeout(pollStudio, 5000);
      return;
    }
    frameLoaded = false;
    $("studio-frame").hidden = true;
    if (w.state === "failed") setOverlay("failed", "生成界面没能启动", w.error);
    else if (w.state === "stopped" || w.state === "stopping") setOverlay("failed", "生成界面后台已停止", "点下面的按钮重新启动。");
    else setOverlay("starting");
    studioTimer = setTimeout(pollStudio, w.state === "starting" ? 1000 : 3000);
  }
  function setOverlay(kind, title, detail) {
    $("studio-overlay").hidden = false;
    $("studio-spinner").classList.toggle("stop", kind === "failed");
    $("studio-title").textContent = kind === "failed" ? title : "正在启动生成界面…";
    $("studio-msg").textContent = kind === "failed" ? "可以重新启动；仍不行就看日志或使用指南里的「出了问题怎么办」。"
      : "第一次打开要加载视频处理组件，一般 10–40 秒。";
    $("studio-log").hidden = kind !== "failed" || !detail;
    $("studio-log").textContent = detail || "";
    $("studio-actions").hidden = kind !== "failed";
  }
  $("studio-frame").addEventListener("load", applyZoom);
  async function restartWebui() {
    try { await post("/api/webui/restart"); toast("正在重新启动生成界面…"); frameLoaded = false; setOverlay("starting"); setTimeout(pollStudio, 800); }
    catch (e) { toast("重启失败：" + e.message); }
  }
  $("studio-restart").onclick = restartWebui;
  $("webui-restart").onclick = () => { switchTab("studio"); restartWebui(); };

  // ---------- 打开文件夹、退出 ----------
  document.addEventListener("click", e => {
    const o = e.target.closest("[data-open]");
    if (o) { post("/api/open", { what: o.dataset.open }).then(r => toast("已打开：" + r.path)).catch(err => toast("打开失败：" + err.message)); return; }
    const g = e.target.closest("[data-goto]");
    if (g) switchTab(g.dataset.goto, { page: g.dataset.page });
  });
  $("quit-btn").onclick = async () => {
    if (!confirm("退出 AI 短视频工厂？\n正在生成的视频会被中断；已生成的视频和设置都会保留。")) return;
    document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font-size:1.1rem">正在停止后台并退出…</div>';
    try { await post("/api/quit"); } catch { /* 服务已经在退出 */ }
    setTimeout(() => { document.body.innerHTML = '<div style="display:grid;place-items:center;height:100vh;font-size:1.1rem">已退出，可以关闭这个页面。</div>'; }, 1500);
  };

  // ---------- 运行状态 ----------
  const fmt = v => v == null ? '<span class="muted">暂不可用</span>' : Number(v).toFixed(1);
  const STATE = { ready: ["运行中", "ok"], starting: ["启动中", ""], failed: ["出错", "bad"], stopped: ["已停止", "bad"], stopping: ["正在停止", ""] };
  let statusTimer = null;
  async function renderStatus() {
    clearTimeout(statusTimer);
    try {
      const [p, st] = await Promise.all([api("/api/processes"), api("/api/status")]);
      const rows = p.processes.map(r => `<tr><td>${esc(r.role)}</td><td>${esc(r.name)}</td><td class="num">${r.pid}</td>
        <td class="num">${r.parent ?? "—"}</td><td class="num">${fmt(r.mem_mib)}</td></tr>`).join("");
      $("proc-body").innerHTML = `<p class="muted">采样时间 <b id="proc-time">${esc(p.sampled_at)}</b> · 口径：${esc(p.metric)} · 单位 MiB（1 MiB = 1024² 字节）· 共 <b id="proc-count">${p.count}</b> 个进程</p>
        <div class="table-wrap"><table class="data" id="proc-table"><thead><tr><th>职责</th><th>进程名</th><th class="num">PID</th><th class="num">父 PID</th><th class="num">内存（MiB）</th></tr></thead>
        <tbody>${rows}</tbody>${p.total_mib != null ? `<tfoot><tr><td colspan="4">本应用合计（${p.count} 个进程）</td><td class="num">${p.total_mib.toFixed(1)}</td></tr></tfoot>` : ""}</table></div>
        <ul class="proc-notes">
          <li><b>${esc(p.backend_note)}</b></li>
          ${p.total_note ? `<li>${esc(p.total_note)}</li>` : ""}
          ${p.note ? `<li>${esc(p.note)}</li>` : ""}
          <li>应用外部的独立依赖（不计入合计）：${p.external.map(x => esc(x.name) + "——" + esc(x.note)).join("；")}</li>
          <li>主进程里的线程（共用主进程 PID，不是独立进程）：${p.threads.map(esc).join("、")}</li>
          <li>Windows 任务管理器「详细信息」默认显示「专用工作集」，比这里的工作集小；macOS 活动监视器「内存」列不是 RSS，口径不同，数值会有差别。</li>
        </ul>`;
      const w = st.webui, s = STATE[w.state] || [w.state, ""];
      $("webui-status").innerHTML = `<span class="k">状态</span><span class="v"><span class="badge ${s[1]}">${s[0]}</span></span>
        <span class="k">PID</span><span class="v">${w.pid ?? "—"}</span>
        <span class="k">端口</span><span class="v">127.0.0.1:${w.port ?? "—"}（窗口经本机服务 127.0.0.1:${st.port} 转发访问）</span>
        <span class="k">重启次数</span><span class="v">${w.restarts}</span>
        <span class="k">上游版本</span><span class="v">${esc(st.upstream.name)} ${esc(st.upstream.version)}（commit ${esc(st.upstream.commit)}）</span>
        <span class="k">显示方式</span><span class="v">${esc(st.window)}</span>
        ${w.error ? `<span class="k">错误</span><span class="v"><pre class="log">${esc(w.error)}</pre></span>` : ""}`;
      const P = st.paths;
      $("paths").innerHTML = [["数据文件夹", P.data], ["设置与 API Key", P.config], ["生成的视频", P.output], ["字体", P.fonts],
        ["背景音乐", P.songs], ["日志", P.logs]].map(([k, v]) => `<span class="k">${k}</span><span class="v">${esc(v)}</span>`).join("");
    } catch (e) {
      $("proc-body").innerHTML = `<p class="banner error">暂不可用：${esc(e.message)}</p>`;
    }
    if (tab === "status" && $("proc-auto").checked) statusTimer = setTimeout(renderStatus, 3000);
  }
  $("proc-refresh").onclick = renderStatus;
  $("proc-auto").onchange = () => { if ($("proc-auto").checked) renderStatus(); else clearTimeout(statusTimer); };

  // ---------- 启动 ----------
  $("app-version").textContent = "v" + BOOT.version;
  $("upstream-version").textContent = `MoneyPrinterTurbo ${BOOT.upstream.version || ""}`;
  document.title = `AI 短视频工厂 ${BOOT.version}`;
  const guide = window.SVFGuide({ api, toast, switchTab, saveSettings: v => post("/api/settings", v).catch(() => {}),
    initialPage: BOOT.settings.guide_page });
  const review = window.SVFReview({ api, toast });
  const calls = window.SVFCalls({ api, toast });
  applyZoom();
  switchTab(tab);
  pollStudio();
  // 生成界面（iframe 里的 Streamlit 页面）上的「审片 / 分镜」「模型调用」链接经 inject.js 调这里：SVFApp.openTask("review", 任务 ID)
  const openTask = (name, task, extra) => switchTab(name, Object.assign({ task }, extra || {}));
  window.SVFApp = { switchTab, setZoom, renderStatus, openTask };
})();
