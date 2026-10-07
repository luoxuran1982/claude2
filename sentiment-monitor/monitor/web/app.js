"use strict";
/* 舆情监控 前端：原生 JS，无第三方依赖。哈希路由 + SSE 实时推送 + SVG 图表。 */

// ------------------------------------------------------------------ 工具
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LABEL = { pos: "利好", neg: "利空", neu: "中性" };
const ARROW = { pos: "▲", neg: "▼", neu: "●" };
const TITLES = { overview: "总览", feed: "资讯流", entities: "监控对象", sources: "数据源", alerts: "预警中心", settings: "设置" };
const TZ = -new Date().getTimezoneOffset();

const state = {
  boot: null, settings: {}, entities: [], sources: [], quotes: {}, unread: 0,
  page: "overview", query: {}, feedNew: 0, shutdown: false, es: null, downSince: 0,
};

async function api(method, path, body) {
  const opt = { method, headers: { "X-Monitor": "1" } };
  if (body !== undefined) {
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || r.statusText);
  return data;
}
const GET = (p) => api("GET", p);
const POST = (p, b = {}) => api("POST", p, b);
const DEL = (p) => api("DELETE", p);

function qstr(obj) {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(obj)) if (v !== "" && v != null && v !== false) p.set(k, v === true ? "1" : v);
  const s = p.toString();
  return s ? "?" + s : "";
}

function fmtTime(ts) {
  const d = new Date(ts * 1000), now = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  if (d.getFullYear() === now.getFullYear()) return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`;
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
}
function ago(ts) {
  if (!ts) return "从未";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return `${Math.floor(s / 60)} 分钟前`;
  if (s < 86400) return `${Math.floor(s / 3600)} 小时前`;
  return fmtTime(ts);
}
const idx100 = (v) => (v == null ? "—" : (v * 100).toFixed(0));
const sentClass = (v) => (v == null ? "" : v >= 0.15 ? "up" : v <= -0.15 ? "down" : "");
function labelTag(label, score) {
  const sc = score != null ? ` ${idx100(score)}` : "";
  return `<span class="tag ${label}" title="情感得分（-100 ~ 100）">${ARROW[label]} ${LABEL[label]}${sc}</span>`;
}
function debounce(fn, ms) {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
}

function toast(msg, kind = "", onclick) {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.innerHTML = msg;
  el.onclick = () => { el.remove(); onclick && onclick(); };
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), kind === "danger" ? 12000 : 6000);
  while ($("#toasts").children.length > 5) $("#toasts").firstChild.remove();
}
function errToast(e) { toast(esc(e.message || e), "error"); }

// ------------------------------------------------------------------ 主题与配色
function applyTheme() {
  let t = null;
  try { t = localStorage.getItem("theme"); } catch (e) { /* 无痕模式 */ }
  if (t) document.documentElement.dataset.theme = t;
  else delete document.documentElement.dataset.theme;
}
function applyColors() {
  document.documentElement.dataset.colors = state.settings.color_scheme === "us" ? "us" : "cn";
}
function cssVar(name) {
  return getComputedStyle($(".app")).getPropertyValue(name).trim();
}

// ------------------------------------------------------------------ 图表（SVG）
const tipEl = () => $("#tip");
function showTip(ev, html) {
  const t = tipEl();
  t.innerHTML = html;
  t.hidden = false;
  const w = t.offsetWidth, h = t.offsetHeight;
  let x = ev.clientX + 14, y = ev.clientY + 14;
  if (x + w > innerWidth - 8) x = ev.clientX - w - 14;
  if (y + h > innerHeight - 8) y = ev.clientY - h - 14;
  t.style.left = x + "px";
  t.style.top = y + "px";
}
function hideTip() { tipEl().hidden = true; }

/** 纵轴取整：4 格，每格是 1/2/5×10^n，保证刻度都是整数 */
function niceMax(v) {
  const raw = Math.max(1, v / 4);
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 5, 10]) if (m * p >= raw) return m * p * 4;
  return raw * 4;
}
const dayLabel = (d) => d.slice(5);

/** 堆叠柱状图：rows=[{day,pos,neu,neg}]，series=[{key,label,color}] */
function stackedBars(el, rows, series) {
  const W = Math.max(280, el.clientWidth), H = 220, m = { l: 34, r: 8, t: 8, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const totals = rows.map((r) => series.reduce((s, x) => s + (r[x.key] || 0), 0));
  const max = niceMax(Math.max(1, ...totals));
  const band = iw / rows.length, bw = Math.max(4, Math.min(28, band * 0.62));
  const y = (v) => m.t + ih - (v / max) * ih;
  let g = "";
  for (let i = 0; i <= 4; i++) {
    const v = (max / 4) * i, yy = y(v);
    g += `<line class="grid-line" x1="${m.l}" x2="${W - m.r}" y1="${yy}" y2="${yy}"/><text x="${m.l - 6}" y="${yy + 4}" text-anchor="end">${Math.round(v)}</text>`;
  }
  let bars = "", hits = "", xl = "";
  const step = Math.ceil(rows.length / Math.max(2, Math.floor(iw / 48)));
  rows.forEach((r, i) => {
    const x = m.l + band * i + (band - bw) / 2;
    let acc = 0;
    const segs = series.filter((s) => r[s.key] > 0);
    segs.forEach((s, j) => {
      const v = r[s.key];
      const y0 = y(acc), y1 = y(acc + v);
      const top = j === segs.length - 1;
      const hgt = Math.max(0, y0 - y1 - (j > 0 ? 2 : 0)); // 段与段之间留 2px 间隙
      bars += top ? `<path d="${roundTop(x, y1, bw, hgt, Math.min(4, bw / 2, hgt))}" fill="${s.color}"/>`
        : `<rect x="${x}" y="${y1}" width="${bw}" height="${hgt}" fill="${s.color}"/>`;
      acc += v;
    });
    hits += `<rect class="hover-col" data-i="${i}" x="${m.l + band * i}" y="${m.t}" width="${band}" height="${ih}"/>`;
    if (i % step === 0 || i === rows.length - 1) xl += `<text x="${m.l + band * i + band / 2}" y="${H - 6}" text-anchor="middle">${dayLabel(r.day)}</text>`;
  });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="每日资讯量"><g class="axis">${g}${xl}</g><line class="zero-line" x1="${m.l}" x2="${W - m.r}" y1="${y(0)}" y2="${y(0)}"/>${bars}${hits}</svg>`;
  $$(".hover-col", el).forEach((rc) => {
    rc.addEventListener("mousemove", (ev) => {
      const r = rows[+rc.dataset.i];
      const total = totals[+rc.dataset.i];
      showTip(ev, `<b>${r.day}</b>` + series.map((s) => `<div class="row"><span><i style="background:${s.color}"></i>${s.label}</span><span class="num">${r[s.key] || 0}</span></div>`).join("") + `<div class="row"><span>合计</span><span class="num">${total}</span></div>`);
    });
    rc.addEventListener("mouseleave", hideTip);
  });
}
function roundTop(x, y, w, h, r) {
  if (h <= 0) return "";
  r = Math.max(0, Math.min(r, h));
  return `M${x},${y + h}V${y + r}Q${x},${y} ${x + r},${y}H${x + w - r}Q${x + w},${y} ${x + w},${y + r}V${y + h}Z`;
}

