/* 县域医共体信息化平台 管理端 SPA */
"use strict";

/* 管理端 · 公共层：请求、鉴权、表格/图表助手、路由。
 *
 * 阶段十二把原来 5489 行的 app.js 按顶层块边界切成 5 个文件，**不引入构建工具**
 * ——build-free 是既定约束，拆文件即可，不要借机上打包器。
 *
 * 加载顺序在 index.html 里定死，且有含义：本文件最先，页面文件居中，
 * app.js（页面注册表 + 启动）最后。**注册表必须最后**——它在求值时就要拿到
 * 每个 renderX 的引用，而函数声明只在**同一个文件内**提升。
 */

const $ = (sel) => document.querySelector(sel);
let token = localStorage.getItem("medplat_token") || "";

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const resp = await fetch(path, { ...options, headers });
  if (resp.status === 401) { logout(); throw new Error("登录已过期"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

function logout() {
  token = "";
  localStorage.removeItem("medplat_token");
  localStorage.removeItem("medplat_role");
  stopTodoPolling();
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
}

/* ---------------- 待办铃铛（轮询 /api/todos） ---------------- */

let todoTimer = null;

async function pollTodos() {
  try {
    // 待办是"该我处理的活"，站内消息是"该我知道的事"，两者都进同一个铃铛：
    // 用户不会为了看有没有新消息去点第二个图标。
    const [data, notify] = await Promise.all([
      api("/api/todos"), api("/api/notifications/unread-count")]);
    const total = data.total + notify.unread;
    const count = $("#todo-count");
    count.textContent = total > 99 ? "99+" : total;
    count.classList.toggle("hidden", total === 0);
    const panel = $("#todo-panel");
    const notifyBlock = notify.unread
      ? `<h4>站内消息（${notify.unread}）</h4><div class="todo-item">有未读消息，点击左侧「站内消息」查看</div>`
      : "";
    panel.innerHTML = data.items.length || notify.unread
      ? notifyBlock + data.items.map((it) => `<h4>${esc(it.title)}（${it.count}）</h4>${
          it.list.slice(0, 5).map((row) =>
            `<div class="todo-item">${esc(row.item_name || row.diagnosis_name || row.drug_name || row.conclusion || `#${row.id}`)}</div>`).join("")
        }`).join("")
      : '<div class="todo-empty">暂无待办事项</div>';
  } catch (e) { /* 登录过期等由 api() 统一处理 */ }
}

function startTodoPolling() {
  $("#todo-bell").classList.remove("hidden");
  pollTodos();
  if (!todoTimer) todoTimer = setInterval(pollTodos, 30000);
}

function stopTodoPolling() {
  if (todoTimer) { clearInterval(todoTimer); todoTimer = null; }
  const bell = $("#todo-bell");
  if (bell) { bell.classList.add("hidden"); $("#todo-panel").classList.add("hidden"); }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function table(cols, rows, renderRow) {
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = rows.length
    ? rows.map(renderRow).join("")
    : `<tr><td colspan="${cols.length}" style="color:#8a939e">暂无数据</td></tr>`;
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

function setMsg(id, text, ok = true) {
  const el = $(id);
  if (el) { el.textContent = text; el.className = `msg ${ok ? "ok" : "err"}`; }
}

/* ---------------- 页面定义 ---------------- */

const ROLE_NAMES = { admin: "平台管理员", director: "管理层", doctor: "医师", pharmacist: "药师", public_health: "公卫人员", operator: "经办人员" };

function currentRole() { return localStorage.getItem("medplat_role") || ""; }

function pageAllowed(p) {
  if (!p.roles) return true;
  const role = currentRole();
  return role === "admin" || p.roles.includes(role);
}

const CENTER_NAMES = { imaging: "影像", ecg: "心电", lab: "检验", pathology: "病理" };
const ORG_TYPES = { lead_hospital: "牵头医院", township: "乡镇卫生院", village: "村卫生室", public_health: "公卫机构" };
const LEVELS = { county: "县级", township: "乡级", village: "村级" };
// 慢病病种：启动为兜底值，进入慢病页时从 /api/chronic/disease-types 目录刷新（块1）
let DISEASES = { hypertension: "高血压", diabetes: "2型糖尿病", copd: "慢阻肺" };
const RX_STATUS = { auto_passed: ["系统审通过", "green"], pending_review: ["待药师审", "orange"], approved: ["药师审通过", "green"], rejected: ["已退回", "red"] };
const EXAM_STATUS = { pending: ["待诊断", "orange"], diagnosing: ["诊断中", ""], reported: ["已报告", "green"], recognized: ["已互认", "green"] };
const REF_STATUS = { pending: ["待接诊", "orange"], accepted: ["已接诊", ""], completed: ["已结案", "green"], rejected: ["已退回", "red"] };

function nav(pageId) {
  location.hash = pageId;
}

async function route() {
  if (!token) return;
  const id = location.hash.replace("#", "") || "dashboard";
  let page = PAGES.find((p) => p.id === id) || PAGES[1];
  if (!pageAllowed(page)) page = PAGES[1];
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === page.id));
  $("#main").innerHTML = `<h2>${esc(page.title)}</h2><div class="desc" id="page-desc"></div><div id="page-body">加载中…</div>`;
  try { await page.render(); }
  catch (e) { $("#page-body").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; }
}

/* 横向条形图（纯SVG，无外部依赖） */
function barChart(items, { color = "#0b6e6e", unit = "" } = {}) {
  const max = Math.max(...items.map(([, v]) => v), 1);
  const rowH = 30, labelW = 150, chartW = 480;
  const rows = items.map(([label, value], i) => {
    const w = Math.round((value / max) * (chartW - 70));
    const y = i * rowH;
    return `<text x="${labelW - 8}" y="${y + 19}" text-anchor="end" font-size="12.5" fill="#5b6773">${esc(label)}</text>
      <rect x="${labelW}" y="${y + 6}" width="${Math.max(w, 2)}" height="16" rx="3" fill="${color}"></rect>
      <text x="${labelW + Math.max(w, 2) + 6}" y="${y + 19}" font-size="12.5" fill="#24292f">${esc(value)}${esc(unit)}</text>`;
  }).join("");
  return `<svg width="${labelW + chartW}" height="${items.length * rowH}" role="img">${rows}</svg>`;
}

/* ---------------- 各页面 ---------------- */

function lineChart(months, series, colors) {
  const w = 640, h = 200, padL = 36, padB = 24, padT = 10;
  const all = Object.values(series).flat();
  const max = Math.max(...all, 1);
  const x = (i) => padL + (i * (w - padL - 10)) / Math.max(months.length - 1, 1);
  const y = (v) => padT + (h - padT - padB) * (1 - v / max);
  let svg = "";
  Object.entries(series).forEach(([name, values], si) => {
    const points = values.map((v, i) => `${x(i)},${y(v)}`).join(" ");
    svg += `<polyline points="${points}" fill="none" stroke="${colors[si % colors.length]}" stroke-width="2"/>`;
    values.forEach((v, i) => { svg += `<circle cx="${x(i)}" cy="${y(v)}" r="2.5" fill="${colors[si % colors.length]}"/>`; });
  });
  months.forEach((mo, i) => { svg += `<text x="${x(i)}" y="${h - 6}" font-size="10.5" fill="#5b6773" text-anchor="middle">${mo.slice(2)}</text>`; });
  svg += `<text x="4" y="${y(max) + 4}" font-size="10.5" fill="#5b6773">${max}</text><text x="4" y="${y(0) + 4}" font-size="10.5" fill="#5b6773">0</text>`;
  return `<svg width="${w}" height="${h}" role="img">${svg}</svg>`;
}

/* 块2：指标下钻——指标卡/预警横幅点击后拉取明细，行可跳转对应业务页 */
async function openDrilldown(metric, offset = 0) {
  const panel = $("#drill-panel");
  if (!panel) return;
  panel.classList.remove("hidden");
  panel.innerHTML = "<div class='panel'>明细加载中…</div>";
  const limit = 20;
  const d = await api(`/api/metrics/drilldown?metric=${encodeURIComponent(metric)}&offset=${offset}&limit=${limit}`);
  const pager = [];
  if (offset > 0) pager.push(`<button class="btn secondary" data-drillpage="${Math.max(offset - limit, 0)}">上一页</button>`);
  if (offset + limit < d.total) pager.push(`<button class="btn secondary" data-drillpage="${offset + limit}">下一页</button>`);
  panel.innerHTML = `<div class="panel" style="border-left:4px solid #0b6e6e">
    <h3>${esc(d.label)} 明细（${d.total}）　<button class="btn secondary" data-drillclose="1">关闭</button></h3>
    <p class="desc" style="font-size:12.5px">点击明细行跳转「${esc(d.page)}」业务页；口径与驾驶舱指标、预警横幅一致</p>
    ${table(d.columns, d.items, (row) =>
      `<tr data-drillgo="${esc(d.page)}" style="cursor:pointer">${
        d.fields.map((f) => `<td>${esc(row[f] ?? "—")}</td>`).join("")}</tr>`)}
    <div style="margin-top:8px">${pager.join(" ")}　<span style="font-size:12.5px;color:#5b6773">第 ${Math.floor(offset / limit) + 1} 页 / 共 ${Math.max(Math.ceil(d.total / limit), 1)} 页</span></div></div>`;
  panel.dataset.metric = metric;
  panel.dataset.offset = String(offset);
}

async function renderDashboard() {
  $("#page-desc").textContent = "指标口径对齐《紧密型县域医共体监测指标体系（2024版）》；指标卡与预警可点击下钻明细";
  const m = await api("/api/metrics/overview");
  // 第4项为下钻指标 key（与 /api/metrics/drilldown 的 metric 同名，口径服务端统一）
  const cards = [
    ["成员单位数", m.resources.organizations],
    ["建档患者数", m.resources.patients],
    ["基层诊疗人次占比", m.service_division.grassroots_encounter_ratio_pct + "%", false, "grassroots_encounters"],
    ["远程诊断量", m.remote_diagnosis.reported_total, false, "reported_exams"],
    ["结果互认量", m.remote_diagnosis.recognized_total, false, "recognized_exams"],
    ["危急值", m.remote_diagnosis.critical_values, m.remote_diagnosis.critical_values > 0, "critical_values"],
    ["上转", m.referrals.up, false, "referrals_up"],
    ["下转", m.referrals.down, false, "referrals_down"],
    ["审方总量", m.prescription_review.total],
    ["待药师审", m.prescription_review.pending_review, m.prescription_review.pending_review > 0, "pending_reviews"],
    ["退回处方", m.prescription_review.rejected, m.prescription_review.rejected > 0, "rejected_prescriptions"],
    ["慢病在管人数", m.chronic_management.total],
    ["缺药预警", m.pharmacy.stock_alerts, m.pharmacy.stock_alerts > 0, "stock_alerts"],
  ];
  const chronicItems = Object.entries(m.chronic_management.by_level).map(([lvl, n]) => [`${lvl} 级`, n]);
  let perfHtml = "";
  try {
    const perf = await api("/api/performance/orgs");
    const top = perf.scorecards.slice(0, 8).map((c) => [c.org_name, c.score]);
    if (top.length) perfHtml = `<div class="panel"><h3>机构绩效评分（前8）</h3>${barChart(top, { unit: " 分" })}</div>`;
  } catch (e) { /* 绩效不可用不阻塞驾驶舱 */ }
  const [alerts, trends] = await Promise.all([api("/api/metrics/alerts"), api("/api/metrics/trends?months=6")]);
  const alertBanner = alerts.total
    ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 风险预警（${alerts.total}）</h3>
       <p style="font-size:13.5px">${alerts.items.map((a) =>
        `<span class="tag red" style="margin-right:8px;cursor:pointer" data-drill="${esc(a.type)}">${esc(a.label)} ${a.count}</span>`).join("")}</p></div>`
    : "";
  const trendColors = ["#0b6e6e", "#0a4d78", "#b26a00", "#8d4bab"];
  const trendNames = { encounters: "就诊", exam_reports: "远程诊断", referrals: "转诊", prescriptions: "处方" };
  const legend = Object.keys(trends.series).map((k, i) =>
    `<span style="font-size:12.5px;margin-right:14px"><span style="display:inline-block;width:10px;height:10px;background:${trendColors[i]};border-radius:2px;margin-right:4px"></span>${trendNames[k] || k}</span>`).join("");
  $("#page-body").innerHTML =
    `${alertBanner}
     <div class="cards">${cards.map(([label, value, warn, metric]) =>
      `<div class="card"${metric ? ` data-drill="${esc(metric)}" style="cursor:pointer" title="点击查看明细"` : ""}>
        <div class="label">${esc(label)}${metric ? " ▸" : ""}</div><div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}</div>
     <div id="drill-panel" class="hidden"></div>
     <div class="panel"><h3>近6月业务量趋势</h3><div style="margin-bottom:6px">${legend}</div>${lineChart(trends.months, trends.series, trendColors)}</div>
     ${chronicItems.length ? `<div class="panel"><h3>慢病分级分组</h3>${barChart(chronicItems, { color: "#b26a00", unit: " 人" })}</div>` : ""}
     ${perfHtml}`;
  $("#page-body").onclick = async (e) => {
    const hit = e.target.closest("[data-drill],[data-drillgo],[data-drillpage],[data-drillclose]");
    if (!hit) return;
    const panel = $("#drill-panel");
    try {
      if (hit.dataset.drillclose) return panel.classList.add("hidden");
      if (hit.dataset.drillgo) return nav(hit.dataset.drillgo);
      if (hit.dataset.drillpage) return await openDrilldown(panel.dataset.metric, Number(hit.dataset.drillpage));
      await openDrilldown(hit.dataset.drill, 0);
    } catch (err) { panel.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
}

async function renderConsultations() {
  $("#page-desc").textContent = "申请 → 受理 → 出具意见 → 评价";
  const consultations = await api("/api/consultations");
  const CS = { applied: ["已申请", "orange"], accepted: ["已受理", ""], completed: ["已完成", "green"], declined: ["已拒绝", "red"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>会诊申请</h3>
      <form class="inline" id="cons-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="申请机构ID" required>
        <input name="to_org_id" type="number" placeholder="受邀机构ID" required>
        <input name="question" placeholder="会诊问题" required style="min-width:240px">
        <button>提交</button>
      </form><p class="msg" id="cons-msg"></p></div>
    <div class="panel">${table(["ID", "患者", "申请→受邀", "问题", "专家", "意见", "评价", "状态", "操作"], consultations, (c) => {
      const [text, color] = CS[c.status] || [c.status, ""];
      const actions = c.status === "applied"
        ? `<button class="btn secondary" data-act="accept" data-id="${c.id}">受理</button>
           <button class="btn danger" data-act="decline" data-id="${c.id}">拒绝</button>`
        : c.status === "accepted"
        ? `<button class="btn secondary" data-act="complete" data-id="${c.id}">出意见</button>`
        : c.status === "completed" && !c.rating
        ? `<button class="btn secondary" data-act="rate" data-id="${c.id}">评价</button>` : "—";
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.from_org_id} → ${c.to_org_id}</td>
        <td>${esc(c.question)}</td><td>${esc(c.expert_name) || "—"}</td><td>${esc(c.opinion) || "—"}</td>
        <td>${c.rating ? "★".repeat(c.rating) : "—"}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
    })}</div>`;
  $("#cons-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/consultations", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), from_org_id: Number(f.get("from_org_id")),
        to_org_id: Number(f.get("to_org_id")), question: f.get("question") }) });
      route();
    } catch (err) { setMsg("#cons-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { act, id } = e.target.dataset;
    if (!act || !id) return;
    try {
      if (act === "accept") {
        const expert = prompt("受理专家姓名"); if (!expert) return;
        await api(`/api/consultations/${id}/accept`, { method: "POST", body: JSON.stringify({ expert_name: expert }) });
      } else if (act === "decline") {
        await api(`/api/consultations/${id}/decline`, { method: "POST" });
      } else if (act === "complete") {
        const opinion = prompt("会诊意见"); if (!opinion) return;
        await api(`/api/consultations/${id}/complete`, { method: "POST", body: JSON.stringify({ opinion }) });
      } else if (act === "rate") {
        const rating = Number(prompt("评价（1-5星）")); if (!rating) return;
        await api(`/api/consultations/${id}/rate`, { method: "POST", body: JSON.stringify({ rating }) });
      }
      route();
    } catch (err) { setMsg("#cons-msg", err.message, false); }
  };
}

async function renderContracts() {
  $("#page-desc").textContent = "线上签约、服务包管理、履约记录";
  const contracts = await api("/api/contracts");
  const PKG = { basic: "基础包", standard: "标准包", premium: "个性包" };
  const SVC = { visit: "上门服务", consult: "健康咨询", followup: "随访", referral: "转诊协助" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>签约</h3>
      <form class="inline" id="ct-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="doctor_name" placeholder="家庭医生" required>
        <select name="package">${Object.entries(PKG).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="signed_date" placeholder="签约日期 YYYY-MM-DD">
        <button>签约</button>
      </form><p class="msg" id="ct-msg"></p></div>
    <div class="panel">${table(["ID", "患者", "机构", "医生", "服务包", "状态", "操作"], contracts, (c) =>
      `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.org_id}</td><td>${esc(c.doctor_name)}</td>
       <td><span class="tag">${PKG[c.package]}</span></td>
       <td><span class="tag ${c.status === "active" ? "green" : "red"}">${c.status === "active" ? "履约中" : "已解约"}</span></td>
       <td>${c.status === "active"
         ? `<button class="btn secondary" data-svc="${c.id}">记录履约</button>
            <button class="btn danger" data-term="${c.id}">解约</button>` : "—"}</td></tr>`)}</div>`;
  $("#ct-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/contracts", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), org_id: Number(f.get("org_id")),
        doctor_name: f.get("doctor_name"), package: f.get("package"), signed_date: f.get("signed_date") }) });
      route();
    } catch (err) { setMsg("#ct-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { svc, term } = e.target.dataset;
    try {
      if (svc) {
        const type = prompt("履约类型：visit/consult/followup/referral", "followup"); if (!type) return;
        const note = prompt("备注") || "";
        await api(`/api/contracts/${svc}/services`, { method: "POST", body: JSON.stringify({ service_type: type, note }) });
        alert("履约已记录");
      }
      if (term) { await api(`/api/contracts/${term}/terminate`, { method: "POST" }); route(); }
    } catch (err) { setMsg("#ct-msg", err.message, false); }
  };
  await drawHomeVisits();  // 块4⑨ 上门服务调度
}

async function renderAppointments() {
  $("#page-desc").textContent = "机构发布分时段号源，一站式预约挂号/检查/检验";
  const [slots, appointments] = await Promise.all([api("/api/appointments/slots"), api("/api/appointments")]);
  const RT = { outpatient: "门诊", exam: "检查", lab: "检验" };
  const AS = { booked: ["已预约", "green"], cancelled: ["已取消", "red"], fulfilled: ["已就诊", ""] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>发布号源</h3>
      <form class="inline" id="slot-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type">${Object.entries(RT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="resource_name" placeholder="资源名称（如CT室上午）" required>
        <input name="slot_date" placeholder="日期 YYYY-MM-DD" required>
        <input name="slot_time" placeholder="时段（如09:00-10:00）">
        <input name="capacity" type="number" value="5" min="1" style="min-width:70px">
        <button>发布</button>
      </form>
      <h3 style="margin-top:14px">预约</h3>
      <form class="inline" id="book-form">
        <input name="slot_id" type="number" placeholder="号源ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <button>预约</button>
      </form><p class="msg" id="apt-msg"></p></div>
    <div class="panel"><h3>号源</h3>${table(["ID", "机构", "类型", "资源", "日期/时段", "已约/容量"], slots, (s) =>
      `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${RT[s.resource_type]}</td><td>${esc(s.resource_name)}</td>
       <td>${esc(s.slot_date)} ${esc(s.slot_time)}</td>
       <td><span class="tag ${s.booked >= s.capacity ? "red" : "green"}">${s.booked}/${s.capacity}</span></td></tr>`)}</div>
    <div class="panel"><h3>预约记录</h3>${table(["ID", "号源", "患者", "状态", "操作"], appointments, (a) => {
      const [text, color] = AS[a.status] || [a.status, ""];
      return `<tr><td>${a.id}</td><td>${a.slot_id}</td><td>${a.patient_id}</td>
        <td><span class="tag ${color}">${text}</span></td>
        <td>${a.status === "booked"
          ? `<button class="btn secondary" data-fulfill="${a.id}">核销</button>
             <button class="btn danger" data-cancel="${a.id}">取消</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#slot-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/appointments/slots", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), resource_type: f.get("resource_type"),
        resource_name: f.get("resource_name"), slot_date: f.get("slot_date"),
        slot_time: f.get("slot_time"), capacity: Number(f.get("capacity")) }) });
      route();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  $("#book-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/appointments", { method: "POST", body: JSON.stringify({
        slot_id: Number(f.get("slot_id")), patient_id: Number(f.get("patient_id")) }) });
      route();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { cancel, fulfill } = e.target.dataset;
    try {
      if (cancel) { await api(`/api/appointments/${cancel}/cancel`, { method: "POST" }); route(); }
      if (fulfill) { await api(`/api/appointments/${fulfill}/fulfill`, { method: "POST" }); route(); }
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
}

async function downloadCsv(path, filename, msgSel) {
  try {
    const resp = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
    if (!resp.ok) throw new Error(`导出失败(${resp.status})`);
    const url = URL.createObjectURL(await resp.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    a.click();
    URL.revokeObjectURL(url);
  } catch (err) { setMsg(msgSel, err.message, false); }
}

async function renderPerformance() {
  $("#page-desc").textContent = "按机构自动汇算：转诊结案、远程诊断、慢病随访、处方合格、家医履约；监测指标上报导出";
  const [data, monitoring] = await Promise.all([
    api("/api/performance/orgs"), api("/api/reports/monitoring").catch(() => null)]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>上报报表导出（管理层）</h3>
      <p style="margin-bottom:8px">
        <button class="btn secondary" id="exp-monitor">监测指标CSV（14项）</button>
        <button class="btn secondary" id="exp-ops">运营月报CSV（累计）</button>
        <button class="btn" id="exp-ops-period">按月导出运营月报</button></p>
      <p class="msg" id="rpt-msg"></p>
      ${monitoring ? table(["#", "指标名", "口径", "当期值", "数据来源"], monitoring.indicators, (i) =>
        `<tr><td>${i.no}</td><td>${esc(i.name)}</td><td style="font-size:12.5px;color:#5b6773">${esc(i.caliber)}</td>
         <td><b>${esc(i.value)}</b> ${esc(i.unit)}</td><td><span class="tag">${esc(i.source)}</span></td></tr>`) : ""}</div>
    <div class="panel"><h3>机构评分排名</h3>
      ${data.scorecards.length ? barChart(data.scorecards.map((c) => [c.org_name, c.score]), { unit: " 分" }) : "暂无数据"}</div>
    <div class="panel">${table(["排名", "机构", "层级", "总分", "转诊结案", "远程诊断", "慢病随访", "处方合格", "家医履约"],
      data.scorecards, (c, i) => {
        const d = c.detail;
        return `<tr><td>${data.scorecards.indexOf(c) + 1}</td><td>${esc(c.org_name)}</td><td>${LEVELS[c.level] || c.level}</td>
          <td><b>${c.score}</b></td>
          <td>${d.referral_completion.completed}/${d.referral_completion.total}</td>
          <td>${d.remote_exams}</td>
          <td>${d.chronic_followup.followed}/${d.chronic_followup.total}</td>
          <td>${d.rx_pass.passed}/${d.rx_pass.total}</td>
          <td>${d.contract_services}</td></tr>`;
      })}</div>`;
  $("#exp-monitor").onclick = () => downloadCsv("/api/reports/monitoring/export", "monitoring_indicators.csv", "#rpt-msg");
  $("#exp-ops").onclick = () => downloadCsv("/api/reports/operations/export", "operations_report_all.csv", "#rpt-msg");
  $("#exp-ops-period").onclick = () => {
    const period = prompt("导出月份 YYYY-MM（如 2026-07）");
    if (!period) return;
    downloadCsv(`/api/reports/operations/export?period=${encodeURIComponent(period)}`, `operations_report_${period}.csv`, "#rpt-msg");
  };
  await drawImprovementTasks();  // 块4㉟ 绩效自评改进
}

async function renderCssd() {
  $("#page-desc").textContent = "器械批次：灭菌中 → 已灭菌 → 已发放 → 已回收，全程追溯";
  const batches = await api("/api/cssd/batches");
  const BS = { sterilizing: ["灭菌中", "orange"], sterile: ["已灭菌", ""], dispatched: ["已发放", "green"], recycled: ["已回收", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>新建批次</h3>
      <form class="inline" id="batch-form">
        <input name="batch_no" placeholder="批次号" required>
        <input name="center_org_id" type="number" placeholder="消毒中心机构ID" required>
        <input name="item_name" placeholder="器械名称" required>
        <input name="quantity" type="number" placeholder="数量" required min="1">
        <button>创建</button>
      </form><p class="msg" id="cssd-msg"></p></div>
    <div class="panel">${table(["ID", "批次号", "器械", "数量", "接收机构", "状态", "操作"], batches, (b) => {
      const [text, color] = BS[b.status] || [b.status, ""];
      const next = { sterilizing: "标记已灭菌", sterile: "发放", dispatched: "回收" }[b.status];
      return `<tr><td>${b.id}</td><td><span class="tag">${esc(b.batch_no)}</span></td><td>${esc(b.item_name)}</td>
        <td>${b.quantity}</td><td>${b.dispatched_to_org_id ?? "—"}</td>
        <td><span class="tag ${color}">${text}</span></td>
        <td>${next ? `<button class="btn secondary" data-adv="${b.id}" data-next="${b.status}">${next}</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/cssd/batches", { method: "POST", body: JSON.stringify({
        batch_no: f.get("batch_no"), center_org_id: Number(f.get("center_org_id")),
        item_name: f.get("item_name"), quantity: Number(f.get("quantity")) }) });
      route();
    } catch (err) { setMsg("#cssd-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { adv, next } = e.target.dataset;
    if (!adv) return;
    try {
      let qs = "";
      if (next === "sterile") {
        const org = prompt("接收机构ID"); if (!org) return;
        qs = `?dispatched_to_org_id=${Number(org)}`;
      }
      await api(`/api/cssd/batches/${adv}/advance${qs}`, { method: "POST" });
      route();
    } catch (err) { setMsg("#cssd-msg", err.message, false); }
  };
  await drawCssdCosts();  // 块4⑥ 消毒供应成本核算
}

async function renderMedwaste() {
  $("#page-desc").textContent = "收集→暂存→交接全过程监管，超2天未交接自动预警";
  const [wastes, alerts] = await Promise.all([api("/api/medwaste"), api("/api/medwaste/alerts")]);
  const alertIds = new Set(alerts.map((w) => w.id));
  const WT = { infectious: "感染性", sharp: "损伤性", pathological: "病理性", pharmaceutical: "药物性", chemical: "化学性" };
  const WS = { collected: ["已收集", "orange"], stored: ["已暂存", "orange"], handed_over: ["已交接", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>收集登记</h3>
      <form class="inline" id="waste-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="waste_type">${Object.entries(WT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="weight_kg" type="number" step="any" placeholder="重量(kg)" required>
        <input name="collected_date" placeholder="收集日期 YYYY-MM-DD" required>
        <button>登记</button>
      </form><p class="msg" id="waste-msg"></p></div>
    ${alerts.length ? `<div class="panel"><h3>⚠ 滞留预警（${alerts.length}）</h3><p class="desc">收集超过2天仍未交接</p></div>` : ""}
    <div class="panel">${table(["ID", "机构", "类别", "重量", "收集日期", "转运人", "状态", "操作"], wastes, (w) => {
      const [text, color] = WS[w.status] || [w.status, ""];
      return `<tr><td>${w.id}</td><td>${w.org_id}</td><td>${WT[w.waste_type]}</td><td>${w.weight_kg}kg</td>
        <td>${esc(w.collected_date)}${alertIds.has(w.id) ? ' <span class="tag red">滞留</span>' : ""}</td>
        <td>${esc(w.handler_name) || "—"}</td><td><span class="tag ${color}">${text}</span></td>
        <td>${w.status !== "handed_over" ? `<button class="btn secondary" data-hand="${w.id}">交接</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#waste-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/medwaste", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), waste_type: f.get("waste_type"),
        weight_kg: Number(f.get("weight_kg")), collected_date: f.get("collected_date") }) });
      route();
    } catch (err) { setMsg("#waste-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.hand;
    if (!id) return;
    const handler = prompt("转运人员姓名"); if (!handler) return;
    try {
      await api(`/api/medwaste/${id}/handover`, { method: "POST", body: JSON.stringify({ handler_name: handler }) });
      route();
    } catch (err) { setMsg("#waste-msg", err.message, false); }
  };
}

async function renderOrgs() {
  $("#page-desc").textContent = "县—乡—村三级医共体成员单位";
  const orgs = await api("/api/organizations");
  const options = orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>新增机构</h3>
      <form class="inline" id="org-form">
        <input name="name" placeholder="机构名称" required>
        <select name="org_type">${Object.entries(ORG_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="level">${Object.entries(LEVELS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="parent_id"><option value="">无上级机构</option>${options}</select>
        <button>新增</button>
      </form><p class="msg" id="org-msg"></p></div>
    <div class="panel">${table(["ID", "名称", "类型", "层级", "上级机构ID"], orgs, (o) =>
      `<tr><td>${o.id}</td><td>${esc(o.name)}</td><td>${ORG_TYPES[o.org_type] || esc(o.org_type)}</td>
       <td><span class="tag">${LEVELS[o.level] || esc(o.level)}</span></td><td>${o.parent_id ?? "—"}</td></tr>`)}</div>`;
  $("#org-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/organizations", { method: "POST", body: JSON.stringify({
        name: f.get("name"), org_type: f.get("org_type"), level: f.get("level"),
        parent_id: f.get("parent_id") ? Number(f.get("parent_id")) : null }) });
      route();
    } catch (err) { setMsg("#org-msg", err.message, false); }
  };
}

async function renderPatients() {
  $("#page-desc").textContent = "EMPI：身份证号去重，自动签发电子健康卡号";
  const draw = async (keyword = "") => {
    const patients = await api(`/api/patients?keyword=${encodeURIComponent(keyword)}`);
    $("#patient-table").innerHTML = table(["ID", "电子健康卡号", "姓名", "身份证号", "性别", "电话"], patients, (p) =>
      `<tr><td>${p.id}</td><td><span class="tag">${esc(p.ehc_no)}</span></td><td>${esc(p.name)}</td>
       <td>${esc(p.id_card)}</td><td>${esc(p.gender)}</td><td>${esc(p.phone)}</td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>建档（重复身份证号幂等返回既有档案）</h3>
      <form class="inline" id="patient-form">
        <input name="name" placeholder="姓名" required>
        <input name="id_card" placeholder="身份证号" required minlength="15">
        <select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="phone" placeholder="电话">
        <button>建档</button>
      </form><p class="msg" id="patient-msg"></p></div>
    <div class="panel">
      <form class="inline" id="patient-search"><input name="keyword" placeholder="姓名/身份证/健康卡号"><button>搜索</button></form>
      <div id="patient-table"></div></div>
    <div class="panel"><h3>档案调阅授权（医师/经办代录，患者知情）</h3>
      <form class="inline" id="auth-grant-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="grantee_org_id" type="number" placeholder="被授权机构ID" required>
        <select name="scope"><option value="all">全部档案</option><option value="encounter">就诊记录</option><option value="exam">检查报告</option></select>
        <input name="expire_date" placeholder="有效期至 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>授权</button></form>
      <form class="inline" id="auth-list-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><button>查授权记录</button></form>
      <form class="inline" id="auth-check-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="调阅机构ID" required>
        <select name="scope"><option value="all">全部档案</option><option value="encounter">就诊记录</option><option value="exam">检查报告</option></select>
        <button>校验调阅权限</button></form>
      <p class="msg" id="auth-msg"></p><div id="auth-table"></div></div>`;
  await draw();
  const SCOPES = { all: "全部档案", encounter: "就诊记录", exam: "检查报告" };
  const drawAuths = async (pid) => {
    const auths = await api(`/api/patients/${pid}/authorizations`);
    $("#auth-table").innerHTML = table(["ID", "被授权机构", "范围", "有效期至", "状态", "操作"], auths, (a) =>
      `<tr><td>${a.id}</td><td>${a.grantee_org_id}</td><td>${SCOPES[a.scope] || esc(a.scope)}</td><td>${esc(a.expire_date)}</td>
       <td><span class="tag ${a.status === "active" ? "green" : "red"}">${a.status === "active" ? "有效" : "已撤销"}</span></td>
       <td>${a.status === "active" ? `<button class="btn danger" data-revoke="${a.id}" data-pid="${pid}">撤销</button>` : "—"}</td></tr>`);
  };
  $("#auth-grant-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "grantee_org_id"]);
    const pid = body.patient_id;
    delete body.patient_id;
    try {
      await api(`/api/patients/${pid}/authorizations`, { method: "POST", body: JSON.stringify(body) });
      setMsg("#auth-msg", "授权已登记");
      await drawAuths(pid);
    } catch (err) { setMsg("#auth-msg", err.message, false); }
  };
  $("#auth-list-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawAuths(new FormData(e.target).get("patient_id")); }
    catch (err) { setMsg("#auth-msg", err.message, false); }
  };
  $("#auth-check-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api(`/api/patients/${f.get("patient_id")}/authorizations/check?org_id=${f.get("org_id")}&scope=${f.get("scope")}`);
      setMsg("#auth-msg", r.allowed ? "校验通过：该机构持有有效授权" : "校验不通过：无有效授权", r.allowed);
    } catch (err) { setMsg("#auth-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { revoke, pid } = e.target.dataset;
    if (!revoke) return;
    try {
      await api(`/api/patients/${pid}/authorizations/${revoke}/revoke`, { method: "POST" });
      await drawAuths(pid);
    } catch (err) { setMsg("#auth-msg", err.message, false); }
  };
  $("#patient-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const p = await api("/api/patients", { method: "POST", body: JSON.stringify({
        name: f.get("name"), id_card: f.get("id_card"), gender: f.get("gender"), phone: f.get("phone") }) });
      setMsg("#patient-msg", `建档成功，电子健康卡号：${p.ehc_no}`);
      await draw();
    } catch (err) { setMsg("#patient-msg", err.message, false); }
  };
  $("#patient-search").onsubmit = async (e) => { e.preventDefault(); await draw(new FormData(e.target).get("keyword")); };
}

async function renderDicts() {
  $("#page-desc").textContent = "诊断/药品/耗材/收费“四统一”编码字典";
  const systems = { diagnosis: "诊断(ICD-10)", drug: "药品", consumable: "耗材", charge: "收费" };
  const draw = async (system) => {
    const entries = await api(`/api/dictionaries/${system}/entries`);
    $("#dict-table").innerHTML = table(["编码", "名称"], entries, (d) =>
      `<tr><td><span class="tag">${esc(d.code)}</span></td><td>${esc(d.name)}</td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="panel">
      <form class="inline" id="dict-form">
        <select id="dict-system">${Object.entries(systems).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <button>新增条目</button>
      </form><p class="msg" id="dict-msg"></p>
      <div id="dict-table"></div></div>`;
  await draw("diagnosis");
  $("#dict-system").onchange = (e) => draw(e.target.value);
  $("#dict-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const system = $("#dict-system").value;
    try {
      await api(`/api/dictionaries/${system}/entries`, { method: "POST",
        body: JSON.stringify({ code: f.get("code"), name: f.get("name") }) });
      setMsg("#dict-msg", "已新增");
      await draw(system);
    } catch (err) { setMsg("#dict-msg", err.message, false); }
  };
}

async function renderExams() {
  $("#page-desc").textContent = "影像/心电/检验/病理：基层检查、上级诊断、结果互认、危急值管理";
  const [requests, critical] = await Promise.all([api("/api/exams"), api("/api/exams/critical")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>开单（先查互认）</h3>
      <form class="inline" id="exam-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="申请机构ID" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <input name="clinical_info" placeholder="临床信息">
        <button>提交申请</button>
      </form><p class="msg" id="exam-msg"></p></div>
    ${critical.length ? `<div class="panel"><h3>⚠ 危急值（${critical.length}）</h3>${
      table(["报告ID", "申请单", "结论", "操作"], critical, (r) =>
        `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
         <td><button class="btn secondary" data-printreport="${r.id}">打印报告</button></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>申请单</h3>${table(["ID", "患者", "中心", "项目", "状态", "操作"], requests, (r) => {
      const [text, color] = EXAM_STATUS[r.status] || [r.status, ""];
      let actions = r.status === "pending"
        ? `<button class="btn secondary" data-claim="${r.id}">领取</button>`
        : r.status === "diagnosing"
        ? `<button class="btn secondary" data-report="${r.id}">出报告</button>` : "";
      actions += ` <button class="btn secondary" data-printreq="${r.id}">打印申请单</button>`;
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${CENTER_NAMES[r.center_type]}</td>
        <td>${esc(r.item_name)}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
    })}</div>
    <div class="panel"><h3>报告打印</h3>
      <form class="inline" id="exam-print-form">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <button>打印报告单</button></form>
      <p class="msg" id="exam-print-msg"></p></div>
    <div class="panel"><h3>报告附件（影像截图/PDF，≤10MB，医师/经办上传）</h3>
      <form class="inline" id="exam-att-form">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传</button></form>
      <form class="inline" id="exam-att-query">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <button>查附件</button></form>
      <p class="msg" id="exam-att-msg"></p><div id="exam-att-list"></div></div>`;
  $("#exam-att-form").onsubmit = async (e) => {
    e.preventDefault();
    const reportId = new FormData(e.target).get("report_id");
    try {
      await uploadAttachment("exam_report", reportId, e.target.querySelector("input[type=file]"));
      setMsg("#exam-att-msg", "附件已上传");
      await drawAttachments("exam_report", reportId, "#exam-att-list", "#exam-att-msg");
    } catch (err) { setMsg("#exam-att-msg", err.message, false); }
  };
  $("#exam-att-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawAttachments("exam_report", new FormData(e.target).get("report_id"), "#exam-att-list", "#exam-att-msg"); }
    catch (err) { setMsg("#exam-att-msg", err.message, false); }
  };
  $("#exam-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const patientId = Number(f.get("patient_id")), itemCode = f.get("item_code");
    try {
      const check = await api(`/api/exams/recognition-check?patient_id=${patientId}&item_code=${encodeURIComponent(itemCode)}`);
      let extra = {};
      if (check.recognizable &&
          confirm(`30天内已有同项目报告（结论：${check.conclusion}）。互认该结果、不再重复检查？`)) {
        extra = { accept_recognition_of: check.request_id };
      } else if (check.recognizable) {
        extra = { recognition_declined_reason: prompt("请填写不互认理由（监管留痕）") || "未填写" };
      }
      await api("/api/exams", { method: "POST", body: JSON.stringify({
        patient_id: patientId, from_org_id: Number(f.get("from_org_id")),
        center_type: f.get("center_type"), item_code: itemCode,
        item_name: f.get("item_name"), clinical_info: f.get("clinical_info"), ...extra }) });
      route();
    } catch (err) { setMsg("#exam-msg", err.message, false); }
  };
  $("#exam-print-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await openPrintPage(`/api/print/exam-reports/${new FormData(e.target).get("report_id")}`); }
    catch (err) { setMsg("#exam-print-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const claim = e.target.dataset.claim, report = e.target.dataset.report;
    const { printreq, printreport } = e.target.dataset;
    try {
      if (printreq) return await openPrintPage(`/api/print/exam-requests/${printreq}`);
      if (printreport) return await openPrintPage(`/api/print/exam-reports/${printreport}`);
      if (claim) { await api(`/api/exams/${claim}/claim`, { method: "POST" }); route(); }
      if (report) {
        const conclusion = prompt("诊断结论");
        if (!conclusion) return;
        const isCritical = confirm("是否为危急值？（确定=是）");
        await api(`/api/exams/${report}/report`, { method: "POST",
          body: JSON.stringify({ conclusion, critical: isCritical }) });
        route();
      }
    } catch (err) { setMsg("#exam-msg", err.message, false); }
  };
}

async function renderReferrals() {
  $("#page-desc").textContent = "医共体内上转/下转：申请 → 接诊 → 结案";
  const referrals = await api("/api/referrals");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>转诊申请</h3>
      <form class="inline" id="ref-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="转出机构ID" required>
        <input name="to_org_id" type="number" placeholder="转入机构ID" required>
        <select name="direction"><option value="up">上转</option><option value="down">下转</option></select>
        <input name="reason" placeholder="转诊原因">
        <button>提交</button>
      </form><p class="msg" id="ref-msg"></p></div>
    <div class="panel">${table(["ID", "患者", "方向", "转出→转入", "原因", "状态", "操作"], referrals, (r) => {
      const [text, color] = REF_STATUS[r.status] || [r.status, ""];
      const actions = r.status === "pending"
        ? `<button class="btn secondary" data-status="accepted" data-id="${r.id}">接诊</button>
           <button class="btn danger" data-status="rejected" data-id="${r.id}">退回</button>`
        : r.status === "accepted"
        ? `<button class="btn secondary" data-status="completed" data-id="${r.id}">结案</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${r.direction === "up" ? "上转" : "下转"}</td>
        <td>${r.from_org_id} → ${r.to_org_id}</td><td>${esc(r.reason)}</td>
        <td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
    })}</div>`;
  $("#ref-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/referrals", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), from_org_id: Number(f.get("from_org_id")),
        to_org_id: Number(f.get("to_org_id")), direction: f.get("direction"), reason: f.get("reason") }) });
      route();
    } catch (err) { setMsg("#ref-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { status, id } = e.target.dataset;
    if (!status || !id) return;
    try { await api(`/api/referrals/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) }); route(); }
    catch (err) { setMsg("#ref-msg", err.message, false); }
  };
}

async function renderRx() {
  $("#page-desc").textContent = "“系统+药师”双重审方，每方必审；事后处方点评（药师）与合理率监管";
  const [prescriptions, rules, cstats, creviews] = await Promise.all([
    api("/api/prescriptions"), api("/api/prescriptions/rules"),
    api("/api/prescriptions/comment-stats"), api("/api/prescriptions/comment-reviews")]);
  const canComment = ["pharmacist", "admin"].includes(currentRole());
  const commented = new Set(creviews.map((c) => c.prescription_id));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>开方（单药演示）</h3>
      <form class="inline" id="rx-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="diagnosis_name" placeholder="诊断">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="daily_dose" type="number" step="any" placeholder="日剂量" required>
        <input name="days" type="number" value="7" min="1" style="min-width:70px">
        <button>提交处方</button>
      </form><p class="msg" id="rx-msg"></p></div>
    <div class="panel"><h3>用药规则库</h3>
      <form class="inline" id="rule-form">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="max_daily_dose" type="number" step="any" placeholder="日剂量上限" required>
        <input name="dose_unit" placeholder="单位" value="mg" style="min-width:70px">
        <button>新增规则</button>
      </form>
      ${table(["药品编码", "日剂量上限", "相互作用", "禁忌诊断", "特殊人群", "肝肾功能提示"], rules, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${r.max_daily_dose}${esc(r.dose_unit)}</td>
         <td>${esc(r.interactions) || "—"}</td><td>${esc(r.contraindicated_diagnoses) || "—"}</td>
         <td>${esc(r.special_groups) || "—"}</td><td>${esc(r.renal_hepatic_note) || "—"}</td></tr>`)}</div>
    <div class="panel"><h3>处方队列</h3>${table(["ID", "患者", "诊断", "状态", "审方意见", "操作"], prescriptions, (p) => {
      const [text, color] = RX_STATUS[p.status] || [p.status, ""];
      let actions = p.status === "pending_review"
        ? `<button class="btn secondary" data-approve="1" data-id="${p.id}">通过</button>
           <button class="btn danger" data-approve="0" data-id="${p.id}">退回</button>` : "";
      if (canComment && !commented.has(p.id)) actions += ` <button class="btn" data-rxcomment="${p.id}">点评</button>`;
      actions += ` <button class="btn secondary" data-printrx="${p.id}">打印</button>`;
      return `<tr><td>${p.id}</td><td>${p.patient_id}</td><td>${esc(p.diagnosis_name)}</td>
        <td><span class="tag ${color}">${text}</span></td><td>${esc(p.review_comment) || "—"}</td><td>${actions || "—"}</td></tr>`;
    })}</div>
    <div class="panel"><h3>处方点评（事后监管）</h3>
      <div class="cards">
        <div class="card"><div class="label">已点评处方</div><div class="value">${cstats.commented}</div></div>
        <div class="card"><div class="label">不合理处方</div><div class="value${cstats.unreasonable ? " warn" : ""}">${cstats.unreasonable}</div></div>
        <div class="card"><div class="label">点评合理率</div><div class="value">${cstats.reasonable_rate_pct}%</div></div></div>
      ${table(["处方ID", "结论", "问题类型", "点评意见", "时间"], creviews, (c) =>
        `<tr><td>${c.prescription_id}</td>
         <td><span class="tag ${c.grade === "reasonable" ? "green" : "red"}">${c.grade === "reasonable" ? "合理" : "不合理"}</span></td>
         <td>${esc(c.issues) || "—"}</td><td>${esc(c.comment) || "—"}</td><td>${esc(c.at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>`;
  $("#rx-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const p = await api("/api/prescriptions", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), org_id: Number(f.get("org_id")),
        diagnosis_name: f.get("diagnosis_name"),
        items: [{ drug_code: f.get("drug_code"), drug_name: f.get("drug_name"),
          daily_dose: Number(f.get("daily_dose")), days: Number(f.get("days")) }] }) });
      const base = p.status === "auto_passed" ? "系统审通过" : `转入药师审核：${p.review_comment}`;
      // 块2：肝肾功能提示为非拦截提醒，附在审方结论之后
      const tips = (p.advisories || []).length ? `｜${p.advisories.join("；")}` : "";
      setMsg("#rx-msg", base + tips, p.status === "auto_passed");
      route();
    } catch (err) { setMsg("#rx-msg", err.message, false); }
  };
  $("#rule-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/prescriptions/rules", { method: "POST", body: JSON.stringify({
        drug_code: f.get("drug_code"), max_daily_dose: Number(f.get("max_daily_dose")), dose_unit: f.get("dose_unit") }) });
      route();
    } catch (err) { setMsg("#rx-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { approve, id, rxcomment, printrx } = e.target.dataset;
    if (printrx) {
      try { return await openPrintPage(`/api/print/prescriptions/${printrx}`); }
      catch (err) { return setMsg("#rx-msg", err.message, false); }
    }
    if (rxcomment) {
      // 块2：点评规则化——先调阅该方药品的点评要点与肝肾提示，再作结论
      try {
        const rp = await api(`/api/prescriptions/${rxcomment}/review-points`);
        const lines = rp.items.map((i) => `${i.drug_name}（${i.drug_code}）${i.dose_exceeded ? "【日剂量超限】" : ""}\n  要点：${i.review_points || "规则库未维护"}\n  肝肾：${i.renal_hepatic_note || "—"}`);
        alert(`处方 ${rp.prescription_id} 点评要点（规则覆盖 ${rp.rule_coverage_pct}%）：\n\n${lines.join("\n")}`);
      } catch (err) { setMsg("#rx-msg", err.message, false); }
      const reasonable = confirm("点评结论：该处方是否合理？（确定=合理，取消=不合理）");
      const body = { grade: reasonable ? "reasonable" : "unreasonable" };
      if (!reasonable) {
        body.issues = prompt("问题类型（如：用法用量不适宜）") || "";
        body.comment = prompt("点评意见") || "";
      }
      return postAction(`/api/prescriptions/${rxcomment}/comment-review`, body, "#rx-msg");
    }
    if (approve === undefined || !id) return;
    const comment = prompt("药师意见") || "";
    try {
      await api(`/api/prescriptions/${id}/review`, { method: "POST",
        body: JSON.stringify({ approve: approve === "1", comment }) });
      route();
    } catch (err) { setMsg("#rx-msg", err.message, false); }
  };
}

async function renderPharmacy() {
  $("#page-desc").textContent = "库存管理、县乡村余缺调拨、缺药预警";
  const [stocks, alerts] = await Promise.all([api("/api/pharmacy/stocks"), api("/api/pharmacy/alerts")]);
  const alertIds = new Set(alerts.map((a) => a.id));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>入库</h3>
      <form class="inline" id="stock-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="quantity" type="number" placeholder="数量" required min="0">
        <input name="threshold" type="number" placeholder="预警阈值" value="0" min="0">
        <button>入库</button>
      </form>
      <h3 style="margin-top:14px">调拨</h3>
      <form class="inline" id="transfer-form">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="from_org_id" type="number" placeholder="调出机构ID" required>
        <input name="to_org_id" type="number" placeholder="调入机构ID" required>
        <input name="quantity" type="number" placeholder="数量" required min="1">
        <button>调拨</button>
      </form><p class="msg" id="pharm-msg"></p></div>
    <div class="panel"><h3>库存${alerts.length ? `（<span style="color:#c62828">${alerts.length} 项缺药预警</span>）` : ""}</h3>
      ${table(["机构ID", "药品", "数量", "阈值", "状态"], stocks, (s) =>
        `<tr><td>${s.org_id}</td><td>${esc(s.drug_name)}（${esc(s.drug_code)}）</td><td>${s.quantity}</td><td>${s.threshold}</td>
         <td>${alertIds.has(s.id) ? '<span class="tag red">缺药</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}</div>`;
  $("#stock-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/pharmacy/stocks", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), drug_code: f.get("drug_code"), drug_name: f.get("drug_name"),
        quantity: Number(f.get("quantity")), threshold: Number(f.get("threshold")) }) });
      route();
    } catch (err) { setMsg("#pharm-msg", err.message, false); }
  };
  $("#transfer-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/pharmacy/transfers", { method: "POST", body: JSON.stringify({
        drug_code: f.get("drug_code"), from_org_id: Number(f.get("from_org_id")),
        to_org_id: Number(f.get("to_org_id")), quantity: Number(f.get("quantity")) }) });
      route();
    } catch (err) { setMsg("#pharm-msg", err.message, false); }
  };
}

