'use strict';
/* QuantLab 前端：原生 JS + ECharts，所有数据来自本机服务 /api/*。 */

const TOKEN = new URLSearchParams(location.search).get('t') || '';
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const pct = (v, d = 1) => (v == null || !isFinite(v) ? '—' : (v * 100).toFixed(d) + '%');
const num = (v, d = 2) => (v == null || !isFinite(v) ? '—' : Number(v).toLocaleString('zh-CN', { minimumFractionDigits: d, maximumFractionDigits: d }));
const sgn = (v) => (v > 0 ? 'up' : v < 0 ? 'down' : '');
const COLORS = ['#1d4ed8', '#f59e0b', '#10b981', '#ef4444', '#8b5cf6', '#06b6d4', '#ec4899', '#64748b'];
const UP = '#d92d20', DOWN = '#079455';

const S = { catalog: null, status: null, ds: localStorage.getItem('ql.ds') || '', runs: [], run: null, runId: null,
  jobId: null, jobDone: new Set(), charts: {}, factorRows: [], factorSort: { k: 'rank_ic', abs: true, dir: -1 } };

async function api(path, body) {
  const opt = body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-QuantLab-Token': TOKEN }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  let data = {};
  try { data = await r.json(); } catch (e) { /* 空响应 */ }
  if (!r.ok) throw new Error(data.error || `请求失败（${r.status}）`);
  return data;
}

function toast(msg, bad = false, ms = 4000) {
  const d = document.createElement('div');
  d.textContent = msg;
  if (bad) d.className = 'bad';
  $('#toast').appendChild(d);
  setTimeout(() => d.remove(), ms);
}

function chart(id, el) {
  if (S.charts[id]) S.charts[id].dispose();
  S.charts[id] = echarts.init(el);
  return S.charts[id];
}
window.addEventListener('resize', () => Object.values(S.charts).forEach((c) => { try { c.resize(); } catch (e) { /* */ } }));

function modal(title, html, onOk) {
  const wrap = document.createElement('div');
  wrap.style.cssText = 'position:fixed;inset:0;background:rgba(15,23,42,.45);display:flex;align-items:center;justify-content:center;z-index:20';
  wrap.innerHTML = `<div class="card" style="width:min(720px,92vw);margin:0"><h2>${esc(title)}</h2>${html}
    <div class="row" style="justify-content:flex-end"><button class="ghost" data-x>取消</button>${onOk ? '<button class="primary" data-ok>确定</button>' : ''}</div></div>`;
  document.body.appendChild(wrap);
  wrap.querySelector('[data-x]').onclick = () => wrap.remove();
  if (onOk) wrap.querySelector('[data-ok]').onclick = async () => { if ((await onOk(wrap)) !== false) wrap.remove(); };
  return wrap;
}

/* ------------------------------------------------------------------ 导航 */
const LOADERS = { data: loadDataPage, factors: loadFactorsPage, new: loadNewPage, results: loadResultsPage, compare: loadComparePage, picks: loadPicksPage };
function go(page) {
  $$('nav a').forEach((a) => a.classList.toggle('on', a.dataset.page === page));
  $$('.page').forEach((p) => p.classList.toggle('on', p.id === 'page-' + page));
  if (LOADERS[page]) LOADERS[page]();
  setTimeout(() => Object.values(S.charts).forEach((c) => c.resize()), 50);
}
$$('nav a').forEach((a) => (a.onclick = () => go(a.dataset.page)));

async function refreshStatus() {
  S.status = await api('/api/status');
  $('#version').textContent = 'v' + S.status.version;
  const list = S.status.datasets;
  if (!list.find((d) => d.id === S.ds)) setDs(list[0] ? list[0].id : '');
  else setDs(S.ds);
  $$('.ds-select').forEach((sel) => {
    const cur = sel.value || S.ds;
    sel.innerHTML = list.map((d) => `<option value="${esc(d.id)}">${esc(d.title)}（${d.n_symbols} 只）</option>`).join('') || '<option value="">（还没有数据集）</option>';
    sel.value = list.find((d) => d.id === cur) ? cur : S.ds;
  });
  $('#imp-merge').innerHTML = '<option value="">不合并，新建数据集</option>' + list.filter((d) => !d.demo).map((d) => `<option value="${esc(d.id)}">${esc(d.title)}</option>`).join('');
  if (S.status.job && S.status.job.status === 'running') startPolling();
}
function setDs(id) {
  S.ds = id;
  localStorage.setItem('ql.ds', id);
  const d = S.status && S.status.datasets.find((x) => x.id === id);
  $('#ds-current').textContent = d ? '当前数据：' + d.title : '尚无数据';
}

/* ------------------------------------------------------------------ 任务 */
let pollTimer = null;
function startPolling() { if (!pollTimer) { pollTimer = setInterval(pollJob, 1000); pollJob(); } }
async function pollJob() {
  let job;
  try { job = (await api('/api/job')).job; } catch (e) { return; }
  const bar = $('#jobbar');
  if (!job) { bar.hidden = true; return; }
  bar.hidden = false;
  $('#job-title').textContent = job.title;
  $('#job-msg').textContent = job.status === 'running' ? job.message : ({ done: '完成', error: '失败：' + job.error, cancelled: '已取消' }[job.status]);
  $('#job-bar').style.width = (job.progress * 100).toFixed(1) + '%';
  $('#job-bar').style.background = job.status === 'error' ? UP : job.status === 'done' ? DOWN : '';
  $('#job-pct').textContent = `${(job.progress * 100).toFixed(0)}% · ${job.elapsed}s`;
  $('#job-cancel').hidden = job.status !== 'running';
  if (!$('#job-logs').hidden) { $('#job-logs').textContent = job.logs.join('\n'); $('#job-logs').scrollTop = 1e9; }
  if (job.status !== 'running') {
    clearInterval(pollTimer); pollTimer = null;
    if (!S.jobDone.has(job.id)) { S.jobDone.add(job.id); onJobFinished(job); }
    setTimeout(() => { if (!pollTimer) bar.hidden = true; }, 15000);
  }
}
async function onJobFinished(job) {
  if (job.status === 'error') return toast(job.title + ' 失败：' + job.error, true, 9000);
  if (job.status === 'cancelled') return toast('已取消');
  if (job.kind === 'import' || job.kind === 'demo') {
    setDs(job.result.dataset);
    await refreshStatus();
    toast('数据已就绪');
    if ($('#page-data').classList.contains('on')) loadDataPage();
  } else if (job.kind === 'run') {
    toast('实验完成');
    S.runId = job.result.run;
    go('results');
  } else if (job.kind === 'factors') {
    renderFactors(job.result);
  }
}
$('#job-cancel').onclick = () => api('/api/job/cancel', {}).catch((e) => toast(e.message, true));
$('#job-logs-btn').onclick = () => { $('#job-logs').hidden = !$('#job-logs').hidden; pollJob(); };
async function startJob(path, body) {
  const r = await api(path, body);
  S.jobId = r.job.id;
  startPolling();
  return r.job;
}

