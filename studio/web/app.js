"use strict";

// ---------------------------------------------------------------- 基础
const TOKEN = (() => {
  const u = new URL(location.href);
  const t = u.searchParams.get("t");
  if (t) {
    sessionStorage.setItem("studio-token", t);
    u.searchParams.delete("t");
    history.replaceState(null, "", u.pathname + u.hash);
  }
  return t || sessionStorage.getItem("studio-token") || "";
})();

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const media = (path) => `${path}${path.includes("?") ? "&" : "?"}t=${encodeURIComponent(TOKEN)}`;

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    headers: { "Content-Type": "application/json", "X-Studio-Token": TOKEN },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
  return data;
}

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "toast" + (error ? " error" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), error ? 6000 : 2500);
}

async function guard(fn) {
  try { return await fn(); } catch (e) { toast(e.message, true); throw e; }
}

const state = {
  boot: null,
  project: null,
  estimate: null,
  sceneId: null,
  orient: localStorage.getItem("orient") || "landscape",
  page: "projects",
  job: null,
  lastJobId: null,
};

// ---------------------------------------------------------------- 导航
function navigate(page) {
  if (["brief", "editor", "review", "export"].includes(page) && !state.project) page = "projects";
  state.page = page;
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.page === page));
  $$(".page").forEach((s) => s.classList.toggle("active", s.id === "page-" + page));
  location.hash = page;
  ({ projects: loadProjects, brief: renderBrief, editor: renderEditor, review: renderReview, export: renderExport,
     library: renderLibrary, settings: renderSettings })[page]?.();
}

function updateNav() {
  $$("#nav a").forEach((a) => {
    if (["brief", "editor", "review", "export"].includes(a.dataset.page)) a.classList.toggle("disabled", !state.project);
  });
  $("#currentTitle").textContent = state.project ? "当前：" + (state.project.title || state.project.brief.topic || state.project.id) : "";
}

// ---------------------------------------------------------------- 项目
async function loadProjects() {
  const { projects } = await api("GET", "/api/projects");
  const tbl = $("#projectTable");
  if (!projects.length) {
    tbl.innerHTML = `<tr><td class="empty">还没有项目。点右上角“新建项目”。</td></tr>`;
    return;
  }
  tbl.innerHTML = `<tr><th>标题</th><th>更新</th><th>分镜</th><th>口播字数</th><th>导出</th><th></th></tr>` +
    projects.map((p) => `<tr data-id="${esc(p.id)}">
      <td class="title">${esc(p.title)}</td><td>${esc(p.updated)}</td><td>${p.scenes}</td><td>${p.chars}</td><td>${p.exports}</td>
      <td class="row"><button class="small" data-act="open">打开</button><button class="small ghost" data-act="dup">复制</button><button class="small ghost danger" data-act="del">删除</button></td></tr>`).join("");
}

$("#projectTable").addEventListener("click", async (e) => {
  const tr = e.target.closest("tr[data-id]");
  if (!tr) return;
  const id = tr.dataset.id;
  const act = e.target.dataset.act || (e.target.closest("td.title") ? "open" : null);
  if (act === "open") {
    await openProject(id);
    navigate(state.project.scenes.length ? "editor" : "brief");
  } else if (act === "dup") {
    await guard(() => api("POST", `/api/projects/${id}/duplicate`));
    loadProjects();
  } else if (act === "del") {
    if (!confirm("删除这个项目？（会移到数据目录的 trash 文件夹）")) return;
    await guard(() => api("DELETE", `/api/projects/${id}`));
    if (state.project?.id === id) { state.project = null; updateNav(); }
    loadProjects();
  }
});

$("#newProject").onclick = async () => {
  const { project } = await guard(() => api("POST", "/api/projects", { brief: { minutes: 3 } }));
  setProject(project);
  navigate("brief");
};

async function openProject(id) {
  const data = await guard(() => api("GET", `/api/projects/${id}`));
  setProject(data.project, data.estimate);
  localStorage.setItem("lastProject", id);
}

function setProject(p, estimate) {
  state.project = p;
  if (estimate) state.estimate = estimate;
  if (!p.scenes.find((s) => s.id === state.sceneId)) state.sceneId = p.scenes[0]?.id || null;
  updateNav();
}

// 自动保存：改动后 600ms 写盘
let saveTimer = null, saving = Promise.resolve(), editVersion = 0;
function scheduleSave(after) {
  editVersion++;
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saving = saveNow().then(after || (() => {})); }, 600);
}
async function saveNow() {
  clearTimeout(saveTimer);
  saveTimer = null;
  if (!state.project) return;
  const v = editVersion;
  const data = await guard(() => api("PUT", `/api/projects/${state.project.id}`, { project: state.project }));
  // 保存期间又有新的输入：保留本地内容，稍后再存一次
  if (v === editVersion) state.project = data.project;
  else scheduleSave();
  state.estimate = data.estimate;
  updateNav();
  renderStats();
}

// ---------------------------------------------------------------- 创作
function fillSelect(sel, obj, value) {
  sel.innerHTML = Object.entries(obj).map(([k, v]) => `<option value="${esc(k)}">${esc(v)}</option>`).join("");
  if (value !== undefined) sel.value = value;
}