/** 折线图：rows=[{day, avg}]，值域 -1..1，显示为 -100..100，带十字准线 */
function lineChart(el, rows, color) {
  const W = Math.max(280, el.clientWidth), H = 220, m = { l: 34, r: 12, t: 10, b: 24 };
  const iw = W - m.l - m.r, ih = H - m.t - m.b;
  const n = rows.length;
  const x = (i) => m.l + (n === 1 ? iw / 2 : (iw * i) / (n - 1));
  const y = (v) => m.t + ih / 2 - (v / 100) * (ih / 2);
  let g = "";
  for (const v of [-100, -50, 0, 50, 100]) {
    g += `<line class="${v === 0 ? "zero-line" : "grid-line"}" x1="${m.l}" x2="${W - m.r}" y1="${y(v)}" y2="${y(v)}"/><text x="${m.l - 6}" y="${y(v) + 4}" text-anchor="end">${v}</text>`;
  }
  let path = "", pen = false, dots = "";
  rows.forEach((r, i) => {
    if (r.avg == null) { pen = false; return; }
    const v = r.avg * 100;
    path += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`;
    pen = true;
    const prev = rows[i - 1], next = rows[i + 1];
    if ((!prev || prev.avg == null) && (!next || next.avg == null)) dots += `<circle cx="${x(i)}" cy="${y(v)}" r="3" fill="${color}"/>`;
  });
  let xl = "";
  const step = Math.ceil(n / Math.max(2, Math.floor(iw / 48)));
  rows.forEach((r, i) => { if (i % step === 0 || i === n - 1) xl += `<text x="${x(i)}" y="${H - 6}" text-anchor="middle">${dayLabel(r.day)}</text>`; });
  el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" height="${H}" role="img" aria-label="情绪指数"><g class="axis">${g}${xl}</g>
    <path d="${path}" fill="none" stroke="${color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>${dots}
    <line class="cross" x1="0" x2="0" y1="${m.t}" y2="${m.t + ih}" stroke="var(--muted)" stroke-dasharray="3 3" visibility="hidden"/>
    <circle class="cross-dot" r="5" fill="${color}" stroke="var(--surface)" stroke-width="2" visibility="hidden"/>
    <rect class="cap" x="${m.l}" y="${m.t}" width="${iw}" height="${ih}" fill="transparent"/></svg>`;
  const svg = $("svg", el), cross = $(".cross", el), dot = $(".cross-dot", el);
  $(".cap", el).addEventListener("mousemove", (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = ((ev.clientX - rect.left) / rect.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - m.l) / iw) * (n - 1))));
    const r = rows[i];
    cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
    if (r.avg != null) { dot.setAttribute("cx", x(i)); dot.setAttribute("cy", y(r.avg * 100)); dot.setAttribute("visibility", "visible"); }
    else dot.setAttribute("visibility", "hidden");
    const total = (r.pos || 0) + (r.neu || 0) + (r.neg || 0);
    showTip(ev, `<b>${r.day}</b><div class="row"><span>情绪指数</span><span class="num">${idx100(r.avg)}</span></div><div class="row"><span>资讯数</span><span class="num">${total}</span></div>`);
  });
  $(".cap", el).addEventListener("mouseleave", () => { hideTip(); cross.setAttribute("visibility", "hidden"); dot.setAttribute("visibility", "hidden"); });
}

function sentimentSeries() {
  return [
    { key: "pos", label: "利好", color: cssVar("--c-up") },
    { key: "neu", label: "中性", color: cssVar("--neutral") },
    { key: "neg", label: "利空", color: cssVar("--c-down") },
  ];
}
function legendHTML(series) {
  return `<div class="legend">${series.map((s) => `<span><i style="background:${s.color}"></i>${s.label}</span>`).join("")}</div>`;
}
function drawTrend(barEl, lineEl, rows) {
  const series = sentimentSeries();
  barEl.previousElementSibling.outerHTML = legendHTML(series);
  stackedBars(barEl, rows, series);
  lineChart(lineEl, rows, cssVar("--accent"));
}

// ------------------------------------------------------------------ 公共片段
function quoteHTML(eid) {
  const q = state.quotes[eid];
  if (!q) return `<span class="muted">—</span>`;
  const cls = q.pct > 0 ? "up" : q.pct < 0 ? "down" : "";
  const sign = q.pct > 0 ? "+" : "";
  return `<span class="quote num ${cls}">${q.price} <small>${sign}${q.pct.toFixed(2)}%</small></span>`;
}
function entityCode(e) {
  const mk = { A: "A股", HK: "港股", US: "美股" }[e.market] || "";
  return [e.code, mk].filter(Boolean).join(" · ");
}
function articleHTML(a, fresh) {
  const ents = (a.entities || []).map((e) => `<span class="tag ent" data-ent="${e.id}">${esc(e.name)}</span>`).join("");
  const risks = (a.risk || "").split(",").filter(Boolean).map((r) => `<span class="tag risk">⚠ ${esc(r)}</span>`).join("");
  const demo = a.source_name === "演示数据" ? `<span class="tag demo">演示数据</span>` : "";
  const title = a.url ? `<a class="t" href="${esc(a.url)}" target="_blank" rel="noopener noreferrer">${esc(a.title)}</a>` : `<span class="t">${esc(a.title)}</span>`;
  return `<div class="item${fresh ? " fresh" : ""}">
    <div class="score">${labelTag(a.label)}<div class="num ${sentClass(a.score)}" title="情感得分">${idx100(a.score)}</div></div>
    <div>${title}${a.summary ? `<div class="s">${esc(a.summary)}</div>` : ""}
      <div class="m"><span>${fmtTime(a.published_at)}</span><span>${esc(a.source_name)}${a.media && a.media !== a.source_name ? " · " + esc(a.media) : ""}</span>${ents}${risks}${demo}
      ${a.hits ? `<span title="命中的情感词（+ 正面，- 负面）">词：${esc(a.hits)}</span>` : ""}</div>
    </div></div>`;
}
function bindEntityTags(root) {
  $$(".tag.ent", root).forEach((t) => t.addEventListener("click", () => go("feed", { entity: t.dataset.ent })));
}
function alertHTML(al) {
  const icon = al.kind === "risk" ? "!" : al.kind === "spike" ? "↑" : "▼";
  const link = al.url ? ` · <a href="${esc(al.url)}" target="_blank" rel="noopener noreferrer">原文</a>` : "";
  const kind = { risk: "风险信号", negative: "负面舆情", spike: "热度异动" }[al.kind] || al.kind;
  return `<div class="alert-row ${al.is_read ? "" : "unread"}" data-id="${al.id}">
    <div class="ic ${al.level}" title="${kind}">${icon}</div>
    <div><div class="msg">${esc(al.message)}</div><div class="meta">${kind} · ${fmtTime(al.created_at)}${al.source_name ? " · " + esc(al.source_name) : ""}${al.score != null && al.article_id ? " · 得分 " + idx100(al.score) : ""}${link}</div></div>
    <div>${al.is_read ? "" : `<button class="btn small" data-read="${al.id}">已读</button>`}</div></div>`;
}