/* ------------------------------------------------------------------ 数据中心 */
async function loadDataPage() {
  await refreshStatus();
  const list = S.status.datasets;
  $('#ds-empty').hidden = list.length > 0;
  $('#ds-table tbody').innerHTML = list.map((d) => `<tr>
    <td><b>${esc(d.title)}</b> ${d.demo ? '<span class="chip warn">模拟</span>' : ''} ${d.id === S.ds ? '<span class="chip">当前</span>' : ''}</td>
    <td class="muted">${esc(d.id)}</td><td class="num">${d.n_symbols}</td><td>${esc(d.start)} ~ ${esc(d.end)}</td>
    <td class="num">${num(d.n_rows, 0)}</td><td class="num">${d.adjust_events ?? 0}</td><td class="num">${d.issues_count ?? 0}</td>
    <td><button class="sm" data-view="${esc(d.id)}">查看 / 设为当前</button> <button class="sm danger" data-del="${esc(d.id)}">删除</button></td></tr>`).join('');
  $$('#ds-table [data-view]').forEach((b) => (b.onclick = () => { setDs(b.dataset.view); loadDataPage(); }));
  $$('#ds-table [data-del]').forEach((b) => (b.onclick = async () => {
    if (!confirm('删除这个数据集？（实验结果不受影响）')) return;
    try { await api('/api/dataset/delete', { id: b.dataset.del }); loadDataPage(); } catch (e) { toast(e.message, true); }
  }));
  if (S.ds) showDataset(S.ds); else $('#ds-detail').hidden = true;
}

async function showDataset(id) {
  let meta;
  try { meta = await api('/api/dataset?id=' + encodeURIComponent(id)); } catch (e) { return; }
  $('#ds-detail').hidden = false;
  $('#ds-detail-title').textContent = `${meta.title}  ·  ${meta.start} ~ ${meta.end}`;
  const chips = Object.entries(meta.boards || {}).map(([k, v]) => `<span class="chip">${esc(k)} ${v}</span>`);
  Object.entries(meta.kinds || {}).forEach(([k, v]) => { if (k !== 'stock') chips.push(`<span class="chip gray">${{ index: '指数', fund: '基金', bond: '债券' }[k] || k} ${v}</span>`); });
  if (meta.demo) chips.push('<span class="chip warn">模拟数据，非真实行情</span>');
  $('#ds-boards').innerHTML = chips.join('');
  const issues = meta.issues || [];
  $('#ds-issues').hidden = !issues.length;
  $('#ds-issues ul').innerHTML = issues.slice(0, 300).map((x) => `<li>${esc(x)}</li>`).join('');
  const runs = (await api('/api/runs')).runs;
  const sel = $('#kl-run');
  const keep = sel.value;
  sel.innerHTML = '<option value="">叠加实验信号（可选）</option>' + runs.map((r) => `<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.created)}</option>`).join('');
  if (runs.find((r) => r.id === keep)) sel.value = keep;
  await searchSymbols();
}

let symTimer = null;
$('#sym-q').oninput = () => { clearTimeout(symTimer); symTimer = setTimeout(searchSymbols, 250); };
async function searchSymbols() {
  if (!S.ds) return;
  const r = await api(`/api/symbols?id=${encodeURIComponent(S.ds)}&q=${encodeURIComponent($('#sym-q').value)}`);
  const kindName = { index: '指数', fund: '基金', bond: '债券', stock: '' };
  $('#sym-list').innerHTML = r.symbols.map((s) => `<div class="item" data-sym="${esc(s.symbol)}"><b>${esc(s.symbol)}</b> ${esc(s.name || '')} <span class="muted">${kindName[s.kind] || ''}</span>
    <small>${esc(s.start)} ~ ${esc(s.end)} · ${s.rows} 行${s.events && s.events.length ? ' · 除权 ' + s.events.length : ''}</small></div>`).join('') || '<div class="empty">没有匹配</div>';
  $$('#sym-list .item').forEach((el) => (el.onclick = () => loadKline(el.dataset.sym)));
  if (!S.kSym && r.symbols[0]) loadKline(r.symbols[0].symbol);
}
$('#kl-run').onchange = () => S.kSym && loadKline(S.kSym);

function ma(arr, n) {
  const out = []; let s = 0;
  for (let i = 0; i < arr.length; i++) { s += arr[i]; if (i >= n) s -= arr[i - n]; out.push(i >= n - 1 ? +(s / n).toFixed(3) : null); }
  return out;
}