function renderBrief() {
  const p = state.project, b = p.brief;
  for (const k of ["topic", "audience", "minutes", "series", "views", "materials", "style"]) $("#b-" + k).value = b[k] ?? "";
  fillSelect($("#b-theme"), state.boot.themes, p.theme);
  briefEstimate();
}

function readBrief() {
  const p = state.project;
  for (const k of ["topic", "audience", "series", "views", "materials", "style"]) p.brief[k] = $("#b-" + k).value;
  p.brief.minutes = parseFloat($("#b-minutes").value) || 3;
  p.theme = $("#b-theme").value;
  if (!p.title) p.title = p.brief.topic;
}

function briefEstimate() {
  const cfg = state.boot.settings.llm;
  const minutes = parseFloat($("#b-minutes").value) || 3;
  const mats = $("#b-materials").value.length;
  const calls = minutes <= 6 ? 1 : 1 + Math.max(2, Math.min(8, Math.ceil(minutes / 3)));
  const inTok = calls * (1800 + mats * 0.7 + 400);
  const outTok = minutes * 280 * 2.4 + 600;
  const cost = (inTok * cfg.price_input + outTok * cfg.price_output) / 1e6;
  $("#b-estimate").textContent = `预计 ${calls} 次模型调用，约输入 ${Math.round(inTok / 1000)}k、输出 ${Math.round(outTok / 1000)}k token，约 ¥${cost.toFixed(3)}（按设置里的单价估算）。模型：${cfg.model}`;
}
["#b-minutes", "#b-materials"].forEach((s) => $(s).addEventListener("input", briefEstimate));
$$("#page-brief input, #page-brief textarea, #page-brief select").forEach((el) => {
  if (["b-script", "b-json"].includes(el.id)) return;
  el.addEventListener("input", () => { readBrief(); scheduleSave(); });
});

async function startGenerate(fresh) {
  readBrief();
  if (!state.project.brief.topic.trim()) return toast("请先填写主题", true);
  if (!state.project.brief.views.trim() && !confirm("还没有写作者观点。没有观点的视频容易被判“没有原创见解”。仍然生成？")) return;
  await saveNow();
  const { job } = await guard(() => api("POST", `/api/projects/${state.project.id}/generate`, { fresh }));
  onJobStarted(job);
}
$("#b-generate").onclick = () => startGenerate(false);
$("#b-regenerate").onclick = () => startGenerate(true);

$("#b-fromtext").onclick = async () => {
  if (state.project.scenes.length && !confirm("会替换现有分镜，继续？")) return;
  readBrief(); await saveNow();
  const { project } = await guard(() => api("POST", `/api/projects/${state.project.id}/from_text`, { text: $("#b-script").value }));
  setProject(project); toast(`切出 ${project.scenes.length} 个分镜，没有调用模型`); navigate("editor");
};
$("#b-importjson").onclick = async () => {
  if (state.project.scenes.length && !confirm("会替换现有分镜，继续？")) return;
  readBrief(); await saveNow();
  const { project } = await guard(() => api("POST", `/api/projects/${state.project.id}/import_json`, { data: $("#b-json").value }));
  setProject(project); toast(`导入 ${project.scenes.length} 个分镜`); navigate("editor");
};

// ---------------------------------------------------------------- 分镜编辑
const FIELDS = {
  title: [["subtitle", "副标题", "text"]],
  bullets: [["items", "要点（每行一条，建议 ≤16 字）", "lines"]],
  keyword: [["keyword", "关键词（≤8 字）", "text"], ["note", "补充说明", "text"]],
  stat: [["value", "数字", "text"], ["label", "说明", "text"]],
  code: [["language", "语言", "text"], ["code", "代码（≤12 行）", "code"], ["highlight", "强调的行号（逗号分隔）", "nums"]],
  flow: [["nodes", "节点（每行一个，2–6 个）", "lines"]],
  chart: [["chart", "图表类型", "select:bar=柱状图,line=折线图"], ["labels", "标签（每行一个）", "lines"], ["values", "数值（每行一个，和标签一一对应）", "numlines"], ["unit", "单位", "text"], ["source", "数据来源", "text"]],
  compare: [["left_title", "左栏标题", "text"], ["left", "左栏条目（每行一条）", "lines"], ["right_title", "右栏标题", "text"], ["right", "右栏条目（每行一条）", "lines"]],
  table: [["headers", "表头（用 | 分隔）", "pipe"], ["rows", "表格行（每行一条，用 | 分隔）", "table"]],
  quote: [["text", "引语 / 你的观点", "textarea"], ["source", "出处", "text"]],
  image: [["caption", "图片说明", "text"]],
};

