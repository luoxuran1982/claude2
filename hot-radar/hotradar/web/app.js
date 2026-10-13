"use strict";
// 热点雷达主界面：首页（抓取/定时/运行记录）、最新报告、历史报告、配置、运行状态、退出。
// 「使用指南」和进程表在 guide.js；缩放在 zoom.js；Markdown 渲染在 md.js。
(function () {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const TOKEN = new URLSearchParams(location.search).get("t") || "";
  const TAB_KEY = "hotradar-tab", CFG_KEY = "hotradar-cfg";
  const store = { get: k => { try { return localStorage.getItem(k); } catch { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* 忽略 */ } } };

  async function api(path, body) {
    const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json", "X-HotRadar-Token": TOKEN }, body: JSON.stringify(body) };
    const res = await fetch(path, opt);
    const ct = res.headers.get("Content-Type") || "";
    const data = ct.includes("json") ? await res.json() : await res.text();
    if (!res.ok && !(res.status === 409 && data && data.started === false)) {
      const msg = (data && data.error) || data.reason || res.statusText;
      if (res.status === 403) throw new Error("请求被拒绝：" + msg + "（页面令牌无效时，请关闭后从应用重新打开）");
      throw new Error(msg);
    }
    return data;
  }
  function toast(msg, ms = 3200) {
    const t = $("toast"); t.textContent = msg; t.hidden = false;
    clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.hidden = true; }, ms);
  }
  function showError(msg) { const e = $("error"); e.textContent = msg || ""; e.hidden = !msg; }
  const fmtTime = iso => { if (!iso) return "—"; const d = new Date(iso); return isNaN(d) ? iso : d.toLocaleString("zh-CN", { hour12: false }); };
  const fmtBytes = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MiB" : n >= 1024 ? (n / 1024).toFixed(1) + " KiB" : n + " B";
  const STATUS = { ok: "成功", partial: "部分成功", failed: "失败", cancelled: "已停止" };
  const TASKS = { crawl: "抓取", demo: "离线演示", "test-notify": "推送测试", doctor: "环境体检" };

  // ---------------------------------------------------------------- 栏目
  const views = ["home", "report", "history", "config", "status", "guide"];
  let current = store.get(TAB_KEY);
  if (!views.includes(current)) current = "home";
  function switchTab(name) {
    if (!views.includes(name)) name = "home";
    current = name; store.set(TAB_KEY, name);
    document.querySelectorAll(".tabs button").forEach(b => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    document.querySelectorAll(".view").forEach(v => v.classList.toggle("active", v.dataset.view === name));
    if (name === "report") loadReport();
    if (name === "history") loadHistory();
    if (name === "config") loadConfig();
    if (name === "status") { guide.renderProcs(); renderBg(); }
    if (name === "guide") guide.renderGuide();
    if (name === "home") loadRuns();
  }
  document.querySelectorAll(".tabs button").forEach(b => { b.onclick = () => switchTab(b.dataset.tab); });

  // ---------------------------------------------------------------- 状态轮询
  let status = null, wasRunning = false, pollTimer = null;
  async function refreshStatus() {
    clearTimeout(pollTimer);
    try {
      status = await api("/api/status");
      showError("");
      renderStatus();
    } catch (e) {
      showError("连接本机服务失败：" + e.message + "。如果刚点了「退出」，可以关闭这个窗口。");
    }
    const running = !!(status && status.job);
    if (wasRunning && !running) { onJobFinished(); }
    wasRunning = running;
    pollTimer = setTimeout(refreshStatus, running ? 1000 : 4000);
  }

  function renderStatus() {
    const s = status;
    $("app-version").textContent = "v" + s.version;
    $("up-version").textContent = s.upstream.version;
    $("network-note").textContent = s.network_note;
    $("api-url").textContent = s.api_url;
    const job = s.job;
    $("job-dot").className = "dot " + (job ? "run" : s.last_run ? (s.last_run.status === "ok" ? "ok" : s.last_run.status === "partial" ? "" : "bad") : "");
    $("job-text").textContent = job ? `正在${job.label}：${job.stage}（${job.percent}%）`
      : s.last_run ? `空闲 · 上次${TASKS[s.last_run.task] || ""} ${fmtTime(s.last_run.started_at)}：${STATUS[s.last_run.status] || s.last_run.status}` : "空闲 · 还没有抓取过";
    $("job-bar").hidden = !job;
    if (job) $("job-bar").firstElementChild.style.width = job.percent + "%";
    const sc = s.schedule;
    $("sched-text").textContent = sc.enabled ? `定时抓取：开（每 ${sc.interval_min} 分钟${sc.next_at ? "，下次 " + new Date(sc.next_at).toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" }) : ""}）` : "定时抓取：关";
    $("mcp-text").textContent = s.mcp.running ? `MCP：运行中（端口 ${s.mcp.port}）` : "MCP：关";
    // 首页抓取卡片
    $("btn-crawl").disabled = $("btn-demo").disabled = !!job;
    $("btn-cancel").hidden = !job;
    $("job-box").hidden = !job;
    if (job) {
      $("job-stage").textContent = `${job.label}：${job.stage}`;
      $("job-pct").textContent = `${job.percent}%` + (job.platforms_total ? ` · 平台 ${job.platforms_done}/${job.platforms_total}` : "") + (job.rss_total ? ` · RSS ${job.rss_done}/${job.rss_total}` : "");
      $("job-pid").textContent = `工作进程 PID ${job.pid} · ${job.trigger}触发 · 开始于 ${fmtTime(job.started_at)}`;
      $("job-fill").style.width = job.percent + "%";
      $("job-platforms").innerHTML = platformChips(job);
      const log = $("job-log"); const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 20;
      log.textContent = job.log_tail || "";
      if (atBottom) log.scrollTop = log.scrollHeight;
    }
    renderLast(s.last_run);
    // 定时
    const sel = $("sched-interval");
    if (!sel.options.length) sel.innerHTML = sc.intervals.map(i => `<option value="${i}">${i >= 60 ? (i / 60) + " 小时" : i + " 分钟"}</option>`).join("");
    if (document.activeElement !== sel) sel.value = String(sc.interval_min);
    $("sched-on").checked = sc.enabled;
    $("sched-status").innerHTML = (sc.enabled ? `<b>已开启</b>：每 ${esc(sel.selectedOptions[0]?.textContent || "")} 抓取一次` + (sc.next_at ? `，下次约 ${esc(fmtTime(sc.next_at))}` : "") : "<b>已关闭</b>") + "。" + esc(sc.note) + "。";
    // 数据概况
    const st = s.stats;
    $("stats").innerHTML = `<span class="k">数据目录</span><span class="v">${esc(s.data_dir)}</span>
      <span class="k">热榜数据库</span><span class="v">${st.news_db_days} 天（${fmtBytes(st.news_db_bytes)}）${st.first_day ? `，${esc(st.first_day)} 至 ${esc(st.last_day)}` : ""}</span>
      <span class="k">RSS 数据库</span><span class="v">${st.rss_db_days} 天（${fmtBytes(st.rss_db_bytes)}）</span>
      <span class="k">HTML 报告</span><span class="v">${st.html_files} 份（${fmtBytes(st.html_bytes)}）</span>`;
    // MCP 面板、运行状态页的后台任务表
    renderMcp(s.mcp);
    if (current === "status") renderBg();
  }

  function platformChips(job) {
    const ok = new Set(job.platforms_ok), bad = job.platforms_failed || {};
    const names = cfgCache ? Object.fromEntries((cfgCache.values["platforms.sources"] || []).map(p => [p.id, p.name])) : {};
    const all = [...new Set([...job.platforms_ok, ...Object.keys(bad)])];
    return all.map(id => `<span class="chip ${ok.has(id) ? "ok" : "bad"}" title="${esc(bad[id] || "成功")}">${esc(names[id] || id)}${ok.has(id) ? " ✓" : " ✗"}</span>`).join("")
      + (job.errors && job.errors.length ? `<span class="chip bad" title="${esc(job.errors.join("\n"))}">出错 ${job.errors.length}</span>` : "");
  }

  function renderLast(r) {
    const box = $("last-box");
    if (!r) { box.innerHTML = '<p class="muted">还没有运行过。点「立即抓取」开始；网络不通时可以先点「离线演示」。</p>'; return; }
    const failed = Object.entries(r.platforms_failed || {});
    box.innerHTML = `<div class="row"><b>上次${esc(TASKS[r.task] || r.label)}</b><span class="st ${esc(r.status)}">${esc(STATUS[r.status] || r.status)}</span>
        <span class="muted">${esc(fmtTime(r.started_at))} · 用时 ${r.duration_sec ?? "—"} 秒${r.workspace === "demo" ? " · 样例数据" : ""}</span></div>
      <p>${esc(r.summary || "")}</p>
      ${failed.length ? `<p class="muted">失败的平台：${failed.map(([id, why]) => `${esc(id)}（${esc(why)}）`).join("；")}</p>` : ""}
      ${r.notify && r.notify.length ? `<p class="muted">推送：${r.notify.map(esc).join("；")}</p>` : ""}
      <div class="row">${r.report ? `<button class="btn small primary" data-open-report="${esc(r.workspace)}|${esc(reportRel(r.report))}">查看这次的报告</button>` : ""}
        <button class="btn small" data-log="${esc(r.id)}">查看日志</button></div>`;
  }
  const reportRel = p => String(p || "").replace(/\\/g, "/").replace(/^.*output\/html\//, "");

  async function onJobFinished() {
    loadRuns();
    const last = status && status.last_run;
    if (last) toast(`${TASKS[last.task] || "任务"}结束：${STATUS[last.status] || last.status}。${last.summary || ""}`, 6000);
    if (current === "report") loadReport();
    if (current === "history") loadHistory();
  }

  // ---------------------------------------------------------------- 首页操作
  async function run(task) {
    try {
      const res = await api("/api/run", { task });
      if (!res.started) return toast(res.reason || "没有开始");
      toast(`已开始${TASKS[task]}`);
      if (task !== "crawl" && task !== "demo") switchTab("home");
      refreshStatus();
    } catch (e) { toast("启动失败：" + e.message, 6000); }
  }
  $("btn-crawl").onclick = () => run("crawl");
  $("btn-demo").onclick = () => run("demo");
  $("btn-cancel").onclick = async () => { await api("/api/cancel", {}); toast("已停止"); refreshStatus(); };
  $("push-test").onclick = () => run("test-notify");
  $("run-doctor").onclick = () => run("doctor");
  async function saveSchedule() {
    try {
      await api("/api/schedule", { enabled: $("sched-on").checked, interval_min: +$("sched-interval").value });
      toast($("sched-on").checked ? "定时抓取已开启（只在应用开着时运行）" : "定时抓取已关闭");
      refreshStatus();
    } catch (e) { toast("保存失败：" + e.message, 6000); }
  }
  $("sched-on").onchange = saveSchedule;
  $("sched-interval").onchange = saveSchedule;
  $("open-data").onclick = () => api("/api/open", { what: "data-dir" }).catch(e => toast(e.message));

  async function loadRuns() {
    try {
      const { runs } = await api("/api/runs");
      const tb = $("runs-table").querySelector("tbody");
      tb.innerHTML = runs.length ? runs.slice(0, 100).map(r => `<tr>
        <td>${esc(fmtTime(r.started_at))}</td><td>${esc(TASKS[r.task] || r.label)}</td><td>${esc(r.trigger || "")}</td>
        <td><span class="st ${esc(r.status)}">${esc(STATUS[r.status] || r.status)}</span></td><td>${esc(r.summary || "")}</td>
        <td class="num">${r.duration_sec != null ? r.duration_sec + " 秒" : "—"}</td>
        <td><button class="link" data-log="${esc(r.id)}">日志</button>${r.report ? `<button class="link" data-open-report="${esc(r.workspace)}|${esc(reportRel(r.report))}">报告</button>` : ""}</td></tr>`).join("")
        : '<tr><td colspan="7" class="muted">还没有记录</td></tr>';
    } catch (e) { /* 状态栏会提示连接问题 */ }
  }

  document.addEventListener("click", async e => {
    const lg = e.target.closest("[data-log]");
    if (lg) {
      $("log-title").textContent = "运行日志 " + lg.dataset.log;
      $("log-body").textContent = "正在读取…";
      $("log-dialog").showModal();
      try { $("log-body").textContent = await api("/api/run-log?id=" + encodeURIComponent(lg.dataset.log)) || "（空）"; }
      catch (err) { $("log-body").textContent = "读取失败：" + err.message; }
      return;
    }
    const rp = e.target.closest("[data-open-report]");
    if (rp) {
      const [ws, rel] = rp.dataset.openReport.split("|");
      return openHistory(ws, rel);
    }
    const u = e.target.closest("[data-url]");
    if (u) { e.preventDefault(); api("/api/open", { what: "url", url: u.dataset.url }).catch(err => toast(err.message)); }
  });

  // ---------------------------------------------------------------- 报告
  const MODE = { current: "当前榜单", daily: "全天汇总", incremental: "增量监控" };
  let reportShown = null;
  async function loadReport() {
    const ws = $("rp-ws").value;
    try {
      const r = await api("/api/reports?ws=" + ws);
      const latest = r.latest;
      $("rp-empty").hidden = !!latest;
      $("rp-wrap").hidden = !latest;
      if (!latest) { $("rp-info").textContent = ""; reportShown = null; return; }
      const src = `/report/${ws}/${latest.path}`;
      $("rp-info").textContent = `${MODE[latest.mode] || latest.mode}模式 · 生成于 ${fmtTime(latest.mtime)}${ws === "demo" ? " · 离线演示（样例数据，不是当前热点）" : ""}`;
      if (reportShown !== src + latest.mtime) { $("rp-frame").src = src + "?v=" + encodeURIComponent(latest.mtime); reportShown = src + latest.mtime; }
      $("rp-external").onclick = () => api("/api/open", { what: "report", ws, path: latest.path }).catch(e => toast(e.message));
    } catch (e) { toast("读取报告失败：" + e.message); }
  }
  $("rp-ws").onchange = () => { reportShown = null; loadReport(); };
  $("rp-reload").onclick = () => { reportShown = null; loadReport(); };

  let histSel = null;
  async function loadHistory() {
    const ws = $("hs-ws").value;
    try {
      const r = await api("/api/reports?ws=" + ws);
      const days = {};
      r.reports.forEach(x => (days[x.date] = days[x.date] || []).push(x));
      $("hs-sub").textContent = `${r.reports.length} 份${ws === "demo" ? "（离线演示）" : ""}`;
      $("hs-list").innerHTML = r.reports.length ? Object.entries(days).map(([d, list]) => `<div class="hist-day">${esc(d)}</div>` + list.map(x =>
        `<button class="hist-item${histSel === ws + "|" + x.path ? " on" : ""}" data-hist="${esc(ws)}|${esc(x.path)}"><span>${esc(x.time)} ${x.kind === "rss" ? "RSS 报告" : "热榜报告"}</span><span class="muted">${fmtBytes(x.size)}</span></button>`).join("")).join("")
        : '<p class="muted">还没有报告。</p>';
    } catch (e) { toast("读取历史失败：" + e.message); }
  }
  function openHistory(ws, rel) {
    histSel = ws + "|" + rel;
    $("hs-ws").value = ws;
    if (current !== "history") switchTab("history"); else loadHistory();
    $("hs-frame").src = `/report/${ws}/${rel}`;
    $("hs-title").textContent = `${ws === "demo" ? "离线演示 · " : ""}${rel.replace(".html", "").replace("/", " ").replace("rss_", "RSS ")}`;
    $("hs-external").disabled = false;
    $("hs-external").onclick = () => api("/api/open", { what: "report", ws, path: rel }).catch(e => toast(e.message));
  }
  $("hs-list").addEventListener("click", e => {
    const b = e.target.closest("[data-hist]"); if (!b) return;
    const [ws, rel] = b.dataset.hist.split("|");
    openHistory(ws, rel);
    document.querySelectorAll(".hist-item").forEach(x => x.classList.toggle("on", x === b));
  });
  $("hs-ws").onchange = loadHistory;

  // ---------------------------------------------------------------- 配置
  const ENUM_LABELS = {
    "report.mode": { current: "当前榜单（current）", daily: "全天汇总（daily）", incremental: "增量监控（incremental）" },
    "report.display_mode": { keyword: "按关键词分组", platform: "按平台分组" },
    "ai_analysis.mode": { follow_report: "跟随报告模式", daily: "全天汇总", current: "当前榜单", incremental: "增量" },
    "notification.channels.wework.msg_type": { markdown: "markdown（企业微信群机器人）", text: "text（个人微信应用）" },
    "schedule.preset": { always_on: "全天候（always_on）", morning_evening: "全天 + 晚间汇总（morning_evening）", office_hours: "工作日三段式（office_hours）", night_owl: "夜猫子（night_owl）", custom: "自定义（custom）" },
  };
  let cfgCache = null, cfgPanel = store.get(CFG_KEY) || "keywords", kwSaved = "";
  function cfgMsg(msg, kind = "ok") { const m = $("cfg-msg"); m.className = "banner " + kind; m.textContent = msg; m.hidden = !msg; if (msg) m.scrollIntoView({ block: "nearest" }); }

  async function loadConfig(force) {
    showPanel(cfgPanel);
    if (cfgCache && !force) return;
    try { applyConfig(await api("/api/config")); }
    catch (e) { cfgMsg("读取配置失败：" + e.message, "error"); }
  }
  function showPanel(name) {
    cfgPanel = name; store.set(CFG_KEY, name);
    document.querySelectorAll("#cfg-nav button").forEach(b => b.classList.toggle("on", b.dataset.cfg === name));
    document.querySelectorAll(".cfg-panel").forEach(p => p.classList.toggle("on", p.dataset.panel === name));
    if (name === "mcp") refreshMcpLog();
  }
  $("cfg-nav").addEventListener("click", e => { const b = e.target.closest("[data-cfg]"); if (b) { cfgMsg(""); showPanel(b.dataset.cfg); } });

  function applyConfig(c) {
    cfgCache = c;
    const v = c.values;
    document.querySelectorAll("[data-k]").forEach(el => {
      const k = el.dataset.k, val = v[k];
      if (el.type === "checkbox") el.checked = !!val;
      else if (el.tagName === "SELECT") {
        const opts = k === "schedule.preset" ? c.presets.map(p => p.id) : (c.enums[k] || []);
        el.innerHTML = opts.map(o => `<option value="${esc(o)}">${esc((ENUM_LABELS[k] || {})[o] || o)}</option>`).join("");
        el.value = val ?? "";
      } else el.value = val ?? "";
    });
    document.querySelectorAll('input[name="filter-method"]').forEach(r => { r.checked = r.value === v["filter.method"]; });
    kwSaved = c.keywords;
    $("kw-text").value = c.keywords;
    $("kw-dirty").textContent = "";
    $("ai-interests").value = c.interests;
    renderPlatforms(v["platforms.sources"]);
    renderFeeds(v["rss.feeds"]);
    renderStandalone();
    renderChannels(c);
    presetDesc();
    $("cfg-files").innerHTML = `配置文件：<br>${esc(c.files.config)}<br>关键词：<br>${esc(c.files.keywords)}<br>密钥：<br>${esc(c.files.secrets)}`;
    checkKeywords(true);
  }

  // 平台、RSS 表格
  function renderPlatforms(list) {
    $("plat-table").querySelector("tbody").innerHTML = list.map((p, i) => platRow(p, i)).join("");
  }
  const platRow = (p, i, isNew) => `<tr data-row>
    <td><input type="checkbox" data-f="enabled" ${p.enabled ? "checked" : ""} aria-label="启用"></td>
    <td><input data-f="name" value="${esc(p.name)}" aria-label="显示名称"></td>
    <td><input data-f="id" value="${esc(p.id)}" ${isNew ? 'placeholder="如 36kr"' : "readonly"} aria-label="平台 ID"></td>
    <td><input data-f="expected_domain" value="${esc(p.expected_domain)}" placeholder="如 36kr.com" aria-label="校验域名"></td>
    <td><button class="link" data-del-row title="删除">删除</button></td></tr>`;
  function renderFeeds(list) {
    $("feed-table").querySelector("tbody").innerHTML = list.map(f => feedRow(f)).join("");
  }
  const feedRow = (f, isNew) => `<tr data-row>
    <td><input type="checkbox" data-f="enabled" ${f.enabled ? "checked" : ""} aria-label="启用"></td>
    <td><input data-f="name" value="${esc(f.name)}" aria-label="名称"></td>
    <td><input data-f="id" value="${esc(f.id)}" ${isNew ? 'placeholder="英文 ID"' : "readonly"} aria-label="ID"></td>
    <td><input data-f="url" value="${esc(f.url)}" placeholder="https://…/feed.xml" aria-label="订阅地址"></td>
    <td><input data-f="max_age_days" type="number" min="0" class="w5" value="${f.max_age_days ?? ""}" placeholder="默认" aria-label="天数"></td>
    <td><button class="link" data-del-row>删除</button></td></tr>`;
  $("plat-add").onclick = () => $("plat-table").querySelector("tbody").insertAdjacentHTML("beforeend", platRow({ id: "", name: "", expected_domain: "", enabled: true }, 0, true));
  $("feed-add").onclick = () => $("feed-table").querySelector("tbody").insertAdjacentHTML("beforeend", feedRow({ id: "", name: "", url: "", enabled: true, max_age_days: null }, true));
  document.querySelector('[data-panel="sources"]').addEventListener("click", e => { const d = e.target.closest("[data-del-row]"); if (d) d.closest("tr").remove(); });
  function readRows(table) {
    return [...$(table).querySelectorAll("tr[data-row]")].map(tr => {
      const o = {};
      tr.querySelectorAll("[data-f]").forEach(i => { o[i.dataset.f] = i.type === "checkbox" ? i.checked : i.value.trim(); });
      if ("max_age_days" in o) o.max_age_days = o.max_age_days === "" ? null : +o.max_age_days;
      return o;
    }).filter(o => o.id || o.name || o.url);
  }
  function renderStandalone() {
    const sel = new Set(cfgCache.values["display.standalone.platforms"] || []);
    $("standalone-plats").innerHTML = (cfgCache.values["platforms.sources"] || []).map(p =>
      `<label class="switch"><input type="checkbox" data-standalone="${esc(p.id)}" ${sel.has(p.id) ? "checked" : ""}><span>${esc(p.name)}</span></label>`).join("") || '<span class="muted">（没有平台）</span>';
  }
  function presetDesc() {
    const p = (cfgCache.presets || []).find(x => x.id === $("preset-select").value);
    $("preset-desc").textContent = p ? p.description : "";
  }
  $("preset-select").onchange = presetDesc;

  // 推送渠道
  const CHANNELS = [
    { name: "飞书", help: "飞书群 → 设置 → 群机器人 → 添加机器人 → 自定义机器人，复制 Webhook 地址。", f: [{ s: "feishu_webhook_url" }] },
    { name: "钉钉", help: "钉钉电脑版 → 群设置 → 机器人 → 添加机器人 → 自定义；安全设置选「自定义关键词」并填「热点」（推送标题里一定有这个词），复制 Webhook 地址。", f: [{ s: "dingtalk_webhook_url" }] },
    { name: "企业微信", help: "企业微信群 → 右上角「…」→「消息推送」→「添加」，复制 Webhook 地址；推送到个人微信时消息类型选 text。", f: [{ s: "wework_webhook_url" }, { k: "notification.channels.wework.msg_type", label: "消息类型" }] },
    { name: "Telegram", help: "找 @BotFather 建机器人拿 Token；Chat ID 可从 @userinfobot 获取。", f: [{ s: "telegram_bot_token" }, { s: "telegram_chat_id" }] },
    { name: "邮件", help: "密码一般填邮箱的「授权码」，不是登录密码；SMTP 留空时按邮箱域名自动识别。", f: [{ s: "email_from" }, { s: "email_password" }, { s: "email_to" }, { k: "notification.channels.email.smtp_server", label: "SMTP 服务器（可空）" }, { k: "notification.channels.email.smtp_port", label: "SMTP 端口（可空）" }] },
    { name: "ntfy", help: "开源推送服务，可用公共的 ntfy.sh 或自建。", f: [{ k: "notification.channels.ntfy.server_url", label: "服务器地址" }, { s: "ntfy_topic" }, { s: "ntfy_token" }] },
    { name: "Bark", help: "iOS 推送 App，地址形如 https://api.day.app/你的设备码。", f: [{ s: "bark_url" }] },
    { name: "Slack", help: "Slack 的 Incoming Webhook 地址。", f: [{ s: "slack_webhook_url" }] },
    { name: "通用 Webhook", help: "Discord、Matrix、IFTTT 等；模板为 JSON，支持 {title} 和 {content} 占位符，留空用默认格式。", f: [{ s: "generic_webhook_url" }, { s: "generic_webhook_template" }] },
  ];
  const secretWidget = (key, info) => `<span class="secret"><input type="password" autocomplete="off" data-secret-input="${esc(key)}" placeholder="${info.set ? "已填写，留空表示不修改" : "未填写"}" aria-label="${esc(info.label)}">
    <span class="state${info.set ? " set" : ""}">${esc(info.display)}</span>${info.set ? `<button class="btn small" type="button" data-clear="${esc(key)}">清除</button>` : ""}</span>
    ${info.in_config_yaml ? '<small class="muted">config.yaml 里也写了这个值；应用里填写的优先</small>' : ""}`;
  function renderChannels(c) {
    const S = c.secrets;
    $("push-channels").innerHTML = CHANNELS.map(ch => {
      const filled = ch.f.some(f => f.s && S[f.s].set);
      return `<div class="channel"><h3>${esc(ch.name)}<span class="chip ${filled ? "ok" : ""}">${filled ? "已配置" : "未配置"}</span></h3>
        <p class="muted">${esc(ch.help)}</p><div class="form-grid">${ch.f.map(f => f.s
          ? `<label>${esc(S[f.s].label)} ${secretWidget(f.s, S[f.s])}</label>`
          : `<label>${esc(f.label)} ${f.k.endsWith("msg_type") ? `<select data-k="${f.k}"></select>` : `<input data-k="${f.k}">`}</label>`).join("")}</div></div>`;
    }).join("");
    // 新生成的 data-k 控件要再填一次值
    $("push-channels").querySelectorAll("[data-k]").forEach(el => {
      const k = el.dataset.k;
      if (el.tagName === "SELECT") el.innerHTML = (c.enums[k] || []).map(o => `<option value="${esc(o)}">${esc((ENUM_LABELS[k] || {})[o] || o)}</option>`).join("");
      el.value = c.values[k] ?? "";
    });
    document.querySelector('[data-secret="ai_api_key"]').innerHTML = secretWidget("ai_api_key", S.ai_api_key);
  }
  document.querySelector(".cfg-body").addEventListener("click", async e => {
    const c = e.target.closest("[data-clear]");
    if (!c) return;
    if (!confirm("清除已保存的「" + cfgCache.secrets[c.dataset.clear].label + "」？")) return;
    try { applyConfig(await api("/api/secrets", { changes: { [c.dataset.clear]: null } })); cfgMsg("已清除"); }
    catch (err) { cfgMsg("清除失败：" + err.message, "error"); }
  });

  function collect(panel) {
    const changes = {};
    document.querySelector(`[data-panel="${panel}"]`).querySelectorAll("[data-k]").forEach(el => {
      const k = el.dataset.k;
      if (el.type === "checkbox") changes[k] = el.checked;
      else if (el.type === "number") { if (el.value !== "") changes[k] = +el.value; }
      else changes[k] = el.value;
    });
    return changes;
  }
  async function saveConfig(changes, okMsg) {
    try { applyConfig(await api("/api/config", { changes })); cfgMsg(okMsg || "已保存，下一次抓取生效"); return true; }
    catch (e) { cfgMsg("没有保存：" + e.message, "error"); return false; }
  }
  document.querySelector(".cfg-body").addEventListener("click", async e => {
    const b = e.target.closest("[data-save]");
    if (!b) return;
    const panel = b.dataset.save;
    if (panel === "filter") {
      const m = document.querySelector('input[name="filter-method"]:checked');
      return saveConfig({ "filter.method": m ? m.value : "keyword" }, m && m.value === "ai" ? "已改为 AI 智能筛选：记得在「AI 分析与翻译」填好模型和 Key" : "已改为关键词匹配");
    }
    const changes = collect(panel);
    if (panel === "sources") {
      changes["platforms.sources"] = readRows("plat-table");
      changes["rss.feeds"] = readRows("feed-table");
    }
    if (panel === "report") changes["display.standalone.platforms"] = [...document.querySelectorAll("[data-standalone]:checked")].map(x => x.dataset.standalone);
    if (panel === "push" || panel === "ai") {
      const sec = {};
      document.querySelector(`[data-panel="${panel}"]`).querySelectorAll("[data-secret-input]").forEach(i => { if (i.value.trim()) sec[i.dataset.secretInput] = i.value.trim(); });
      if (Object.keys(sec).length) {
        try { await api("/api/secrets", { changes: sec }); } catch (err) { return cfgMsg("密钥没有保存：" + err.message, "error"); }
      }
    }
    saveConfig(changes);
  });

  // 关键词
  $("kw-text").addEventListener("input", () => { $("kw-dirty").textContent = $("kw-text").value !== kwSaved ? "（有未保存的修改）" : ""; });
  $("kw-save").onclick = async () => {
    try {
      applyConfig(await api("/api/keywords", { text: $("kw-text").value }));
      cfgMsg("关键词已保存（旧文件备份为 frequency_words.txt.bak），下一次抓取生效");
    } catch (e) { cfgMsg("没有保存：" + e.message, "error"); }
  };
  $("kw-revert").onclick = () => { $("kw-text").value = kwSaved; $("kw-dirty").textContent = ""; checkKeywords(true); };
  $("kw-check").onclick = () => checkKeywords(false);
  $("kw-try").onclick = () => checkKeywords(false, true);
  async function checkKeywords(quiet, withTitles) {
    const titles = withTitles ? $("kw-titles").value.split("\n").map(s => s.trim()).filter(Boolean) : [];
    try {
      const r = await api("/api/keywords/check", { text: $("kw-text").value, titles });
      const s = r.summary;
      const w = x => `<span class="w${x.regex ? " re" : ""}" title="${x.regex ? "正则" : "普通词"}">${esc(x.regex ? "/" + x.word + "/" : x.word)}${x.display ? " → " + esc(x.display) : ""}</span>`;
      $("kw-parse").innerHTML = (s.warnings.length ? `<div class="banner warn small">${s.warnings.map(esc).join("<br>")}</div>` : "")
        + `<p class="muted">共 ${s.groups.length} 个词组、${s.filters.length} 个过滤词、${s.global_filters.length} 个全局过滤词${s.groups.length ? "" : "（没有词组时上游会显示全部新闻）"}</p>`
        + (s.global_filters.length ? `<div class="grp"><b>全局过滤</b>${s.global_filters.map(w).join("")}</div>` : "")
        + (s.filters.length ? `<div class="grp"><b>过滤词</b>${s.filters.map(w).join("")}</div>` : "")
        + s.groups.map(g => `<div class="grp"><b>${esc(g.name)}</b>${g.normal.map(w).join("")}${g.required.map(x => w(x).replace('class="w', 'class="w req')).join("")}${g.max_count ? ` <span class="muted">最多 ${g.max_count} 条</span>` : ""}</div>`).join("");
      if (withTitles) {
        $("kw-try-out").innerHTML = r.results.length ? `<table class="data small"><tbody>${r.results.map(x => `<tr><td>${x.matched ? '<span class="st ok">✓ 命中</span>' : '<span class="st failed">✗</span>'}</td><td>${esc(x.title)}</td><td class="muted">${x.matched ? "词组：" + esc(x.groups.join("、")) : esc(x.reason)}</td></tr>`).join("")}</tbody></table>` : '<p class="muted">先在上面输入标题，每行一个。</p>';
      }
      if (!quiet && !withTitles) cfgMsg(s.warnings.length ? "检查完成，有警告（见解析结果）" : "语法检查通过", s.warnings.length ? "warn" : "ok");
    } catch (e) { if (!quiet) cfgMsg("检查失败：" + e.message, "error"); }
  }
  $("kw-add-btn").onclick = () => { $("kw-add").hidden = !$("kw-add").hidden; };
  $("kw-add-ok").onclick = () => {
    const name = $("kw-add-name").value.trim();
    const split = s => s.split(/[,，、\s]+/).map(x => x.trim()).filter(Boolean);
    const words = split($("kw-add-words").value), req = split($("kw-add-req").value), not = split($("kw-add-not").value);
    if (!words.length && !req.length) return cfgMsg("至少填一个关键词", "error");
    const block = [name ? `[${name}]` : "", ...words, ...req.map(x => "+" + x), ...not.map(x => "!" + x)].filter(Boolean).join("\n");
    const t = $("kw-text").value.replace(/\s+$/, "");
    $("kw-text").value = t + "\n\n" + block + "\n";
    $("kw-text").dispatchEvent(new Event("input"));
    ["kw-add-name", "kw-add-words", "kw-add-req", "kw-add-not"].forEach(id => { $(id).value = ""; });
    $("kw-add").hidden = true;
    checkKeywords(true);
    cfgMsg("已加到文件末尾，检查无误后点「保存关键词」", "warn");
  };
  $("interests-save").onclick = async () => {
    try {
      await api("/api/interests", { text: $("ai-interests").value });
      await saveConfig(collect("keywords"), "AI 兴趣描述和参数已保存");
    } catch (e) { cfgMsg("没有保存：" + e.message, "error"); }
  };

  // ---------------------------------------------------------------- MCP
  function renderMcp(m) {
    if (!m) return;
    if (document.activeElement !== $("mcp-port")) $("mcp-port").value = m.port;
    $("mcp-on").checked = m.running;
    $("mcp-state").textContent = m.running ? `运行中 · PID ${m.pid} · ${m.url}` : (m.exit_code != null ? `已停止（上次退出码 ${m.exit_code}，见日志）` : "未运行");
    $("mcp-howto").textContent = `地址：${m.url}（Streamable HTTP）
Cherry Studio：设置 → MCP 服务器 → 添加 → 类型选「可流式传输的 HTTP」，URL 填上面的地址。
Claude Desktop 等只支持本地命令的客户端：可用 mcp-remote 转接，例如
  {"mcpServers": {"hotradar": {"command": "npx", "args": ["mcp-remote", "${m.url}"]}}}
${m.note}。`;
  }
  async function refreshMcpLog() { try { $("mcp-log").textContent = await api("/api/mcp-log") || "（还没有日志）"; } catch { /* 忽略 */ } }
  $("mcp-on").onchange = async () => {
    try {
      const m = await api("/api/mcp", { enabled: $("mcp-on").checked, port: +$("mcp-port").value || undefined });
      renderMcp(m); toast(m.running ? "MCP 服务已启动" : "MCP 服务已停止");
      setTimeout(refreshMcpLog, 2500); refreshStatus();
    } catch (e) { toast("操作失败：" + e.message, 6000); $("mcp-on").checked = !$("mcp-on").checked; }
  };

  // ---------------------------------------------------------------- 运行状态：后台任务
  function renderBg() {
    if (!status) return;
    const s = status, j = s.job;
    $("bg-body").innerHTML = `<table class="data"><tbody>
      <tr><td>抓取任务</td><td>${j ? `<b>运行中</b>：${esc(j.label)}，工作进程 PID ${j.pid}，${esc(j.stage)} ${j.percent}%` : "没有正在运行的任务"}</td></tr>
      <tr><td>定时抓取</td><td>${s.schedule.enabled ? `开，每 ${s.schedule.interval_min} 分钟，下次 ${esc(fmtTime(s.schedule.next_at))}` : "关"}。${esc(s.schedule.note)}</td></tr>
      <tr><td>MCP 服务</td><td>${s.mcp.running ? `运行中，PID ${s.mcp.pid}，${esc(s.mcp.url)}` : "关"}</td></tr>
      <tr><td>本机服务</td><td>${esc(location.host)}（只监听 127.0.0.1，主进程里的线程）</td></tr>
      <tr><td>外部依赖</td><td>newsnow 热榜接口（第三方公开服务，不是本机进程）；开启 AI 或推送时还会访问对应服务商</td></tr>
    </tbody></table>`;
  }

  // ---------------------------------------------------------------- 退出
  $("quit-btn").onclick = async () => {
    const busy = status && (status.job || status.mcp.running);
    if (!confirm(busy ? "退出会停止正在进行的任务和 MCP 服务，确定退出？" : "退出热点雷达？")) return;
    try { await api("/api/quit", {}); } catch { /* 服务已停 */ }
    document.body.innerHTML = '<div style="padding:4rem;text-align:center;font:16px sans-serif;color:#666">热点雷达已退出，后台任务已全部停止。可以关闭这个页面了。</div>';
  };

  // ---------------------------------------------------------------- 启动
  const guide = window.HGuide({ api, toast, switchTab, esc, fmtTime });
  window.addEventListener("hotradar-zoom", () => { /* iframe 由 CSS 变量自动缩放，这里不用处理 */ });
  switchTab(current);
  refreshStatus();
  api("/api/config").then(c => { if (!cfgCache) applyConfig(c); }).catch(() => {});
})();