async function loadKline(sym) {
  S.kSym = sym;
  $$('#sym-list .item').forEach((el) => el.classList.toggle('on', el.dataset.sym === sym));
  const run = $('#kl-run').value;
  let d;
  try { d = await api(`/api/bars?id=${encodeURIComponent(S.ds)}&symbol=${encodeURIComponent(sym)}${run ? '&run=' + encodeURIComponent(run) : ''}`); } catch (e) { return toast(e.message, true); }
  const closes = d.ohlc.map((x) => x[1]);
  const last = d.ohlc[d.ohlc.length - 1], prev = d.ohlc[d.ohlc.length - 2] || last;
  const chg = last[1] / prev[1] - 1;
  const nm = ($(`#sym-list .item[data-sym="${sym}"]`) || {}).textContent || '';
  $('#kl-head').innerHTML = `<b>${esc(sym)}</b><span>${esc(nm.split('\n')[0].replace(sym, '').trim())}</span>
    <span>收 ${num(last[1])}</span><span class="${sgn(chg)}">${pct(chg, 2)}</span><span class="muted">${esc(d.dates[d.dates.length - 1])} · 前复权${d.events.length ? ' · 除权 ' + d.events.length + ' 次' : ''}</span>`;
  const hasScore = d.scores && d.scores.dates.length;
  const c = chart('kline', $('#kline'));
  const grids = hasScore ? [{ left: 60, right: 30, top: 30, height: '52%' }, { left: 60, right: 30, top: '64%', height: '14%' }, { left: 60, right: 30, top: '82%', height: '11%' }]
    : [{ left: 60, right: 30, top: 30, height: '62%' }, { left: 60, right: 30, top: '76%', height: '16%' }];
  const xAxes = grids.map((g, i) => ({ type: 'category', data: d.dates, gridIndex: i, boundaryGap: true, axisLabel: { show: i === grids.length - 1 }, axisTick: { show: false } }));
  const yAxes = grids.map((g, i) => ({ scale: true, gridIndex: i, splitNumber: i ? 2 : 5, axisLabel: { fontSize: 11 }, ...(i === 2 ? { min: 0, max: 1 } : {}) }));
  const marks = [];
  (d.trades || []).forEach((t) => {
    const i = d.dates.indexOf(t.date);
    if (i < 0) return;
    const buy = t.side === '买入';
    marks.push({ coord: [t.date, buy ? d.ohlc[i][2] : d.ohlc[i][3]], value: buy ? '买' : '卖', symbol: buy ? 'triangle' : 'pin', symbolSize: buy ? 12 : 26,
      symbolRotate: 0, symbolOffset: [0, buy ? 10 : -14], itemStyle: { color: buy ? UP : DOWN }, label: { show: !buy, fontSize: 10 } });
  });
  const series = [
    { name: 'K线', type: 'candlestick', data: d.ohlc, itemStyle: { color: UP, color0: DOWN, borderColor: UP, borderColor0: DOWN },
      markPoint: { data: marks, animation: false },
      markLine: d.events.length ? { symbol: 'none', silent: true, lineStyle: { color: '#f59e0b', type: 'dashed' }, label: { formatter: '除权', fontSize: 10 }, data: d.events.map((e) => ({ xAxis: e })) } : undefined },
    { name: 'MA5', type: 'line', data: ma(closes, 5), showSymbol: false, lineStyle: { width: 1 }, color: '#f59e0b' },
    { name: 'MA20', type: 'line', data: ma(closes, 20), showSymbol: false, lineStyle: { width: 1 }, color: '#8b5cf6' },
    { name: 'MA60', type: 'line', data: ma(closes, 60), showSymbol: false, lineStyle: { width: 1 }, color: '#06b6d4' },
    { name: '成交量', type: 'bar', xAxisIndex: 1, yAxisIndex: 1, data: d.volume.map((v, i) => ({ value: v, itemStyle: { color: d.ohlc[i][1] >= d.ohlc[i][0] ? UP : DOWN } })) },
  ];
  if (hasScore) {
    const map = Object.fromEntries(d.scores.dates.map((x, i) => [x, d.scores.score[i]]));
    series.push({ name: '模型得分', type: 'line', xAxisIndex: 2, yAxisIndex: 2, data: d.dates.map((x) => map[x] ?? null), connectNulls: true, showSymbol: false, color: COLORS[0], areaStyle: { opacity: 0.08 } });
  }
  const start = Math.max(0, 100 - (250 / d.dates.length) * 100);
  c.setOption({
    animation: false, legend: { top: 0, data: ['MA5', 'MA20', 'MA60'].concat(hasScore ? ['模型得分'] : []) },
    tooltip: { trigger: 'axis', axisPointer: { type: 'cross' } }, axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: grids, xAxis: xAxes, yAxis: yAxes, series,
    dataZoom: [{ type: 'inside', xAxisIndex: grids.map((g, i) => i), start, end: 100 }, { type: 'slider', xAxisIndex: grids.map((g, i) => i), bottom: 2, height: 16, start, end: 100 }],
  });
}

$('#imp-pick').onclick = async () => {
  try {
    const r = await api('/api/pick-folder', {});
    if (r.mode === 'browser') return toast('浏览器模式下请直接粘贴文件夹路径');
    if (r.path) $('#imp-path').value = r.path;
  } catch (e) { toast(e.message, true); }
};
$('#imp-go').onclick = async () => {
  const kinds = ['stock'];
  if ($('#imp-index').checked) kinds.push('index');
  if ($('#imp-fund').checked) kinds.push('fund');
  try {
    await startJob('/api/import', { path: $('#imp-path').value, title: $('#imp-title').value, merge_into: $('#imp-merge').value, kinds, auto_adjust: $('#imp-adjust').checked });
  } catch (e) { toast(e.message, true); }
};
$('#demo-go').onclick = async () => {
  try { await startJob('/api/demo', { n_stocks: +$('#demo-n').value, years: +$('#demo-years').value }); } catch (e) { toast(e.message, true); }
};
$('#open-data-dir').onclick = () => api('/api/open-dir', { which: 'data' }).catch((e) => toast(e.message, true));

/* ------------------------------------------------------------------ 因子研究 */
async function loadFactorsPage() { await refreshStatus(); }
$('#fa-go').onclick = async () => {
  const cfg = { dataset: $('#fa-ds').value, label: { horizon: +$('#fa-h').value }, backtest: { rebalance_days: +$('#fa-step').value },
    period: { start: $('#fa-start').value, end: $('#fa-end').value } };
  try { await startJob('/api/factors', { config: cfg }); toast('开始单因子检验'); } catch (e) { toast(e.message, true); }
};
function spark(series) {
  const v = (series || []).filter((x) => x != null);
  if (v.length < 2) return '';
  const lo = Math.min(0, ...v), hi = Math.max(0, ...v), span = hi - lo || 1;
  const pts = series.map((x, i) => (x == null ? null : `${(i / (series.length - 1)) * 120},${28 - ((x - lo) / span) * 26 - 1}`)).filter(Boolean).join(' ');
  const zero = 28 - ((0 - lo) / span) * 26 - 1;
  return `<svg class="spark" viewBox="0 0 120 28"><line x1="0" x2="120" y1="${zero}" y2="${zero}" stroke="#d0d5dd" stroke-dasharray="2 2"/><polyline fill="none" stroke="${v[v.length - 1] >= 0 ? UP : DOWN}" stroke-width="1.3" points="${pts}"/></svg>`;
}
function renderFactors(res) {
  S.factorRows = res.factors;
  $('#fa-result').hidden = false;
  drawFactorTable();
  const top = res.factors.slice(0, 30).reverse();
  const c = chart('fa', $('#fa-chart'));
  c.setOption({
    title: { text: `单因子 RankIC（${res.start} ~ ${res.end}，${res.horizon} 日，${num(res.n_samples, 0)} 样本）`, textStyle: { fontSize: 13 } },
    grid: { left: 130, right: 30, top: 40, bottom: 20 }, tooltip: { trigger: 'axis' },
    xAxis: { type: 'value' }, yAxis: { type: 'category', data: top.map((r) => r.name), axisLabel: { fontSize: 11 } },
    series: [{ type: 'bar', data: top.map((r) => ({ value: r.rank_ic, itemStyle: { color: r.rank_ic >= 0 ? UP : DOWN } })) }],
  });
  $('#fa-chart').style.height = Math.max(340, top.length * 18 + 60) + 'px';
  c.resize();
}
function drawFactorTable() {
  const { k, dir, abs } = S.factorSort;
  const rows = [...S.factorRows].sort((a, b) => {
    let x = a[k], y = b[k];
    if (typeof x === 'number' || typeof y === 'number') { x = abs ? Math.abs(x || 0) : (x || 0); y = abs ? Math.abs(y || 0) : (y || 0); return (x - y) * dir; }
    return String(x).localeCompare(String(y), 'zh') * dir;
  });
  $('#fa-table tbody').innerHTML = rows.map((r) => `<tr><td><b>${esc(r.name)}</b></td><td>${esc(r.group)}</td><td class="wrap">${esc(r.desc)}</td>
    <td class="num ${sgn(r.rank_ic)}">${num(r.rank_ic, 4)}</td><td class="num">${num(r.icir, 3)}</td><td class="num">${pct(r.positive)}</td>
    <td class="num ${sgn(r.spread)}">${pct(r.spread, 2)}</td><td class="num">${pct(r.coverage, 0)}</td><td>${spark(r.series)}</td></tr>`).join('');
}
$$('#fa-table th[data-k]').forEach((th) => (th.onclick = () => {
  const k = th.dataset.k;
  S.factorSort = { k, dir: S.factorSort.k === k ? -S.factorSort.dir : -1, abs: ['rank_ic', 'icir', 'spread'].includes(k) };
  drawFactorTable();
}));