// ------------------------------------------------------------------ 路由
function parseHash() {
  const h = location.hash.replace(/^#\/?/, "");
  const [page, qs] = h.split("?");
  return { page: TITLES[page] ? page : "overview", query: Object.fromEntries(new URLSearchParams(qs || "")) };
}
function go(page, query = {}) {
  location.hash = `#/${page}${qstr(query)}`;
}
async function route() {
  const { page, query } = parseHash();
  state.page = page;
  state.query = query;
  closeModal();
  $$(".nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $("#pageTitle").textContent = TITLES[page];
  document.title = `${TITLES[page]} · 舆情监控`;
  hideTip();
  try {
    await PAGES[page](query);
  } catch (e) {
    $("#view").innerHTML = `<div class="card empty">加载失败：${esc(e.message)}</div>`;
  }
}

// ------------------------------------------------------------------ 页面：总览
const PAGES = {};
PAGES.overview = async () => {
  const v = $("#view");
  if (!$("#ov", v)) {
    v.innerHTML = `<div id="ov">
      <div class="grid kpis" id="kpis"></div>
      <div class="grid cols-2">
        <div class="card"><h3>每日资讯量</h3><div class="sub">近 14 天，按情感分类</div><div class="legend"></div><div class="chart" id="chBars"></div></div>
        <div class="card"><h3>情绪指数</h3><div class="sub">每日平均情感得分，-100 极度负面 ~ 100 极度正面</div><div class="chart" id="chLine"></div></div>
      </div>
      <div class="grid cols-3-1 stack">
        <div class="card"><div class="card-head"><h3>监控对象 · 近 24 小时</h3><a href="#/entities">管理</a></div><div class="sub">点击一行查看该对象的资讯</div><div class="table-wrap" id="board"></div></div>
        <div class="grid" style="align-content:start">
          <div class="card"><div class="card-head"><h3>最新预警</h3><a href="#/alerts">全部</a></div><div id="ovAlerts"></div></div>
          <div class="card"><h3>来源分布</h3><div class="sub">近 24 小时</div><div id="srcDist"></div></div>
        </div>
      </div></div>`;
  }
  const d = await GET(`/api/overview?tz=${TZ}&days=14`);
  state.ovData = d;
  const o = d.overview;
  const delta = (a, b) => {
    if (!b) return a ? "前 24 小时无数据" : "";
    const p = ((a - b) / b) * 100;
    return `较前 24 小时 ${p >= 0 ? "+" : ""}${p.toFixed(0)}%`;
  };
  $("#kpis").innerHTML = [
    ["资讯数（24h）", o.count_24h, delta(o.count_24h, o.count_prev)],
    ["利好", `<span class="up">${o.pos_24h}</span>`, o.count_24h ? `占 ${((o.pos_24h / o.count_24h) * 100).toFixed(0)}%` : ""],
    ["利空", `<span class="down">${o.neg_24h}</span>`, o.count_24h ? `占 ${((o.neg_24h / o.count_24h) * 100).toFixed(0)}%` : ""],
    ["情绪指数", `<span class="${sentClass(o.avg_24h)}">${o.count_24h ? idx100(o.avg_24h) : "—"}</span>`, o.count_prev ? `前 24 小时 ${idx100(o.avg_prev)}` : ""],
    ["风险信号", o.risk_24h, `未读预警 ${o.unread_alerts} 条`],
  ].map(([l, val, dl]) => `<div class="card kpi"><div class="label">${l}</div><div class="value num">${val}</div><div class="delta">${dl || "&nbsp;"}</div></div>`).join("");
  drawTrend($("#chBars"), $("#chLine"), d.trend);
  state.entities = d.entities;
  renderBoard();
  const maxS = Math.max(1, ...d.sources.map((s) => s.n));
  $("#srcDist").innerHTML = d.sources.length ? `<table><tbody>${d.sources.map((s) => `<tr><td>${esc(s.name)}</td><td style="width:45%"><div class="bar-cell"><div class="bar" style="width:${(s.n / maxS) * 100}%"></div><span class="num">${s.n}</span></div></td></tr>`).join("")}</tbody></table>` : `<div class="empty">暂无数据</div>`;
  $("#ovAlerts").innerHTML = d.alerts.length ? d.alerts.slice(0, 6).map(alertHTML).join("") : `<div class="empty">暂无预警</div>`;
  bindAlertButtons($("#ovAlerts"));
  if (!o.total) {
    $("#kpis").insertAdjacentHTML("beforebegin", `<div class="card" id="firstRun" style="margin-bottom:16px">还没有数据。后台正在按数据源的间隔抓取（首次约 1 分钟内出结果）。也可以：<button class="btn small primary" id="btnFetchNow">立即抓取</button> <button class="btn small" id="btnDemo">先看演示数据</button></div>`);
    $("#btnFetchNow").onclick = () => { POST("/api/refresh"); toast("已开始抓取，结果会实时出现"); };
    $("#btnDemo").onclick = async () => { const r = await POST("/api/demo"); toast(`已生成 ${r.count} 条演示数据`); $("#firstRun").remove(); route(); };
  } else $("#firstRun")?.remove();
  refreshQuotes();
};

function renderBoard() {
  const el = $("#board");
  if (!el) return;
  const rows = state.entities;
  if (!rows.length) { el.innerHTML = `<div class="empty">还没有监控对象，<a href="#/entities">去添加</a></div>`; return; }
  const maxN = Math.max(1, ...rows.map((r) => r.n24));
  el.innerHTML = `<table><thead><tr><th>对象</th><th class="r">行情</th><th>热度（24h）</th><th class="r">较前日</th><th class="r">情绪</th><th class="r">利好 / 利空</th></tr></thead><tbody>
    ${rows.map((r) => {
      const dd = r.n24 - r.nprev;
      return `<tr class="clickable" data-id="${r.id}"><td><b>${esc(r.name)}</b>${r.enabled ? "" : ' <span class="tag off">已停用</span>'}<div class="muted" style="font-size:12px">${esc(entityCode(r)) || "关键词"}</div></td>
      <td class="r">${quoteHTML(r.id)}</td>
      <td><div class="bar-cell"><div class="bar" style="width:${(r.n24 / maxN) * 120}px"></div><span class="num">${r.n24}</span></div></td>
      <td class="r num">${dd > 0 ? "+" : ""}${dd}</td>
      <td class="r num ${sentClass(r.avg24)}">${r.n24 ? idx100(r.avg24) : "—"}</td>
      <td class="r num"><span class="up">${r.pos24}</span> / <span class="down">${r.neg24}</span></td></tr>`;
    }).join("")}</tbody></table>`;
  $$("tr.clickable", el).forEach((tr) => (tr.onclick = () => go("feed", { entity: tr.dataset.id })));
}

let quoteTimer = null;
async function refreshQuotes() {
  clearTimeout(quoteTimer);
  if (!["overview", "entities"].includes(state.page) || state.shutdown) return;
  try {
    const r = await GET("/api/quotes");
    state.quotes = r.quotes;
    if (state.page === "overview") renderBoard();
    else if (state.page === "entities") renderEntityTable();
  } catch (e) { /* 行情失败不影响主功能 */ }
  quoteTimer = setTimeout(refreshQuotes, Math.max(10, state.settings.quote_refresh_sec || 30) * 1000);
}

// ------------------------------------------------------------------ 页面：资讯流
const feed = { items: [], total: 0, loading: false };
PAGES.feed = async (q) => {
  if (!state.sources.length) state.sources = await GET("/api/sources");
  if (!state.entities.length) state.entities = await GET("/api/entities");
  const opt = (list, cur, all) => `<option value="">${all}</option>` + list.map((x) => `<option value="${x.id}" ${String(x.id) === String(cur) ? "selected" : ""}>${esc(x.name)}</option>`).join("");
  $("#view").innerHTML = `
    <div class="toolbar">
      <select id="fEntity" title="监控对象">${opt(state.entities, q.entity, "全部对象")}</select>
      <select id="fLabel" title="情感">${opt([{ id: "pos", name: "利好" }, { id: "neg", name: "利空" }, { id: "neu", name: "中性" }], q.label, "全部情感")}</select>
      <select id="fSource" title="来源">${opt(state.sources, q.source, "全部来源")}</select>
      <select id="fDays" title="时间范围">${opt([{ id: 1, name: "近 24 小时" }, { id: 3, name: "近 3 天" }, { id: 7, name: "近 7 天" }, { id: 30, name: "近 30 天" }], q.days, "全部时间")}</select>
      <label class="chk"><input type="checkbox" id="fRisk" ${q.risk ? "checked" : ""}> 只看风险</label>
      <label class="chk"><input type="checkbox" id="fMatched" ${q.matched ? "checked" : ""}> 只看命中对象</label>
      <input type="search" class="grow" id="fQ" placeholder="搜索标题或摘要，回车" value="${esc(q.q || "")}">
      <a class="btn" id="fExport" title="导出为 Excel 可打开的 CSV">导出 CSV</a>
    </div>
    <div class="new-bar" id="newBar" hidden><button class="btn primary small" id="btnNew"></button></div>
    <div class="card"><div class="sub" id="feedCount"></div><div class="feed" id="feedList"></div>
      <div style="text-align:center;margin-top:10px"><button class="btn" id="btnMore" hidden>加载更多</button></div></div>`;
  const update = () => {
    const nq = {
      entity: $("#fEntity").value, label: $("#fLabel").value, source: $("#fSource").value, days: $("#fDays").value,
      risk: $("#fRisk").checked, matched: $("#fMatched").checked, q: $("#fQ").value.trim(),
    };
    go("feed", nq);
  };
  ["#fEntity", "#fLabel", "#fSource", "#fDays", "#fRisk", "#fMatched"].forEach((s) => ($(s).onchange = update));
  $("#fQ").addEventListener("keydown", (e) => { if (e.key === "Enter") update(); });
  $("#fQ").addEventListener("search", update);
  $("#fExport").href = "/api/export" + qstr({ ...q, days: q.days || 0 });
  $("#btnMore").onclick = () => loadFeed(false);
  $("#btnNew").onclick = () => loadFeed(true);
  await loadFeed(true);
};
async function loadFeed(reset) {
  if (feed.loading) return;
  feed.loading = true;
  const q = state.query;
  try {
    const offset = reset ? 0 : feed.items.length;
    const r = await GET("/api/articles" + qstr({ ...q, limit: 50, offset }));
    feed.items = reset ? r.items : feed.items.concat(r.items);
    feed.total = r.total;
    const list = $("#feedList");
    if (!list) return;
    if (reset) {
      list.innerHTML = feed.items.length ? feed.items.map((a) => articleHTML(a)).join("") : `<div class="empty">没有符合条件的资讯</div>`;
      state.feedNew = 0;
      $("#newBar").hidden = true;
    } else list.insertAdjacentHTML("beforeend", r.items.map((a) => articleHTML(a)).join(""));
    bindEntityTags(list);
    $("#feedCount").textContent = `共 ${feed.total} 条`;
    $("#btnMore").hidden = feed.items.length >= feed.total;
  } catch (e) { errToast(e); } finally { feed.loading = false; }
}

// ------------------------------------------------------------------ 页面：监控对象
PAGES.entities = async (q) => {
  $("#view").innerHTML = `
    <div class="toolbar"><div class="muted">资讯里出现名称、别名或代码即算命中。搜索类数据源会用名称逐个查询。</div><span class="spacer"></span><button class="btn primary" id="btnAddEnt">添加监控对象</button></div>
    <div class="card"><div class="table-wrap" id="entTable"></div></div>
    <div id="entDetail"></div>`;
  $("#btnAddEnt").onclick = () => entityForm({});
  state.entities = await GET("/api/entities");
  renderEntityTable();
  if (q.id) showEntityDetail(+q.id);
  refreshQuotes();
};
function renderEntityTable() {
  const el = $("#entTable");
  if (!el) return;
  const kinds = Object.fromEntries((state.boot.kinds || []).map((k) => [k.id, k.name]));
  el.innerHTML = state.entities.length ? `<table><thead><tr><th>启用</th><th>名称</th><th>类型</th><th>代码</th><th>别名 / 排除词</th><th class="r">行情</th><th class="r">24h</th><th class="r">累计</th><th class="r">情绪</th><th></th></tr></thead><tbody>
    ${state.entities.map((e) => `<tr class="clickable" data-id="${e.id}">
      <td><label class="switch" onclick="event.stopPropagation()"><input type="checkbox" data-toggle="${e.id}" ${e.enabled ? "checked" : ""}><span></span></label></td>
      <td><b>${esc(e.name)}</b></td><td>${esc(kinds[e.kind] || e.kind)}</td><td class="num">${esc(entityCode(e)) || "—"}</td>
      <td style="max-width:280px">${esc(e.aliases) || '<span class="muted">—</span>'}${e.exclude ? `<div class="muted" style="font-size:12px">排除：${esc(e.exclude)}</div>` : ""}</td>
      <td class="r">${quoteHTML(e.id)}</td><td class="r num">${e.n24}</td><td class="r num">${e.total}</td>
      <td class="r num ${sentClass(e.avg24)}">${e.n24 ? idx100(e.avg24) : "—"}</td>
      <td class="r" style="white-space:nowrap"><button class="btn small" data-edit="${e.id}">编辑</button> <button class="btn small danger-ghost" data-del="${e.id}">删除</button></td></tr>`).join("")}
    </tbody></table>` : `<div class="empty">还没有监控对象</div>`;
  $$("tr.clickable", el).forEach((tr) => (tr.onclick = (ev) => { if (!ev.target.closest("button,label")) showEntityDetail(+tr.dataset.id); }));
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => entityForm(state.entities.find((e) => e.id === +b.dataset.edit))));
  $$("[data-del]", el).forEach((b) => (b.onclick = async () => {
    const e = state.entities.find((x) => x.id === +b.dataset.del);
    if (!confirm(`删除监控对象「${e.name}」？相关的预警也会删除（资讯保留）。`)) return;
    await DEL(`/api/entities/${e.id}`).catch(errToast);
    PAGES.entities({});
  }));
  $$("[data-toggle]", el).forEach((c) => (c.onchange = async () => {
    const e = state.entities.find((x) => x.id === +c.dataset.toggle);
    await POST("/api/entities", { ...e, enabled: c.checked }).catch(errToast);
    e.enabled = c.checked ? 1 : 0;
  }));
}
async function showEntityDetail(id) {
  const e = state.entities.find((x) => x.id === id);
  if (!e) return;
  const el = $("#entDetail");
  el.innerHTML = `<div class="grid cols-2 entity-detail">
      <div class="card"><h3>${esc(e.name)} · 每日资讯量</h3><div class="sub">近 30 天</div><div class="legend"></div><div class="chart" id="edBars"></div></div>
      <div class="card"><h3>${esc(e.name)} · 情绪指数</h3><div class="sub">近 30 天</div><div class="chart" id="edLine"></div></div>
    </div>
    <div class="card stack"><div class="card-head"><h3>最新资讯</h3><a href="#/feed?entity=${id}">查看全部</a></div><div class="feed" id="edFeed"></div></div>`;
  const [trend, arts] = await Promise.all([GET(`/api/entities/${id}/trend?days=30&tz=${TZ}`), GET(`/api/articles?entity=${id}&limit=15`)]);
  drawTrend($("#edBars"), $("#edLine"), trend);
  $("#edFeed").innerHTML = arts.items.length ? arts.items.map((a) => articleHTML(a)).join("") : `<div class="empty">暂无资讯</div>`;
  bindEntityTags($("#edFeed"));
  el.scrollIntoView({ behavior: "smooth", block: "start" });
}
function entityForm(e) {
  const b = state.boot;
  const sel = (list, cur) => list.map((x) => `<option value="${x.id}" ${x.id === (cur ?? "") ? "selected" : ""}>${x.name}</option>`).join("");
  openModal(`<h2>${e.id ? "编辑" : "添加"}监控对象</h2>
    <div class="form">
      <label class="k">名称 *</label><input type="text" id="eName" value="${esc(e.name || "")}" placeholder="如：贵州茅台、新能源汽车、某品牌">
      <label class="k">类型</label><select id="eKind">${sel(b.kinds, e.kind || "stock")}</select>
      <label class="k">市场</label><select id="eMarket">${sel(b.markets, e.market ?? "A")}</select>
      <label class="k">代码</label><input type="text" id="eCode" value="${esc(e.code || "")}" placeholder="600519 / 00700 / AAPL">
      <div class="hint">用于匹配资讯和显示行情：A 股 6 位数字、港股 5 位数字、美股字母代码。</div>
      <label class="k">别名</label><input type="text" id="eAliases" value="${esc(e.aliases || "")}" placeholder="逗号分隔，如：茅台,Moutai">
      <div class="hint">资讯里出现任一别名也算命中。简称太短（如“平安”）会误伤，可配合排除词。</div>
      <label class="k">排除词</label><input type="text" id="eExclude" value="${esc(e.exclude || "")}" placeholder="逗号分隔；包含这些词的资讯不算命中">
      <label class="k">启用</label><label class="switch"><input type="checkbox" id="eEnabled" ${e.enabled === 0 ? "" : "checked"}><span></span></label>
    </div>
    <div class="actions"><button class="btn" data-close>取消</button><button class="btn primary" id="eSave">保存</button></div>`);
  $("#eSave").onclick = async () => {
    try {
      await POST("/api/entities", {
        id: e.id, name: $("#eName").value, kind: $("#eKind").value, market: $("#eMarket").value, code: $("#eCode").value,
        aliases: $("#eAliases").value, exclude: $("#eExclude").value, enabled: $("#eEnabled").checked,
      });
      closeModal();
      toast("已保存。新资讯会按新规则匹配");
      PAGES.entities({});
    } catch (err) { errToast(err); }
  };
  $("#eName").focus();
}

