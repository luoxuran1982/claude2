"use strict";
// 「模型调用」：先选任务（默认最新一个有调用的），再按时间顺序列出这个任务的每一次大模型调用，原汁原味。
// 每次调用分块，用中文标明「谁说的」：
//   ① 程序发给模型的（每条 message 原样，标 role；固定模板 / 程序填进去的变量 / 程序加的要求用颜色区分；
//      前面某次调用的输出出现在这里的位置，用绿色下划线标出「来自 #N 的输出」）
//   ② 模型的思考过程（reasoning_content，有才显示，默认折叠）
//   ③ 模型返回的内容（原文）
//   ④ 程序最后怎么用它（取作文案 / 解析成关键词列表 / 解析成分镜 JSON……）
//   ⑤ 用量和耗时（次要）  ⑥「查看原始数据」（整条记录的 JSON，已去掉 API Key）
// 数据：/api/llm-calls?task=…（svf/review.py 读 llm_calls.jsonl 并算调用链）。
window.SVFCalls = function (ctx) {
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const LONG = 1500;   // 超过这么多字的文本默认收起
  let tasks = [], taskId = null, data = null;

  $("lc-legend").innerHTML = `<b>颜色说明</b>
    <span class="run tpl">固定模板（程序每次都发的提示词原文）</span>
    <span class="run var"><span class="tag">变量名</span>程序填进去的变量（你的主题、目标字数……）</span>
    <span class="run prog"><span class="tag">程序加的</span>程序按设置加的整段要求</span>
    <span class="run from"><span class="tag from">来自 #1 的输出</span>上一次调用的输出，在这次输入里出现的地方</span>`;

  function copy(text) {   // 独立窗口（pywebview）里 navigator.clipboard 可能不可用，退回 execCommand
    const fallback = () => new Promise((resolve, reject) => {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      try { document.execCommand("copy") ? resolve() : reject(new Error("copy")); } catch (err) { reject(err); }
      ta.remove();
    });
    return navigator.clipboard && window.isSecureContext ? navigator.clipboard.writeText(text).catch(fallback) : fallback();
  }
  const stash = [];
  const copyBtn = text => { stash.push(text); return `<button type="button" class="btn small" data-copy="${stash.length - 1}">复制</button>`; };

  async function loadTasks(prefer) {
    const r = await ctx.api("/api/tasks");
    tasks = r.tasks || [];
    const want = prefer || taskId;
    taskId = tasks.some(t => t.id === want) ? want : (tasks.find(t => t.calls) || tasks[0] || {}).id || null;
    $("lc-task").innerHTML = tasks.length ? tasks.map(t => `<option value="${esc(t.id)}">${esc(t.mtime)} · ${esc(t.subject || "（无主题）")} · ${t.calls ? t.calls + " 次调用" : "没有调用大模型"}</option>`).join("")
      : `<option value="">还没有任务</option>`;
    if (taskId) $("lc-task").value = taskId;
  }

  async function render(opts) {
    opts = opts || {};
    try { await loadTasks(opts.task); } catch (e) { $("lc-body").innerHTML = `<p class="banner error">读不到任务列表：${esc(e.message)}</p>`; return; }
    if (!taskId) { $("lc-body").innerHTML = `<p class="muted">还没有任务。</p>`; return; }
    try { data = await ctx.api("/api/llm-calls?task=" + encodeURIComponent(taskId)); }
    catch (e) { $("lc-body").innerHTML = `<p class="banner error">读不到这个任务的调用记录：${esc(e.message)}</p>`; return; }
    draw();
  }

  function runsHTML(runs) {
    let lastFrom = null;
    return runs.map(r => {
      let tag = "";
      if (r.from != null && r.from !== lastFrom) tag += `<span class="tag from">来自 #${r.from} 的输出</span>`;
      if ((r.k === "var" || r.k === "prog") && r.n) tag += `<span class="tag">${esc(r.n)}</span>`;
      lastFrom = r.from;
      return `<span class="run ${esc(r.k)}${r.from != null ? " from" : ""}" title="${r.k === "var" ? "程序填进去的变量：" + esc(r.n) : r.k === "prog" ? "程序加的：" + esc(r.n) : r.k === "tpl" ? "固定模板" : ""}${r.from != null ? "（来自第 " + r.from + " 次调用的输出）" : ""}">${tag}${esc(r.t)}</span>`;
    }).join("");
  }

  function block(title, cls, body, text, open) {
    const long = (text || "").length > LONG;
    return `<details class="lc-block ${cls}"${open && !long ? " open" : ""}><summary><span>${title}</span>
      <span class="muted small">${(text || "").length} 字${long ? "，较长，点开查看" : ""}</span>${text != null ? copyBtn(text) : ""}</summary>${body}</details>`;
  }

  function usageLine(c) {
    const u = c.usage || {};
    const tok = Object.keys(u).length ? `token：输入 ${u.prompt_tokens ?? "?"}，输出 ${u.completion_tokens ?? "?"}${u.reasoning_tokens != null ? `（其中思考 ${u.reasoning_tokens}）` : ""}，合计 ${u.total_tokens ?? "?"}`
      : "token 用量：接口没有返回";
    return `${esc(c.provider || "?")} / <code>${esc(c.model || "?")}</code>${c.base_url ? " · " + esc(c.base_url) : ""} · 耗时 ${(+c.elapsed || 0).toFixed(1)} 秒 · ${tok}`;
  }

  function draw() {
    stash.length = 0;
    const calls = data.calls || [];
    const info = data.info || {};
    let head = `<div class="lc-head">任务「${esc(data.subject || taskId)}」共 <b>${calls.length}</b> 次大模型调用，按时间顺序。`;
    if (info.length_mode) head += ` 字数控制方式：<b>${esc({ strict: "让大模型自己数（严格）", check: "程序检查（推荐）", off: "不控制" }[info.length_mode] || info.length_mode)}</b>`
      + (info.target_chars ? `，目标约 ${info.target_chars} 字` + (info.first_chars != null ? `，模型写了 ${info.first_chars} 字` : "") + (info.revised ? `，改写后 ${info.final_chars} 字` : "") : "") + "。";
    head += `</div>`;
    if (!calls.length) {
      $("lc-body").innerHTML = head + `<p class="muted">这个任务没有调用大模型：自己写好文案、又用「规则切分」知识卡片时，整个任务都不需要大模型。</p>`;
      return;
    }
    $("lc-body").innerHTML = head + calls.map((c, i) => {
      const msgs = c.messages.map(m => block(`① 程序发给模型的 <code>role: ${esc(m.role)}</code>${m.role === "system" ? "（系统提示）" : "（用户消息）"}`, "in",
        `<pre class="lc-text">${runsHTML(m.runs)}</pre>`, m.content, true)).join("");
      const from = c.inputs_from.length ? `<div class="lc-chain">↖ 这次的输入里用到了 ${c.inputs_from.map(f => `<a href="#lc-${f.call}">#${f.call} 的输出</a>（${f.chars} 字）`).join("、")}，下面绿色下划线标出了位置。</div>` : "";
      const reasoning = c.reasoning ? block("② 模型的思考过程 <code>reasoning_content</code>", "think", `<pre class="lc-text">${esc(c.reasoning)}</pre>`, c.reasoning, false)
        : `<div class="lc-none muted small">② 模型的思考过程：这次没有（模型没返回 reasoning_content，或不是推理模型）</div>`;
      const out = block(`③ 模型返回的内容（原文）${c.ok ? "" : ` <span class="badge bad">出错：${esc(c.error)}</span>`}`, "out",
        `<pre class="lc-text">${esc(c.output || "（空）")}</pre>`, c.output || "", true);
      const uses = `<div class="lc-use"><b>④ 程序最后怎么用它</b><ul>${c.uses.map(u => `<li>${esc(u)}</li>`).join("")}</ul>
        ${c.outputs_to.length ? `<div class="lc-chain">↓ 这次的输出进了 ${c.outputs_to.map(f => `<a href="#lc-${f.call}">#${f.call} 的输入</a>`).join("、")}</div>` : ""}</div>`;
      const raw = `<details class="lc-raw"><summary>⑥ 查看原始数据（整条记录的 JSON，已去掉 API Key）</summary>${copyBtn(JSON.stringify(c.raw, null, 2))}<pre class="lc-text">${esc(JSON.stringify(c.raw, null, 2))}</pre></details>`;
      return `<section class="lc-call${c.ok ? "" : " failed"}" id="lc-${c.index}">
        <h3>#${c.index} ${esc(c.step_label)} <span class="muted small">第 ${c.attempt} 次尝试 · ${c.ok ? "成功" : "失败"} · ${esc(c.time)}${c.from_ui ? " · 在界面上点「生成文案」时调用的" : ""}</span></h3>
        ${from}${msgs}${reasoning}${out}${uses}
        <div class="lc-usage muted small">⑤ ${usageLine(c)}</div>${raw}</section>${i + 1 < calls.length ? `<div class="lc-arrow">↓</div>` : ""}`;
    }).join("");
  }

  $("lc-body").addEventListener("click", e => {
    const b = e.target.closest("[data-copy]");
    if (b) {
      e.preventDefault(); e.stopPropagation();
      copy(stash[+b.dataset.copy] || "").then(() => ctx.toast("已复制"), () => ctx.toast("复制失败，请手动选中文字复制"));
      return;
    }
    const a = e.target.closest('a[href^="#lc-"]');
    if (a) { e.preventDefault(); const el = document.querySelector(a.getAttribute("href")); if (el) el.scrollIntoView({ behavior: "smooth" }); }
  });
  $("lc-task").onchange = e => { taskId = e.target.value; render(); };
  $("lc-refresh").onclick = () => render();
  return { render };
};