/* ------------------------------------------------------------------ 新建实验 */
const PRESETS = {
  default: (c) => c,
  quick: (c) => { c.universe.min_amount = 3e7; c.walk.retrain_days = 126; c.features.groups = ['momentum', 'trend', 'volatility', 'volume', 'technical']; return c; },
  lowturn: (c) => { c.label.horizon = 20; c.backtest.rebalance_days = 20; c.backtest.top_k = 30; c.backtest.buffer = 2; return c; },
  ensemble: (c) => { c.model.types = ['lgbm', 'ridge', 'mlp']; return c; },
  rank: (c) => { c.model.types = ['lgbm_rank']; return c; },
};
let formFilled = false;
async function loadNewPage() {
  if (!S.catalog) S.catalog = await api('/api/catalog');
  await refreshStatus();
  if (!formFilled) { fillForm(JSON.parse(JSON.stringify(S.catalog.defaults))); formFilled = true; }
  await fillBench();
}
async function fillBench() {
  const form = $('#cfg-form');
  const ds = form.elements.dataset.value;
  const sel = $('#cfg-bench');
  const cur = sel.value || 'auto';
  let opts = '<option value="auto">股票池等权（默认）</option>';
  if (ds) {
    try {
      const meta = await api('/api/dataset?id=' + encodeURIComponent(ds));
      const idx = (meta.symbols || []).filter((s) => s.kind === 'index');
      const pref = ['sh000300', 'sh000905', 'sh000852', 'sh000001', 'sz399001', 'sz399006'];
      idx.sort((a, b) => ((pref.indexOf(a.symbol) + 1 || 99) - (pref.indexOf(b.symbol) + 1 || 99)));
      opts += idx.slice(0, 200).map((s) => `<option value="${esc(s.symbol)}">${esc(s.symbol)} ${esc(s.name || '')}</option>`).join('');
    } catch (e) { /* */ }
  }
  sel.innerHTML = opts;
  sel.value = [...sel.options].some((o) => o.value === cur) ? cur : 'auto';
}
$('#cfg-form').elements.dataset.onchange = fillBench;

function fillForm(cfg) {
  const cat = S.catalog;
  const f = $('#cfg-form');
  $('#cfg-boards').innerHTML = Object.entries(cat.boards).map(([k, v]) => `<label><input type="checkbox" name="board" value="${k}" ${cfg.universe.boards.includes(k) ? 'checked' : ''}> ${v}</label>`).join('');
  const counts = {};
  cat.features.forEach((x) => (counts[x.group] = (counts[x.group] || 0) + 1));
  $('#cfg-groups').innerHTML = Object.entries(cat.groups).map(([k, v]) => `<label title="${esc(cat.features.filter((x) => x.group === k).map((x) => x.name + '：' + x.desc).join('\n'))}"><input type="checkbox" name="fgroup" value="${k}" ${cfg.features.groups.includes(k) ? 'checked' : ''}> ${v}（${counts[k] || 0}）</label>`).join('');
  $('#cfg-models').innerHTML = Object.entries(cat.models).map(([k, v]) => `<label><input type="checkbox" name="mtype" value="${k}" ${cfg.model.types.includes(k) ? 'checked' : ''}> ${v}</label>`).join('');
  const set = (name, v) => { const el = f.elements[name]; if (!el) return; if (el.type === 'checkbox') el.checked = !!v; else el.value = v ?? ''; };
  set('name', cfg.name);
  if (cfg.dataset) set('dataset', cfg.dataset); else if (S.ds) set('dataset', S.ds);
  set('period.start', cfg.period.start); set('period.end', cfg.period.end);
  ['min_listed_days', 'min_price', 'exclude_st'].forEach((k) => set('universe.' + k, cfg.universe[k]));
  set('universe.min_amount_wan', Math.round(cfg.universe.min_amount / 1e4));
  set('features.normalize', cfg.features.normalize);
  set('features.names', (cfg.features.names || []).join(', '));
  ['horizon', 'price', 'transform', 'excess'].forEach((k) => set('label.' + k, cfg.label[k]));
  set('model.params', Object.keys(cfg.model.params || {}).length ? JSON.stringify(cfg.model.params, null, 1) : '');
  ['train_days', 'retrain_days', 'valid_ratio', 'min_train_days', 'train_tradable_only'].forEach((k) => set('walk.' + k, cfg.walk[k]));
  ['top_k', 'rebalance_days', 'capital', 'commission', 'min_commission', 'stamp_tax', 'slippage', 'buffer', 'stop_loss'].forEach((k) => set('backtest.' + k, cfg.backtest[k]));
  S.pendingBench = cfg.backtest.benchmark;
  fillBench().then(() => { if (S.pendingBench) { const sel = $('#cfg-bench'); if ([...sel.options].some((o) => o.value === S.pendingBench)) sel.value = S.pendingBench; } });
}