function fieldToText(kind, v) {
  if (kind === "lines" || kind === "numlines") return (v || []).join("\n");
  if (kind === "nums") return (v || []).join(", ");
  if (kind === "pipe") return (v || []).join(" | ");
  if (kind === "table") return (v || []).map((r) => r.join(" | ")).join("\n");
  return v ?? "";
}
function textToField(kind, s) {
  if (kind === "lines") return s.split("\n").map((x) => x.trim()).filter(Boolean);
  if (kind === "numlines") return s.split("\n").map((x) => parseFloat(x.replace(/,/g, ""))).filter((x) => !isNaN(x));
  if (kind === "nums") return s.split(/[,，\s]+/).map((x) => parseInt(x)).filter((x) => !isNaN(x));
  if (kind === "pipe") return s.split("|").map((x) => x.trim()).filter(Boolean);
  if (kind === "table") return s.split("\n").filter((x) => x.trim()).map((r) => r.split("|").map((x) => x.trim()));
  return s;
}

function renderEditor() {
  const p = state.project;
  $("#p-title").value = p.title;
  fillSelect($("#p-theme"), state.boot.themes, p.theme);
  const music = { "": "无" };
  state.boot.music.forEach((m) => (music[m] = m));
  fillSelect($("#p-music"), music, p.music_file || "");
  $("#p-description").value = p.description;
  $("#p-tags").value = p.tags.join(", ");
  $$(".seg").forEach((b) => b.classList.toggle("active", b.dataset.orient === state.orient));
  renderStats();
  renderSceneList();
  renderSceneForm();
}

function renderStats() {
  const e = state.estimate, p = state.project;
  if (!e || !p) return;
  const shortChars = p.scenes.filter((s) => s.short).reduce((n, s) => n + s.narration.length, 0);
  $("#p-stats").textContent = `口播 ${e.chars} 字 / 目标 ${e.target_chars} 字 · 约 ${Math.floor(e.seconds / 60)} 分 ${e.seconds % 60} 秒 · ` +
    `竖版精简约 ${Math.round(shortChars / 280 * 60)} 秒 · 已调用模型 ${e.llm_calls} 次，约 ¥${e.llm_cost}`;
}

$("#p-title").oninput = (e) => { state.project.title = e.target.value; scheduleSave(); };
$("#p-theme").onchange = (e) => { state.project.theme = e.target.value; scheduleSave(refreshPreview); };
$("#p-music").onchange = (e) => { state.project.music_file = e.target.value; scheduleSave(); };
$("#p-description").oninput = (e) => { state.project.description = e.target.value; scheduleSave(); };
$("#p-tags").oninput = (e) => { state.project.tags = e.target.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean); scheduleSave(); };
$$(".seg").forEach((b) => (b.onclick = () => {
  state.orient = b.dataset.orient; localStorage.setItem("orient", state.orient);
  $$(".seg").forEach((x) => x.classList.toggle("active", x === b)); refreshPreview();
}));

function renderSceneList() {
  const p = state.project;
  $("#sceneList").innerHTML = p.scenes.map((s, i) => `<li data-id="${s.id}" class="${s.id === state.sceneId ? "active" : ""}">
    <div class="t">${i + 1}. <span class="badge">${esc(state.boot.scene_types[s.type])}</span>${s.short ? '<span class="badge short">精简</span>' : ""}</div>
    <div class="h">${esc(s.heading || s.narration.slice(0, 16) || "（空）")}</div></li>`).join("") ||
    `<li class="hint">没有分镜。到「创作」页生成，或点“新增”。</li>`;
}
$("#sceneList").onclick = (e) => {
  const li = e.target.closest("li[data-id]");
  if (!li) return;
  state.sceneId = li.dataset.id;
  renderSceneList(); renderSceneForm();
};

function currentScene() { return state.project.scenes.find((s) => s.id === state.sceneId); }

// 切换画面类型时按口播预填数据，避免空模板被服务端退回关键词卡
function defaultData(sc) {
  const h = sc.heading || "要点";
  const sents = sc.narration.split(/[。！？；\n]/).map((x) => x.trim()).filter(Boolean).map((x) => x.slice(0, 16));
  return {
    title: { subtitle: "" },
    bullets: { items: sents.length >= 2 ? sents.slice(0, 4) : [h, "第二点"] },
    keyword: { keyword: h.slice(0, 8), note: "" },
    stat: { value: "0", label: h },
    code: { language: "python", code: "# 在这里写代码\nprint('hello')", highlight: [] },
    flow: { nodes: ["第一步", "第二步", "第三步"] },
    chart: { chart: "bar", labels: ["A", "B", "C"], values: [1, 2, 3], unit: "", source: "" },
    compare: { left_title: "方案 A", left: ["特点"], right_title: "方案 B", right: ["特点"] },
    table: { headers: ["项目", "说明"], rows: [["", ""]] },
    quote: { text: sents[0] || h, source: "" },
    image: { caption: h },
  }[sc.type] || {};
}