// ------------------------------------------------------------------ 页面：数据源
PAGES.sources = async () => {
  $("#view").innerHTML = `
    <div class="toolbar"><div class="muted">快讯类数据源抓取全部资讯后自动匹配监控对象；“按监控对象”的搜索类数据源会用每个对象的名称分别查询。</div><span class="spacer"></span><button class="btn primary" id="btnAddSrc">添加数据源</button></div>
    <div class="card"><div class="table-wrap" id="srcTable"></div></div>`;
  $("#btnAddSrc").onclick = () => sourceForm({ enabled: 1, interval_min: 10, params: {} });
  await loadSources();
};
async function loadSources() {
  state.sources = await GET("/api/sources");
  renderSources();
}
const loadSourcesSoon = debounce(() => state.page === "sources" && loadSources(), 400);
function renderSources() {
  const el = $("#srcTable");
  if (!el) return;
  const types = Object.fromEntries(state.boot.source_types.map((t) => [t.type, t]));
  el.innerHTML = `<table><thead><tr><th>启用</th><th>名称</th><th class="r">间隔</th><th>状态</th><th class="r">累计入库</th><th></th></tr></thead><tbody>
    ${state.sources.map((s) => {
      const t = types[s.type] || { label: s.type };
      const st = s.running ? `<span class="status-dot run"></span>抓取中…` : !s.last_run ? `<span class="status-dot"></span><span class="muted">等待首次抓取</span>`
        : s.last_error && s.last_ok < s.last_run ? `<span class="status-dot err"></span>${ago(s.last_run)} 失败<div class="err-text">${esc(s.last_error)}</div>`
          : `<span class="status-dot ok"></span>${ago(s.last_run)} · 获取 ${s.last_count} 条，新增 ${s.last_new} 条${s.last_error ? `<div class="err-text">${esc(s.last_error)}</div>` : ""}`;
      const p = Object.entries(s.params || {}).map(([k, v]) => `${k}=${v}`).join(" ");
      return `<tr><td><label class="switch"><input type="checkbox" data-toggle="${s.id}" ${s.enabled ? "checked" : ""}><span></span></label></td>
        <td><b>${esc(s.name)}</b><div class="muted" style="font-size:12px">${esc(t.label)}${t.per_entity ? " · 按监控对象" : ""}${p ? " · " + esc(p) : ""}</div></td>
        <td class="r num" style="white-space:nowrap">${s.interval_min} 分钟</td><td>${st}</td><td class="r num">${s.total}</td>
        <td class="r" style="white-space:nowrap"><button class="btn small" data-run="${s.id}" ${s.running ? "disabled" : ""}>抓取</button> <button class="btn small" data-edit="${s.id}">编辑</button> <button class="btn small danger-ghost" data-del="${s.id}">删除</button></td></tr>`;
    }).join("")}</tbody></table>`;
  $$("[data-run]", el).forEach((b) => (b.onclick = async () => { b.disabled = true; await POST(`/api/sources/${b.dataset.run}/run`).catch(errToast); }));
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => sourceForm(state.sources.find((s) => s.id === +b.dataset.edit))));
  $$("[data-del]", el).forEach((b) => (b.onclick = async () => {
    const s = state.sources.find((x) => x.id === +b.dataset.del);
    if (!confirm(`删除数据源「${s.name}」？已抓取的资讯会保留。`)) return;
    await DEL(`/api/sources/${s.id}`).catch(errToast);
    loadSources();
  }));
  $$("[data-toggle]", el).forEach((c) => (c.onchange = async () => {
    const s = state.sources.find((x) => x.id === +c.dataset.toggle);
    await POST("/api/sources", { ...s, enabled: c.checked }).catch(errToast);
    loadSources();
  }));
}
function sourceForm(s) {
  const types = state.boot.source_types;
  openModal(`<h2>${s.id ? "编辑" : "添加"}数据源</h2>
    <div class="form">
      <label class="k">类型</label><select id="sType">${types.map((t) => `<option value="${t.type}" ${t.type === s.type ? "selected" : ""}>${esc(t.label)}${t.per_entity ? "（按监控对象）" : ""}</option>`).join("")}</select>
      <div class="hint" id="sDesc"></div>
      <label class="k">名称 *</label><input type="text" id="sName" value="${esc(s.name || "")}">
      <label class="k">抓取间隔（分钟）</label><input type="number" id="sInterval" min="1" step="1" value="${s.interval_min || 10}">
      <div class="hint">太频繁可能被网站限制访问，快讯类建议 3–5 分钟，搜索类 15–60 分钟。</div>
      <label class="k">启用</label><label class="switch"><input type="checkbox" id="sEnabled" ${s.enabled ? "checked" : ""}><span></span></label>
      <div id="sParams" style="display:contents"></div>
    </div>
    <div id="sPreview"></div>
    <div class="actions"><button class="btn" id="sTest">测试</button><span class="spacer"></span><button class="btn" data-close>取消</button><button class="btn primary" id="sSave">保存</button></div>`);
  const renderParams = () => {
    const t = types.find((x) => x.type === $("#sType").value);
    $("#sDesc").textContent = t.desc;
    if (!$("#sName").value || types.some((x) => x.label === $("#sName").value)) $("#sName").value = s.type === t.type && s.name ? s.name : t.label;
    $("#sParams").innerHTML = t.params.map((p) => {
      const val = s.type === t.type && s.params[p.key] != null ? s.params[p.key] : p.default || "";
      return `<label class="k">${esc(p.label)}</label><input type="text" data-param="${p.key}" value="${esc(val)}" placeholder="${esc(p.placeholder || "")}">`;
    }).join("");
  };
  const collect = () => {
    const params = {};
    $$("[data-param]").forEach((i) => { if (i.value.trim()) params[i.dataset.param] = i.value.trim(); });
    return { id: s.id, type: $("#sType").value, name: $("#sName").value, interval_min: +$("#sInterval").value, enabled: $("#sEnabled").checked, params };
  };
  $("#sType").onchange = renderParams;
  renderParams();
  $("#sTest").onclick = async () => {
    const b = $("#sTest");
    b.disabled = true; b.textContent = "测试中…";
    $("#sPreview").innerHTML = "";
    try {
      const r = await POST("/api/sources/test", collect());
      $("#sPreview").innerHTML = r.ok
        ? `<div class="preview"><b>成功</b>，获取 ${r.count} 条，用时 ${r.ms} 毫秒${r.tested_with ? `（用「${esc(r.tested_with)}」测试）` : ""}<ul>${r.items.map((i) => `<li>${fmtTime(i.published_at)} ${esc(i.title)}</li>`).join("")}</ul></div>`
        : `<div class="preview"><b class="down">失败</b>（${r.ms} 毫秒）：${esc(r.error)}<div class="muted" style="margin-top:6px">常见原因：网络不通或需要代理（设置 → 网络代理）、网站改版、访问过于频繁被限制。</div></div>`;
    } catch (e) { errToast(e); } finally { b.disabled = false; b.textContent = "测试"; }
  };
  $("#sSave").onclick = async () => {
    try {
      await POST("/api/sources", collect());
      closeModal();
      toast("已保存");
      loadSources();
    } catch (e) { errToast(e); }
  };
}

