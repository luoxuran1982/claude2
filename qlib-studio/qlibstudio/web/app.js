"use strict";
// Qlib 量化研究台主界面：数据、实验、报告、与 QuantLab 对比、运行状态、退出。
// 「使用指南」和进程表在 guide.js；缩放在 zoom.js；Markdown 渲染在 md.js；图表用随应用打包的 ECharts。
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TOKEN = new URLSearchParams(location.search).get("t") || "";
  const TAB_KEY = "qlibstudio-tab", EXP_KEY = "qlibstudio-exp", RUN_KEY = "qlibstudio-run";
  const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 忽略 */ } } };

  async function api(path, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-QlibStudio-Token": TOKEN }, body: JSON.stringify(body) };
    const res = await fetch(path, opt);
    const ct = res.headers.get("Content-Type") || "";
    const data = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok) {
      const msg = (data && data.error) || res.statusText;
      if (res.status === 403) throw new Error("请求被拒绝：" + msg + "（页面令牌无效时，请关闭后从应用重新打开）");
      throw new Error(msg);
    }
    return data;
  }
  function toast(msg, ms = 3600) {
    const t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function showError(msg) { const e = $("error"); e.textContent = msg || ""; e.hidden = !msg; }
  const pct = (x, d = 2) => x == null || !isFinite(x) ? "—" : (x * 100).toFixed(d) + "%";
  const num = (x, d = 3) => x == null || !isFinite(x) ? "—" : Number(x).toFixed(d);
  const cls = x => x == null ? "" : x > 0 ? "up" : x < 0 ? "down" : "";
  const money = x => x == null ? "—" : Number(x).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
  let catalog = null, status = null;

  // ---------------------------------------------------------------- 栏目
  const views = ["data", "exp", "report", "compare", "status", "guide"];
  let current = store.get(TAB_KEY);
  if (!views.includes(current)) current = "data";
  function switchTab(name) {
    if (!views.includes(name)) name = "data";
    current = name; store.set(TAB_KEY, name);
    document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.dataset.view === name));
    if (name === "data") loadDatasets();
    if (name === "exp") { loadDatasets().then(fillExpDatasets); loadRuns(); }
    if (name === "report") loadReportList();
    if (name === "compare") loadCompareItems();
    if (name === "status") { guide.renderProcs(); loadJobs(); }
    if (name === "guide") guide.renderGuide();
    setTimeout(resizeCharts, 30);
  }
  document.querySelectorAll(".tabs button").forEach(b => { b.onclick = () => switchTab(b.dataset.tab); });
  function bindSubtabs(barId, onShow) {
    const bar = $(barId);
    bar.addEventListener("click", e => {
      const b = e.target.closest("button[data-sub]"); if (!b) return;
      bar.querySelectorAll("button").forEach(x => x.classList.toggle("on", x === b));
      bar.parentElement.querySelectorAll(":scope > .subpanel").forEach(p => p.classList.toggle("on", p.id === b.dataset.sub));
      if (onShow) onShow(b.dataset.sub);
      setTimeout(resizeCharts, 30);
    });
  }

  // ---------------------------------------------------------------- 状态条与任务轮询
  let pollTimer = null, lastJobId = null, lastJobRunning = false;
  async function refreshStatus() {
    clearTimeout(pollTimer);
    try {
      status = await api("/api/status");
      showError("");
      renderStatus();
    } catch (e) {
      showError("连接本机服务失败：" + e.message + "。如果刚点了「退出」，可以关闭这个窗口。");
    }
    const running = !!(status && status.job && status.job.running);
    pollTimer = setTimeout(refreshStatus, running ? 1000 : 4000);
  }
  function renderStatus() {
    const s = status, j = s.job;
    $("app-version").textContent = "v" + s.version;
    $("up-version").textContent = s.upstream.version;
    $("data-dir").textContent = s.data_dir;
    $("futu-fake").hidden = !s.fake_futu;
    $("comm-url").textContent = s.community_url;
    if (!$("futu-port").dataset.touched) $("futu-port").value = s.settings.futu_port;
    const running = !!(j && j.running);
    $("job-dot").className = "dot " + (running ? "run" : j ? (j.status === "完成" ? "ok" : "bad") : "");
    $("job-text").textContent = running ? `正在${j.title}：${j.message || "准备中"}（${Math.round(j.progress * 100)}%）`
      : j ? `空闲 · 上一个任务「${j.title}」${j.status}${j.result && j.result.error ? "：" + j.result.error : ""}` : "空闲";
    $("job-bar").hidden = !running;
    $("job-cancel").hidden = !running;
    if (running) $("job-bar").firstElementChild.style.width = Math.round(j.progress * 100) + "%";
    renderExpJob(j);
    if (j && lastJobId === j.id && lastJobRunning && !running) onJobFinished(j);
    lastJobId = j ? j.id : null; lastJobRunning = running;
  }
  function onJobFinished(j) {
    const r = j.result || {};
    if (r.ok) toast("✓ " + (r.summary || j.title + " 完成"), 6000);
    else if (r.cancelled) toast("任务已取消");
    else toast("✗ " + j.title + " 失败：" + (r.error || "见日志"), 9000);
    if (j.kind === "futu-check" && r.ok) renderQuota(r.quota, r.fake);
    if (j.kind === "futu-download") { loadFutuCache(); if (r.download && r.download.quota_after) renderQuotaShort(r.download); }
    if (r.dataset) { loadDatasets(); }
    if (j.kind === "experiment" && r.ok) { store.set(RUN_KEY, r.run_id); loadRuns(); }
    if (current === "status") loadJobs();
  }
  async function startJob(kind, params) {
    try {
      const v = await api("/api/job/start", { kind, params });
      toast(`已开始：${v.title}（工作进程 PID ${v.pid}）`);
      lastJobId = v.id; lastJobRunning = true;
      refreshStatus();
      return v;
    } catch (e) { toast(e.message, 6000); return null; }
  }
  async function cancelJob() {
    if (!confirm("取消正在运行的任务？已完成的部分不会保存。")) return;
    try { await api("/api/job/cancel", {}); refreshStatus(); } catch (e) { toast(e.message); }
  }
  $("job-cancel").onclick = cancelJob;
  $("x-cancel").onclick = cancelJob;
  async function showLog(id, title) {
    try {
      const t = await api("/api/job-log?id=" + encodeURIComponent(id) + "&tail=600");
      $("log-title").textContent = title || "任务日志"; $("log-body").textContent = t || "（空）"; $("log-dialog").showModal();
    } catch (e) { toast(e.message); }
  }

  // ---------------------------------------------------------------- 数据
  let dsCache = [];
  async function loadDatasets() {
    try { dsCache = (await api("/api/datasets")).datasets; } catch (e) { $("ds-list").innerHTML = `<p class="banner error">${esc(e.message)}</p>`; return dsCache; }
    const SRC = (catalog && catalog.sources) || {};
    $("ds-list").innerHTML = dsCache.length ? dsCache.map(d => {
      const lv = d.quality_level || (d.quality_summary || {}).level;
      const lvCls = lv === "良好" ? "ok" : lv === "需注意" ? "warn" : lv ? "bad" : "";
      return `<div class="ds" data-id="${esc(d.id)}"><div><div class="t">${esc(d.title)}<span class="tag">${esc(SRC[d.source] || d.source)}</span>
          ${lv ? `<span class="tag ${lvCls}">质量：${esc(lv)}</span>` : ""}${d.fake_opend ? '<span class="tag warn">模拟 OpenD</span>' : ""}</div>
        <div class="meta">${esc(d.start)} ~ ${esc(d.end)}（${d.calendar_days} 个交易日）· ${d.n_stocks} 只股票${d.n_symbols > d.n_stocks ? `、${d.n_symbols - d.n_stocks} 个指数/基金` : ""}
          · 股票池：${(d.pools || []).map(p => esc(p.name) + " " + p.count).join("、")} · 基准候选：${esc((d.benchmarks || []).join("、") || "无指数（用股票池等权）")}
          · ${d.size_mib} MiB · 编号 ${esc(d.id)}</div></div>
        <div class="acts"><button class="btn small" data-act="quality">质量检查</button><button class="btn small" data-act="exp">用它做实验</button>
          <button class="btn small" data-act="rename">改名</button><button class="btn small danger" data-act="delete">删除</button></div></div>`;
    }).join("") : '<p class="muted">还没有数据集。先在下面「获取数据」里生成演示数据，或导入自己的数据。</p>';
    return dsCache;
  }
  $("ds-refresh").onclick = loadDatasets;
  $("ds-list").addEventListener("click", async e => {
    const b = e.target.closest("button[data-act]"); if (!b) return;
    const id = b.closest(".ds").dataset.id, d = dsCache.find(x => x.id === id);
    if (b.dataset.act === "quality") return showQuality(id);
    if (b.dataset.act === "exp") { store.set(EXP_KEY + "-ds", id); return switchTab("exp"); }
    if (b.dataset.act === "rename") {
      const t = prompt("新名称", d.title); if (!t) return;
      try { await api("/api/dataset/rename", { id, title: t }); loadDatasets(); } catch (err) { toast(err.message); }
    }
    if (b.dataset.act === "delete") {
      if (!confirm(`删除数据集「${d.title}」？会删除它的 Qlib 数据目录（${d.size_mib} MiB），不影响原始文件和已有实验结果。`)) return;
      try { await api("/api/dataset/delete", { id }); loadDatasets(); $("quality-card").hidden = true; } catch (err) { toast(err.message); }
    }
  });
  let qShown = null;
  async function showQuality(id) {
    qShown = id;
    try {
      const r = await api("/api/dataset?id=" + encodeURIComponent(id));
      const q = r.quality || {}, s = q.summary || {};
      $("quality-card").hidden = false;
      $("q-title").textContent = r.meta.title;
      $("q-sub").textContent = `检查 ${s.checked} 只${s.sampled ? "（抽查）" : ""} · ${s.start} ~ ${s.end} · 结论：${s.level}`;
      const tile = (k, v, sub) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
      const imp = (q.import || []);
      const adj = {}; imp.forEach(x => { const k = (x.adjust || "").replace(/估算除权 \d+ 次/, "估算除权"); adj[k] = (adj[k] || 0) + 1; });
      $("q-body").innerHTML = `<div class="tiles">
          ${tile("结论", esc(s.level))}${tile("缺失/停牌（交易日·只）", s.missing ?? "—", "上市区间内没有数据的交易日合计")}
          ${tile("零成交天数", s.zero_volume ?? "—", "有价格但成交量为 0")}${tile("长停牌股票", s.long_suspend ?? "—", "连续缺失超过 20 个交易日")}
          ${tile("疑似未复权", s.suspect_exright ?? "—", "因子不变但有超涨跌停的跳空")}${tile("数据中断", s.stale ?? "—", "最后一天早于数据集末尾 20 天以上")}
          ${tile("有复权因子", s.with_factor ?? "—", "因子在变化（已处理除权）")}${tile("错误", s.bad ?? 0)}</div>
        ${(s.notes || []).length ? `<ul class="help">${s.notes.map(n => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
        ${Object.keys(adj).length ? `<p class="help"><b>复权处理：</b>${Object.entries(adj).map(([k, v]) => `${esc(k)} ${v} 只`).join("；")}</p>` : ""}
        ${(r.meta.read_problems || []).length ? `<details><summary>读取问题（${r.meta.read_problems.length}）</summary><ul class="help">${r.meta.read_problems.map(p => `<li>${esc(p)}</li>`).join("")}</ul></details>` : ""}
        <h3>问题清单（${(q.issues || []).length}）</h3>
        <div class="table-wrap scroll-y"><table class="data small"><thead><tr><th>级别</th><th>代码</th><th>说明</th></tr></thead><tbody>
        ${(q.issues || []).slice(0, 300).map(i => `<tr><td>${esc(i.level)}</td><td>${esc(i.symbol)}</td><td>${esc(i.text)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">没有发现问题</td></tr>'}</tbody></table></div>
        <h3>逐只明细（问题多的在前，最多 2000 只）</h3>
        <div class="table-wrap scroll-y"><table class="data small"><thead><tr><th>代码</th><th>类型</th><th>开始</th><th>结束</th><th class="num">交易日</th><th class="num">缺失</th>
          <th class="num">零成交</th><th class="num">长停牌段</th><th class="num">疑似除权跳空</th><th>复权因子</th><th>中断</th></tr></thead><tbody>
        ${(q.rows || []).slice(0, 2000).map(x => `<tr><td>${esc(x.symbol)}</td><td>${esc({ stock: "股票", index: "指数", fund: "基金", bond: "债券" }[x.kind] || x.kind)}</td>
          <td>${esc(x.start)}</td><td>${esc(x.end)}</td><td class="num">${x.days}</td><td class="num">${x.missing}</td><td class="num">${x.zero_volume}</td>
          <td class="num">${x.long_suspend}</td><td class="num">${x.suspect_exright}</td><td>${esc(x.factor)}</td><td>${x.stale ? "是" : ""}</td></tr>`).join("")}</tbody></table></div>`;
      $("quality-card").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (e) { toast(e.message); }
  }
  $("q-close").onclick = () => { $("quality-card").hidden = true; };
  $("q-recheck").onclick = () => qShown && startJob("quality", { dataset: qShown });
  bindSubtabs("src-tabs", sub => { if (sub === "src-futu") loadFutuCache(); });

  $("demo-go").onclick = () => startJob("demo", { n_stocks: +$("demo-n").value, years: +$("demo-years").value, title: $("demo-title").value.trim() });
  async function pick(kind, target, append) {
    try {
      const r = await api("/api/pick", { kind });
      if (r.mode === "browser") { $("pick-note").textContent = "浏览器模式下不能弹出选择框，请直接把路径粘贴到上面的输入框"; return; }
      if (r.paths && r.paths.length) {
        const el = $(target);
        el.value = append && el.value.trim() ? el.value.trim() + "\n" + r.paths.join("\n") : r.paths.join("\n");
      }
    } catch (e) { toast(e.message); }
  }
  $("pick-folder").onclick = () => pick("folder", "files-paths", true);
  $("pick-files").onclick = () => pick("files", "files-paths", true);
  $("comm-pick").onclick = () => pick("file", "comm-path", false);
  $("files-go").onclick = () => {
    const paths = $("files-paths").value.split("\n").map(s => s.trim()).filter(Boolean);
    if (!paths.length) return toast("请先填写或选择文件/文件夹路径");
    startJob("import", { paths, adjust: $("files-adjust").value, title: $("files-title").value.trim() });
  };
  $("futu-port").oninput = () => { $("futu-port").dataset.touched = "1"; };
  async function saveFutuPort() {
    try { await api("/api/settings", { changes: { futu_host: "127.0.0.1", futu_port: +$("futu-port").value } }); } catch (e) { toast(e.message); throw e; }
  }
  $("futu-check").onclick = async () => { await saveFutuPort(); startJob("futu-check", {}); };
  $("futu-end").value = new Date().toISOString().slice(0, 10);
  $("futu-go").onclick = async () => {
    const codes = $("futu-codes").value.trim();
    if (!codes) return toast("请先填写股票代码");
    await saveFutuPort();
    startJob("futu-download", { codes, start: $("futu-start").value, end: $("futu-end").value, title: $("futu-title").value.trim() });
  };
  function renderQuota(q, fake) {
    const box = $("futu-quota"); box.hidden = false;
    box.innerHTML = `<b>${fake ? "模拟 OpenD" : "OpenD"} 已连上。</b>历史 K 线额度：已用 <b>${q.used}</b> / 共 <b>${q.total}</b>，剩余 <b>${q.remain}</b>（按最近 30 天请求过的不同股票数计；30 天内重复请求同一只不再占用）。
      ${q.detail && q.detail.length ? `<details><summary>已占用额度的股票（${q.detail.length}）</summary>${q.detail.map(d => esc(d.code) + (d.time ? `（${esc(d.time)}）` : "")).join("、")}</details>` : ""}`;
  }
  function renderQuotaShort(r) {
    const box = $("futu-quota"); box.hidden = false;
    const a = r.quota_after;
    box.innerHTML = `下载完成：新下载 ${r.downloaded.length} 只、缓存命中 ${r.skipped.length} 只（没有请求）、失败 ${r.failed.length} 只。额度：已用 <b>${a.used}</b> / 共 <b>${a.total}</b>，剩余 <b>${a.remain}</b>。
      ${r.failed.length ? `<ul>${r.failed.map(f => `<li>${esc(f.code)}：${esc(f.reason)}</li>`).join("")}</ul>` : ""}`;
  }
  async function loadFutuCache() {
    try {
      const c = (await api("/api/futu/cache")).cache;
      $("futu-cache-n").textContent = c.length;
      $("futu-cache").innerHTML = c.length ? `<table class="data small"><thead><tr><th>代码</th><th>名称</th><th>已覆盖日期</th><th class="num">行数</th><th>更新时间</th></tr></thead><tbody>
        ${c.map(x => `<tr><td>${esc(x.code)}</td><td>${esc(x.name)}</td><td>${esc((x.covered || []).join(" ~ "))}</td><td class="num">${x.rows}</td><td>${esc(x.updated)}</td></tr>`).join("")}</tbody></table>`
        : '<p class="muted">还没有缓存。</p>';
    } catch (e) { $("futu-cache").textContent = e.message; }
  }
  $("comm-go").onclick = () => {
    if (!confirm("开始下载社区 A 股数据集（约 570 MB，解压后约 2–3 GB）？下载中断后再点会续传。")) return;
    startJob("community-download", { delete_archive: $("comm-del").checked });
  };
  $("comm-import").onclick = () => {
    const p = $("comm-path").value.trim(); if (!p) return toast("请先填写 .tar.gz 文件路径");
    startJob("community-import", { path: p });
  };

  // ---------------------------------------------------------------- 实验
  function fillSelect(el, entries, value) {
    el.innerHTML = entries.map(([v, label]) => `<option value="${esc(v)}">${esc(label)}</option>`).join("");
    if (value != null && entries.some(([v]) => v === value)) el.value = value;
  }
  function expPrefs() { try { return JSON.parse(store.get(EXP_KEY) || "{}"); } catch { return {}; } }
  function initExpForm() {
    const c = catalog, d = c.defaults, p = expPrefs();
    fillSelect($("x-factors"), Object.entries(c.factors), p.factors || d.factors);
    fillSelect($("x-model"), [...Object.entries(c.models)], p.model || d.model);
    $("x-model").insertAdjacentHTML("beforeend", Object.entries(c.optional_models).map(([k, v]) => `<option value="${k}" disabled>深度模型（可选，需另装 PyTorch）</option>`).join(""));
    $("x-model-note").textContent = "深度模型：" + Object.values(c.optional_models).join("");
    fillSelect($("x-deal"), Object.entries(c.deal_prices), p.deal_price || d.deal_price);
    fillSelect($("x-limit"), Object.entries(c.limit_modes), p.limit || d.limit);
    for (const [id, key] of [["x-topk", "topk"], ["x-ndrop", "n_drop"]]) if (p.strategy && p.strategy[key] != null) $(id).value = p.strategy[key];
    if (p.label_horizon) $("x-horizon").value = p.label_horizon;
    if (p.costs) { $("x-open").value = p.costs.open_cost; $("x-close").value = p.costs.close_cost; $("x-min").value = p.costs.min_cost; }
    if (p.account) $("x-account").value = p.account;
    if (p.rolling) { $("x-rolling").checked = !!p.rolling.enabled; $("x-step").value = p.rolling.step_days || 63; $("x-expand").value = p.rolling.expanding === false ? "0" : "1"; }
  }
  let calendarInfo = null;
  async function fillExpDatasets() {
    const sel = $("x-dataset"), want = store.get(EXP_KEY + "-ds") || sel.value;
    fillSelect(sel, dsCache.map(d => [d.id, `${d.title}（${d.start} ~ ${d.end}，${d.n_stocks} 只）`]), want);
    $("x-go").disabled = !dsCache.length;
    $("x-go-note").textContent = dsCache.length ? "" : "还没有数据集，先到「数据」页生成或导入";
    if (dsCache.length) onDatasetChange();
  }
  async function onDatasetChange() {
    const d = dsCache.find(x => x.id === $("x-dataset").value); if (!d) return;
    store.set(EXP_KEY + "-ds", d.id);
    const p = expPrefs();
    fillSelect($("x-pool"), (d.pools || []).filter(x => x.id !== "all").map(x => [x.id, `${x.name}（${x.count} 只）`]).concat([["all", "全部代码（含指数）"]]),
      p.pool && (d.pools || []).some(x => x.id === p.pool) ? p.pool : (d.pools || []).some(x => x.id === "csi300") ? "csi300" : "stocks");
    fillSelect($("x-bench"), [["auto", "自动（有指数用指数，否则股票池等权）"], ["equal", "股票池等权"], ...(d.benchmarks || []).map(b => [b, "指数 " + b])], p.benchmark || "auto");
    calendarInfo = { start: d.start, end: d.end, days: d.calendar_days };
    $("x-range-note").textContent = `数据区间 ${d.start} ~ ${d.end}，共 ${d.calendar_days} 个交易日。Alpha158/Alpha360 要用前 60 个交易日算因子，训练开始日要晚于数据开始。`;
    if (!$("x-train0").value || $("x-train0").dataset.ds !== d.id) autoSegments();
  }
  $("x-dataset").onchange = onDatasetChange;
  function autoSegments() {
    const d = dsCache.find(x => x.id === $("x-dataset").value); if (!d) return;
    // 和后端 experiment.auto_segments 同一规则：跳过前 60 天，其余 6:2:2（按自然日近似，后端会按交易日校验）
    const t0 = new Date(d.start), t1 = new Date(d.end);
    const span = t1 - t0, day = 864e5;
    const s = new Date(+t0 + Math.min(span * 0.12, 90 * day));
    const rest = t1 - s;
    const a = new Date(+s + rest * 0.6), b = new Date(+s + rest * 0.8);
    const f = x => x.toISOString().slice(0, 10);
    $("x-train0").value = f(s); $("x-train1").value = f(new Date(+a - day));
    $("x-valid0").value = f(a); $("x-valid1").value = f(new Date(+b - day));
    $("x-test0").value = f(b); $("x-test1").value = f(new Date(+t1 - day));
    $("x-train0").dataset.ds = d.id;
  }
  $("x-auto").onclick = autoSegments;
  function expConfig() {
    return {
      dataset: $("x-dataset").value, pool: $("x-pool").value, factors: $("x-factors").value, model: $("x-model").value,
      segments: { train: [$("x-train0").value, $("x-train1").value], valid: [$("x-valid0").value, $("x-valid1").value], test: [$("x-test0").value, $("x-test1").value] },
      rolling: { enabled: $("x-rolling").checked, step_days: +$("x-step").value, expanding: $("x-expand").value === "1" },
      label_horizon: +$("x-horizon").value, deal_price: $("x-deal").value, limit: $("x-limit").value, benchmark: $("x-bench").value,
      strategy: { topk: +$("x-topk").value, n_drop: +$("x-ndrop").value },
      costs: { open_cost: +$("x-open").value, close_cost: +$("x-close").value, min_cost: +$("x-min").value },
      account: +$("x-account").value, name: $("x-name").value.trim(),
    };
  }
  $("x-go").onclick = async () => {
    const cfg = expConfig();
    if (!cfg.dataset) return toast("请先选择数据集");
    store.set(EXP_KEY, JSON.stringify(cfg));
    $("x-open-report").hidden = true;
    await startJob("experiment", { config: cfg });
  };
  function renderExpJob(j) {
    const box = $("x-job");
    if (!j || j.kind !== "experiment") { if (!j) box.hidden = true; return; }
    box.hidden = false;
    $("x-job-title").textContent = j.running ? "运行中" : j.status;
    $("x-job-msg").textContent = j.message || (j.result && j.result.error) || "";
    $("x-job-bar").style.width = Math.round((j.running ? j.progress : 1) * 100) + "%";
    $("x-job-steps").innerHTML = (j.steps || []).map(s => `<li>${esc(s.t)} ${esc(s.msg)}</li>`).join("");
    $("x-cancel").hidden = !j.running;
    $("x-log").onclick = () => showLog(j.id, "实验日志");
    const ok = !j.running && j.result && j.result.ok;
    $("x-open-report").hidden = !ok;
    if (ok) $("x-open-report").onclick = () => { store.set(RUN_KEY, j.result.run_id); switchTab("report"); };
    if (!j.running && j.result && !j.result.ok) $("x-job-msg").innerHTML = `<span class="st failed">${esc(j.result.error)}</span>`;
  }
  let runsCache = [];
  async function loadRuns() {
    try { runsCache = (await api("/api/runs")).runs; } catch (e) { $("runs-table").innerHTML = `<p class="banner error">${esc(e.message)}</p>`; return; }
    const M = (catalog && catalog.models) || {};
    $("runs-table").innerHTML = runsCache.length ? `<table class="data"><thead><tr><th>名称</th><th>数据集</th><th>模型</th><th>因子</th><th>测试区间</th>
      <th class="num">年化收益</th><th class="num">年化超额</th><th class="num">最大回撤</th><th class="num">夏普</th><th class="num">RankIC</th><th>时间</th><th></th></tr></thead><tbody>
      ${runsCache.map(r => `<tr class="clickable" data-run="${esc(r.id)}"><td>${esc(r.name)}${r.demo ? '<span class="tag warn">演示数据</span>' : ""}${r.rolling ? '<span class="tag">滚动</span>' : ""}</td>
        <td>${esc(r.dataset)}</td><td>${esc((M[r.model] || r.model || "").split("（")[0])}</td><td>${esc(r.factors)}</td><td>${esc((r.test || []).join(" ~ "))}</td>
        <td class="num ${cls(r.metrics.annual_return)}">${pct(r.metrics.annual_return)}</td><td class="num ${cls(r.metrics.excess_annual_return)}">${pct(r.metrics.excess_annual_return)}</td>
        <td class="num">${pct(r.metrics.max_drawdown)}</td><td class="num">${num(r.metrics.sharpe, 2)}</td><td class="num">${num(r.rank_ic, 4)}</td><td>${esc(r.created)}</td>
        <td><button class="btn small danger" data-del="${esc(r.id)}">删除</button></td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">还没有实验。</p>';
  }
  $("runs-refresh").onclick = loadRuns;
  $("runs-table").addEventListener("click", async e => {
    const del = e.target.closest("[data-del]");
    if (del) {
      e.stopPropagation();
      if (!confirm("删除这次实验的结果？")) return;
      try { await api("/api/run/delete", { id: del.dataset.del }); loadRuns(); } catch (err) { toast(err.message); }
      return;
    }
    const tr = e.target.closest("[data-run]");
    if (tr) { store.set(RUN_KEY, tr.dataset.run); switchTab("report"); }
  });

  // ---------------------------------------------------------------- 图表
  const charts = {};
  const zoomNow = () => (window.QZoom ? window.QZoom.get() : 100) / 100;
  const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
  function chart(id, option) {
    if (!window.echarts) return;
    const el = $(id); if (!el) return;
    let c = charts[id];
    if (!c || c.getDom() !== el) { if (c) c.dispose(); c = charts[id] = echarts.init(el, null, { renderer: "canvas" }); }
    const z = zoomNow(), fs = Math.round(12 * z);
    const base = {
      textStyle: { fontSize: fs, color: css("--text-2") }, animation: false,
      grid: { left: 16 * z, right: 24 * z, top: 58 * z, bottom: 44 * z, containLabel: true },
      tooltip: { trigger: "axis", textStyle: { fontSize: fs } },
      legend: { top: 26 * z, left: 0, textStyle: { fontSize: fs, color: css("--text-2") }, itemWidth: 18 * z, itemHeight: 10 * z },
      title: { left: 0, top: 0, textStyle: { fontSize: Math.round(13 * z), color: css("--text") } },
    };
    const opt = { ...base, ...option };
    opt.grid = { ...base.grid, ...(option.grid || {}) };
    opt.legend = option.legend === false ? undefined : { ...base.legend, ...(option.legend || {}) };
    opt.title = { ...base.title, ...(option.title || {}), textStyle: base.title.textStyle };
    if (option.legend === false) opt.grid.top = (option.grid && option.grid.top) || 36 * z;
    const axisFont = ax => (Array.isArray(ax) ? ax : [ax]).map(a => a && ({ ...a, axisLabel: { fontSize: fs, color: css("--muted"), ...(a.axisLabel || {}) },
      nameTextStyle: { fontSize: fs } }));
    if (opt.xAxis) opt.xAxis = axisFont(opt.xAxis);
    if (opt.yAxis) opt.yAxis = axisFont(opt.yAxis);
    c.setOption(opt, true);
    c.resize();
    charts[id]._opt = option;
  }
  function resizeCharts() { Object.values(charts).forEach(c => { try { c.resize(); } catch { /* 隐藏中 */ } }); }
  window.addEventListener("resize", resizeCharts);
  window.addEventListener("qlibstudio-zoom", () => { Object.entries(charts).forEach(([id, c]) => { if (c._opt) chart(id, c._opt); }); });
  const dz = [{ type: "inside" }, { type: "slider", height: 18, bottom: 6 }];
  const PALETTE = ["#1f5fbf", "#e8590c", "#2b8a3e", "#ae3ec9", "#c92a2a", "#0b7285"];
  const rolling = (arr, n) => arr.map((_, i) => { const s = arr.slice(Math.max(0, i - n + 1), i + 1).filter(v => v != null); return s.length ? s.reduce((a, b) => a + b, 0) / s.length : null; });

  // ---------------------------------------------------------------- 报告
  let report = null, positions = null, trades = null;
  async function loadReportList() {
    try { runsCache = (await api("/api/runs")).runs; } catch (e) { toast(e.message); return; }
    const sel = $("r-select"), want = store.get(RUN_KEY);
    fillSelect(sel, runsCache.map(r => [r.id, `${r.name}（${r.created}）`]), want);
    if (!runsCache.length) { $("r-title").textContent = "报告"; $("r-sub").textContent = "还没有实验结果。先在「实验」里运行一次。"; $("r-tiles").innerHTML = ""; return; }
    if (!report || report.id !== sel.value) loadReport(sel.value); else setTimeout(resizeCharts, 30);
  }
  $("r-select").onchange = () => { store.set(RUN_KEY, $("r-select").value); loadReport($("r-select").value); };
  async function loadReport(id) {
    try { report = await api("/api/run?id=" + encodeURIComponent(id)); } catch (e) { toast(e.message); return; }
    positions = trades = null;
    store.set(RUN_KEY, id);
    const r = report, m = r.metrics, c = r.config, M = catalog.models;
    $("r-title").textContent = r.name;
    $("r-sub").textContent = `数据集「${r.dataset.title}」· 股票池 ${c.pool} · ${c.factors} · ${M[c.model] || c.model} · 训练 ${c.segments.train.join("~")} · 验证 ${c.segments.valid.join("~")} · 测试 ${c.segments.test.join("~")}`
      + `${c.rolling.enabled ? ` · 滚动训练每 ${c.rolling.step_days} 天（${c.rolling.expanding === false ? "固定长度" : "扩展窗口"}）` : ""} · 基准 ${r.benchmark} · Qlib ${r.qlib.version} · 用时 ${r.elapsed} 秒`;
    $("r-warn").innerHTML = r.dataset.demo ? '<div class="banner warn small">这是用<b>演示数据（模拟 A 股）</b>跑的结果，只用来检验流程，不代表任何真实策略的表现。</div>' : "";
    const tile = (k, v, cl, sub) => `<div class="tile"><div class="k">${k}</div><div class="v ${cl || ""}">${v}</div>${sub ? `<div class="s">${sub}</div>` : ""}</div>`;
    $("r-tiles").innerHTML = tile("累计收益", pct(m.total_return), cls(m.total_return), `${m.start} ~ ${m.end}，${m.days} 天`)
      + tile("年化收益", pct(m.annual_return), cls(m.annual_return), "复利，按 244 天/年")
      + tile("基准年化", pct(m.bench_annual_return), cls(m.bench_annual_return), esc(r.benchmark))
      + tile("年化超额", pct(m.excess_annual_return), cls(m.excess_annual_return), "组合年化 − 基准年化")
      + tile("最大回撤", pct(m.max_drawdown), "down")
      + tile("夏普比率", num(m.sharpe, 2), "", `年化波动 ${pct(m.volatility)}`)
      + tile("超额年化（Qlib 口径）", pct(m.qlib_excess_with_cost_annualized), cls(m.qlib_excess_with_cost_annualized), `含费用；不含费用 ${pct(m.qlib_excess_annualized)}`)
      + tile("信息比率（Qlib）", num(m.qlib_excess_with_cost_ir, 2), "", `超额最大回撤 ${pct(m.qlib_excess_with_cost_mdd)}`)
      + tile("IC 均值", num(r.ic.ic_mean, 4), cls(r.ic.ic_mean), `ICIR ${num(r.ic.icir, 3)}`)
      + tile("RankIC 均值", num(r.ic.rank_ic_mean, 4), cls(r.ic.rank_ic_mean), `RankICIR ${num(r.ic.rank_icir, 3)}`)
      + tile("日均换手", pct(m.turnover_daily, 1), "", `费用合计 ${money(m.cost_total)} 元`)
      + tile("日胜率", pct(m.win_rate_daily, 1), "", `跑赢基准天数 ${pct(m.excess_win_rate, 1)}`);
    drawReport();
  }
  function activeReportTab() { const b = document.querySelector("#r-tabs button.on"); return b ? b.dataset.sub : "r-nav"; }
  bindSubtabs("r-tabs", () => drawReport());
  async function drawReport() {
    if (!report) return;
    const r = report, d = r.daily, tab = activeReportTab();
    if (tab === "r-nav") {
      chart("c-nav", { title: { text: "净值（测试区间，已扣费用）" },
        xAxis: { type: "category", data: d.dates }, yAxis: [{ type: "value", scale: true }, { type: "value", scale: true, axisLabel: { formatter: v => (v * 100).toFixed(0) + "%" } }],
        dataZoom: dz, tooltip: { trigger: "axis", valueFormatter: v => v == null ? "—" : Number(v).toFixed(4) },
        series: [{ name: "组合", type: "line", data: d.nav, showSymbol: false, lineStyle: { width: 2 }, color: PALETTE[0] },
                 { name: "基准", type: "line", data: d.bench_nav, showSymbol: false, color: "#868e96" },
                 { name: "累计超额（Qlib 口径，右轴）", type: "line", yAxisIndex: 1, data: d.excess_cum, showSymbol: false, areaStyle: { opacity: .12 }, color: PALETTE[1] }] });
      chart("c-dd", { title: { text: "回撤" }, legend: false, xAxis: { type: "category", data: d.dates }, 
        yAxis: { type: "value", axisLabel: { formatter: v => (v * 100).toFixed(0) + "%" } }, dataZoom: dz,
        series: [{ name: "回撤", type: "line", data: d.drawdown, showSymbol: false, areaStyle: { opacity: .25 }, color: "#2b8a3e" }] });
    }
    if (tab === "r-ic") {
      const ic = r.ic;
      $("r-ic-tiles").innerHTML = [["IC 均值", num(ic.ic_mean, 4)], ["IC 标准差", num(ic.ic_std, 4)], ["ICIR", num(ic.icir, 3)], ["IC>0 天数占比", pct(ic.ic_positive, 1)],
        ["RankIC 均值", num(ic.rank_ic_mean, 4)], ["RankIC 标准差", num(ic.rank_ic_std, 4)], ["RankICIR", num(ic.rank_icir, 3)], ["RankIC>0 占比", pct(ic.rank_ic_positive, 1)]]
        .map(([k, v]) => `<div class="tile"><div class="k">${k}</div><div class="v">${v}</div></div>`).join("");
      chart("c-ic", { title: { text: `每日 IC / RankIC（${ic.n_days} 天）` }, xAxis: { type: "category", data: ic.dates }, yAxis: { type: "value" }, dataZoom: dz,
        series: [{ name: "RankIC", type: "bar", data: ic.rank_ic_series, color: "rgba(31,95,191,.45)" },
                 { name: "IC", type: "bar", data: ic.ic_series, color: "rgba(232,89,12,.35)" },
                 { name: "RankIC 20 日均值", type: "line", data: rolling(ic.rank_ic_series, 20), showSymbol: false, color: PALETTE[0], lineStyle: { width: 2 } }] });
    }
    if (tab === "r-groups") {
      const g = r.groups || {};
      if (!g.groups) { $("r-groups-note").textContent = "没有分层数据"; return; }
      chart("c-gmean", { title: { text: "各组下一期平均收益（每天按预测分 5 组）" }, legend: false, xAxis: { type: "category", data: g.groups }, 
        yAxis: { type: "value", axisLabel: { formatter: v => (v * 100).toFixed(2) + "%" } },
        tooltip: { trigger: "axis", valueFormatter: v => (v * 100).toFixed(3) + "%" },
        series: [{ type: "bar", data: g.mean.map((v, i) => ({ value: v, itemStyle: { color: i === 4 ? "#c92a2a" : i === 0 ? "#2b8a3e" : "#868e96" } })) }] });
      chart("c-gcurve", { title: { text: "各组累计收益（不重叠持有期复利）" }, xAxis: { type: "category", data: g.dates }, yAxis: { type: "value", scale: true }, dataZoom: dz,
        series: g.curves.map((cv, i) => ({ name: g.groups[i], type: "line", data: cv, showSymbol: false }))
          .concat([{ name: "多空（第5组−第1组）", type: "line", data: g.long_short, showSymbol: false, lineStyle: { type: "dashed", width: 2 }, color: "#000" }]) });
      $("r-groups-note").textContent = `${g.note}。多空日均 ${pct(g.long_short_mean, 3)}。分层看的是预测排序本身好不好，不含交易费用和涨跌停限制。`;
    }
    if (tab === "r-pos") {
      if (!positions) { try { positions = await api("/api/run/positions?id=" + encodeURIComponent(r.id)); } catch (e) { toast(e.message); return; } }
      const sel = $("r-pos-date");
      if (sel.dataset.run !== r.id) { fillSelect(sel, positions.map((p, i) => [String(i), p.date]).reverse(), String(positions.length - 1)); sel.dataset.run = r.id; }
      const p = positions[+sel.value] || positions[positions.length - 1];
      if (!p) { $("r-pos-table").innerHTML = '<p class="muted">没有持仓记录</p>'; return; }
      $("r-pos-sum").textContent = `${p.holdings.length} 只 · 持仓市值 ${money(p.holdings.reduce((a, h) => a + (h.value || 0), 0))} 元 · 现金 ${money(p.cash)} 元 · 账户 ${money(p.value)} 元`;
      $("r-pos-table").innerHTML = `<table class="data small"><thead><tr><th>代码</th><th>名称</th><th class="num">市值（元）</th><th class="num">权重</th><th class="num">已持有天数</th></tr></thead><tbody>
        ${p.holdings.map(h => `<tr><td>${esc(h.symbol)}</td><td>${esc(h.name)}</td><td class="num">${money(h.value)}</td><td class="num">${pct(h.weight, 2)}</td><td class="num">${h.days}</td></tr>`).join("")}</tbody></table>`;
    }
    if (tab === "r-trades") {
      if (!trades) { try { trades = await api("/api/run/trades?id=" + encodeURIComponent(r.id)); } catch (e) { toast(e.message); return; } }
      renderTrades();
    }
    if (tab === "r-train") {
      const f = r.folds || [];
      $("r-folds").innerHTML = `<table class="data small"><thead><tr><th>段</th><th>训练</th><th>验证</th><th>测试（预测）</th><th class="num">预测条数</th><th class="num">最佳轮数</th><th class="num">用时（秒）</th></tr></thead><tbody>
        ${f.map(x => `<tr><td>${x.fold}</td><td>${esc(x.train.join(" ~ "))}</td><td>${esc(x.valid.join(" ~ "))}</td><td>${esc(x.test.join(" ~ "))}</td>
          <td class="num">${x.n_pred}</td><td class="num">${x.best_iteration ?? "—"}</td><td class="num">${x.seconds}</td></tr>`).join("")}</tbody></table>`;
      const loss = (f[0] || {}).loss;
      if (loss) chart("c-loss", { title: { text: "第 1 段训练过程（损失）" }, xAxis: { type: "category", data: Object.values(loss)[0].map((_, i) => i + 1) }, yAxis: { type: "value", scale: true },
        series: Object.entries(loss).map(([k, v]) => ({ name: k.replace("train", "训练").replace("valid", "验证"), type: "line", data: v, showSymbol: false })) });
      else { if (charts["c-loss"]) charts["c-loss"].clear(); }
      const imp = r.importance || [];
      if (imp.length) chart("c-imp", { title: { text: "因子重要性（LightGBM gain，前 30）" }, legend: false,
        xAxis: { type: "value" }, yAxis: { type: "category", data: imp.map(x => x.name).reverse() }, tooltip: { trigger: "axis" },
        series: [{ type: "bar", data: imp.map(x => x.value).reverse(), color: PALETTE[0] }] });
      else { if (charts["c-imp"]) charts["c-imp"].clear(); }
    }
    if (tab === "r-cfg") $("r-cfg-body").textContent = JSON.stringify(r.config, null, 2);
  }
  $("r-pos-date").onchange = () => drawReport();
  function renderTrades() {
    const q = $("r-tr-filter").value.trim();
    const list = q ? trades.filter(t => (t.symbol + t.name + t.date).includes(q)) : trades;
    const buys = list.filter(t => t.side === "买入").length;
    $("r-tr-sum").textContent = `${list.length} 笔（买 ${buys}、卖 ${list.length - buys}），费用 ${money(list.reduce((a, t) => a + (t.cost || 0), 0))} 元${list.length > 1500 ? "；只显示前 1500 笔，全部请导出 CSV" : ""}`;
    $("r-tr-table").innerHTML = `<table class="data small"><thead><tr><th>日期</th><th>代码</th><th>名称</th><th>方向</th><th class="num">股数</th><th class="num">成交价（不复权）</th><th class="num">成交额（元）</th><th class="num">费用（元）</th></tr></thead><tbody>
      ${list.slice(0, 1500).map(t => `<tr><td>${esc(t.date)}</td><td>${esc(t.symbol)}</td><td>${esc(t.name)}</td><td class="${t.side === "买入" ? "up" : "down"}">${esc(t.side)}</td>
        <td class="num">${(t.shares ?? 0).toLocaleString("zh-CN")}</td><td class="num">${num(t.price, 2)}</td><td class="num">${money(t.value)}</td><td class="num">${money(t.cost)}</td></tr>`).join("")}</tbody></table>`;
  }
  $("r-tr-filter").oninput = () => trades && renderTrades();
  async function saveOrDownload(kind, url) {
    if (!report) return toast("先选择一次实验");
    try {
      const r = await api("/api/save", { kind, id: report.id });
      if (r.mode === "browser") { location.href = url; return; }
      if (r.saved) toast("已保存：" + r.saved, 6000);
    } catch (e) { toast("保存失败：" + e.message); }
  }
  $("r-export").onclick = () => saveOrDownload("run-export", "/api/run/export?id=" + encodeURIComponent(report ? report.id : ""));
  $("r-trades-csv").onclick = () => saveOrDownload("trades-csv", "/api/run/trades.csv?id=" + encodeURIComponent(report ? report.id : ""));

  // ---------------------------------------------------------------- 对比
  let cmpItems = [];
  async function loadCompareItems() {
    try { cmpItems = (await api("/api/compare/items")).items; } catch (e) { toast(e.message); return; }
    const checked = new Set(JSON.parse(store.get("qlibstudio-cmp") || "[]"));
    $("cmp-items").innerHTML = cmpItems.length ? `<table class="data small"><thead><tr><th></th><th>来源</th><th>应用</th><th>名称</th><th>数据集</th><th>回测区间</th><th>时间</th><th></th></tr></thead><tbody>
      ${cmpItems.map(x => `<tr><td><input type="checkbox" data-key="${esc(x.key)}" ${checked.has(x.key) ? "checked" : ""}></td><td>${esc(x.source)}</td><td><b>${esc(x.app)}</b></td><td>${esc(x.name)}</td>
        <td>${esc(x.dataset)}</td><td>${esc(((x.period || {}).start || "") + " ~ " + ((x.period || {}).end || ""))}</td><td>${esc(x.imported)}</td>
        <td>${x.key.startsWith("file:") ? `<button class="btn small danger" data-cdel="${esc(x.key)}">移除</button>` : ""}</td></tr>`).join("")}</tbody></table>`
      : '<p class="muted">还没有可对比的结果：导入 QuantLab 的结果文件，或先在本应用跑一次实验。</p>';
  }
  $("cmp-items").addEventListener("change", () => {
    store.set("qlibstudio-cmp", JSON.stringify([...document.querySelectorAll("#cmp-items input:checked")].map(i => i.dataset.key)));
  });
  $("cmp-items").addEventListener("click", async e => {
    const b = e.target.closest("[data-cdel]"); if (!b) return;
    try { await api("/api/compare/delete", { key: b.dataset.cdel }); loadCompareItems(); } catch (err) { toast(err.message); }
  });
  $("cmp-file").onchange = async e => {
    for (const f of e.target.files) {
      try { const r = await api("/api/compare/import", { content: await f.text() }); toast(`已导入：${r.app} · ${r.name}`); }
      catch (err) { toast(`${f.name}：${err.message}`, 9000); }
    }
    e.target.value = "";
    loadCompareItems();
  };
  $("cmp-qldir-pick").onclick = () => pick("folder", "cmp-qldir", false);
  $("cmp-qldir-go").onclick = async () => {
    const d = $("cmp-qldir").value.trim(); if (!d) return toast("请先填写 QuantLab 的实验目录");
    try { const r = await api("/api/compare/import", { quantlab_dir: d }); toast(`已导入：${r.app} · ${r.name}`); loadCompareItems(); }
    catch (e) { toast(e.message, 9000); }
  };
  $("cmp-go").onclick = async () => {
    const keys = [...document.querySelectorAll("#cmp-items input:checked")].map(i => i.dataset.key);
    if (keys.length < 1) return toast("至少勾选一项（通常是一份 QuantLab 结果 + 一次本应用的实验）");
    let res;
    try { res = await api("/api/compare?keys=" + encodeURIComponent(keys.join(",")) + "&common=" + ($("cmp-common").checked ? "1" : "0")); } catch (e) { return toast(e.message, 8000); }
    $("cmp-result").hidden = false;
    $("cmp-warn").innerHTML = (res.warnings || []).map(w => `<div class="banner warn small">${esc(w)}</div>`).join("")
      + (res.common ? `<p class="muted">共同区间：${esc(res.common.start)} ~ ${esc(res.common.end)}</p>` : "");
    const items = res.items, L = res.labels;
    const head = `<tr><th>指标</th>${items.map((x, i) => `<th style="color:${PALETTE[i % PALETTE.length]}">${esc(x.app)}<br><small>${esc(x.name)}</small></th>`).join("")}</tr>`;
    const recalc = ["total_return", "annual_return", "volatility", "sharpe", "max_drawdown", "excess_annual_return", "info_ratio"];
    const fmt = (k, v) => v == null ? "—" : (L[k] || ["", "num"])[1] === "pct" ? pct(v) : num(v, 3);
    const rows = recalc.map(k => `<tr><td>${esc(L[k][0])}${$("cmp-common").checked ? "（共同区间重算）" : ""}</td>${items.map(x => `<td class="num ${["sharpe", "info_ratio", "volatility"].includes(k) ? "" : cls(x.metrics_common[k])}">${fmt(k, x.metrics_common[k])}</td>`).join("")}</tr>`).join("")
      + Object.keys(L).map(k => `<tr><td>${esc(L[k][0])}（原始文件）</td>${items.map(x => `<td class="num">${fmt(k, (x.metrics_original || {})[k])}</td>`).join("")}</tr>`).join("")
      + `<tr><td>回测区间（原始）</td>${items.map(x => `<td>${esc((x.period || {}).start)} ~ ${esc((x.period || {}).end)}</td>`).join("")}</tr>`
      + `<tr><td>数据集</td>${items.map(x => `<td>${esc((x.dataset || {}).title)}</td>`).join("")}</tr>`;
    $("cmp-table").innerHTML = `<table class="data small"><thead>${head}</thead><tbody>${rows}</tbody></table>`;
    const dates = [...new Set(items.flatMap(x => x.curve.dates))].sort();
    const align = (x, key) => { const m = new Map(x.curve.dates.map((d, i) => [d, x.curve[key][i]])); return dates.map(d => m.has(d) ? m.get(d) : null); };
    chart("c-cmp-nav", { title: { text: "净值（区间起点归一）" }, xAxis: { type: "category", data: dates }, yAxis: { type: "value", scale: true }, dataZoom: dz,
      series: items.map((x, i) => ({ name: `${x.app} · ${x.name}`, type: "line", data: align(x, "nav"), showSymbol: false, connectNulls: true, color: PALETTE[i % PALETTE.length] }))
        .concat(items.map((x, i) => ({ name: `基准 · ${x.app}`, type: "line", data: align(x, "bench_nav"), showSymbol: false, connectNulls: true, lineStyle: { type: "dotted" }, color: PALETTE[i % PALETTE.length] }))) });
    chart("c-cmp-ex", { title: { text: "相对基准（组合净值 ÷ 基准净值）" }, xAxis: { type: "category", data: dates }, yAxis: { type: "value", scale: true }, dataZoom: dz,
      series: items.map((x, i) => ({ name: `${x.app} · ${x.name}`, type: "line", data: align(x, "excess"), showSymbol: false, connectNulls: true, color: PALETTE[i % PALETTE.length] })) });
    const icDates = [...new Set(items.flatMap(x => x.ic.dates))].sort();
    chart("c-cmp-ic", { title: { text: "RankIC（20 期均值；QuantLab 每个调仓日一个值，Qlib 每天一个值）" }, xAxis: { type: "category", data: icDates }, yAxis: { type: "value" }, dataZoom: dz,
      series: items.map((x, i) => { const m = new Map(x.ic.dates.map((d, k) => [d, rolling(x.ic.rank_ic.length ? x.ic.rank_ic : x.ic.ic, 20)[k]]));
        return { name: `${x.app} · ${x.name}`, type: "line", data: icDates.map(d => m.has(d) ? m.get(d) : null), showSymbol: false, connectNulls: true, color: PALETTE[i % PALETTE.length] }; }) });
    const skeys = [...new Set(items.flatMap(x => Object.keys(x.settings || {})))];
    const SK = { factors: "因子", n_features: "因子数", models: "模型", horizon: "预测周期", top_k: "持仓数", rebalance: "调仓", deal_price: "成交价", costs: "费用",
      limit: "涨跌停", benchmark: "基准", pool: "股票池", rolling: "滚动训练" };
    $("cmp-settings").innerHTML = `<table class="data small"><thead>${head.replace("指标", "设置")}</thead><tbody>${skeys.map(k => `<tr><td>${esc(SK[k] || k)}</td>${items.map(x => {
      const v = (x.settings || {})[k]; return `<td>${esc(typeof v === "object" && v !== null ? JSON.stringify(v) : v ?? "—")}</td>`; }).join("")}</tr>`).join("")}</tbody></table>`;
    $("cmp-result").scrollIntoView({ behavior: "smooth" });
  };

  // ---------------------------------------------------------------- 运行状态：任务记录
  async function loadJobs() {
    try {
      const j = await api("/api/jobs");
      $("jobs-table").innerHTML = j.jobs.length ? `<table class="data small"><thead><tr><th>时间</th><th>任务</th><th>状态</th><th class="num">工作进程 PID</th><th>结果</th><th></th></tr></thead><tbody>
        ${j.jobs.map(x => `<tr><td>${esc(x.created)}</td><td>${esc(x.title)}</td><td class="st ${x.status === "完成" ? "ok" : x.status === "运行中" ? "partial" : "failed"}">${esc(x.status)}${x.running ? ` ${Math.round(x.progress * 100)}%` : ""}</td>
          <td class="num">${x.pid ?? "—"}${x.running ? "" : '<small class="muted">（已结束）</small>'}</td><td>${esc((x.result && (x.result.summary || x.result.error)) || x.message || "")}</td>
          <td><button class="btn small" data-log="${esc(x.id)}">日志</button></td></tr>`).join("")}</tbody></table>` : '<p class="muted">还没有任务。</p>';
    } catch (e) { $("jobs-table").textContent = e.message; }
  }
  $("jobs-table").addEventListener("click", e => { const b = e.target.closest("[data-log]"); if (b) showLog(b.dataset.log, "任务日志"); });
  $("open-data-dir").onclick = () => api("/api/open", { what: "data-dir" }).catch(e => toast(e.message));

  // ---------------------------------------------------------------- 外部链接、退出
  document.addEventListener("click", e => {
    const u = e.target.closest("[data-url]");
    if (u) { e.preventDefault(); api("/api/open", { what: "url", url: u.dataset.url }).catch(err => toast(err.message)); }
  });
  $("quit-btn").onclick = async () => {
    const busy = status && status.job && status.job.running;
    if (!confirm(busy ? `退出会结束正在运行的「${status.job.title}」，确定退出？` : "退出 Qlib 量化研究台？")) return;
    try { await api("/api/quit", {}); } catch { /* 服务已停 */ }
    document.body.innerHTML = '<div style="padding:4rem;text-align:center;font:16px sans-serif;color:#666">Qlib 量化研究台已退出，工作进程已全部结束。可以关闭这个页面了。</div>';
  };

  // ---------------------------------------------------------------- 启动
  const guide = window.QGuide({ api, toast, switchTab, esc });
  api("/api/catalog").then(c => {
    catalog = c; initExpForm();
    switchTab(current);
  }).catch(e => showError("读取配置失败：" + e.message));
  refreshStatus();
})();