function renderSceneForm() {
  const sc = currentScene(), box = $("#sceneForm");
  if (!sc) { box.innerHTML = `<p class="hint">选择左侧的分镜</p>`; $("#previewImg").removeAttribute("src"); return; }
  const idx = state.project.scenes.indexOf(sc);
  const secs = Math.round(sc.narration.length / 280 * 60);
  const fields = (FIELDS[sc.type] || []).map(([key, label, kind]) => {
    const v = fieldToText(kind, sc.data[key]);
    if (kind.startsWith("select:")) {
      const opts = kind.slice(7).split(",").map((o) => o.split("="));
      return `<label>${label}<select data-key="${key}" data-kind="text">${opts.map(([k, n]) => `<option value="${k}" ${v === k ? "selected" : ""}>${n}</option>`).join("")}</select></label>`;
    }
    if (["lines", "numlines", "table", "textarea", "code"].includes(kind))
      return `<label>${label}<textarea data-key="${key}" data-kind="${kind}" rows="${kind === "code" ? 10 : 4}" class="${kind === "code" ? "code" : ""}">${esc(v)}</textarea></label>`;
    return `<label>${label}<input data-key="${key}" data-kind="${kind}" value="${esc(v)}"></label>`;
  }).join("");
  const img = sc.type === "image" ? `<label>英文画面描述（用于和素材库标签比对）<input id="f-image_query" value="${esc(sc.image_query)}"></label>
      <div class="row"><button id="f-pickimg" class="small">选图（当前得分 ${sc.image.score ?? 0}${sc.image.id ? "" : "，未配图→文字卡"}）</button></div>` : "";
  box.innerHTML = `
    <div class="row between"><b>第 ${idx + 1} 镜</b>
      <div class="row"><button class="small ghost" data-op="up">上移</button><button class="small ghost" data-op="down">下移</button>
      <button class="small ghost" data-op="dup">复制</button><button class="small ghost danger" data-op="del">删除</button></div></div>
    <div class="grid2">
      <label>画面类型<select id="f-type">${Object.entries(state.boot.scene_types).map(([k, v]) => `<option value="${k}" ${k === sc.type ? "selected" : ""}>${v}</option>`).join("")}</select></label>
      <label>内容类别<select id="f-category">${Object.entries(state.boot.categories).map(([k, v]) => `<option value="${k}" ${k === sc.category ? "selected" : ""}>${v}</option>`).join("")}</select></label>
    </div>
    <label>画面标题<input id="f-heading" value="${esc(sc.heading)}"></label>
    <label>口播 <span class="hint" id="f-count">${sc.narration.length} 字 · 约 ${secs} 秒</span><textarea id="f-narration" rows="5">${esc(sc.narration)}</textarea></label>
    ${fields}
    ${img}
    <label class="check"><input type="checkbox" id="f-short" ${sc.short ? "checked" : ""}> 放进竖版精简版</label>
    <hr>
    <div class="row"><input id="f-instruction" placeholder="改写要求，例如：口播更口语化，加一个生活中的例子"><button id="f-rewrite" class="ghost">AI 改写本镜（1 次调用）</button></div>`;
  refreshPreview();
}

$("#sceneForm").addEventListener("input", (e) => {
  const sc = currentScene();
  if (!sc) return;
  const t = e.target;
  if (t.id === "f-heading") sc.heading = t.value;
  else if (t.id === "f-narration") { sc.narration = t.value; $("#f-count").textContent = `${t.value.length} 字 · 约 ${Math.round(t.value.length / 280 * 60)} 秒`; }
  else if (t.id === "f-image_query") sc.image_query = t.value;
  else if (t.id === "f-short") sc.short = t.checked;
  else if (t.dataset.key) sc.data[t.dataset.key] = textToField(t.dataset.kind, t.value);
  else return;
  if (t.id === "f-heading") renderSceneList();
  scheduleSave(refreshPreview);
});
$("#sceneForm").addEventListener("change", (e) => {
  const sc = currentScene();
  if (!sc) return;
  if (e.target.id === "f-type" || e.target.id === "f-category") {
    sc[e.target.id === "f-type" ? "type" : "category"] = e.target.value;
    if (e.target.id === "f-type") sc.data = defaultData(sc);
    if (sc.type === "image") sc.category = "concrete";
    saving = saveNow().then(() => { renderSceneList(); renderSceneForm(); });
  } else if (e.target.id === "f-short") {
    renderSceneList();
  }
});
$("#sceneForm").addEventListener("click", async (e) => {
  const sc = currentScene();
  if (!sc) return;
  const op = e.target.dataset.op;
  const list = state.project.scenes, i = list.indexOf(sc);
  if (op === "up" && i > 0) [list[i - 1], list[i]] = [list[i], list[i - 1]];
  else if (op === "down" && i < list.length - 1) [list[i + 1], list[i]] = [list[i], list[i + 1]];
  else if (op === "dup") list.splice(i + 1, 0, { ...JSON.parse(JSON.stringify(sc)), id: "" });
  else if (op === "del") { if (!confirm("删除这一镜？")) return; list.splice(i, 1); state.sceneId = list[Math.min(i, list.length - 1)]?.id; }
  else if (e.target.id === "f-pickimg") return pickImage(sc.id);
  else if (e.target.id === "f-rewrite") {
    await saveNow();
    const { job } = await guard(() => api("POST", `/api/projects/${state.project.id}/scenes/${sc.id}/rewrite`, { instruction: $("#f-instruction").value }));
    return onJobStarted(job);
  } else return;
  await saveNow();
  if (op === "dup") state.sceneId = state.project.scenes[i + 1].id;
  renderSceneList(); renderSceneForm();
});
$("#addScene").onclick = async () => {
  const list = state.project.scenes;
  const i = list.findIndex((s) => s.id === state.sceneId);
  list.splice(i + 1, 0, { type: "keyword", category: "abstract", heading: "新分镜", narration: "", data: { keyword: "新分镜", note: "" } });
  await saveNow();
  state.sceneId = state.project.scenes[i + 1].id;
  renderSceneList(); renderSceneForm();
};

