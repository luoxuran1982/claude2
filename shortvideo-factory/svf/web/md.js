"use strict";
// 小型 Markdown 渲染器（随应用打包，不依赖网络）。支持：YAML 头、标题、段落、粗体/斜体/行内代码、
// 链接与图片、有序/无序列表（可嵌套）、引用、代码块、表格、分隔线。先转义 HTML，不执行文档里的任何脚本。
(function (root) {
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  // resolve(href, isImage) 由调用方提供：把文档里的相对链接换成应用内地址
  function inline(text, resolve) {
    const codes = [];
    let s = text.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = esc(s);
    const unq = h => h.replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").replace(/&quot;/g, '"').replace(/&#39;/g, "'");
    const target = h => { h = unq(h.trim()); if (h.startsWith("<") && h.endsWith(">")) h = h.slice(1, -1); return h; };
    s = s.replace(/!\[([^\]]*)\]\(((?:&lt;[^]*?&gt;)|[^)\s]+)\)/g, (_, alt, h) => {
      const r = resolve ? resolve(target(h), true) : { href: target(h) };
      return `<img src="${esc(r.href)}" alt="${alt}" loading="lazy">`;
    });
    s = s.replace(/\[([^\]]+)\]\(((?:&lt;[^]*?&gt;)|[^)\s]+)\)/g, (_, label, h) => {
      const r = resolve ? resolve(target(h), false) : { href: target(h) };
      const attrs = r.doc ? ` data-doc="${esc(r.doc)}"${r.anchor ? ` data-anchor="${esc(r.anchor)}"` : ""}`
        : r.source ? ` data-source="${esc(r.source)}"` : r.sources ? ' data-sources="1"' : r.external ? ' target="_blank" rel="noreferrer"' : "";
      return `<a href="${esc(r.href)}"${attrs}>${label}</a>`;
    });
    s = s.replace(/&lt;(https?:\/\/[^\s&]+)&gt;/g, '<a href="$1" target="_blank" rel="noreferrer">$1</a>');
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
  }

  const slug = t => t.trim().toLowerCase().replace(/<[^>]+>/g, "").replace(/[\s]+/g, "-").replace(/[^\w一-鿿-]/g, "");
  const cells = line => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map(c => c.trim().replace(/\\\|/g, "|"));

  function render(src, resolve) {
    let lines = src.replace(/\r\n?/g, "\n").split("\n");
    let meta = "";
    if (lines[0] === "---") {
      const end = lines.indexOf("---", 1);
      if (end > 0) {
        const rows = lines.slice(1, end).filter(l => l.includes(":")).map(l => {
          const k = l.slice(0, l.indexOf(":")), v = l.slice(l.indexOf(":") + 1);
          return `<tr><th>${esc(k.trim())}</th><td>${esc(v.trim())}</td></tr>`;
        });
        meta = `<table class="md-meta">${rows.join("")}</table>`;
        lines = lines.slice(end + 1);
      }
    }
    const out = [];
    let i = 0;
    const isTableSep = l => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
    const blockStart = l => /^(#{1,6}\s|```|~~~|>\s?|\s*([-*+]|\d+[.)])\s+|\s*(-{3,}|\*{3,}|_{3,})\s*$)/.test(l);
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = line.match(/^(```|~~~)\s*([\w+-]*)/))) {
        const fence = m[1], body = [];
        i++;
        while (i < lines.length && !lines[i].startsWith(fence)) body.push(lines[i++]);
        i++;
        out.push(`<pre class="md-code"><code${m[2] ? ` data-lang="${esc(m[2])}"` : ""}>${esc(body.join("\n"))}</code></pre>`);
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
        const n = m[1].length, html = inline(m[2], resolve);
        out.push(`<h${n} id="${esc(slug(m[2]))}">${html}</h${n}>`);
        i++; continue;
      }
      if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
      if (/^>/.test(line)) {
        const body = [];
        while (i < lines.length && /^>/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ""));
        out.push(`<blockquote>${render(body.join("\n"), resolve)}</blockquote>`);
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = cells(line), aligns = cells(lines[i + 1]).map(c => c.endsWith(":") ? (c.startsWith(":") ? "center" : "right") : "");
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes("|") && lines[i].trim()) rows.push(cells(lines[i++]));
        const td = (tag, c, k) => `<${tag}${aligns[k] ? ` style="text-align:${aligns[k]}"` : ""}>${inline(c, resolve)}</${tag}>`;
        out.push(`<div class="md-table"><table><thead><tr>${head.map((c, k) => td("th", c, k)).join("")}</tr></thead><tbody>` +
          rows.map(r => `<tr>${head.map((_, k) => td("td", r[k] || "", k)).join("")}</tr>`).join("") + "</tbody></table></div>");
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) { i = list(lines, i, out, resolve); continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !blockStart(lines[i]) &&
             !(lines[i].includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1]))) para.push(lines[i++].trim());
      out.push(`<p>${para.map(p => inline(p, resolve)).join("<br>")}</p>`);
    }
    return meta + out.join("\n");
  }

  function list(lines, i, out, resolve) {
    const indent = l => l.match(/^\s*/)[0].replace(/\t/g, "    ").length;
    const base = indent(lines[i]);
    const ordered = /^\s*\d+[.)]\s+/.test(lines[i]);
    const items = [];
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        if (i + 1 < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i + 1]) && indent(lines[i + 1]) >= base) { i++; continue; }
        break;
      }
      const ind = indent(l), m = l.match(/^\s*([-*+]|\d+[.)])\s+(.*)$/);
      if (m && ind === base) { if (/\d/.test(m[1]) !== ordered) break; items.push({ text: [m[2]], sub: [] }); i++; continue; }
      if (ind > base && items.length) {
        if (m) { const sub = []; i = list(lines, i, sub, resolve); items[items.length - 1].sub.push(sub.join("")); continue; }
        items[items.length - 1].text.push(l.trim()); i++; continue;
      }
      break;
    }
    const tag = ordered ? "ol" : "ul";
    out.push(`<${tag}>${items.map(it => {
      let t = it.text.join(" "), box = "";
      const cb = t.match(/^\[([ xX])\]\s+(.*)$/);
      if (cb) { box = `<input type="checkbox" disabled${cb[1] !== " " ? " checked" : ""}> `; t = cb[2]; }
      return `<li>${box}${inline(t, resolve)}${it.sub.join("")}</li>`;
    }).join("")}</${tag}>`);
    return i;
  }

  const api = { render, slug };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.SVFMarkdown = api;
})(typeof window !== "undefined" ? window : globalThis);