// ------------------------------------------------------------------ 页面：预警
PAGES.alerts = async (q) => {
  $("#view").innerHTML = `
    <div class="toolbar"><label class="chk"><input type="checkbox" id="aUnread" ${q.unread ? "checked" : ""}> 只看未读</label><span class="spacer"></span>
      <button class="btn" id="aReadAll">全部标为已读</button></div>
    <div class="card"><div class="sub">规则：风险词命中（立案、减持、爆雷等）→ 红色；情感得分低于阈值 → 黄色；某对象近 1 小时资讯量异常放大 → 热度异动。阈值在“设置”里调整。</div><div id="alertList"></div></div>`;
  $("#aUnread").onchange = () => go("alerts", { unread: $("#aUnread").checked });
  $("#aReadAll").onclick = async () => { const r = await POST("/api/alerts/read", {}); setUnread(r.unread); PAGES.alerts(state.query); };
  const rows = await GET("/api/alerts" + qstr({ unread: q.unread }));
  $("#alertList").innerHTML = rows.length ? rows.map(alertHTML).join("") : `<div class="empty">暂无预警</div>`;
  bindAlertButtons($("#alertList"));
};
function bindAlertButtons(root) {
  $$("[data-read]", root).forEach((b) => (b.onclick = async () => {
    const r = await POST("/api/alerts/read", { ids: [+b.dataset.read] });
    setUnread(r.unread);
    const row = b.closest(".alert-row");
    row.classList.remove("unread");
    b.remove();
  }));
}
function setUnread(n) {
  state.unread = n;
  const b = $("#navAlertBadge");
  b.hidden = !n;
  b.textContent = n > 99 ? "99+" : n;
}