async function renderChronic() {
  $("#page-desc").textContent = "病种目录驱动分级规则与随访周期，3级建议上转；膳食运动指导要点自动嵌入";
  const [chronicList, overdue, types] = await Promise.all([
    api("/api/chronic"), api("/api/chronic/overdue"), api("/api/chronic/disease-types?active=true"),
  ]);
  DISEASES = Object.fromEntries(types.map((t) => [t.code, t.name]));
  const overdueIds = new Set(overdue.map((c) => c.id));
  // 各病种分级指标：随访录入时提示该病种应采集的指标与周期
  const metricHint = types.map((t) => {
    const keys = ((t.level_rules || {}).metrics || []).map((m) => `${m.name}(${m.key})`).join("、");
    return `<tr><td>${esc(t.name)}</td><td>${esc(t.code)}</td><td>${esc(keys) || "—"}</td><td>${t.followup_interval_days} 天</td></tr>`;
  }).join("");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>慢病建档</h3>
      <form class="inline" id="chronic-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="disease">${Object.entries(DISEASES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="managed_by_org_id" type="number" placeholder="管理机构ID" required>
        <button>建档</button>
      </form>
      <h3 style="margin-top:14px">随访录入</h3>
      <form class="inline" id="fu-form">
        <input name="chronic_id" type="number" placeholder="档案ID" required>
        <input name="sbp" type="number" placeholder="收缩压">
        <input name="dbp" type="number" placeholder="舒张压">
        <input name="glucose" type="number" step="any" placeholder="空腹血糖">
        <input name="metrics" placeholder="其他指标 如 cat_score=22">
        <input name="next_due" placeholder="下次随访(留空按周期自动建议)">
        <button>提交随访</button>
      </form><p class="msg" id="chronic-msg"></p>
      <h3 style="margin-top:14px">病种目录</h3>
      <table><thead><tr><th>病种</th><th>编码</th><th>分级指标</th><th>随访周期</th></tr></thead><tbody>${metricHint}</tbody></table></div>
    <div class="panel"><h3>在管名单${overdue.length ? `（<span style="color:#c62828">${overdue.length} 人随访超期</span>）` : ""}</h3>
      ${table(["档案ID", "患者", "病种", "分级", "下次随访", "随访状态"], chronicList, (c) =>
        `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${esc(DISEASES[c.disease] || c.disease)}</td>
         <td><span class="tag ${c.level === 3 ? "red" : c.level === 2 ? "orange" : "green"}">${c.level} 级</span></td>
         <td>${esc(c.next_due) || "—"}</td>
         <td>${overdueIds.has(c.id) ? '<span class="tag red">超期</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}</div>`;
  $("#chronic-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/chronic", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), disease: f.get("disease"),
        managed_by_org_id: Number(f.get("managed_by_org_id")) }) });
      route();
    } catch (err) { setMsg("#chronic-msg", err.message, false); }
  };
  $("#fu-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const num = (k) => (f.get(k) ? Number(f.get(k)) : null);
    // "cat_score=22, mrs_score=3" → {cat_score: 22, mrs_score: 3}
    const metrics = {};
    (f.get("metrics") || "").split(",").forEach((pair) => {
      const [k, v] = pair.split("=").map((s) => (s || "").trim());
      if (k && v !== undefined && v !== "" && !Number.isNaN(Number(v))) metrics[k] = Number(v);
    });
    try {
      const result = await api(`/api/chronic/${f.get("chronic_id")}/followups`, { method: "POST",
        body: JSON.stringify({ sbp: num("sbp"), dbp: num("dbp"), glucose: num("glucose"), metrics, next_due: f.get("next_due") }) });
      alert(`分级：${result.level} 级${result.refer_up_suggested ? "（建议上转！）" : ""}\n下次随访：${result.next_due}${result.next_due_suggested ? "（按病种周期自动建议）" : ""}\n指导要点：${result.guidance_points}`);
      route();
    } catch (err) { setMsg("#chronic-msg", err.message, false); }
  };
}