function readForm() {
  const f = $('#cfg-form');
  const v = (n) => f.elements[n].value;
  const n = (k) => Number(f.elements[k].value);
  const chk = (k) => f.elements[k].checked;
  let params = {};
  if (v('model.params').trim()) {
    try { params = JSON.parse(v('model.params')); } catch (e) { throw new Error('模型参数不是有效的 JSON'); }
  }
  return {
    name: v('name').trim(), dataset: v('dataset'), period: { start: v('period.start'), end: v('period.end') },
    universe: { boards: $$('input[name=board]:checked').map((x) => x.value), min_listed_days: n('universe.min_listed_days'),
      min_amount: n('universe.min_amount_wan') * 1e4, min_price: n('universe.min_price'), exclude_st: chk('universe.exclude_st') },
    features: { groups: $$('input[name=fgroup]:checked').map((x) => x.value), names: v('features.names').split(/[,，\s]+/).filter(Boolean), normalize: v('features.normalize') },
    label: { horizon: n('label.horizon'), price: v('label.price'), transform: v('label.transform'), excess: chk('label.excess') },
    model: { types: $$('input[name=mtype]:checked').map((x) => x.value), params },
    walk: { train_days: n('walk.train_days'), retrain_days: n('walk.retrain_days'), valid_ratio: n('walk.valid_ratio'), min_train_days: n('walk.min_train_days'), train_tradable_only: chk('walk.train_tradable_only') },
    backtest: { top_k: n('backtest.top_k'), rebalance_days: n('backtest.rebalance_days'), capital: n('backtest.capital'), commission: n('backtest.commission'),
      min_commission: n('backtest.min_commission'), stamp_tax: n('backtest.stamp_tax'), slippage: n('backtest.slippage'), buffer: n('backtest.buffer'),
      stop_loss: n('backtest.stop_loss'), benchmark: v('backtest.benchmark') || 'auto', lot: 100 },
  };
}
$('#cfg-form').onsubmit = async (e) => {
  e.preventDefault();
  $('#cfg-error').textContent = '';
  try {
    const cfg = readForm();
    await startJob('/api/run/start', { config: cfg });
    toast('实验已开始，可以在顶部看进度');
  } catch (err) { $('#cfg-error').textContent = err.message; }
};
$$('[data-preset]').forEach((b) => (b.onclick = () => {
  const base = JSON.parse(JSON.stringify(S.catalog.defaults));
  base.dataset = $('#cfg-form').elements.dataset.value;
  fillForm(PRESETS[b.dataset.preset](base));
  toast('已套用预设：' + b.textContent);
}));
$('#cfg-export').onclick = () => {
  let cfg;
  try { cfg = readForm(); } catch (e) { return toast(e.message, true); }
  const text = JSON.stringify(cfg, null, 2);
  modal('实验配置 JSON（可保存后用命令行 python run.py run 配置.json 运行）', `<textarea rows="18" readonly>${esc(text)}</textarea>`);
  if (navigator.clipboard) navigator.clipboard.writeText(text).then(() => toast('已复制到剪贴板')).catch(() => {});
};
$('#cfg-import').onclick = () => modal('粘贴配置 JSON', '<textarea rows="16" id="cfg-paste"></textarea>', (w) => {
  try {
    const cfg = JSON.parse($('#cfg-paste', w).value);
    fillForm(mergeDeep(JSON.parse(JSON.stringify(S.catalog.defaults)), cfg));
    toast('已载入配置');
  } catch (e) { toast('JSON 无效：' + e.message, true); return false; }
});
function mergeDeep(a, b) {
  Object.entries(b || {}).forEach(([k, v]) => { if (v && typeof v === 'object' && !Array.isArray(v) && a[k] && typeof a[k] === 'object' && k !== 'params') mergeDeep(a[k], v); else a[k] = v; });
  return a;
}

/* ------------------------------------------------------------------ 实验结果 */
async function loadResultsPage() {
  S.runs = (await api('/api/runs')).runs;
  const el = $('#run-list');
  el.innerHTML = S.runs.map((r) => {
    const p = r.perf || {};
    return `<div class="item" data-id="${esc(r.id)}"><b>${esc(r.name)}</b><small>${esc(r.created)} · ${esc(r.dataset_title || '')}</small>
      <div class="kpis"><span class="${sgn(p.cagr)}">年化 ${pct(p.cagr)}</span><span>夏普 ${num(p.sharpe)}</span><span class="down">回撤 ${pct(p.max_drawdown)}</span></div></div>`;
  }).join('') || '<div class="empty">还没有实验</div>';
  $$('#run-list .item').forEach((x) => (x.onclick = () => openRun(x.dataset.id)));
  const target = S.runs.find((r) => r.id === S.runId) ? S.runId : S.runs[0] && S.runs[0].id;
  if (target) openRun(target); else $('#run-detail').innerHTML = '<div class="empty">左侧选择一个实验。还没有实验？去“新建实验”。</div>';
}

const KPI = [
  ['cagr', '年化收益', pct, true], ['total_return', '总收益', pct, true], ['sharpe', '夏普比率', (v) => num(v)], ['max_drawdown', '最大回撤', pct],
  ['calmar', '卡玛比率', (v) => num(v)], ['excess_cagr', '超额年化', pct, true], ['info_ratio', '信息比率', (v) => num(v)], ['bench_cagr', '基准年化', pct, true],
  ['volatility', '年化波动', pct], ['excess_win_rate', '跑赢基准天数', pct],
];