function previewUrl(sid, orient, w) {
  return media(`/api/projects/${state.project.id}/preview/${sid}?o=${orient}&w=${w || ""}&v=${Date.now()}`);
}
function refreshPreview() {
  const sc = currentScene();
  if (!sc || state.page !== "editor") return;
  $(".scene-preview").classList.toggle("portrait", state.orient === "portrait");
  $("#previewImg").src = previewUrl(sc.id, state.orient, state.orient === "portrait" ? 540 : 960);
}

// ---------------------------------------------------------------- 选图
async function pickImage(sid) {
  const { candidates, min_score } = await guard(() => api("GET", `/api/projects/${state.project.id}/scenes/${sid}/candidates`));
  const sc = state.project.scenes.find((s) => s.id === sid);
  $("#modalTitle").textContent = `为「${sc.heading}」选图 · 及格线 ${min_score}`;
  $("#modalContent").innerHTML = `<p class="hint">画面描述：${esc(sc.image_query || "（无）")}。灰色的是低于及格线的候选。</p>
    <div class="row" style="margin-bottom:10px"><button data-act="reject" class="small">这张不对，换下一张</button><button data-act="clear" class="small ghost">不用图，改用文字卡</button></div>
    <div class="cand-grid">${candidates.map((c) => `<div class="cand ${c.score < min_score ? "below" : ""}" data-id="${c.id}" data-score="${c.score}">
      <img src="${media("/media/library/" + c.file + "?thumb=1")}">
      <div class="body">得分 ${c.score} · ${esc((c.tags || []).join(", ")).slice(0, 80)}</div></div>`).join("") || '<p class="hint">素材库里没有可用的图。到「素材库」导入。</p>'}</div>`;
  $("#modal").classList.remove("hidden");
  $("#modalContent").onclick = async (e) => {
    const card = e.target.closest(".cand");
    const act = e.target.dataset.act;
    let body = null;
    if (card) body = { action: "choose", image_id: card.dataset.id, score: card.dataset.score };
    else if (act) body = { action: act };
    else return;
    const { project } = await guard(() => api("POST", `/api/projects/${state.project.id}/scenes/${sid}/image`, body));
    setProject(project);
    $("#modal").classList.add("hidden");
    if (state.page === "review") renderReview(); else renderSceneForm();
  };
}
$("#modalClose").onclick = () => $("#modal").classList.add("hidden");

// ---------------------------------------------------------------- 审片
async function renderReview() {
  const p = state.project;
  $("#r-declared").checked = p.ai_declared;
  const data = await guard(() => api("GET", `/api/projects/${p.id}/review`));
  const sevName = { high: "高", medium: "中", low: "低" };
  $("#r-issues").innerHTML = data.issues.map((i) => `<li><span class="badge ${i.severity}">${sevName[i.severity]}</span><b>${esc(i.category)}</b>
    ${i.index ? `<a href="#" data-goto="${i.scene}">第 ${i.index} 镜</a>` : ""} ${i.match ? `<code>${esc(i.match)}</code>` : ""}<div class="hint">${esc(i.message)}</div></li>`).join("") || "<li>没有发现规则命中的问题。规则只能查出一部分，仍需人工审片。</li>";
  const confName = { high: "高", medium: "中", low: "低" };
  $("#r-claims").innerHTML = p.claims.map((c, i) => `<li><input type="checkbox" data-claim="${i}" ${c.checked ? "checked" : ""}>
    <div><span class="badge ${c.confidence === "low" ? "high" : c.confidence === "medium" ? "medium" : "low"}">把握${confName[c.confidence]}</span>${esc(c.text)}${c.note ? `<div class="hint">${esc(c.note)}</div>` : ""}</div></li>`).join("") || "<li class='hint'>没有事实清单。</li>";
  const pr = data.pronunciation;
  $("#r-pron").innerHTML = `<p>英文术语：${pr.terms.map((t) => `<code>${esc(t.term)}${t.mapped ? " → " + esc(t.mapped) : ""}</code>`).join(" ") || "无"}</p>
    <p>数字：${pr.numbers.map((n) => `<code>${esc(n)}</code>`).join(" ") || "无"}</p>
    <p class="hint">读错的词到「设置 → 读音替换」里加一行，例如 <code>GPU=G P U</code>。只影响配音，字幕不变。</p>`;
  const flagged = new Set(data.issues.filter((i) => i.scene).map((i) => i.scene));
  $("#r-grid").innerHTML = p.scenes.map((s, i) => `<div class="scene-tile ${flagged.has(s.id) ? "flag" : ""}">
    <img loading="lazy" data-sid="${s.id}" data-type="${s.type}" src="${previewUrl(s.id, "landscape", 480)}">
    <div class="body"><span class="badge">${i + 1} · ${esc(state.boot.scene_types[s.type])}</span>${s.type === "image" ? `<span class="badge">${s.image.id ? "配图 " + s.image.score : "无图→文字卡"}</span>` : ""}
    <div>${esc(s.narration)}</div></div></div>`).join("");
}
$("#r-grid").onclick = (e) => {
  const img = e.target.closest("img[data-sid]");
  if (!img) return;
  if (img.dataset.type === "image") return pickImage(img.dataset.sid);
  state.sceneId = img.dataset.sid; navigate("editor");
};
$("#r-issues").onclick = (e) => {
  const a = e.target.closest("[data-goto]");
  if (!a) return;
  e.preventDefault(); state.sceneId = a.dataset.goto; navigate("editor");
};
$("#r-claims").onchange = (e) => {
  const i = e.target.dataset.claim;
  if (i === undefined) return;
  state.project.claims[i].checked = e.target.checked;
  scheduleSave(renderReview);
};
$("#r-declared").onchange = (e) => { state.project.ai_declared = e.target.checked; scheduleSave(renderReview); };
$("#r-refresh").onclick = renderReview;
$("#r-factcheck").onclick = async () => {
  await saveNow();
  const { job } = await guard(() => api("POST", `/api/projects/${state.project.id}/factcheck`));
  onJobStarted(job);
};

