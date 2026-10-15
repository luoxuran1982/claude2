"use strict";
// 「审片 / 分镜」：生成完成后只读查看每一页画面。不影响一键生成（生成前不需要审核）。
// 审片（总览）：照「AI 知识视频工坊」的审片页，一屏铺开多列缩略图网格，每格：缩略图、序号和版式名、起止时间、标题、口播文字；
//   点一格，左边的成片跳到这一页的时间播放。
// 分镜（逐镜详情）：照工坊的分镜页，左边分镜列表（序号、版式、标题），右边这一镜的大图、画面类型、标题、副标题、
//   口播原文和字数、时长、起止时间、对应句子编号，以及导演给的结构化数据（表格、代码、要点……）。
// 数据：/api/storyboard?task=…（svf/review.py 读任务目录的 svf_storyboard.json），图片和成片：/taskfiles/<任务>/<文件>。
window.SVFReview = function (ctx) {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const fmt = t => { t = Math.max(0, +t || 0); const m = Math.floor(t / 60); return `${m}:${(t - m * 60).toFixed(1).padStart(4, "0")}`; };
  const MODE_TEXT = { ai: "AI 导演设计的分镜", rule: "规则切分（程序按标点切句、按句子特征选版式）", stock: "库存视频素材" };
  let tasks = [], taskId = null, data = null, mode = "grid", sel = 0;

  const file = rel => rel ? `/taskfiles/${encodeURIComponent(taskId)}/${rel.split("/").map(encodeURIComponent).join("/")}` : "";

  async function loadTasks(prefer) {
    const r = await ctx.api("/api/tasks");
    tasks = r.tasks || [];
    const want = prefer || taskId;
    taskId = tasks.some(t => t.id === want) ? want : (tasks.find(t => t.storyboard) || tasks[0] || {}).id || null;
    $("rv-task").innerHTML = tasks.length ? tasks.map(t => `<option value="${esc(t.id)}">${esc(t.mtime)} · ${esc(t.subject || "（无主题）")} · ${esc(t.source || "?")}${t.storyboard ? "" : "（无分镜）"}</option>`).join("")
      : `<option value="">还没有任务</option>`;
    if (taskId) $("rv-task").value = taskId;
  }

  async function render(opts) {
    opts = opts || {};
    if (opts.mode) mode = opts.mode;
    try { await loadTasks(opts.task); } catch (e) { $("rv-body").innerHTML = `<p class="banner error">读不到任务列表：${esc(e.message)}</p>`; return; }
    if (!taskId) { $("rv-info").innerHTML = ""; $("rv-body").innerHTML = `<p class="muted">还没有任务。在「视频生成」里生成一条视频后再来这里看。</p>`; setMode(); return; }
    try { data = await ctx.api("/api/storyboard?task=" + encodeURIComponent(taskId)); }
    catch (e) { $("rv-body").innerHTML = `<p class="banner error">读不到这个任务：${esc(e.message)}</p>`; return; }
    sel = opts.page != null ? Math.max(0, opts.page) : Math.min(sel, ((data.manifest || {}).pages || []).length - 1);
    if (sel < 0) sel = 0;
    draw();
  }

  function info() {
    const m = data.manifest;
    if (!m) return `<div class="note warn">这个任务没有分镜清单。分镜视图支持「程序生成」组（知识讲解卡片、AI 导演、卡片 + AI 插图、混合）和库存视频来源（Pexels、Pixabay 等），
      而且要是 1.1.0 及以后生成的任务；AI 视频、OpenAI 兼容文生图、本地文件来源暂不支持。</div>`;
    const d = m.director || {}, il = m.illustration || {};
    const bits = [`<b>${esc(m.source_label || m.source)}</b>`, `分镜：${esc(MODE_TEXT[m.mode] || m.mode)}`, `${(m.pages || []).length} 页`];
    if (m.theme) bits.push(`主题 ${esc(m.theme)}`);
    if (m.W) bits.push(`${m.W}×${m.H}`);
    let extra = "";
    if (d.used && !d.ok) extra += `<div class="note warn">AI 导演没有用上，退回了规则切分。原因：${esc(d.fallback_reason)}（详细见「模型调用」）</div>`;
    if (d.used && d.ok && (d.warnings || []).length) extra += `<div class="note">AI 导演的分镜通过了校验（第 ${d.attempts} 次），程序修整了：${esc(d.warnings.join("；"))}</div>`;
    if (il.skipped) extra += `<div class="note warn">AI 插图：${esc(il.skipped)}</div>`;
    if ((il.results || []).length) extra += `<div class="note">AI 插图：${il.results.map(r => `第 ${r.page} 页${r.ok ? "已生成" : "没生成（" + esc(r.error) + "）"}`).join("；")}</div>`;
    if (m.note) extra += `<div class="note">${esc(m.note)}</div>`;
    return `<div class="rv-meta">${bits.join(" · ")}${data.video ? "" : " · <span class='muted'>还没有成片</span>"}</div>${extra}`;
  }

  function setMode() {
    $("rv-mode-grid").classList.toggle("on", mode === "grid");
    $("rv-mode-detail").classList.toggle("on", mode === "detail");
  }

  function draw() {
    setMode();
    $("rv-info").innerHTML = info();
    const m = data.manifest;
    if (!m || !(m.pages || []).length) { $("rv-body").innerHTML = ""; return; }
    const portrait = m.H > m.W;
    if (mode === "grid") {
      const video = data.video ? `<div class="rv-player${portrait ? " portrait" : ""}"><video id="rv-video" controls preload="metadata" src="${file(data.video)}"></video>
        <p class="muted small">点右边任意一格，成片跳到那一页开始播放；播放时当前页会高亮。</p></div>` : "";
      $("rv-body").innerHTML = `<div class="rv-grid-wrap${video ? "" : " novideo"}">${video}<div class="rv-grid${portrait ? " portrait" : ""}" id="rv-grid">${m.pages.map((p, i) => `
        <div class="tile" data-i="${i}" tabindex="0" title="点一下：成片跳到 ${fmt(p.start)} 播放">
          ${p.thumb ? `<img loading="lazy" src="${file(p.thumb)}" alt="第 ${p.index} 页">` : `<div class="noimg">没有画面</div>`}
          <div class="tile-body"><div class="tile-head"><span class="badge">${p.index} · ${esc(p.kind_label)}</span>
            <span class="muted small">${p.approx ? "约 " : ""}${fmt(p.start)}–${fmt(p.end)}</span>
            <a href="#" class="tile-more" data-detail="${i}">详情 ›</a></div>
          <b class="tile-title">${esc(p.heading)}</b>
          ${p.text ? `<div class="tile-text">${esc(p.text)}</div>` : ""}</div>
        </div>`).join("")}</div></div>`;
      const v = $("rv-video");
      if (v) v.addEventListener("timeupdate", () => highlight(v.currentTime));
    } else {
      $("rv-body").innerHTML = `<div class="rv-detail"><aside class="rv-list" id="rv-list">${m.pages.map((p, i) => `
        <button type="button" class="rv-li${i === sel ? " on" : ""}" data-sel="${i}"><span class="badge">${p.index}</span> <small>${esc(p.kind_label)}</small><br>${esc(p.heading)}</button>`).join("")}</aside>
        <article class="rv-one" id="rv-one">${detail(m.pages[sel], portrait)}</article></div>`;
    }
  }

  function highlight(t) {
    const pages = data.manifest.pages;
    const i = pages.findIndex(p => t >= p.start && t < p.end);
    document.querySelectorAll("#rv-grid .tile").forEach(el => el.classList.toggle("playing", +el.dataset.i === i));
  }

  function dataHTML(p) {
    const d = p.data || {}, k = p.kind;
    const list = a => `<ol>${(a || []).map(x => `<li>${esc(x)}</li>`).join("")}</ol>`;
    if (k === "bullets") return list(d.items);
    if (k === "flow") return `<div class="flow">${(d.nodes || []).map(esc).map(x => `<span>${x}</span>`).join(" → ")}</div>`;
    if (k === "compare") return `<div class="cmp"><div><b>${esc(d.left_title)}</b>${list(d.left)}</div><div><b>${esc(d.right_title)}</b>${list(d.right)}</div></div>`;
    if (k === "table") return `<div class="table-wrap"><table class="data"><thead><tr>${(d.headers || []).map(h => `<th>${esc(h)}</th>`).join("")}</tr></thead>
      <tbody>${(d.rows || []).map(r => `<tr>${r.map(c => `<td>${esc(c)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`;
    if (k === "code") return `<div class="muted small">语言：${esc(d.language)}${(d.highlight || []).length ? "，强调第 " + d.highlight.join("、") + " 行" : ""}</div><pre class="code">${esc(d.code)}</pre>`;
    if (k === "quote") return `<blockquote>${esc(d.text)}${d.source ? `<br><small>—— ${esc(d.source)}</small>` : ""}</blockquote>`;
    if (k === "stat") return `<div class="stat"><b>${esc(d.value)}</b> ${esc(d.label)}</div>`;
    if (k === "chart") return `<div class="muted small">${d.chart === "line" ? "折线图" : "柱状图"}${d.unit ? "，单位 " + esc(d.unit) : ""}</div>
      <table class="data"><tr>${(d.labels || []).map(x => `<th>${esc(x)}</th>`).join("")}</tr><tr>${(d.values || []).map(x => `<td>${esc(x)}</td>`).join("")}</tr></table>`;
    if (k === "keyword") return `<div class="stat"><b>${esc(d.keyword)}</b> ${esc(d.note || "")}</div>`;
    if (k === "title") return d.subtitle ? `<div>副标题：${esc(d.subtitle)}</div>` : `<div class="muted">（没有副标题）</div>`;
    if (k === "text") return `<div>${esc(d.body || "")}</div>`;
    if (k === "stock") return `<div>搜索关键词：<b>${esc(d.search_term || "—")}</b><br>素材文件：${esc(d.clip || "")}${d.provider ? "（" + esc(d.provider) + "）" : ""}
      ${d.source_page ? `<br>素材页：${esc(d.source_page)}` : ""}${d.card_kind ? "<br>（混合来源：这一页原本是知识卡片，换成了这段素材）" : ""}</div>`;
    return `<pre class="code">${esc(JSON.stringify(d, null, 2))}</pre>`;
  }

  function detail(p, portrait) {
    const row = (k, v) => `<tr><th>${k}</th><td>${v}</td></tr>`;
    return `<div class="rv-one-grid${portrait ? " portrait" : ""}">
      <div class="rv-big">${p.image ? `<img src="${file(p.image)}" alt="第 ${p.index} 页大图">` : `<div class="noimg">没有画面</div>`}</div>
      <div class="rv-fields"><h2>${p.index} · ${esc(p.heading)}</h2>
        <table class="kvt">
          ${row("画面类型", `${esc(p.kind_label)} <code>${esc(p.kind)}</code>`)}
          ${row("标题", esc(p.heading))}
          ${p.kind === "title" ? row("副标题", esc(p.subtitle || "—")) : ""}
          ${row("时长", `${(+p.duration).toFixed(1)} 秒`)}
          ${row("起止时间", `${p.approx ? "约 " : ""}${fmt(p.start)} – ${fmt(p.end)}`)}
          ${p.sentences ? row("对应原文句子", "第 " + p.sentences.join("、") + " 句") : ""}
          ${p.text ? row("口播原文", `${esc(p.text)}<div class="muted small">${p.chars} 字</div>`) : ""}
          ${p.reveals && p.reveals.length > 1 ? row("逐条亮起", p.reveals.map(fmt).join("、")) : ""}
          ${p.illustration ? row("AI 插图", `<img class="illus" src="${file(p.illustration)}" alt="AI 插图">`) : ""}
        </table>
        <h3>结构化数据</h3>${dataHTML(p)}
        <details><summary>原始数据（JSON）</summary><pre class="code">${esc(JSON.stringify(p.data, null, 2))}</pre></details>
        <div class="btn-row">${data.video ? `<button class="btn" type="button" data-play="${p.index - 1}">▶ 在成片里从这一页播放</button>` : ""}
          <button class="btn" type="button" data-step="-1">‹ 上一镜</button><button class="btn" type="button" data-step="1">下一镜 ›</button></div>
      </div></div>`;
  }

  function play(i) {
    mode = "grid"; draw();
    const v = $("rv-video"), p = data.manifest.pages[i];
    if (!v || !p) return;
    const go = () => { v.currentTime = p.start + 0.05; v.play().catch(() => {}); highlight(p.start + 0.05); };
    if (v.readyState >= 1) go(); else v.addEventListener("loadedmetadata", go, { once: true });
    const el = document.querySelector(`#rv-grid .tile[data-i="${i}"]`);
    if (el) el.scrollIntoView({ block: "nearest" });
  }

  $("rv-body").addEventListener("click", e => {
    const more = e.target.closest("[data-detail]");
    if (more) { e.preventDefault(); sel = +more.dataset.detail; mode = "detail"; draw(); return; }
    const tile = e.target.closest(".tile");
    if (tile) {
      const v = $("rv-video"), p = data.manifest.pages[+tile.dataset.i];
      if (v) { v.currentTime = p.start + 0.05; v.play().catch(() => {}); highlight(p.start + 0.05); }
      else { sel = +tile.dataset.i; mode = "detail"; draw(); }
      return;
    }
    const li = e.target.closest("[data-sel]");
    if (li) { sel = +li.dataset.sel; draw(); return; }
    const st = e.target.closest("[data-step]");
    if (st) { sel = Math.min(Math.max(0, sel + +st.dataset.step), data.manifest.pages.length - 1); draw(); return; }
    const pl = e.target.closest("[data-play]");
    if (pl) play(+pl.dataset.play);
  });
  $("rv-body").addEventListener("keydown", e => {
    if (e.key === "Enter" && e.target.classList.contains("tile")) e.target.click();
  });
  $("rv-task").onchange = e => { taskId = e.target.value; sel = 0; render(); };
  document.querySelectorAll("[data-rvmode]").forEach(b => { b.onclick = () => { mode = b.dataset.rvmode; if (data) draw(); }; });
  $("rv-refresh").onclick = () => render();
  return { render };
};
