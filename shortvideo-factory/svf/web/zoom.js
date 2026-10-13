"use strict";
// 界面缩放的档位逻辑（外壳页面和单元测试共用）：70%–120%，每 5% 一档，共 11 档，默认 100%。
(function (root) {
  const STEPS = [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120];
  const MIN = STEPS[0], MAX = STEPS[STEPS.length - 1];

  function clamp(pct) {
    const n = Math.round(Number(pct) / 5) * 5;
    if (!Number.isFinite(n)) return 100;
    return Math.min(MAX, Math.max(MIN, n));
  }

  // dir = -1（−）/ +1（＋）/ 0（恢复 100%）
  function step(pct, dir) {
    if (dir === 0) return 100;
    const i = STEPS.indexOf(clamp(pct));
    return STEPS[Math.min(STEPS.length - 1, Math.max(0, i + (dir > 0 ? 1 : -1)))];
  }

  function buttons(pct) {
    const p = clamp(pct);
    return { minusDisabled: p <= MIN, plusDisabled: p >= MAX, resetDisabled: p === 100, label: p + "%" };
  }

  const api = { STEPS, MIN, MAX, clamp, step, buttons };
  if (typeof module !== "undefined" && module.exports) module.exports = api; else root.SVFZoom = api;
})(typeof window !== "undefined" ? window : globalThis);
