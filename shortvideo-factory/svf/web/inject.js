"use strict";
// 由本机服务插入到上游 Streamlit 页面（/app/）里运行。外壳和 Streamlit 经反向代理同源，所以能做到：
// 1. 缩放：外壳顶栏选的档位同样作用到 Streamlit 页面内部。Streamlit 的尺寸几乎都以 rem 为单位，
//    所以改根字号（16px × 档位）就能整页缩放；实测比 CSS zoom 好：CSS zoom 会让下拉菜单弹出位置错开。
// 2. 中文化：把 Streamlit 自带、上游没法翻译的英文提示（上传框、下拉框空状态、连接状态等）换成中文；
// 3. 外部链接（申请 API Key 的网站等）在独立窗口里交给系统浏览器打开，不在应用窗口里跳走。
// 不修改上游任何文件；去掉这个脚本，Streamlit 页面照常工作，只是没有上述三点。
(function () {
  if (window.__svfInjected) return;
  window.__svfInjected = true;

  const shell = (() => { try { return window.parent !== window && window.parent.SVF ? window.parent.SVF : null; } catch { return null; } })();

  // ---------- 1. 缩放 ----------
  function applyZoom(pct) {
    const z = Math.min(120, Math.max(70, Math.round(Number(pct) / 5) * 5 || 100)) / 100;
    document.documentElement.style.fontSize = (16 * z) + "px";
    document.documentElement.dataset.svfZoom = String(Math.round(z * 100));
  }
  if (shell) applyZoom(shell.zoom);
  else fetch("/api/settings").then(r => r.json()).then(s => applyZoom(s.zoom)).catch(() => {});
  window.addEventListener("message", e => {
    if (e.origin === location.origin && e.data && e.data.type === "svf-zoom") applyZoom(e.data.value);
  });
  // 焦点在 Streamlit 页面里时，⌘/Ctrl + = / − / 0 也交给外壳处理
  window.addEventListener("keydown", e => {
    if (!(e.metaKey || e.ctrlKey) || !shell) return;
    const dir = e.key === "=" || e.key === "+" ? 1 : e.key === "-" ? -1 : e.key === "0" ? 0 : null;
    if (dir === null) return;
    e.preventDefault();
    shell.zoomStep(dir);
  }, true);

  // ---------- 2. Streamlit 自带英文提示 → 中文 ----------
  const EXACT = {
    "Drag and drop file here": "把文件拖到这里",
    "Drag and drop files here": "把文件拖到这里",
    "Browse files": "选择文件",
    "Browse file": "选择文件",
    "Upload": "上传",
    "Choose an option": "请选择",
    "Choose options": "请选择",
    "Select...": "请选择…",
    "No options to select.": "没有可选项",
    "No results": "没有匹配项",
    "Clear all": "全部清除",
    "Open": "展开",
    "Help": "帮助",
    "Main menu": "主菜单",
    "Link to heading": "标题链接",
    "Open MoneyPrinterTurbo on GitHub": "在 GitHub 打开上游项目 MoneyPrinterTurbo",
    "Open project on GitHub": "在 GitHub 打开上游项目",
    "Deploy": "部署",
    "Settings": "设置",
    "Print": "打印",
    "Record a screencast": "录屏",
    "About": "关于",
    "Clear cache": "清除缓存",
    "Running...": "运行中…",
    "Please wait...": "请稍候…",
    "Connecting": "正在连接",
    "Stop": "停止",
    "Rerun": "重新运行",
    "Download": "下载",
    "Fullscreen": "全屏",
    "Close fullscreen": "退出全屏",
    "View fullscreen": "全屏查看",
    "Exit fullscreen": "退出全屏",
    "Copy to clipboard": "复制",
    "Copied": "已复制",
    "Show password text": "显示密码",
    "Show password": "显示密码",
    "Hide password": "隐藏密码",
    "open": "展开",
    "Hide password text": "隐藏密码",
    "Close": "关闭",
    "Dismiss": "关闭",
    "Uploading...": "正在上传…",
    "Show more": "展开",
    "Show less": "收起",
    "Search": "搜索",
    "Remove": "删除",
    "Delete": "删除",
    "Clear": "清除",
    "Toggle": "切换",
    "Collapse sidebar": "收起侧栏",
    "Expand sidebar": "展开侧栏",
    "Open sidebar": "展开侧栏",
    "Close sidebar": "收起侧栏",
    "Connection error": "连接中断",
    "Is Streamlit still running? If you accidentally stopped Streamlit, just restart it in your terminal:":
      "生成界面后台没有响应。请到顶栏「运行状态」点「重新启动生成界面」，或退出后重新打开应用。",
  };
  const PATTERNS = [
    [/^Limit (\d+(?:\.\d+)?)\s?([KMG]B) per file(.*)$/, (m) => `单个文件不超过 ${m[1]} ${m[2]}${m[3].replace("•", "·")}`],
    [/^Press Enter to apply$/, () => "按回车确认"],
    [/^Press ⌘\+Enter to apply$/, () => "按 ⌘+回车确认"],
    [/^Press Ctrl\+Enter to apply$/, () => "按 Ctrl+回车确认"],
    [/^Press Enter to submit form$/, () => "按回车提交"],
    [/^(\d+) characters? remaining$/, (m) => `还能输入 ${m[1]} 个字`],
    [/^(\d+(?:\.\d+)?)\s?([KMG]B) per file$/, (m) => `单个文件不超过 ${m[1]} ${m[2]}`],
    [/^(\d+) options? available\.?$/, (m) => `共 ${m[1]} 个选项`],
    [/^(\d+) results? available\.?$/, (m) => `找到 ${m[1]} 项`],
    [/^Remove (.+)$/, (m) => `删除 ${m[1]}`],
    [/^Help for (.+)$/, (m) => `「${m[1]}」的说明`],
    [/^(.+), close by backspace$/, (m) => `${m[1]}（按退格键移除）`],
  ];
  const ATTRS = ["placeholder", "aria-label", "title"];

  function tr(text) {
    const t = text.trim();
    if (!t || !/[A-Za-z]/.test(t)) return null;
    if (Object.prototype.hasOwnProperty.call(EXACT, t)) return text.replace(t, EXACT[t]);
    for (const [re, fn] of PATTERNS) {
      const m = t.match(re);
      if (m) return text.replace(t, fn(m));
    }
    return null;
  }

  // 只翻译 Streamlit 组件自身的提示，不碰用户输入的内容（输入框、文本域、代码块、Markdown 正文）
  const SKIP = "input, textarea, pre, code, [contenteditable], .stMarkdown, [data-testid='stMarkdownContainer'], [data-testid='stCode']";
  function fixEl(el) {
    for (const a of ATTRS) {
      const v = el.getAttribute(a);
      const out = v && tr(v);
      if (out && out !== v) el.setAttribute(a, out);
    }
  }
  function fixText(n) {
    const p = n.parentElement;
    // 上传框里的「Upload」按钮文字放在 Markdown 容器里，例外处理
    if (!p || (p.closest(SKIP) && !p.closest("[data-testid='stFileUploaderDropzone']"))) return;
    const out = tr(n.nodeValue);
    if (out !== null && out !== n.nodeValue) n.nodeValue = out;
  }
  function walk(root) {
    if (!root) return;
    if (root.nodeType === 3) return fixText(root);
    if (root.nodeType !== 1) return;
    fixEl(root);
    const tw = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
    for (let n = tw.nextNode(); n; n = tw.nextNode()) {
      if (n.nodeType === 1) fixEl(n); else fixText(n);
    }
  }
  const observer = new MutationObserver(list => {
    for (const m of list) {
      if (m.type === "characterData") walk(m.target);
      else if (m.type === "attributes") walk(m.target);
      else m.addedNodes.forEach(walk);
    }
  });
  function start() {
    walk(document.body);
    observer.observe(document.body, { childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ATTRS });
  }
  if (document.body) start(); else document.addEventListener("DOMContentLoaded", start);
  window.__svfTranslate = tr;  // 供界面检查脚本使用

  // ---------- 3. 外部链接交给系统浏览器 ----------
  document.addEventListener("click", e => {
    const a = e.target.closest && e.target.closest("a[href]");
    if (!a || !shell || !shell.inWindow) return;
    let url;
    try { url = new URL(a.getAttribute("href"), location.href); } catch { return; }
    if (!/^https?:$/.test(url.protocol) || url.host === location.host) return;
    e.preventDefault();
    e.stopPropagation();
    shell.openExternal(url.href);
  }, true);
})();