// ---------------------------------------------------------------- 导出
function renderExport() {
  const p = state.project;
  const titles = p.cover_titles.length ? p.cover_titles : [p.title];
  $("#coverChoices").innerHTML = titles.map((t, i) => `<span class="cover-choice ${i === p.cover_choice ? "active" : ""}" data-i="${i}">${esc(t)}</span>`).join("");
  const title = encodeURIComponent(titles[p.cover_choice] || p.title);
  $("#coverL").src = media(`/api/projects/${p.id}/cover?o=landscape&title=${title}&v=${Date.now()}`);
  $("#coverP").src = media(`/api/projects/${p.id}/cover?o=portrait&title=${title}&v=${Date.now()}`);
  const ex = [...p.exports].reverse();
  $("#exports").innerHTML = ex.map((e) => {
    const base = `/media/projects/${p.id}/${encodeURIComponent(e.dir)}/`;
    const stale = e.hash !== state.estimate?.hash;
    return `<div class="card export-item">
      <video controls preload="metadata" src="${media(base + encodeURIComponent(e.video))}"></video>
      <div><h3>${esc(e.video)}</h3>${stale ? '<p class="hint">分镜在这次导出之后改过，成片不是最新版本。</p>' : ""}
        <p class="hint">${esc(e.time)} · ${e.duration} 秒 · ${e.scenes} 镜 · 配音 ${esc(e.tts)}</p>
        <div class="row wrap">
          <a href="${media(base + encodeURIComponent(e.video))}" download><button class="small">下载视频</button></a>
          <a href="${media(base + encodeURIComponent(e.srt))}" download><button class="small ghost">SRT 字幕</button></a>
          <a href="${media(base + encodeURIComponent("封面-横版.jpg"))}" download><button class="small ghost">横版封面</button></a>
          <a href="${media(base + encodeURIComponent("封面-竖版.jpg"))}" download><button class="small ghost">竖版封面</button></a>
          <a href="${media(base + encodeURIComponent("发布信息.txt"))}" target="_blank"><button class="small ghost">发布信息</button></a>
          <button class="small ghost" data-reveal="${esc(e.dir)}">打开文件夹</button>
        </div></div></div>`;
  }).join("") || `<p class="hint">还没有导出。</p>`;
  $("#jobLog").textContent = (state.job?.log || []).join("\n");
}
$("#coverChoices").onclick = (e) => {
  const c = e.target.closest("[data-i]");
  if (!c) return;
  state.project.cover_choice = +c.dataset.i;
  scheduleSave(renderExport);
  $$(".cover-choice").forEach((x) => x.classList.toggle("active", x === c));
};
$("#exports").onclick = (e) => {
  const b = e.target.closest("[data-reveal]");
  if (b) guard(() => api("POST", `/api/projects/${state.project.id}/reveal`, { dir: b.dataset.reveal }));
};
$$("[data-render]").forEach((b) => (b.onclick = async () => {
  const [orientation, mode] = b.dataset.render.split(":");
  await saveNow();
  if (!state.project.ai_declared && !confirm("还没有在审片页勾选“发布时声明 AI 内容”。继续渲染？")) return;
  const { job } = await guard(() => api("POST", `/api/projects/${state.project.id}/render`, { orientation, mode }));
  onJobStarted(job);
}));

