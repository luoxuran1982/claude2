// 网页端纯函数测试：缩放档位、Markdown 渲染。运行：node tests/web.test.js
"use strict";
const assert = require("assert");
const path = require("path");
const Z = require(path.join(__dirname, "..", "qlibstudio", "web", "zoom.js"));
const M = require(path.join(__dirname, "..", "qlibstudio", "web", "md.js"));

let n = 0;
function test(name, fn) { fn(); n++; console.log("ok -", name); }

test("缩放共 11 档，70%–120%，每 5% 一档", () => {
  assert.deepStrictEqual(Z.ZOOM_STEPS, [70, 75, 80, 85, 90, 95, 100, 105, 110, 115, 120]);
});
test("＋/− 每次 5 个百分点，到上下限停住", () => {
  assert.strictEqual(Z.stepZoom(100, 1), 105);
  assert.strictEqual(Z.stepZoom(100, -1), 95);
  assert.strictEqual(Z.stepZoom(120, 1), 120);
  assert.strictEqual(Z.stepZoom(70, -1), 70);
  let z = 70, seen = [z];
  while (z < 120) { z = Z.stepZoom(z, 1); seen.push(z); }
  assert.deepStrictEqual(seen, Z.ZOOM_STEPS);
});
test("非法值归到合法档位", () => {
  assert.strictEqual(Z.clampZoom("abc"), 100);
  assert.strictEqual(Z.clampZoom(200), 120);
  assert.strictEqual(Z.clampZoom(10), 70);
  assert.strictEqual(Z.clampZoom(102), 100);
  assert.strictEqual(Z.clampZoom(103), 105);
});
test("Markdown：标题、表格、代码块、YAML 头", () => {
  const html = M.render("---\nproject: Qlib 量化研究台\nauthor: Claude\n---\n# 标题\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n```\ncode <x>\n```\n");
  assert.ok(html.includes('<table class="md-meta">') && html.includes("Qlib 量化研究台"));
  assert.ok(html.includes("<h1"));
  assert.ok(html.includes("<td>1</td>"));
  assert.ok(html.includes("code &lt;x&gt;"));
});
test("Markdown 不执行文档里的 HTML", () => {
  const html = M.render("<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>");
  assert.ok(!html.includes("<script>") && !html.includes("<img src=x"));
});
test("相对链接交给解析函数（带尖括号的中文文件名）", () => {
  const html = M.render("见 [使用指南](<01 使用指南.md>)", h => ({ href: "#", doc: "docs/" + h }));
  assert.ok(html.includes('data-doc="docs/01 使用指南.md"'));
});
console.log(`\n${n} 项通过`);
