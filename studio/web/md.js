"use strict";
// 小型 Markdown 渲染器（随应用打包，不依赖网络）。
// 支持：YAML 头、标题、段落、粗体/斜体/删除线、行内代码、代码块、表格、有序/无序（可嵌套）列表、
// 引用、分隔线、链接、图片。所有文本先转义再加标记，不执行文档里的 HTML。
// opts.link(href) -> {href, attrs}；opts.image(src) -> 实际地址。
(function (global) {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function slug(text) {
    return text.toLowerCase().replace(/<[^>]+>/g, "").replace(/[^\p{L}\p{N}\s-]/gu, "").trim().replace(/\s+/g, "-");
  }

  function inline(src, opts) {
    // 先把行内代码抽出来，避免里面的符号被当成标记
    const codes = [];
    let s = src.replace(/(`+)([\s\S]*?[^`])\1(?!`)/g, (_, __, c) => { codes.push(c.trim()); return `\u0000${codes.length - 1}\u0000`; });
    const holds = [];
    const hold = (html) => { holds.push(html); return `\u0001${holds.length - 1}\u0001`; };
    // 图片 ![alt](<src> "title") 或 ![alt](src)
    s = s.replace(/!\[([^\]]*)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)/g, (_, alt, url) => {
      url = url.replace(/^<|>$/g, "");
      const real = opts.image ? opts.image(url) : url;
      return hold(`<img src="${esc(real)}" alt="${esc(alt)}" loading="lazy">`);
    });
    // 链接 [text](<href>) 或 [text](href)
    s = s.replace(/\[([^\]]+)\]\(\s*(<[^>]+>|[^)\s]+)(?:\s+"[^"]*")?\s*\)/g, (_, text, url) => {
      url = url.replace(/^<|>$/g, "");
      const r = opts.link ? opts.link(url) : { href: url, attrs: "" };
      return hold(`<a href="${esc(r.href)}"${r.attrs || ""}>`) + text + hold("</a>");
    });
    // 自动链接
    s = s.replace(/<(https?:\/\/[^>\s]+)>/g, (_, u) => hold(`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`));
    s = esc(s);
    s = s.replace(/\*\*([^*]+?)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+?)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*?)\*(?!\*)/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+?)~~/g, "<del>$1</del>");
    s = s.replace(/ {2,}$/gm, "<br>");
    s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => holds[+i]);
    // 链接文字里可能还有占位，再替换一次
    s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => holds[+i]);
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
    return s;
  }

  function splitRow(line) {
    let t = line.trim();
    if (t.startsWith("|")) t = t.slice(1);
    if (t.endsWith("|") && !t.endsWith("\\|")) t = t.slice(0, -1);
    const cells = [];
    let cur = "", inCode = false;
    for (let i = 0; i < t.length; i++) {
      const ch = t[i];
      if (ch === "`") inCode = !inCode;
      if (ch === "\\" && t[i + 1] === "|") { cur += "|"; i++; continue; }
      if (ch === "|" && !inCode) { cells.push(cur.trim()); cur = ""; continue; }
      cur += ch;
    }
    cells.push(cur.trim());
    return cells;
  }

  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l) && l.includes("-");
  const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  function render(md, opts = {}) {
    let text = String(md || "").replace(/\r\n?/g, "\n");
    let front = "";
    if (text.startsWith("---\n")) {
      const end = text.indexOf("\n---", 4);
      if (end > 0) {
        const yaml = text.slice(4, end).split("\n").filter((l) => l.trim());
        front = `<table class="md-front">${yaml.map((l) => {
          const i = l.indexOf(":");
          return i > 0 ? `<tr><th>${esc(l.slice(0, i).trim())}</th><td>${esc(l.slice(i + 1).trim())}</td></tr>` : "";
        }).join("")}</table>`;
        text = text.slice(end + 4).replace(/^\n+/, "");
      }
    }
    const lines = text.split("\n");
    const out = [];
    let i = 0;
    const toc = [];

    const isBlockStart = (l, next) => /^\s*```/.test(l) || /^#{1,6}\s/.test(l) || /^\s*>/.test(l) || listRe.test(l) ||
      /^\s*([-*_])(\s*\1){2,}\s*$/.test(l) || (l.includes("|") && next !== undefined && isTableSep(next));

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      // 代码块
      let m = line.match(/^\s*(```+|~~~+)\s*([\w+-]*)/);
      if (m) {
        const fence = m[1], lang = m[2];
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence)) buf.push(lines[i++]);
        i++;
        out.push(`<pre class="md-code"${lang ? ` data-lang="${esc(lang)}"` : ""}><code>${esc(buf.join("\n"))}</code></pre>`);
        continue;
      }
      // 标题
      m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (m) {
        const lvl = m[1].length, html = inline(m[2], opts);
        const id = "h-" + slug(m[2]) ;
        toc.push({ level: lvl, text: m[2].replace(/[`*]/g, ""), id });
        out.push(`<h${lvl} id="${esc(id)}">${html}</h${lvl}>`);
        i++;
        continue;
      }
      // 分隔线
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
      // 表格
      if (line.includes("|") && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) rows.push(splitRow(lines[i++]));
        const al = (k) => (aligns[k] ? ` style="text-align:${aligns[k]}"` : "");
        out.push(`<div class="md-table"><table><thead><tr>${head.map((c, k) => `<th${al(k)}>${inline(c, opts)}</th>`).join("")}</tr></thead><tbody>${
          rows.map((r) => `<tr>${head.map((_, k) => `<td${al(k)}>${inline(r[k] || "", opts)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      // 引用
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
        out.push(`<blockquote>${render(buf.join("\n"), opts).html}</blockquote>`);
        continue;
      }
      // 列表（按缩进嵌套）
      if (listRe.test(line)) {
        const items = [];
        while (i < lines.length) {
          const l = lines[i];
          const lm = l.match(listRe);
          if (lm) { items.push({ indent: lm[1].replace(/\t/g, "    ").length, ordered: /\d/.test(lm[2]), start: parseInt(lm[2], 10), text: lm[3] }); i++; continue; }
          if (l.trim() && /^\s{2,}/.test(l) && items.length) { items[items.length - 1].text += "\n" + l.trim(); i++; continue; }
          if (!l.trim() && i + 1 < lines.length && listRe.test(lines[i + 1])) { i++; continue; }
          break;
        }
        out.push(buildList(items, opts));
        continue;
      }
      // 段落
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i], lines[i + 1])) buf.push(lines[i++]);
      out.push(`<p>${inline(buf.join("\n"), opts)}</p>`);
    }
    return { html: front + out.join("\n"), toc };
  }

  function buildList(items, opts) {
    let html = "";
    const stack = [];
    for (const it of items) {
      while (stack.length && it.indent < stack[stack.length - 1].indent) {
        html += `</li></${stack.pop().tag}>`;
      }
      const top = stack[stack.length - 1];
      if (!top || it.indent > top.indent) {
        const tag = it.ordered ? "ol" : "ul";
        stack.push({ indent: it.indent, tag });
        html += `<${tag}${it.ordered && it.start > 1 ? ` start="${it.start}"` : ""}>`;
      } else {
        html += "</li>";
      }
      let body = inline(it.text, opts);
      body = body.replace(/^\[( |x|X)\]\s/, (_, c) => `<input type="checkbox" disabled${c.trim() ? " checked" : ""}> `);
      html += `<li>${body}`;
    }
    while (stack.length) html += `</li></${stack.pop().tag}>`;
    return html;
  }

  global.MD = { render, esc };
})(window);