// ---------------------------------------------------------------- 任务
function onJobStarted(job) {
  state.job = job; state.lastJobId = job.id;
  showJob(job); pollJob();
}
function showJob(job) {
  const bar = $("#jobbar");
  if (!job) { bar.classList.add("hidden"); return; }
  bar.classList.toggle("hidden", job.state !== "running" && Date.now() / 1000 - (job.ended || 0) > 8);
  $("#jobLabel").textContent = job.label;
  $("#jobMsg").textContent = job.message + (job.state === "running" ? `（${job.elapsed} 秒）` : "");
  $("#jobProgress").style.width = Math.round(job.progress * 100) + "%";
  $("#jobCancel").classList.toggle("hidden", job.state !== "running");
  if (state.page === "export") $("#jobLog").textContent = (job.log || []).join("\n");
  $$("[data-render], #b-generate, #b-regenerate, #f-rewrite, #r-factcheck").forEach((b) => (b.disabled = job.state === "running"));
}
let pollTimer = null;
async function pollJob() {
  clearTimeout(pollTimer);
  try {
    const { job } = await api("GET", "/api/job");
    const prev = state.job;
    state.job = job;
    showJob(job);
    if (job && prev && prev.id === job.id && prev.state === "running" && job.state !== "running") onJobFinished(job);
    pollTimer = setTimeout(pollJob, job && job.state === "running" ? 700 : 4000);
  } catch (e) {
    pollTimer = setTimeout(pollJob, 4000);
  }
}
async function onJobFinished(job) {
  if (job.state === "done") toast(job.label + "：" + job.message);
  else if (job.state === "failed") toast(job.label + " " + job.message, true);
  else toast(job.label + " 已取消");
  if (state.project && job.project === state.project.id) {
    await openProject(state.project.id);
    if (job.kind === "generate" && job.state === "done") navigate("editor");
    else navigate(state.page);
  }
}
$("#jobCancel").onclick = () => api("POST", "/api/job/cancel");

// ---------------------------------------------------------------- 素材库
async function renderLibrary() {
  const { images } = await guard(() => api("GET", "/api/library"));
  $("#l-grid").innerHTML = images.map((im) => `<div class="lib-tile" data-id="${im.id}">
    <img loading="lazy" src="${media("/media/library/" + im.file + "?thumb=1")}">
    <div class="body"><input data-f="tags" value="${esc(im.tags.join(", "))}" title="标签">
      <input data-f="license" value="${esc(im.license || "")}" placeholder="授权说明">
      <div class="row between"><span class="hint">${esc(im.source)}${im.approved ? " · 确认 " + im.approved : ""}</span><button class="small ghost danger" data-del>删除</button></div></div></div>`).join("") ||
    `<p class="hint">素材库是空的。没有图也能出片：配图镜头会自动改用文字卡。</p>`;
}
$("#l-grid").addEventListener("change", async (e) => {
  const tile = e.target.closest(".lib-tile");
  if (!tile || !e.target.dataset.f) return;
  const body = {};
  body[e.target.dataset.f] = e.target.dataset.f === "tags" ? e.target.value.split(/[,，]/).map((x) => x.trim()).filter(Boolean) : e.target.value;
  await guard(() => api("POST", `/api/library/${tile.dataset.id}`, body));
  toast("已保存");
});
$("#l-grid").addEventListener("click", async (e) => {
  if (!e.target.hasAttribute("data-del")) return;
  if (!confirm("从素材库删除这张图？")) return;
  await guard(() => api("DELETE", `/api/library/${e.target.closest(".lib-tile").dataset.id}`));
  renderLibrary();
});
$("#l-upload").onclick = async () => {
  const files = Array.from($("#l-files").files);
  if (!files.length) return toast("先选择图片", true);
  const tags = $("#l-tags").value.trim();
  const payload = [];
  for (const f of files) {
    const data = await new Promise((ok) => { const r = new FileReader(); r.onload = () => ok(r.result); r.readAsDataURL(f); });
    payload.push({ name: f.name, data, tags: tags ? tags.split(/[,，]/) : null });
  }
  const { added } = await guard(() => api("POST", "/api/library", { files: payload, license: $("#l-license").value }));
  toast(`导入 ${added.length} 张`);
  $("#l-files").value = "";
  renderLibrary();
};
$("#l-importFolder").onclick = async () => {
  const { count } = await guard(() => api("POST", "/api/library/import_folder", { folder: $("#l-folder").value }));
  toast(`导入 ${count} 张`); renderLibrary();
};
$("#l-open").onclick = () => guard(() => api("POST", "/api/open_folder", { which: "library" }));
$$("[data-open]").forEach((b) => (b.onclick = () => guard(() => api("POST", "/api/open_folder", { which: b.dataset.open }))));

