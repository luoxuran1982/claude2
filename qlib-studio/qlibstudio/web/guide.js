"use strict";
// 「使用指南」与「运行状态」进程表。文档和源码都由本机服务按白名单提供，随应用打包，离线可读。
window.QGuide = function (ctx) {
  const { api, toast, esc } = ctx;
  const $ = id => document.getElementById(id);
  const KEY = "qlibstudio-guide-v1";
  let index = null, current = null;
  try { current = localStorage.getItem(KEY); } catch { /* 无痕模式 */ }
  const remember = v => { current = v; try { localStorage.setItem(KEY, v); } catch { /* 无痕模式 */ } };
  const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B";
  const GROUPS = { shell: "Qlib 量化研究台外壳（我们写的）", upstream: "上游 Qlib v0.9.8（MIT，补丁见 UPSTREAM.md）" };

  async function text(url) {
    const res = await fetch(url);
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    return res.text();
  }

  // 文档里的相对链接 → 应用内地址
  function resolver(docPath) {
    const dir = docPath.includes("/") ? docPath.slice(0, docPath.lastIndexOf("/") + 1) : "";
    return (href, isImage) => {
      if (/^(https?:|mailto:)/.test(href)) return { href, external: true };
      if (href.startsWith("#")) return { href, doc: docPath, anchor: href.slice(1) };
      const [p, anchor] = href.split("#");
      const parts = [];
      for (const seg of decodeURIComponent(dir + p).split("/")) {
        if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg);
      }
      const rel = parts.join("/");
      if (isImage || index.assets.some(a => a.path === rel)) return { href: "/api/guide/file?path=" + encodeURIComponent(rel), external: !isImage };
      if (index.docs.some(d => d.path === rel)) return { href: "#", doc: rel, anchor };
      if (index.sources.some(s => s.path === rel)) return { href: "#", source: rel };
      const dirHit = index.sources.find(s => s.path.startsWith(rel.replace(/\/$/, "") + "/"));
      if (dirHit) return { href: "#", source: "dir:" + rel.replace(/\/$/, "") };
      return { href: "#", doc: null };
    };
  }

  function navHTML() {
    const item = (key, label, sub) => `<button class="gn${current === key ? " on" : ""}" data-key="${esc(key)}">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</button>`;
    const docs = index.docs.map(d => item("doc:" + d.path, d.name, d.path)).join("");
    const assets = index.assets.map(a => `<a class="gn" href="/api/guide/file?path=${encodeURIComponent(a.path)}" target="_blank" rel="noreferrer">${esc(a.name)}<small>${esc(a.note)} · ${esc(a.path)} · ${kb(a.size)}</small></a>`).join("") || '<p class="muted">（暂无）</p>';
    const t = index.source_totals;
    return `<h3>文档（${index.docs.length} 篇）</h3>${docs}
      <h3>源码</h3>${item("sources:shell", "研究台外壳", `${t.shell.files} 个文件 · ${t.shell.lines.toLocaleString("zh-CN")} 行`)}
      ${item("sources:upstream", "上游 Qlib", `${t.upstream.files} 个文件 · ${t.upstream.lines.toLocaleString("zh-CN")} 行`)}
      <button class="gn" data-zip>下载全部源码（zip）<small>外壳 + 上游 Qlib + 文档，目录同仓库 qlib-studio/</small></button>
      <h3>附件</h3>${assets}
      <h3>运行</h3><button class="gn" data-goto="status">运行状态（进程与内存）</button>
      <p class="muted">Qlib 量化研究台 ${esc(index.version)}，上游 Qlib ${esc(index.upstream_version)}（${esc(index.upstream_commit)}）。文档随应用打包，离线可读；内容与仓库 qlib-studio/docs/ 一致。</p>`;
  }

  async function show(key, anchor) {
    if (!index) return;
    const valid = key && (key.startsWith("sources:") || key.startsWith("src:") || index.docs.some(d => "doc:" + d.path === key));
    if (!valid) {
      const first = index.docs.find(d => d.path.startsWith("docs/00")) || index.docs[0];
      key = "doc:" + first.path;
    }
    remember(key);
    $("guide-nav").innerHTML = navHTML();
    const body = $("guide-body");
    body.innerHTML = '<p class="muted">正在读取…</p>';
    try {
      if (key.startsWith("doc:")) {
        const path = key.slice(4);
        const md = await text("/api/guide/doc?path=" + encodeURIComponent(path));
        body.innerHTML = `<div class="doc-path muted">${esc(path)}</div><div class="md">${window.KMarkdown.render(md, resolver(path))}</div>`;
      } else if (key.startsWith("sources:")) {
        body.innerHTML = sourcesHTML(key.slice(8), anchor);
      } else {
        const path = key.slice(4);
        const src = await text("/api/guide/source?path=" + encodeURIComponent(path));
        const lines = src.replace(/\n$/, "").split("\n");
        const g = (index.sources.find(s => s.path === path) || {}).group || "shell";
        body.innerHTML = `<div class="src-head"><button class="btn small" data-key="sources:${g}">← ${esc(GROUPS[g])}</button><b>${esc(path)}</b>
          <span class="muted">${lines.length} 行 · 只读</span></div>
          <div class="src-view"><table><tbody>${lines.map((l, k) => `<tr><td class="ln">${k + 1}</td><td><pre>${esc(l) || " "}</pre></td></tr>`).join("")}</tbody></table></div>`;
      }
    } catch (e) {
      body.innerHTML = `<div class="banner error">读取失败：${esc(e.message)}</div>`;
    }
    const target = anchor && body.querySelector(`[id="${CSS.escape(anchor)}"]`);
    if (target) target.scrollIntoView(); else window.scrollTo(0, 0);
  }

  function sourcesHTML(group, dirFilter) {
    const list = index.sources.filter(s => s.group === group && (!dirFilter || s.path.startsWith(dirFilter + "/")));
    const groups = {};
    list.forEach(s => { const d = s.path.includes("/") ? s.path.slice(0, s.path.lastIndexOf("/")) : "（项目根目录）"; (groups[d] = groups[d] || []).push(s); });
    const total = list.reduce((n, s) => n + s.lines, 0);
    const intro = group === "shell"
      ? `我们为桌面版写的全部文件：窗口、本机服务、任务管理、工作进程、数据导入转换、实验流程、对比、网页界面、打包脚本和测试。阅读顺序建议：
         run.py → qlibstudio/app.py → server.py → jobs.py → worker.py → data/convert.py → experiment.py → compare.py → web/app.js。详见「03 源码阅读与重建指南」。`
      : `上游 Qlib v0.9.8 的源码副本（不含文档图片和预训练模型 .pkl，见 UPSTREAM.md）。改动处带「Qlib 量化研究台补丁」注释。
         阅读顺序建议：qlib/__init__.py → data/data.py → data/ops.py → contrib/data/handler.py → contrib/data/loader.py → data/dataset/__init__.py → contrib/model/gbdt.py → backtest/__init__.py → backtest/exchange.py → contrib/strategy/signal_strategy.py → contrib/evaluate.py。`;
    return `<div class="src-head"><h2>${esc(GROUPS[group])}${dirFilter ? " · " + esc(dirFilter) : ""}</h2><button class="btn primary small" data-zip>下载全部源码（zip）</button></div>
      <p class="muted">${list.length} 个文件、${total.toLocaleString("zh-CN")} 行。${intro} zip 包含外壳、上游和全部文档，目录结构与仓库 qlib-studio/ 相同。</p>
      ${Object.entries(groups).map(([d, items]) => `<h3>${esc(d)}</h3><table class="data src-list"><tbody>${items.map(s =>
        `<tr class="clickable" data-key="src:${esc(s.path)}"><td>${esc(s.path)}</td><td class="num">${s.lines} 行</td><td class="num">${kb(s.size)}</td></tr>`).join("")}</tbody></table>`).join("")}`;
  }

  async function renderGuide() {
    if (!index) {
      try { index = await api("/api/guide"); }
      catch (e) { $("guide-body").innerHTML = `<div class="banner error">读取文档清单失败：${esc(e.message)}</div>`; return; }
    }
    show(current);
  }

  async function downloadZip() {
    try {
      const r = await api("/api/save", { kind: "source-zip" });
      if (r.mode === "browser") { location.href = "/api/guide/source.zip"; return; }
      if (r.saved) toast("已保存：" + r.saved, 6000);
    } catch (e) { toast("保存失败：" + e.message); }
  }

  function onClick(e) {
    const a = e.target.closest("a[data-doc], a[data-source]");
    if (a) {
      e.preventDefault();
      if (a.dataset.source) {
        const s = a.dataset.source;
        if (s.startsWith("dir:")) {
          const dir = s.slice(4), hit = index.sources.find(x => x.path.startsWith(dir + "/"));
          return show("sources:" + (hit ? hit.group : "shell"), dir);
        }
        return show("src:" + s);
      }
      return show("doc:" + a.dataset.doc, a.dataset.anchor);
    }
    const ext = e.target.closest('.md a[target="_blank"]');
    if (ext && /^https?:/.test(ext.getAttribute("href"))) { e.preventDefault(); api("/api/open", { what: "url", url: ext.getAttribute("href") }).catch(() => {}); return; }
    const dead = e.target.closest('.md a[href="#"]');
    if (dead) { e.preventDefault(); return toast("这个链接指向应用外的文件，安装版里没有"); }
    if (e.target.closest("[data-zip]")) return downloadZip();
    const k = e.target.closest("[data-key]");
    if (k) return show(k.dataset.key);
    const g = e.target.closest("[data-goto]");
    if (g) return ctx.switchTab(g.dataset.goto);
  }
  $("guide-nav").addEventListener("click", onClick);
  $("guide-body").addEventListener("click", onClick);

  // ---------- 运行状态：进程与内存 ----------
  let procTimer = null;
  async function renderProcs() {
    clearTimeout(procTimer);
    const box = $("proc-body");
    try {
      const p = await api("/api/processes");
      const rows = p.processes.map(r => `<tr><td>${esc(r.role)}</td><td>${esc(r.name)}</td><td class="num">${r.pid}</td>
        <td class="num">${r.mem_mib == null ? '<span class="muted">暂不可用</span>' : r.mem_mib.toFixed(1)}</td></tr>`).join("");
      box.innerHTML = `<p class="muted">采样时间 <b id="proc-time">${esc(p.sampled_at)}</b> · 口径：${esc(p.metric)} · 单位 MiB（1 MiB = 1024² 字节）· 每 3 秒重新读取</p>
        <div class="table-wrap"><table class="data" id="proc-table"><thead><tr><th>职责</th><th>进程名</th><th class="num">PID</th><th class="num">内存（MiB）</th></tr></thead>
        <tbody>${rows}</tbody>${p.total_mib != null ? `<tfoot><tr><td colspan="3">合计（${p.processes.length} 个进程）</td><td class="num">${p.total_mib.toFixed(1)}</td></tr></tfoot>` : ""}</table></div>
        <ul class="proc-notes">
          <li><b>${esc(p.backend_note)}</b></li>
          ${p.total_note ? `<li>${esc(p.total_note)}</li>` : ""}
          ${p.note ? `<li>${esc(p.note)}</li>` : ""}
          <li>主进程里的线程（共用上面的主进程 PID，不是独立进程）：${p.threads.map(esc).join("、")}</li>
          <li>外部依赖（不计入本应用合计）：${p.external.map(x => esc(x.name) + (x.pid ? `（PID ${x.pid}，${x.mem_mib == null ? "内存暂不可用" : x.mem_mib.toFixed(1) + " MiB"}）` : "（" + esc(x.note) + "）")).join("；")}</li>
          <li>Windows 任务管理器「详细信息」默认显示「专用工作集」，比这里的工作集小；macOS 活动监视器「内存」列不是 RSS，口径不同，数值会有差别。</li>
        </ul>`;
    } catch (e) {
      box.innerHTML = `<p class="banner error">暂不可用：${esc(e.message)}</p>`;
    }
    if (document.querySelector('.view.active[data-view="status"]')) procTimer = setTimeout(() => { renderProcs(); }, 3000);
  }
  $("proc-refresh").onclick = renderProcs;

  return { renderGuide, renderProcs };
};
