/* 小型 Markdown 渲染器（随应用打包，不依赖 CDN）。
 * 支持：YAML 头、标题、段落、粗体/斜体/删除线、行内代码、代码块（mermaid 按源码显示）、
 *       有序/无序/任务列表（可嵌套）、引用、表格（含对齐）、分隔线、链接（含 <带空格的地址>）、图片。
 * 用法：MD.render(text, { link(href) → {href, attrs}, image(src) → src }) 返回 HTML 字符串。
 */
(function () {
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  function slug(t) {
    return t.toLowerCase().replace(/<[^>]+>/g, "").replace(/[^\p{L}\p{N}\- ]/gu, "").trim().replace(/\s+/g, "-");
  }

  function inline(src, opt) {
    const codes = [];
    let s = src.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    const links = [];
    const target = (raw) => {
      raw = raw.trim();
      let m = raw.match(/^<([^>]*)>(?:\s+"[^"]*")?$/);
      if (m) return m[1];
      m = raw.match(/^(\S+)(?:\s+"[^"]*")?$/);
      return m ? m[1] : raw;
    };
    // 图片 ![alt](src)
    s = s.replace(/!\[([^\]]*)\]\(((?:<[^>]*>|[^()\s]+)(?:\s+"[^"]*")?)\)/g, (_, alt, t) => {
      const src2 = opt.image ? opt.image(target(t)) : target(t);
      links.push(`<img src="${esc(src2)}" alt="${esc(alt)}" loading="lazy">`);
      return `\u0001${links.length - 1}\u0001`;
    });
    // 链接 [text](href)
    s = s.replace(/\[([^\]]+)\]\(((?:<[^>]*>|[^()\s]+(?:\([^)]*\))?)(?:\s+"[^"]*")?)\)/g, (_, text, t) => {
      const r = opt.link ? opt.link(target(t)) : { href: target(t), attrs: "" };
      links.push(`<a href="${esc(r.href)}"${r.attrs || ""}>${inlineText(text)}</a>`);
      return `\u0001${links.length - 1}\u0001`;
    });
    // 自动链接 <https://...>
    s = s.replace(/<(https?:\/\/[^>\s]+)>/g, (_, u) => { links.push(`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`); return `\u0001${links.length - 1}\u0001`; });
    s = inlineText(s);
    s = s.replace(/\u0001(\d+)\u0001/g, (_, i) => links[+i]);
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${esc(codes[+i])}</code>`);
    return s;
  }
  function inlineText(s) {
    s = esc(s).replace(/&lt;br\s*\/?&gt;/g, "<br>");
    s = s.replace(/\u0000/g, "\u0000");
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/__([^_]+)__/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    s = s.replace(/~~([^~]+)~~/g, "<del>$1</del>");
    return s;
  }

  function splitRow(line) {
    let l = line.trim();
    if (l.startsWith("|")) l = l.slice(1);
    if (l.endsWith("|") && !l.endsWith("\\|")) l = l.slice(0, -1);
    const cells = [];
    let cur = "", inCode = false;
    for (let i = 0; i < l.length; i++) {
      const c = l[i];
      if (c === "`") inCode = !inCode;
      if (c === "\\" && l[i + 1] === "|") { cur += "|"; i++; continue; }
      if (c === "|" && !inCode) { cells.push(cur.trim()); cur = ""; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  function render(text, opt = {}) {
    const lines = String(text).replace(/\r\n?/g, "\n").split("\n");
    let i = 0;
    const out = [];
    const toc = [];
    // YAML 头
    if (lines[0] === "---") {
      const end = lines.indexOf("---", 1);
      if (end > 0) {
        const rows = lines.slice(1, end).filter((l) => l.trim()).map((l) => {
          const k = l.indexOf(":");
          return k > 0 ? `<tr><th>${esc(l.slice(0, k).trim())}</th><td>${esc(l.slice(k + 1).trim())}</td></tr>` : `<tr><td colspan="2">${esc(l)}</td></tr>`;
        });
        out.push(`<table class="md-yaml">${rows.join("")}</table>`);
        i = end + 1;
      }
    }
    const isBlockStart = (l) => /^(#{1,6})\s/.test(l) || /^```|^~~~/.test(l) || /^\s*([-*+]|\d+[.)])\s+/.test(l) || /^>/.test(l) || /^(\*\s*\*\s*\*|-\s*-\s*-|_\s*_\s*_)[\s*_-]*$/.test(l.trim());

    function parseList(start, baseIndent) {
      const ordered = /^\s*\d+[.)]\s+/.test(lines[start]);
      const tag = ordered ? "ol" : "ul";
      const startNum = ordered ? parseInt(lines[start].trim(), 10) : 1;
      let html = `<${tag}${ordered && startNum !== 1 ? ` start="${startNum}"` : ""}>`;
      let j = start;
      while (j < lines.length) {
        const l = lines[j];
        const m = l.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (!m) break;
        const ind = m[1].replace(/\t/g, "    ").length;
        if (ind < baseIndent) break;
        if (ind > baseIndent) { // 嵌套
          const r = parseList(j, ind);
          html = html.replace(/<\/li>$/, "") + r.html + "</li>";
          j = r.end;
          continue;
        }
        let body = m[3];
        j++;
        // 续行：缩进的非列表行
        while (j < lines.length && lines[j].trim() && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[j]) && /^\s+/.test(lines[j])) { body += " " + lines[j].trim(); j++; }
        let task = "";
        const tm = body.match(/^\[([ xX])\]\s+(.*)$/);
        if (tm) { task = `<input type="checkbox" disabled ${tm[1] !== " " ? "checked" : ""}> `; body = tm[2]; }
        html += `<li>${task}${inline(body, opt)}</li>`;
        // 列表项之间的空行
        if (j < lines.length && !lines[j].trim() && j + 1 < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[j + 1])) j++;
      }
      return { html: html + `</${tag}>`, end: j };
    }

    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = line.match(/^(```+|~~~+)\s*([\w+-]*)/))) {
        const fence = m[1], lang = m[2] || "";
        const buf = [];
        i++;
        while (i < lines.length && !lines[i].startsWith(fence)) buf.push(lines[i++]);
        i++;
        const label = lang === "mermaid" ? `<div class="md-code-label">Mermaid 图（源码形式显示；可在 GitHub 或 Obsidian 中查看渲染后的图）</div>` : lang ? `<div class="md-code-label">${esc(lang)}</div>` : "";
        out.push(`<div class="md-code">${label}<pre><code class="lang-${esc(lang)}">${esc(buf.join("\n"))}</code></pre></div>`);
        continue;
      }
      if ((m = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/))) {
        const lv = m[1].length, id = slug(m[2]);
        toc.push({ level: lv, text: m[2].replace(/[`*]/g, ""), id });
        out.push(`<h${lv} id="${esc(id)}">${inline(m[2], opt)}</h${lv}>`);
        i++;
        continue;
      }
      if (/^(\*\s*\*\s*\*|-\s*-\s*-|_\s*_\s*_)[\s*_-]*$/.test(line.trim())) { out.push("<hr>"); i++; continue; }
      if (/^>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ""));
        out.push(`<blockquote>${render(buf.join("\n"), opt).html}</blockquote>`);
        continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ind = line.match(/^\s*/)[0].length;
        const r = parseList(i, ind);
        out.push(r.html);
        i = r.end;
        continue;
      }
      if (line.includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(lines[i + 1])) {
        const head = splitRow(line);
        const aligns = splitRow(lines[i + 1]).map((c) => (c.startsWith(":") && c.endsWith(":") ? "center" : c.endsWith(":") ? "right" : ""));
        i += 2;
        const body = [];
        while (i < lines.length && lines[i].trim() && lines[i].includes("|")) body.push(splitRow(lines[i++]));
        const al = (k) => (aligns[k] ? ` style="text-align:${aligns[k]}"` : "");
        out.push(`<div class="md-table"><table><thead><tr>${head.map((c, k) => `<th${al(k)}>${inline(c, opt)}</th>`).join("")}</tr></thead><tbody>${body.map((r) => `<tr>${head.map((_, k) => `<td${al(k)}>${inline(r[k] || "", opt)}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`);
        continue;
      }
      const buf = [line];
      i++;
      while (i < lines.length && lines[i].trim() && !isBlockStart(lines[i]) && !(lines[i].includes("|") && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1]))) buf.push(lines[i++]);
      out.push(`<p>${buf.map((l) => inline(l.trim(), opt) + (/ {2}$/.test(l) ? "<br>" : "")).join("\n")}</p>`);
    }
    return { html: out.join("\n"), toc };
  }

  window.MD = { render, escape: esc };
})();
