/* 小型 Markdown 渲染器（随应用打包，不依赖网络）。
 * 支持：YAML 头、标题、段落、粗体/斜体/删除线、行内代码、代码块、有序/无序（可嵌套）列表、
 * 任务列表、表格（含对齐）、引用、分隔线、链接（含 <带空格的地址>）、图片。
 * 安全：所有原文先转义，不放行原始 HTML。链接和图片地址交给调用方的 opts.link / opts.image 改写。
 */
(function (global) {
  const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  function slug(text) {
    return 'h-' + text.toLowerCase().replace(/<[^>]+>/g, '').replace(/[^\p{L}\p{N}]+/gu, '-').replace(/^-|-$/g, '');
  }

  function inline(src, opts) {
    const keep = [];
    const stash = (html) => { keep.push(html); return '\u0000' + (keep.length - 1) + '\u0000'; };
    let s = src;
    s = s.replace(/(`+)([\s\S]*?[^`])\1(?!`)/g, (_, __, code) => stash('<code>' + esc(code.trim()) + '</code>'));
    const target = '(?:<([^>]*)>|([^\\s)]*))(?:\\s+"([^"]*)")?';
    s = s.replace(new RegExp('!\\[([^\\]]*)\\]\\(' + target + '\\)', 'g'), (_, alt, a, b, title) => {
      const url = opts.image ? opts.image(a ?? b) : (a ?? b);
      return stash(`<img src="${esc(url)}" alt="${esc(alt)}"${title ? ` title="${esc(title)}"` : ''} loading="lazy">`);
    });
    s = s.replace(new RegExp('\\[([^\\]]+)\\]\\(' + target + '\\)', 'g'), (_, text, a, b) => {
      const href = a ?? b;
      const info = opts.link ? opts.link(href) : { href };
      const attrs = Object.entries(info).map(([k, v]) => ` ${k === 'href' ? 'href' : 'data-' + k}="${esc(v)}"`).join('');
      return stash(`<a${attrs}>${inline(text, opts)}</a>`);
    });
    s = s.replace(/<(https?:\/\/[^>\s]+)>/g, (_, u) => stash(`<a href="${esc(u)}" target="_blank" rel="noopener">${esc(u)}</a>`));
    s = esc(s);
    s = s.replace(/\*\*([^*]+?)\*\*|__([^_]+?)__/g, (_, a, b) => '<strong>' + (a ?? b) + '</strong>');
    s = s.replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\*)/g, '$1<em>$2</em>');
    s = s.replace(/~~([^~]+)~~/g, '<del>$1</del>');
    s = s.replace(/ {2,}$/gm, '<br>');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => keep[+i]);
  }

  function splitRow(line) {
    let l = line.trim();
    if (l.startsWith('|')) l = l.slice(1);
    if (l.endsWith('|') && !l.endsWith('\\|')) l = l.slice(0, -1);
    const cells = []; let cur = ''; let tick = false;
    for (let i = 0; i < l.length; i++) {
      const c = l[i];
      if (c === '`') tick = !tick;
      if (c === '\\' && l[i + 1] === '|') { cur += '|'; i++; continue; }
      if (c === '|' && !tick) { cells.push(cur.trim()); cur = ''; continue; }
      cur += c;
    }
    cells.push(cur.trim());
    return cells;
  }

  const isTableSep = (l) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  const listRe = /^(\s*)([-*+]|\d+[.)])\s+(.*)$/;

  function parseList(lines, i, opts) {
    const base = lines[i].match(listRe)[1].length;
    const ordered = /\d/.test(lines[i].match(listRe)[2]);
    const start = ordered ? parseInt(lines[i].match(listRe)[2], 10) : 1;
    const items = [];
    while (i < lines.length) {
      const m = lines[i].match(listRe);
      if (!m || m[1].length < base) {
        if (m || !lines[i].trim() || !/^\s{2,}/.test(lines[i]) || !items.length) {
          if (!lines[i].trim() && i + 1 < lines.length && (lines[i + 1].match(listRe) || [])[1]?.length >= base) { i++; continue; }
          break;
        }
        items[items.length - 1].body.push(lines[i].trim()); i++; continue;
      }
      if (m[1].length > base) {
        const sub = parseList(lines, i, opts);
        items[items.length - 1].sub += sub.html; i = sub.i; continue;
      }
      if (/\d/.test(m[2]) !== ordered) break;
      items.push({ body: [m[3]], sub: '' }); i++;
    }
    const html = items.map((it) => {
      let text = it.body.join(' ');
      let box = '';
      const t = text.match(/^\[([ xX])\]\s+(.*)$/);
      if (t) { box = `<input type="checkbox" disabled${t[1] !== ' ' ? ' checked' : ''}> `; text = t[2]; }
      return `<li>${box}${inline(text, opts)}${it.sub}</li>`;
    }).join('');
    const tag = ordered ? 'ol' : 'ul';
    return { html: `<${tag}${ordered && start !== 1 ? ` start="${start}"` : ''}>${html}</${tag}>`, i };
  }

  function render(md, opts = {}) {
    const lines = String(md).replace(/\r\n?/g, '\n').split('\n');
    const out = []; const toc = []; let meta = null;
    let i = 0;
    if (lines[0] === '---') {
      const end = lines.indexOf('---', 1);
      if (end > 0) {
        meta = lines.slice(1, end).map((l) => l.match(/^([\w-]+):\s*(.*)$/)).filter(Boolean).map((m) => [m[1], m[2]]);
        i = end + 1;
      }
    }
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      const fence = line.match(/^\s*(`{3,}|~{3,})\s*([\w+-]*)/);
      if (fence) {
        const buf = []; i++;
        while (i < lines.length && !lines[i].trim().startsWith(fence[1])) buf.push(lines[i++]);
        i++;
        out.push(`<pre class="md-code"${fence[2] ? ` data-lang="${esc(fence[2])}"` : ''}><code>${esc(buf.join('\n'))}</code></pre>`);
        continue;
      }
      const h = line.match(/^(#{1,6})\s+(.*?)\s*#*\s*$/);
      if (h) {
        const html = inline(h[2], opts); const id = slug(h[2]);
        toc.push({ level: h[1].length, text: h[2].replace(/[*`]/g, ''), id });
        out.push(`<h${h[1].length} id="${esc(id)}">${html}</h${h[1].length}>`); i++; continue;
      }
      if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      if (/^\s*>/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ''));
        out.push('<blockquote>' + render(buf.join('\n'), opts).html + '</blockquote>'); continue;
      }
      if (line.includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1])) {
        const head = splitRow(line);
        const align = splitRow(lines[i + 1]).map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : ''));
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) rows.push(splitRow(lines[i++]));
        const td = (tag, c, k) => `<${tag}${align[k] ? ` style="text-align:${align[k]}"` : ''}>${inline(c, opts)}</${tag}>`;
        out.push('<div class="md-table"><table><thead><tr>' + head.map((c, k) => td('th', c, k)).join('') + '</tr></thead><tbody>'
          + rows.map((r) => '<tr>' + head.map((_, k) => td('td', r[k] ?? '', k)).join('') + '</tr>').join('') + '</tbody></table></div>');
        continue;
      }
      if (listRe.test(line)) { const r = parseList(lines, i, opts); out.push(r.html); i = r.i; continue; }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !/^(#{1,6})\s/.test(lines[i]) && !/^\s*(`{3,}|~{3,})/.test(lines[i])
        && !/^\s*>/.test(lines[i]) && !(buf.length && listRe.test(lines[i]))
        && !(lines[i].includes('|') && i + 1 < lines.length && isTableSep(lines[i + 1]))) buf.push(lines[i++]);
      if (!buf.length) { buf.push(lines[i++]); }
      out.push('<p>' + inline(buf.join('\n'), opts) + '</p>');
    }
    return { html: out.join('\n'), toc, meta };
  }

  global.Markdown = { render, esc };
})(window);