// ------------------------------------------------------------------ 页面：设置
const SETTINGS_SPEC = [
  ["启动与退出", [
    ["auto_exit", "bool", "关闭页面后自动退出后台", "关掉后，可以只开着后台持续采集；需要时再打开页面或在页面点“退出”。"],
    ["exit_grace_sec", "number", "退出前等待（秒）", "所有页面断开超过这个时间才退出，留给刷新页面的时间。"],
    ["browser", "select", "打开页面用", "", [["auto", "自动（优先 Edge，其次 Chrome）"], ["edge", "Microsoft Edge"], ["chrome", "Google Chrome"], ["default", "系统默认浏览器"], ["none", "不自动打开"]]],
    ["app_window", "bool", "使用独立应用窗口", "像桌面程序一样单独一个窗口，没有地址栏，关掉窗口即退出。下次启动生效。"],
  ]],
  ["网络", [
    ["proxy", "text", "网络代理", "例：http://127.0.0.1:7890。访问 Google 新闻、Yahoo 等海外源时需要；留空则使用系统代理。"],
    ["timeout", "number", "请求超时（秒）", ""],
  ]],
  ["情感分析", [
    ["pos_threshold", "number", "利好阈值", "得分 ≥ 该值判为利好（-1 ~ 1，默认 0.15）。", null, 0.05],
    ["neg_threshold", "number", "利空阈值", "得分 ≤ 该值判为利空（默认 -0.15）。", null, 0.05],
  ]],
  ["预警", [
    ["alert_risk", "bool", "风险词预警", "命中立案调查、减持、爆雷、退市等风险词时预警。"],
    ["alert_negative", "bool", "负面舆情预警", ""],
    ["alert_negative_score", "number", "负面预警阈值", "命中监控对象且得分 ≤ 该值时预警（默认 -0.4）。", null, 0.05],
    ["alert_spike", "bool", "热度异动预警", ""],
    ["spike_min", "number", "异动最少条数", "近 1 小时至少这么多条才可能触发。"],
    ["spike_ratio", "number", "异动倍数", "近 1 小时条数 ≥ 前 23 小时平均每小时条数 × 该倍数。", null, 0.5],
    ["notify_desktop", "bool", "桌面通知", "需要浏览器允许通知。"],
    ["notify_sound", "bool", "提示音", ""],
  ]],
  ["显示", [
    ["color_scheme", "select", "涨跌配色", "利好 / 利空的颜色也跟随此设置。", [["cn", "红涨绿跌（A 股习惯）"], ["us", "绿涨红跌（美股习惯）"]]],
    ["quote_refresh_sec", "number", "行情刷新间隔（秒）", ""],
  ]],
  ["数据", [
    ["retention_days", "number", "资讯保留天数", "超过天数的资讯和预警自动删除。"],
    ["store_unmatched", "bool", "保存未命中对象的快讯", "关掉后只保存与监控对象相关的资讯。"],
  ]],
];
PAGES.settings = async () => {
  const cfg = (state.settings = await GET("/api/settings"));
  const field = ([key, type, label, hint, options, step]) => {
    const v = cfg[key];
    let input;
    if (type === "bool") input = `<label class="switch"><input type="checkbox" data-key="${key}" ${v ? "checked" : ""}><span></span></label>`;
    else if (type === "select") input = `<select data-key="${key}">${options.map(([val, txt]) => `<option value="${val}" ${val === v ? "selected" : ""}>${txt}</option>`).join("")}</select>`;
    else input = `<input type="${type === "number" ? "number" : "text"}" data-key="${key}" value="${esc(v)}" ${step ? `step="${step}"` : ""}>`;
    return `<label class="k">${label}</label>${input}${hint ? `<div class="hint">${hint}</div>` : ""}`;
  };
  $("#view").innerHTML = SETTINGS_SPEC.map(([g, items]) => `<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:12px">${g}</h3><div class="form">${items.map(field).join("")}</div></div>`).join("") +
    `<div class="card" style="margin-bottom:16px"><h3 style="margin-bottom:12px">维护</h3>
      <div class="form"><label class="k">数据目录</label><code style="overflow-wrap:anywhere">${esc(state.boot.data_dir)}</code>
      <label class="k">版本</label><span>v${esc(state.boot.version)}</span></div>
      <div class="settings-actions"><button class="btn" id="btnNotify">允许桌面通知</button><button class="btn" id="btnDemo2">生成演示数据</button><button class="btn danger-ghost" id="btnClear">清空所有资讯和预警</button></div></div>
    <div style="position:sticky;bottom:12px;text-align:right"><button class="btn primary" id="btnSaveSettings">保存设置</button></div>`;
  $("#btnSaveSettings").onclick = async () => {
    const patch = {};
    $$("[data-key]").forEach((i) => { patch[i.dataset.key] = i.type === "checkbox" ? i.checked : i.type === "number" ? Number(i.value) : i.value; });
    try {
      state.settings = await POST("/api/settings", patch);
      applyColors();
      toast("设置已保存");
    } catch (e) { errToast(e); }
  };
  $("#btnNotify").onclick = async () => {
    if (!("Notification" in window)) return toast("当前浏览器不支持桌面通知");
    const p = await Notification.requestPermission();
    toast(p === "granted" ? "已允许桌面通知" : "浏览器未允许通知");
  };
  $("#btnDemo2").onclick = async () => { const r = await POST("/api/demo"); toast(`已生成 ${r.count} 条演示数据（标有“演示数据”）`); };
  $("#btnClear").onclick = async () => {
    if (!confirm("清空所有已抓取的资讯和预警？监控对象和数据源保留。")) return;
    await POST("/api/clear");
    setUnread(0);
    toast("已清空");
  };
};