async function openRun(id) {
  S.runId = id;
  $$('#run-list .item').forEach((x) => x.classList.toggle('on', x.dataset.id === id));
  let r;
  try { r = await api('/api/run?id=' + encodeURIComponent(id)); } catch (e) { return toast(e.message, true); }
  S.run = r;
  const p = r.perf || {}, t = r.trade_stats || {}, ic = (r.ic && r.ic.rank_ic) || {};
  const warn = [];
  if (r.demo_data) warn.push('<span class="chip warn">模拟数据</span>');
  if (ic.mean > 0.15) warn.push('<span class="chip warn">RankIC 异常偏高，检查数据是否有未来信息或幸存者偏差</span>');
  const kpis = KPI.map(([k, label, f, colored]) => `<div class="kpi"><span>${label}</span><b class="${colored ? sgn(p[k]) : ''}">${f(p[k])}</b></div>`).join('')
    + `<div class="kpi"><span>RankIC 均值</span><b class="${sgn(ic.mean)}">${num(ic.mean, 4)}</b><small>ICIR ${num(ic.ir, 2)} · t=${num(ic.t, 1)}</small></div>`
    + `<div class="kpi"><span>年换手</span><b>${num(t.turnover_annual, 1)} 倍</b><small>手续费/年 ${pct(t.fees_ratio_annual, 2)}</small></div>`
    + `<div class="kpi"><span>交易胜率</span><b>${pct(t.trade_win_rate)}</b><small>${num(t.n_round_trips, 0)} 笔 · 盈亏比 ${num(t.profit_factor)}</small></div>`
    + `<div class="kpi"><span>平均持有</span><b>${num(t.avg_hold_days, 1)} 天</b><small>平均持仓 ${num(t.avg_positions, 1)} 只</small></div>`;
  const models = (r.models || []).map((m) => (S.catalog ? S.catalog.models[m] : m) || m).join(' + ');
  $('#run-detail').innerHTML = `
    <div class="run-head"><div><h2>${esc(r.name)} ${warn.join(' ')}</h2>
      <div class="muted">${esc(r.created)} · ${esc(r.dataset_title)} · ${esc(models)} · 预测 ${r.horizon} 日 · 持仓 ${r.top_k} 只 · 基准 ${esc(r.benchmark)} · ${r.n_stocks} 只股票 · ${r.n_features} 个因子 · 样本外 ${num(r.n_oos, 0)} 样本 · 用时 ${r.elapsed}s</div>
      <div class="muted">回测区间 ${esc(p.start)} ~ ${esc(p.end)}（${p.days} 个交易日）</div></div>
      <div class="btns"><button class="sm" data-act="rename">重命名</button><button class="sm" data-act="rerun">用此配置新建</button>
      <button class="sm" data-exp="trades">导出交易</button><button class="sm" data-exp="nav">导出净值</button><button class="sm" data-exp="predictions">导出预测</button>
      <button class="sm" data-act="dir">打开文件夹</button><button class="sm danger" data-act="del">删除</button></div></div>
    <div class="kpi-grid">${kpis}</div>
    <div class="tabs">${['概览', '模型评估', '交易记录', '月度收益', '当前持仓', '训练过程', '配置'].map((x, i) => `<a data-tab="${i}" class="${i ? '' : 'on'}">${x}</a>`).join('')}</div>
    <div class="tab on" data-tab="0"><div class="card"><div id="c-nav" class="chart tall"></div></div></div>
    <div class="tab" data-tab="1">
      <div class="card"><div id="c-ic" class="chart"></div></div>
      <div class="grid2"><div class="card"><div id="c-grp" class="chart short"></div></div><div class="card"><div id="c-grpbar" class="chart short"></div></div></div>
      <div class="card"><div class="row between"><h2>因子重要性</h2><select id="imp-model"></select></div><div id="c-imp" class="chart tall"></div></div>
      <div class="card" id="pm-ic"></div>
      <div class="card"><h2>单因子 RankIC（样本外区间）</h2><div class="scroll"><table class="tbl" id="run-factors"></table></div></div>
    </div>
    <div class="tab" data-tab="2"><div class="card"><div class="row"><input id="tr-q" placeholder="按代码/名称筛选" class="grow"><span id="tr-count" class="muted"></span></div>
      <div class="scroll"><table class="tbl" id="tr-table"></table></div></div></div>
    <div class="tab" data-tab="3"><div class="card"><table class="tbl heat" id="mon-table"></table></div></div>
    <div class="tab" data-tab="4"><div class="card"><h2>回测结束时的持仓</h2><table class="tbl" id="pos-table"></table></div></div>
    <div class="tab" data-tab="5"><div class="card"><div class="scroll"><table class="tbl" id="fold-table"></table></div></div></div>
    <div class="tab" data-tab="6"><div class="card"><pre style="white-space:pre-wrap;font-size:12px">${esc(JSON.stringify(r.config, null, 2))}</pre></div></div>`;
  $$('#run-detail .tabs a').forEach((a) => (a.onclick = () => {
    $$('#run-detail .tabs a').forEach((x) => x.classList.toggle('on', x === a));
    $$('#run-detail .tab').forEach((x) => x.classList.toggle('on', x.dataset.tab === a.dataset.tab));
    Object.values(S.charts).forEach((c) => c.resize());
  }));
  $$('#run-detail [data-exp]').forEach((b) => (b.onclick = () => exportRun(id, b.dataset.exp)));
  $$('#run-detail [data-act]').forEach((b) => (b.onclick = () => runAction(id, b.dataset.act)));
  drawNav(r);
  drawModelTab(r);
  drawTrades(id, '');
  $('#tr-q').oninput = () => { clearTimeout(S.trTimer); S.trTimer = setTimeout(() => drawTrades(id, $('#tr-q').value), 300); };
  drawMonthly(r.monthly);
  drawPositions(r);
  drawFolds(r);
}

async function runAction(id, act) {
  try {
    if (act === 'del') { if (!confirm('删除这个实验？')) return; await api('/api/run/delete', { id }); S.runId = null; loadResultsPage(); }
    if (act === 'dir') await api('/api/open-dir', { which: 'run', id });
    if (act === 'rename') {
      const name = prompt('新名称', S.run.name);
      if (name) { await api('/api/run/rename', { id, name }); loadResultsPage(); }
    }
    if (act === 'rerun') {
      if (!S.catalog) S.catalog = await api('/api/catalog');
      go('new');
      const cfg = mergeDeep(JSON.parse(JSON.stringify(S.catalog.defaults)), S.run.config);
      cfg.name = '';
      fillForm(cfg);
      formFilled = true;
    }
  } catch (e) { toast(e.message, true); }
}

async function exportRun(id, kind) {
  try {
    const r = await api('/api/save', { id, kind });
    if (r.mode === 'browser') { location.href = `/api/export?id=${encodeURIComponent(id)}&kind=${kind}`; return; }
    if (r.saved) toast('已保存：' + r.saved);
  } catch (e) { toast(e.message, true); }
}

function drawNav(r) {
  const n = r.nav;
  const c = chart('nav', $('#c-nav'));
  c.setOption({
    animation: false, tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : Number(v).toFixed(3)) },
    legend: { top: 0, data: ['策略净值', '基准净值', '相对强弱（策略/基准）', '回撤'] },
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    grid: [{ left: 60, right: 60, top: 34, height: '58%' }, { left: 60, right: 60, top: '74%', height: '18%' }],
    xAxis: [{ type: 'category', data: n.dates, axisLabel: { show: false } }, { type: 'category', data: n.dates, gridIndex: 1 }],
    yAxis: [{ scale: true, name: '净值' }, { scale: true, name: '相对', position: 'right', splitLine: { show: false } },
      { gridIndex: 1, max: 0, axisLabel: { formatter: (v) => (v * 100).toFixed(0) + '%' } }],
    dataZoom: [{ type: 'inside', xAxisIndex: [0, 1] }, { type: 'slider', xAxisIndex: [0, 1], bottom: 0, height: 16 }],
    series: [
      { name: '策略净值', type: 'line', data: n.nav, showSymbol: false, color: COLORS[0], lineStyle: { width: 2 } },
      { name: '基准净值', type: 'line', data: n.bench, showSymbol: false, color: '#98a2b3' },
      { name: '相对强弱（策略/基准）', type: 'line', yAxisIndex: 1, data: n.excess, showSymbol: false, color: COLORS[1], lineStyle: { width: 1, type: 'dashed' } },
      { name: '回撤', type: 'line', xAxisIndex: 1, yAxisIndex: 2, data: n.drawdown, showSymbol: false, color: DOWN, areaStyle: { opacity: 0.25 }, lineStyle: { width: 1 } },
    ],
  });
}

