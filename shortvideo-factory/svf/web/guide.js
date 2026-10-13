"use strict";
// 「使用指南」：README、UPSTREAM、docs/ 全部文档完整显示；附件预览/下载；源码（外壳 + 上游）只读查看与 zip 下载。
// 文档和源码都由本机服务按白名单提供，随应用打包，离线可读。
window.SVFGuide = function (ctx) {
  const { api, toast } = ctx;
  const $ = id => document.getElementById(id);
  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const kb = n => n >= 1048576 ? (n / 1048576).toFixed(1) + " MB" : n >= 1024 ? (n / 1024).toFixed(1) + " KB" : n + " B";
  const GROUPS = { shell: "外壳源码（本项目手写）", upstream: "上游源码（MoneyPrinterTurbo，MIT，含补丁）" };
  let index = null, current = ctx.initialPage || null;

  async function text(url) {
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).error || res.statusText);
    return res.text();
  }

  // 文档里的相对链接 → 应用内地址
  function resolver(docPath) {
    const dir = docPath.includes("/") ? docPath.slice(0, docPath.lastIndexOf("/") + 1) : "";
    return (href, isImage) => {
      if (/^(https?:|mailto:)/.test(href)) return { href, external: true };
      if (href.startsWith("#")) return { href, doc: docPath, anchor: decodeURIComponent(href.slice(1)) };
      const [p, anchor] = href.split("#");
      const parts = [];
      for (const seg of decodeURIComponent(dir + p).split("/")) {
        if (seg === "..") parts.pop(); else if (seg && seg !== ".") parts.push(seg);
      }
      const rel = parts.join("/");
      if (isImage || index.assets.some(a => a.path === rel)) return { href: "/api/guide/file?path=" + encodeURIComponent(rel), external: !isImage };
      if (index.docs.some(d => d.path === rel)) return { href: "#", doc: rel, anchor: anchor && decodeURIComponent(anchor) };
      if (index.sources.some(s => s.path === rel)) return { href: "#", source: rel };
      const dirHit = index.sources.find(s => s.path.startsWith(rel.replace(/\/$/, "") + "/"));
      if (dirHit) return { href: "#", sources: true };
      return { href: "#", doc: null };
    };
  }

  function navHTML() {
    const item = (key, label, sub) => `<button class="gn${current === key ? " on" : ""}" data-key="${esc(key)}">${esc(label)}${sub ? `<small>${esc(sub)}</small>` : ""}</button>`;
    const docs = index.docs.map(d => item("doc:" + d.path, d.name, d.path)).join("");
    const assets = index.assets.length
      ? index.assets.map(a => `<a class="gn" href="/api/guide/file?path=${encodeURIComponent(a.path)}" target="_blank" rel="noreferrer">${esc(a.name)}<small>${esc(a.kind)} · ${esc(a.path)} · ${kb(a.size)}</small></a>`).join("")
      : '<p class="muted">（暂无）</p>';
    const st = index.source_stats;
    return `<h3>文档（${index.docs.length} 篇，建议按顺序读）</h3>${docs}
      <h3>源码</h3>${item("sources", "源码清单与只读查看", `外壳 ${st.shell.files} 个文件 · 上游 ${st.upstream.files} 个文件`)}
      <button class="gn" data-zip="1">下载全部源码（zip）<small>外壳 + 上游主要源码 + 全部文档</small></button>
      <h3>附件</h3>${assets}
      <h3>运行</h3><button class="gn" data-goto="status">运行状态（进程与内存）</button>
      <p class="muted" style="font-size:.78rem;padding:0 .5rem">应用 v${esc(index.version)} · 上游 MoneyPrinterTurbo ${esc(index.upstream_version)}（${esc(index.upstream_commit)}）。
      文档随应用打包，离线可读，内容与仓库 shortvideo-factory/ 下的 README 和 docs/ 一致。</p>`;
  }

  async function show(key, anchor) {
    if (!index) return;
    const valid = k => k && (k === "sources" || (k.startsWith("src:") && index.sources.some(s => "src:" + s.path === k)) ||
      index.docs.some(d => "doc:" + d.path === k));
    if (!valid(key)) {
      const first = index.docs.find(d => d.path.startsWith("docs/00")) || index.docs[0];
      key = "doc:" + first.path;
    }
    current = key;
    ctx.saveSettings({ guide_page: key });
    $("guide-nav").innerHTML = navHTML();
    const body = $("guide-body");
    body.innerHTML = '<p class="muted">正在读取…</p>';
    try {
      if (key.startsWith("doc:")) {
        const path = key.slice(4);
        const md = await text("/api/guide/doc?path=" + encodeURIComponent(path));
        body.innerHTML = `<div class="doc-path muted">${esc(path)}</div><div class="md" data-doc="${esc(path)}">${window.SVFMarkdown.render(md, resolver(path))}</div>`;
      } else if (key === "sources") {
        body.innerHTML = sourcesHTML();
      } else {
        const path = key.slice(4);
        const src = await text("/api/guide/source?path=" + encodeURIComponent(path));
        const lines = src.replace(/\n$/, "").split("\n");
        const meta = index.sources.find(s => s.path === path) || {};
        body.innerHTML = `<div class="src-head"><button class="btn small" data-key="sources">← 源码清单</button><b>${esc(path)}</b>
          <span class="muted">${esc(GROUPS[meta.group] || "")} · ${esc(meta.lang || "")} · ${lines.length} 行 · 只读</span></div>
          <div class="src-view" data-source="${esc(path)}"><table><tbody>${lines.map((l, k) => `<tr><td class="ln">${k + 1}</td><td><pre>${esc(l) || " "}</pre></td></tr>`).join("")}</tbody></table></div>`;
      }
    } catch (e) {
      body.innerHTML = `<div class="banner error">读取失败：${esc(e.message)}</div>`;
    }
    const target = anchor && body.querySelector(`[id="${CSS.escape(anchor)}"]`);
    if (target) target.scrollIntoView(); else body.scrollTop = 0;
  }

  function sourcesHTML() {
    const out = [];
    for (const g of ["shell", "upstream"]) {
      const list = index.sources.filter(s => s.group === g);
      const groups = {};
      list.forEach(s => { const d = s.path.includes("/") ? s.path.slice(0, s.path.lastIndexOf("/")) : "（项目根目录）"; (groups[d] = groups[d] || []).push(s); });
      const total = list.reduce((n, s) => n + s.lines, 0);
      out.push(`<h2>${esc(GROUPS[g])}</h2><p class="muted">${list.length} 个文件，共 ${total.toLocaleString("zh-CN")} 行。</p>` +
        Object.entries(groups).map(([d, items]) => `<h3>${esc(d)}</h3><table class="data src-list"><tbody>${items.map(s =>
          `<tr class="clickable" data-key="src:${esc(s.path)}"><td>${esc(s.path)}</td><td>${esc(s.lang)}</td><td class="num">${s.lines} 行</td><td class="num">${kb(s.size)}</td></tr>`).join("")}</tbody></table>`).join(""));
    }
    return `<div class="src-head"><h2 style="border:0">源码</h2><button class="btn primary small" data-zip="1">下载全部源码（zip）</button></div>
      <p class="muted">覆盖范围：外壳（svf/ 启动器与界面、packaging/ 打包、tests/ 测试、run.py 等，全部由本项目手写）和上游 MoneyPrinterTurbo 的主要源码
      （app/、webui/、cli.py、main.py、配置示例，其中 3 个文件打了补丁，见「上游来源与补丁」）。不含：上游的测试与示例素材、字体和音乐等二进制文件、
      随包 Python 运行时和第三方库（Streamlit、MoviePy 等）、构建产物。阅读顺序见「03 源码阅读与重建指南」。</p>
      ${out.join("")}`;
  }

  async function renderGuide(page) {
    if (!index) {
      try { index = await api("/api/guide"); }
      catch (e) { $("guide-body").innerHTML = `<div class="banner error">读取文档清单失败：${esc(e.message)}</div>`; return; }
    }
    await show(page || current);
  }

  function onClick(e) {
    const a = e.target.closest("a[data-doc], a[data-source], a[data-sources]");
    if (a) {
      e.preventDefault();
      if (a.dataset.source) return show("src:" + a.dataset.source);
      if (a.dataset.sources) return show("sources");
      return show("doc:" + a.dataset.doc, a.dataset.anchor);
    }
    const ext = e.target.closest('a[target="_blank"]');
    if (ext && window.SVF && window.SVF.inWindow && /^https?:/.test(ext.href) && !ext.href.startsWith(location.origin)) {
      e.preventDefault(); return window.SVF.openExternal(ext.href);
    }
    const dead = e.target.closest('.md a[href="#"]');
    if (dead) { e.preventDefault(); return toast("这个链接指向应用外的文件（如仓库里的其他目录），安装版里没有"); }
    if (e.target.closest("[data-zip]")) { location.href = "/api/guide/source.zip"; return; }
    const k = e.target.closest("[data-key]");
    if (k) return show(k.dataset.key);
  }
  $("guide-nav").addEventListener("click", onClick);
  $("guide-body").addEventListener("click", onClick);

  return { renderGuide, show };
};