// ------------------------------------------------------------------ 弹窗
function openModal(html) {
  $("#modal").innerHTML = html;
  $("#modalBack").hidden = false;
  $$("[data-close]", $("#modal")).forEach((b) => (b.onclick = closeModal));
}
function closeModal() { $("#modalBack").hidden = true; $("#modal").innerHTML = ""; }
$("#modalBack").addEventListener("mousedown", (e) => { if (e.target.id === "modalBack") closeModal(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("#modalBack").hidden) closeModal(); });

// ------------------------------------------------------------------ 实时推送（SSE）与退出
function setConn(ok, text) {
  const c = $("#conn");
  c.className = "conn " + (ok ? "ok" : "bad");
  $("span", c).textContent = text || (ok ? "实时" : "已断开");
}
function connect() {
  const es = new EventSource("/api/events");
  state.es = es;
  es.addEventListener("hello", () => {
    setConn(true);
    state.downSince = 0;
    $("#banner").hidden = true;
  });
  es.addEventListener("articles", (e) => onArticles(JSON.parse(e.data)));
  es.addEventListener("alert", (e) => onAlert(JSON.parse(e.data)));
  es.addEventListener("source", (e) => {
    const d = JSON.parse(e.data);
    const s = state.sources.find((x) => x.id === d.id);
    if (s) s.running = d.running;
    if (state.page === "sources") d.running ? renderSources() : loadSourcesSoon();
  });
  es.addEventListener("shutdown", (e) => showShutdown(JSON.parse(e.data).reason));
  es.onerror = () => {
    if (state.shutdown) { es.close(); return; }
    setConn(false, "重连中");
    if (!state.downSince) state.downSince = Date.now();
    const secs = (Date.now() - state.downSince) / 1000;
    if (secs > 3) { $("#banner").hidden = false; $("#banner").textContent = "与后台的连接断开，正在重连…"; }
    if (secs > 20) { es.close(); showShutdown("后台没有响应（可能已被关闭）。"); }
  };
}
const reloadOverview = debounce(() => state.page === "overview" && PAGES.overview(), 1500);
function onArticles(d) {
  if (state.page === "overview") reloadOverview();
  else if (state.page === "feed") {
    state.feedNew += d.count;
    $("#newBar").hidden = false;
    $("#btnNew").textContent = `有 ${state.feedNew} 条新资讯，点击刷新`;
  }
}
let audioCtx = null;
function beep() {
  try {
    audioCtx = audioCtx || new AudioContext();
    const o = audioCtx.createOscillator(), g = audioCtx.createGain();
    o.frequency.value = 880; g.gain.value = 0.08;
    o.connect(g); g.connect(audioCtx.destination);
    o.start(); o.stop(audioCtx.currentTime + 0.18);
  } catch (e) { /* 忽略 */ }
}
function onAlert(al) {
  setUnread(state.unread + 1);
  toast(`<b>${al.level === "danger" ? "⚠ 风险" : "预警"}</b> ${esc(al.message)}`, al.level, () => go("alerts"));
  if (state.settings.notify_sound) beep();
  if (state.settings.notify_desktop && "Notification" in window && Notification.permission === "granted" && document.hidden) {
    try { new Notification("舆情预警", { body: al.message, tag: "alert-" + al.id }); } catch (e) { /* 忽略 */ }
  }
  if (state.page === "alerts") PAGES.alerts(state.query);
  else if (state.page === "overview") reloadOverview();
}
function showShutdown(reason) {
  if (state.shutdown) return;
  state.shutdown = true;
  state.es && state.es.close();
  clearTimeout(quoteTimer);
  setConn(false, "已退出");
  $("#overlayReason").textContent = reason || "";
  $("#overlay").hidden = false;
  setTimeout(() => window.close(), 400); // 应用窗口模式下可以直接关掉
}