function drawModelTab(r) {
  const s = r.ic_series;
  let cum = 0;
  const cumIc = s.rank_ic.map((v) => (cum += v || 0));
  chart('ic', $('#c-ic')).setOption({
    animation: false, title: { text: '样本外 RankIC（每个决策日）与累计 RankIC', textStyle: { fontSize: 13 } },
    tooltip: { trigger: 'axis' }, legend: { right: 70, top: 0, data: ['RankIC', '累计 RankIC'] }, grid: { left: 60, right: 60, top: 40, bottom: 56 },
    xAxis: { type: 'category', data: s.dates }, yAxis: [{ name: 'IC' }, { name: '累计', splitLine: { show: false } }],
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 4 }],
    series: [{ name: 'RankIC', type: 'bar', data: s.rank_ic.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) },
      { name: '累计 RankIC', type: 'line', yAxisIndex: 1, data: cumIc, showSymbol: false, color: COLORS[0] }],
  });
  const g = r.groups || {};
  if (g.dates) {
    chart('grp', $('#c-grp')).setOption({
      animation: false, title: { text: '分层累计收益（按打分分 5 组，不重叠持有期复利）', textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : v.toFixed(3)) }, legend: { bottom: 0 }, grid: { left: 50, right: 20, top: 40, bottom: 50 },
      xAxis: { type: 'category', data: g.dates }, yAxis: { scale: true },
      series: g.curves.map((cv, i) => ({ name: g.groups[i], type: 'line', data: cv, showSymbol: false, color: ['#079455', '#66c61c', '#98a2b3', '#f79009', '#d92d20'][i] }))
        .concat([{ name: '多空', type: 'line', data: g.long_short, showSymbol: false, color: COLORS[4], lineStyle: { type: 'dashed' } }]),
    });
    chart('grpbar', $('#c-grpbar')).setOption({
      animation: false, title: { text: `各组平均 ${r.horizon} 日收益（单调性 ${num(g.monotonic, 2)}）`, textStyle: { fontSize: 13 } },
      tooltip: { trigger: 'axis', valueFormatter: (v) => pct(v, 2) }, grid: { left: 60, right: 20, top: 40, bottom: 30 },
      xAxis: { type: 'category', data: g.groups }, yAxis: { axisLabel: { formatter: (v) => (v * 100).toFixed(1) + '%' } },
      series: [{ type: 'bar', data: g.mean.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? UP : DOWN } })) }],
    });
  }
  const sel = $('#imp-model');
  const models = Object.keys(r.importance || {});
  sel.innerHTML = models.map((m) => `<option value="${m}">${esc((S.catalog && S.catalog.models[m]) || m)}</option>`).join('') || '<option>（该模型不提供重要性）</option>';
  const drawImp = () => {
    const rows = ((r.importance || {})[sel.value] || []).slice(0, 30).reverse();
    chart('imp', $('#c-imp')).setOption({
      animation: false, grid: { left: 130, right: 30, top: 10, bottom: 20 }, tooltip: { trigger: 'axis', valueFormatter: (v) => pct(v, 2) },
      xAxis: { type: 'value', axisLabel: { formatter: (v) => (v * 100).toFixed(0) + '%' } }, yAxis: { type: 'category', data: rows.map((x) => x.name), axisLabel: { fontSize: 11 } },
      series: [{ type: 'bar', data: rows.map((x) => x.value), color: COLORS[0] }],
    });
  };
  sel.onchange = drawImp;
  drawImp();
  const pm = r.per_model_ic || {};
  $('#pm-ic').hidden = !Object.keys(pm).length;
  $('#pm-ic').innerHTML = `<h2>各模型单独的样本外 RankIC（集成前）</h2><table class="tbl"><tr><th>模型</th><th class="num">RankIC</th><th class="num">ICIR</th><th class="num">IC&gt;0</th></tr>` +
    Object.entries(pm).map(([m, v]) => `<tr><td>${esc((S.catalog && S.catalog.models[m]) || m)}</td><td class="num">${num(v.rank_ic.mean, 4)}</td><td class="num">${num(v.rank_ic.ir, 3)}</td><td class="num">${pct(v.rank_ic.positive)}</td></tr>`).join('') + '</table>';
  $('#run-factors').innerHTML = '<tr><th>因子</th><th class="num">RankIC</th><th class="num">ICIR</th><th class="num">IC&gt;0</th><th class="num">多空差</th><th>走势</th></tr>' +
    (r.factors || []).map((f) => `<tr><td>${esc(f.name)}</td><td class="num ${sgn(f.rank_ic)}">${num(f.rank_ic, 4)}</td><td class="num">${num(f.icir, 3)}</td><td class="num">${pct(f.positive)}</td><td class="num">${pct(f.spread, 2)}</td><td>${spark(f.series)}</td></tr>`).join('');
}

async function drawTrades(id, q) {
  const r = await api(`/api/run/trades?id=${encodeURIComponent(id)}&q=${encodeURIComponent(q)}`);
  $('#tr-count').textContent = `共 ${r.total} 条${r.rows.length < r.total ? `，显示最近 ${r.rows.length} 条` : ''}（完整记录请导出）`;
  $('#tr-table').innerHTML = '<tr><th>日期</th><th>代码</th><th>名称</th><th>方向</th><th class="num">成交价</th><th class="num">股数</th><th class="num">金额</th><th class="num">费用</th><th class="num">盈亏</th><th class="num">收益率</th><th class="num">持有天数</th><th>原因</th></tr>' +
    r.rows.map((t) => `<tr><td>${esc(t.date)}</td><td><a href="#" data-k="${esc(t.symbol)}">${esc(t.symbol)}</a></td><td>${esc(t.name)}</td>
      <td class="${t.side === '买入' ? 'up' : 'down'}">${esc(t.side)}</td><td class="num">${num(t.price, 2)}</td><td class="num">${num(t.shares, 0)}</td>
      <td class="num">${num(t.value, 0)}</td><td class="num">${num(t.fee, 2)}</td><td class="num ${sgn(t.pnl)}">${t.pnl == null ? '' : num(t.pnl, 0)}</td>
      <td class="num ${sgn(t.ret)}">${t.ret == null ? '' : pct(t.ret, 2)}</td><td class="num">${t.hold_days ?? ''}</td><td>${esc(t.reason)}</td></tr>`).join('');
  $$('#tr-table [data-k]').forEach((a) => (a.onclick = (e) => { e.preventDefault(); openKline(S.run.dataset, a.dataset.k, id); }));
}

function drawMonthly(m) {
  if (!m || !m.years) return;
  const cell = (v) => {
    if (v == null) return '<td></td>';
    const a = Math.min(Math.abs(v) / 0.1, 1);
    const bg = v >= 0 ? `rgba(217,45,32,${0.08 + a * 0.5})` : `rgba(7,148,85,${0.08 + a * 0.5})`;
    return `<td style="background:${bg}">${(v * 100).toFixed(1)}%</td>`;
  };
  $('#mon-table').innerHTML = '<tr><th>年份</th>' + Array.from({ length: 12 }, (_, i) => `<th>${i + 1}月</th>`).join('') + '<th>全年</th></tr>' +
    m.years.map((y) => `<tr><th>${y}</th>${m.table[y].map(cell).join('')}${cell(m.annual[y])}</tr>`).join('');
}

function drawPositions(r) {
  const rows = r.final_positions || [];
  $('#pos-table').innerHTML = '<tr><th>代码</th><th>名称</th><th class="num">股数</th><th class="num">市值</th><th class="num">浮动收益</th><th>买入日期</th></tr>' +
    (rows.map((p) => `<tr><td><a href="#" data-k="${esc(p.symbol)}">${esc(p.symbol)}</a></td><td>${esc(p.name)}</td><td class="num">${num(p.shares, 0)}</td><td class="num">${num(p.value, 0)}</td><td class="num ${sgn(p.ret)}">${pct(p.ret, 2)}</td><td>${esc(p.since)}</td></tr>`).join('') || '<tr><td colspan="6" class="muted">无持仓</td></tr>');
  $$('#pos-table [data-k]').forEach((a) => (a.onclick = (e) => { e.preventDefault(); openKline(r.dataset, a.dataset.k, r.id); }));
}

