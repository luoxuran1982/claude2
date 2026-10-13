"use strict";
// 缩放档位逻辑测试：node tests/zoom.test.js
const assert = require("assert");
const path = require("path");
const Z = require(path.join(__dirname, "..", "svf", "web", "zoom.js"));

assert.deepStrictEqual(Z.STEPS, [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120], "11 档");
assert.strictEqual(Z.clamp(100), 100);
assert.strictEqual(Z.clamp(10), 70);
assert.strictEqual(Z.clamp(500), 120);
assert.strictEqual(Z.clamp("103"), 105);
assert.strictEqual(Z.clamp("abc"), 100);

// ＋ 从 70 逐档走到 120，每次 +5，到顶不再变
let p = 70;
const seen = [p];
for (let i = 0; i < 12; i++) { p = Z.step(p, 1); seen.push(p); }
assert.deepStrictEqual(seen.slice(0, 11), Z.STEPS);
assert.strictEqual(p, 120);
// − 从 120 走到 70
for (let i = 0; i < 12; i++) p = Z.step(p, -1);
assert.strictEqual(p, 70);
// 恢复 100%
assert.strictEqual(Z.step(115, 0), 100);

// 上下限时按钮禁用；显示的值与档位一致
assert.deepStrictEqual(Z.buttons(70), { minusDisabled: true, plusDisabled: false, resetDisabled: false, label: "70%" });
assert.deepStrictEqual(Z.buttons(120), { minusDisabled: false, plusDisabled: true, resetDisabled: false, label: "120%" });
assert.deepStrictEqual(Z.buttons(100), { minusDisabled: false, plusDisabled: false, resetDisabled: true, label: "100%" });

console.log("zoom.test.js：全部通过");