// ------------------------------------------------------------------ 启动
async function init() {
  applyTheme();
  $("#btnTheme").onclick = () => {
    const dark = document.documentElement.dataset.theme
      ? document.documentElement.dataset.theme === "dark"
      : matchMedia("(prefers-color-scheme: dark)").matches;
    try { localStorage.setItem("theme", dark ? "light" : "dark"); } catch (e) { /* 无痕模式 */ }
    applyTheme();
    route();
  };
  $("#btnRefresh").onclick = async () => {
    await POST("/api/refresh").catch(errToast);
    toast("已让所有启用的数据源开始抓取，新资讯会实时出现");
  };
  $("#btnExit").onclick = async () => {
    if (!confirm("退出舆情监控？后台采集会停止。")) return;
    try { await POST("/api/shutdown"); } catch (e) { /* 后台可能已经退出 */ }
    showShutdown("你点了“退出”。");
  };
  try {
    state.boot = await GET("/api/bootstrap");
  } catch (e) {
    showShutdown("无法连接后台。");
    return;
  }
  state.settings = state.boot.settings;
  applyColors();
  $("#navFoot").textContent = `v${state.boot.version} · 本机运行`;
  GET("/api/overview?days=3").then((d) => setUnread(d.overview.unread_alerts)).catch(() => {});
  GET("/api/sources").then((s) => (state.sources = s)).catch(() => {});
  connect();
  window.addEventListener("hashchange", route);
  window.addEventListener("resize", debounce(() => {
    if (state.page === "overview" && state.ovData) drawTrend($("#chBars"), $("#chLine"), state.ovData.trend);
  }, 200));
  route();
}
init();