// ---------------------------------------------------------------- 设置
const SECRET_FIELDS = ["llm_key", "check_key", "tts_key", "pexels_key"];
function renderSettings() {
  const b = state.boot, s = b.settings;
  $("#s-preset").innerHTML = `<option value="">选择服务商自动填写…</option>` + b.llm_presets.map((p, i) => `<option value="${i}">${esc(p.name)}</option>`).join("");
  $("#s-tts-engine").innerHTML = `<option value="auto">自动（系统语音）</option>` + b.tts_engines.map((e) => `<option value="${e.id}">${esc(e.name)}</option>`).join("");
  for (const [sec, obj] of Object.entries(s)) {
    if (typeof obj !== "object" || sec === "pronunciation") continue;
    for (const [k, v] of Object.entries(obj)) {
      const el = $(`#s-${sec}-${k}`);
      if (!el) continue;
      if (el.type === "checkbox") el.checked = !!v; else el.value = v;
    }
  }
  for (const k of SECRET_FIELDS) { const el = $("#s-" + k); el.value = ""; el.placeholder = b.secrets[k] ? "已保存（留空不修改；输入空格清除）" : "未设置"; }
  $("#s-pron").value = Object.entries(s.pronunciation).map(([a, c]) => `${a}=${c}`).join("\n");
  $("#s-env").innerHTML = `${esc(b.python)} · 平台 ${esc(b.platform)}<br>FFmpeg：${esc(b.ffmpeg || "未找到")}<br>中文字体：${esc(b.fonts.regular || "未找到（把 .ttf/.otf 放进字体文件夹）")}<br>当前配音引擎：${esc(b.tts_resolved)}<br>数据目录：${esc(b.data_dir)}`;
  loadVoices();
}
async function loadVoices() {
  const { voices } = await api("GET", `/api/voices?engine=${$("#s-tts-engine").value}`).catch(() => ({ voices: [] }));
  $("#voiceList").innerHTML = voices.map((v) => `<option value="${esc(v.name)}">${esc(v.lang)}</option>`).join("");
}
$("#s-tts-engine").onchange = loadVoices;
$("#s-preset").onchange = (e) => {
  const p = state.boot.llm_presets[e.target.value];
  if (!p) return;
  $("#s-llm-base_url").value = p.base_url;
  if (p.model) $("#s-llm-model").value = p.model;
};
function collectSettings() {
  const s = JSON.parse(JSON.stringify(state.boot.settings));
  for (const [sec, obj] of Object.entries(s)) {
    if (typeof obj !== "object" || sec === "pronunciation") continue;
    for (const k of Object.keys(obj)) {
      const el = $(`#s-${sec}-${k}`);
      if (!el) continue;
      obj[k] = el.type === "checkbox" ? el.checked : el.type === "number" ? parseFloat(el.value) : el.value;
    }
  }
  s.pronunciation = {};
  $("#s-pron").value.split("\n").forEach((line) => { const i = line.indexOf("="); if (i > 0) s.pronunciation[line.slice(0, i).trim()] = line.slice(i + 1).trim(); });
  const secrets = {};
  for (const k of SECRET_FIELDS) {
    const v = $("#s-" + k).value;
    if (v === "") continue;
    secrets[k] = v.trim() === "" ? "" : v.trim();
  }
  return { settings: s, secrets };
}
async function saveSettings() {
  const data = await guard(() => api("POST", "/api/settings", collectSettings()));
  state.boot.settings = data.settings; state.boot.secrets = data.secrets;
  const b = await api("GET", "/api/bootstrap"); state.boot = b;
  renderSettings();
  $("#s-saved").textContent = "已保存 " + new Date().toLocaleTimeString();
}
$("#s-save").onclick = saveSettings;
$("#s-llmtest").onclick = async () => {
  await saveSettings();
  $("#s-llmres").textContent = "测试中…";
  try {
    const r = await api("POST", "/api/llm/test");
    $("#s-llmres").textContent = `成功：${r.reply}（输入 ${r.usage.prompt_tokens}、输出 ${r.usage.completion_tokens} token）`;
  } catch (e) { $("#s-llmres").textContent = "失败：" + e.message; }
};
$("#s-ttstest").onclick = async () => {
  await saveSettings();
  const r = await guard(() => api("POST", "/api/tts/test", { text: $("#s-ttstext").value }));
  const a = $("#s-audio"); a.classList.remove("hidden"); a.src = media(r.url); a.play().catch(() => {});
  toast(`引擎 ${r.engine}，${r.seconds} 秒`);
};

// ---------------------------------------------------------------- 启动
$$("#nav a").forEach((a) => (a.onclick = () => navigate(a.dataset.page)));
$("#quit").onclick = async () => {
  if (state.job?.state === "running" && !confirm("有任务在运行，退出会中断它。确定退出？")) return;
  if (saveTimer) await saveNow();
  await api("POST", "/api/shutdown").catch(() => {});
  document.body.innerHTML = `<div class="empty">已退出。可以关闭这个页面。</div>`;
};
document.addEventListener("keydown", (e) => {
  if ((e.metaKey || e.ctrlKey) && e.key === "s") { e.preventDefault(); saveNow().then(() => toast("已保存")); }
  if (e.key === "Escape") $("#modal").classList.add("hidden");
});
window.addEventListener("beforeunload", (e) => { if (saveTimer) { saveNow(); } });

(async function init() {
  if (!TOKEN) {
    document.body.innerHTML = `<div class="empty">请从启动程序打印的链接打开本页面（链接里带有访问令牌）。</div>`;
    return;
  }
  state.boot = await guard(() => api("GET", "/api/bootstrap"));
  $("#dataDir").textContent = state.boot.data_dir;
  const last = localStorage.getItem("lastProject");
  if (last) await openProject(last).catch(() => localStorage.removeItem("lastProject"));
  updateNav();
  const hash = location.hash.slice(1);
  navigate(hash || (state.project ? "editor" : "projects"));
  if (state.boot.job) { state.job = state.boot.job; showJob(state.job); }
  pollJob();
})();