function drawFolds(r) {
  const models = r.models || [];
  $('#fold-table').innerHTML = `<tr><th>段</th><th>训练区间</th><th>测试区间（样本外）</th><th class="num">训练样本</th><th class="num">验证样本</th><th class="num">测试样本</th>${models.map((m) => `<th class="num">${esc(m)} 验证RankIC</th><th class="num">迭代</th><th class="num">用时</th>`).join('')}</tr>` +
    (r.folds || []).map((f) => `<tr><td>${f.fold}</td><td>${esc(f.train_start)} ~ ${esc(f.train_end)}</td><td>${esc(f.test_start)} ~ ${esc(f.test_end)}</td>
      <td class="num">${num(f.n_train, 0)}</td><td class="num">${num(f.n_valid, 0)}</td><td class="num">${num(f.n_test, 0)}</td>
      ${f.skipped ? `<td colspan="${models.length * 3}" class="muted">${esc(f.skipped)}</td>` : models.map((m) => { const x = f.models[m] || {}; return `<td class="num">${num(x.valid_rank_ic, 4)}</td><td class="num">${x.best_iteration ?? ''}</td><td class="num">${x.seconds ?? ''}s</td>`; }).join('')}</tr>`).join('');
}

async function openKline(ds, symbol, runId) {
  setDs(ds);
  go('data');
  await new Promise((r) => setTimeout(r, 300));
  $('#sym-q').value = symbol;
  if (runId) { const sel = $('#kl-run'); if ([...sel.options].some((o) => o.value === runId)) sel.value = runId; }
  await searchSymbols();
  loadKline(symbol);
}

/* ------------------------------------------------------------------ 对比 */
async function loadComparePage() {
  S.runs = (await api('/api/runs')).runs;
  $('#cmp-list').innerHTML = S.runs.map((r) => `<label><input type="checkbox" value="${esc(r.id)}"> ${esc(r.name)} <span class="muted">${esc(r.created)}</span></label>`).join('') || '<span class="muted">还没有实验</span>';
  $$('#cmp-list input').forEach((x) => (x.onchange = drawCompare));
}
async function drawCompare() {
  const ids = $$('#cmp-list input:checked').map((x) => x.value).slice(0, 8);
  $('#cmp-result').hidden = !ids.length;
  if (!ids.length) return;
  const r = await api('/api/compare?ids=' + ids.join(','));
  const dates = [...new Set(r.runs.flatMap((x) => x.dates))].sort();
  const series = r.runs.map((x, i) => { const m = Object.fromEntries(x.dates.map((d, j) => [d, x.nav[j]])); return { name: x.name, type: 'line', showSymbol: false, data: dates.map((d) => m[d] ?? null), connectNulls: true, color: COLORS[i % COLORS.length] }; });
  chart('cmp', $('#cmp-chart')).setOption({ animation: false, tooltip: { trigger: 'axis', valueFormatter: (v) => (v == null ? '—' : v.toFixed(3)) }, legend: { top: 0, type: 'scroll' },
    grid: { left: 60, right: 30, top: 40, bottom: 50 }, xAxis: { type: 'category', data: dates }, yAxis: { scale: true }, dataZoom: [{ type: 'inside' }, { type: 'slider', height: 14, bottom: 4 }], series });
  const rows = [['年化收益', (x) => pct(x.perf.cagr)], ['总收益', (x) => pct(x.perf.total_return)], ['夏普', (x) => num(x.perf.sharpe)], ['最大回撤', (x) => pct(x.perf.max_drawdown)],
    ['卡玛', (x) => num(x.perf.calmar)], ['超额年化', (x) => pct(x.perf.excess_cagr)], ['信息比率', (x) => num(x.perf.info_ratio)], ['RankIC', (x) => num(x.ic.rank_ic && x.ic.rank_ic.mean, 4)],
    ['ICIR', (x) => num(x.ic.rank_ic && x.ic.rank_ic.ir, 3)], ['年换手', (x) => num(x.trade_stats.turnover_annual, 1)], ['交易胜率', (x) => pct(x.trade_stats.trade_win_rate)], ['区间', (x) => `${x.perf.start} ~ ${x.perf.end}`]];
  $('#cmp-table').innerHTML = `<tr><th>指标</th>${r.runs.map((x, i) => `<th style="color:${COLORS[i % COLORS.length]}">${esc(x.name)}</th>`).join('')}</tr>` +
    rows.map(([label, f]) => `<tr><td>${label}</td>${r.runs.map((x) => `<td>${f(x)}</td>`).join('')}</tr>`).join('');
}

/* ------------------------------------------------------------------ 最新选股 */
async function loadPicksPage() {
  S.runs = (await api('/api/runs')).runs;
  const sel = $('#pk-run');
  const keep = sel.value || S.runId;
  sel.innerHTML = S.runs.map((r) => `<option value="${esc(r.id)}">${esc(r.name)} · ${esc(r.created)}</option>`).join('') || '<option value="">还没有实验</option>';
  if (S.runs.find((r) => r.id === keep)) sel.value = keep;
  drawPicks();
}
$('#pk-run').onchange = drawPicks;
$('#pk-export').onclick = () => $('#pk-run').value && exportRun($('#pk-run').value, 'picks');
async function drawPicks() {
  const id = $('#pk-run').value;
  if (!id) { $('#pk-table tbody').innerHTML = ''; return; }
  const r = await api('/api/run/picks?id=' + encodeURIComponent(id));
  const run = S.runs.find((x) => x.id === id) || {};
  $('#pk-head').textContent = `打分日期 ${r.date}（下一个交易日开盘执行） · 股票池 ${r.n_universe} 只 · 显示前 ${r.picks.length} 名 · 数据集 ${run.dataset_title || ''}`;
  $('#pk-table tbody').innerHTML = r.picks.map((p) => `<tr><td>${p.rank}</td><td>${esc(p.symbol)}</td><td>${esc(p.name)}</td><td class="num">${num(p.score, 4)}</td><td class="num">${num(p.close, 2)}</td>
    <td><button class="sm" data-k="${esc(p.symbol)}">K线</button></td></tr>`).join('');
  const detail = await api('/api/run?id=' + encodeURIComponent(id)).catch(() => null);
  $$('#pk-table [data-k]').forEach((b) => (b.onclick = () => detail && openKline(detail.dataset, b.dataset.k, id)));
}

/* ------------------------------------------------------------------ 启动 */
(async function init() {
  try {
    S.catalog = await api('/api/catalog');
    await loadDataPage();
  } catch (e) { toast('初始化失败：' + e.message, true, 10000); }
  setInterval(() => api('/api/heartbeat', {}).catch(() => {}), 60000);
})();
