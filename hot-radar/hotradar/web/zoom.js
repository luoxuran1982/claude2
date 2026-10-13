"use strict";
// 界面缩放：70%–120%，每 5% 一档，共 11 档。作用于整个界面（rem 字号）和报告 iframe（CSS zoom）。
// 选择保存在本机浏览器存储里（每台电脑各自记住）；切换页面、刷新和重新打开应用后保留。
// 在 <head> 里同步加载，页面画出来之前就套用上次的档位，避免闪一下。
(function (root) {
  const ZOOM_STEPS = [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120];
  const KEY = "hotradar-zoom";
  const MIN = ZOOM_STEPS[0], MAX = ZOOM_STEPS[ZOOM_STEPS.length - 1];

  function clampZoom(pct) {
    const n = Math.round(Number(pct) / 5) * 5;
    if (!Number.isFinite(n)) return 100;
    return Math.min(MAX, Math.max(MIN, n));
  }
  function stepZoom(pct, dir) { return clampZoom(clampZoom(pct) + 5 * Math.sign(dir)); }

  const api = { ZOOM_STEPS, clampZoom, stepZoom, MIN, MAX };
  if (typeof module !== "undefined" && module.exports) { module.exports = api; return; }

  let current = 100;
  try { current = clampZoom(localStorage.getItem(KEY) || 100); } catch { /* 无痕模式等：用 100% */ }
  const apply = () => document.documentElement.style.setProperty("--zoom", String(current / 100));
  apply();

  function sync() {
    const sel = document.getElementById("zoom-select");
    if (!sel) return;
    sel.value = String(current);
    document.getElementById("zoom-out").disabled = current <= MIN;
    document.getElementById("zoom-in").disabled = current >= MAX;
    document.getElementById("zoom-reset").disabled = current === 100;
  }
  function set(pct) {
    current = clampZoom(pct);
    try { localStorage.setItem(KEY, String(current)); } catch { /* 忽略 */ }
    apply();
    sync();
    root.dispatchEvent(new CustomEvent("hotradar-zoom", { detail: current }));
  }
  function init() {
    const sel = document.getElementById("zoom-select");
    sel.innerHTML = ZOOM_STEPS.map(z => `<option value="${z}">${z}%</option>`).join("");
    sel.onchange = e => set(+e.target.value);
    document.getElementById("zoom-out").onclick = () => set(stepZoom(current, -1));
    document.getElementById("zoom-in").onclick = () => set(stepZoom(current, 1));
    document.getElementById("zoom-reset").onclick = () => set(100);
    document.addEventListener("keydown", e => {
      if (!(e.ctrlKey || e.metaKey)) return;
      if (e.key === "-" || e.key === "_") { e.preventDefault(); set(stepZoom(current, -1)); }
      else if (e.key === "=" || e.key === "+") { e.preventDefault(); set(stepZoom(current, 1)); }
      else if (e.key === "0") { e.preventDefault(); set(100); }
    });
    sync();
  }
  root.HZoom = { ...api, get: () => current, set, init };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init); else init();
})(typeof window !== "undefined" ? window : globalThis);
