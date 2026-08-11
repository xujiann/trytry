/* 县域医共体信息化平台 管理端 SPA */
"use strict";

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
    const data = await api("/api/todos");
    const count = $("#todo-count");
    count.textContent = data.total > 99 ? "99+" : data.total;
    count.classList.toggle("hidden", data.total === 0);
    const panel = $("#todo-panel");
    panel.innerHTML = data.items.length
      ? data.items.map((it) => `<h4>${esc(it.title)}（${it.count}）</h4>${
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

const PAGES = [
  { group: "总览" },
  { id: "dashboard", title: "决策驾驶舱", render: renderDashboard },
  { group: "基础平台" },
  { id: "orgs", title: "机构管理", render: renderOrgs },
  { id: "patients", title: "患者主索引", render: renderPatients },
  { id: "dicts", title: "编码字典", render: renderDicts },
  { group: "业务协同" },
  { id: "exams", title: "共享诊断中心", render: renderExams },
  { id: "critical", title: "危急值操作台", render: renderCritical },
  { id: "recognition", title: "互认目录与统计", render: renderRecognition },
  { id: "consultations", title: "远程会诊", render: renderConsultations },
  { id: "referrals", title: "双向转诊", render: renderReferrals },
  { id: "emergency", title: "智慧急救", render: renderEmergency },
  { id: "emtimeline", title: "急救绿道时间轴", render: renderEmTimeline },
  { id: "inpatient", title: "住院管理", render: renderInpatient },
  { id: "billing", title: "费用结算", render: renderBilling },
  { id: "rx", title: "集中审方", render: renderRx },
  { id: "pharmacy", title: "中心药房", render: renderPharmacy },
  { id: "procure", title: "采购与盘点", render: renderProcure },
  { id: "medication", title: "药事监测", render: renderMedication },
  { id: "insurance", title: "医保协同", render: renderInsurance },
  { id: "blood", title: "血液管理", render: renderBlood },
  { group: "医防融合" },
  { id: "chronic", title: "慢病管理", render: renderChronic },
  { id: "contracts", title: "家医签约", render: renderContracts },
  { id: "infectious", title: "传染病预警", render: renderInfectious },
  { id: "infdir", title: "传染病目录与迟报", render: renderInfectiousDir },
  { id: "publichealth", title: "公卫协同", render: renderPublicHealth },
  { id: "eldercare", title: "老年健康", render: renderEldercare },
  { id: "maternal", title: "妇幼保健", render: renderMaternal },
  { id: "certs", title: "证明与体检", render: renderCerts },
  { id: "vaccination", title: "疫苗接种", render: renderVaccination },
  { group: "便民惠民" },
  { id: "appointments", title: "预约诊疗", render: renderAppointments },
  { id: "telemedicine", title: "互联网+诊疗", render: renderTelemedicine },
  { id: "tcm", title: "中医药服务", render: renderTcm },
  { id: "archive", title: "患者360视图", render: renderArchive },
  { group: "综合管理" },
  { id: "performance", title: "绩效考核", render: renderPerformance, roles: ["director"] },
  { id: "perfind", title: "绩效指标调权", render: renderPerfIndicators, roles: ["director"] },
  { id: "quality", title: "质量安全", render: renderQuality },
  { id: "drgs", title: "DRGs分析", render: renderDrgs },
  { id: "education", title: "远程医学教育", render: renderEducation },
  { id: "knowledge", title: "知识库", render: renderKnowledge },
  { id: "hrfinance", title: "人财物管理", render: renderHrFinance },
  { id: "oaqc", title: "行政与质控", render: renderOaQc },
  { id: "cssd", title: "消毒供应", render: renderCssd },
  { id: "medwaste", title: "医废追溯", render: renderMedwaste },
  { group: "系统管理", roles: ["admin"] },
  { id: "users", title: "用户管理", render: renderUsers, roles: ["admin"] },
  { id: "audit", title: "审计日志", render: renderAudit, roles: ["admin"] },
];

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

async function renderDashboard() {
  $("#page-desc").textContent = "指标口径对齐《紧密型县域医共体监测指标体系（2024版）》";
  const m = await api("/api/metrics/overview");
  const cards = [
    ["成员单位数", m.resources.organizations],
    ["建档患者数", m.resources.patients],
    ["基层诊疗人次占比", m.service_division.grassroots_encounter_ratio_pct + "%"],
    ["远程诊断量", m.remote_diagnosis.reported_total],
    ["结果互认率", m.remote_diagnosis.recognition_ratio_pct + "%"],
    ["危急值", m.remote_diagnosis.critical_values, m.remote_diagnosis.critical_values > 0],
    ["上转/下转", `${m.referrals.up} / ${m.referrals.down}`],
    ["审方总量", m.prescription_review.total],
    ["系统审通过率", m.prescription_review.auto_pass_ratio_pct + "%"],
    ["慢病在管人数", m.chronic_management.total],
    ["缺药预警", m.pharmacy.stock_alerts, m.pharmacy.stock_alerts > 0],
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
       <p style="font-size:13.5px">${alerts.items.map((a) => `<span class="tag red" style="margin-right:8px">${esc(a.label)} ${a.count}</span>`).join("")}</p></div>`
    : "";
  const trendColors = ["#0b6e6e", "#0a4d78", "#b26a00", "#8d4bab"];
  const trendNames = { encounters: "就诊", exam_reports: "远程诊断", referrals: "转诊", prescriptions: "处方" };
  const legend = Object.keys(trends.series).map((k, i) =>
    `<span style="font-size:12.5px;margin-right:14px"><span style="display:inline-block;width:10px;height:10px;background:${trendColors[i]};border-radius:2px;margin-right:4px"></span>${trendNames[k] || k}</span>`).join("");
  $("#page-body").innerHTML =
    `${alertBanner}
     <div class="cards">${cards.map(([label, value, warn]) =>
      `<div class="card"><div class="label">${esc(label)}</div><div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}</div>
     <div class="panel"><h3>近6月业务量趋势</h3><div style="margin-bottom:6px">${legend}</div>${lineChart(trends.months, trends.series, trendColors)}</div>
     ${chronicItems.length ? `<div class="panel"><h3>慢病分级分组</h3>${barChart(chronicItems, { color: "#b26a00", unit: " 人" })}</div>` : ""}
     ${perfHtml}`;
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
      table(["报告ID", "申请单", "结论"], critical, (r) =>
        `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>申请单</h3>${table(["ID", "患者", "中心", "项目", "状态", "操作"], requests, (r) => {
      const [text, color] = EXAM_STATUS[r.status] || [r.status, ""];
      const actions = r.status === "pending"
        ? `<button class="btn secondary" data-claim="${r.id}">领取</button>`
        : r.status === "diagnosing"
        ? `<button class="btn secondary" data-report="${r.id}">出报告</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${CENTER_NAMES[r.center_type]}</td>
        <td>${esc(r.item_name)}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
    })}</div>
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
  $("#page-body").onclick = async (e) => {
    const claim = e.target.dataset.claim, report = e.target.dataset.report;
    try {
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
    const { approve, id, rxcomment } = e.target.dataset;
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

async function renderInfectious() {
  $("#page-desc").textContent = "病例报告 + 滑动窗口多点触发预警（多机构同报升级为高风险）";
  const [cases, alerts] = await Promise.all([api("/api/infectious/cases"), api("/api/infectious/alerts")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>病例报告</h3>
      <form class="inline" id="case-form">
        <input name="org_id" type="number" placeholder="报告机构ID" required>
        <input name="disease_code" placeholder="病种编码" required>
        <input name="disease_name" placeholder="病种名称" required>
        <input name="onset_date" placeholder="发病日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>报告</button>
      </form><p class="msg" id="case-msg"></p></div>
    ${alerts.length ? `<div class="panel"><h3>⚠ 当前预警</h3>${
      table(["病种", "7日病例数", "报告机构数", "风险等级"], alerts, (a) =>
        `<tr><td>${esc(a.disease_name)}</td><td>${a.case_count}</td><td>${a.org_count}</td>
         <td><span class="tag ${a.severity === "high" ? "red" : "orange"}">${a.severity === "high" ? "高" : "中"}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>病例列表</h3>${table(["ID", "机构", "病种", "发病日期"], cases, (c) =>
      `<tr><td>${c.id}</td><td>${c.org_id}</td><td>${esc(c.disease_name)}</td><td>${esc(c.onset_date)}</td></tr>`)}</div>`;
  $("#case-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/infectious/cases", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), disease_code: f.get("disease_code"),
        disease_name: f.get("disease_name"), onset_date: f.get("onset_date") }) });
      route();
    } catch (err) { setMsg("#case-msg", err.message, false); }
  };
}

async function renderArchive() {
  $("#page-desc").textContent = "按电子健康卡号汇聚档案、就诊、报告、慢病、处方";
  $("#page-body").innerHTML = `
    <div class="panel">
      <form class="inline" id="archive-form">
        <input name="ehc_no" placeholder="电子健康卡号" required>
        <button>查询</button>
      </form>
      <div id="archive-result"></div></div>`;
  $("#archive-form").onsubmit = async (e) => {
    e.preventDefault();
    const ehcNo = new FormData(e.target).get("ehc_no");
    try {
      const archive = await api(`/api/archive/${encodeURIComponent(ehcNo)}`);
      $("#archive-result").innerHTML = `<pre class="json">${esc(JSON.stringify(archive, null, 2))}</pre>`;
    } catch (err) { $("#archive-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
}

async function renderUsers() {
  $("#page-desc").textContent = "账号开通、角色分配与变更留痕、系统参数配置（仅管理员）";
  const [usersList, orgs, roleChanges, params] = await Promise.all([
    api("/api/users"), api("/api/organizations"), api("/api/users/role-changes"), api("/api/mgmt/params")]);
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>开通账号</h3>
      <form class="inline" id="user-form">
        <input name="username" placeholder="用户名（≥3位）" required minlength="3">
        <input name="password" type="password" placeholder="初始密码（≥6位）" required minlength="6">
        <input name="full_name" placeholder="姓名">
        <select name="role">${Object.entries(ROLE_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="org_id"><option value="">不挂机构</option>${orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>开通</button>
      </form><p class="msg" id="user-msg"></p></div>
    <div class="panel"><h3>修改本人密码</h3>
      <form class="inline" id="pwd-form">
        <input name="current_password" type="password" placeholder="当前密码" required>
        <input name="new_password" type="password" placeholder="新密码（≥6位）" required minlength="6">
        <button>修改</button>
      </form><p class="msg" id="pwd-msg"></p></div>
    <div class="panel">${table(["ID", "用户名", "姓名", "角色", "所属机构", "操作"], usersList, (u) =>
      `<tr><td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.full_name) || "—"}</td>
       <td><span class="tag">${ROLE_NAMES[u.role] || esc(u.role)}</span></td>
       <td>${u.org_id ? esc(orgNames[u.org_id] || u.org_id) : "—"}</td>
       <td><button class="btn secondary" data-chrole="${u.id}">调角色</button></td></tr>`)}</div>
    <div class="panel"><h3>角色变更记录（留痕，变更即吊销旧令牌）</h3>${
      table(["用户ID", "原角色", "新角色", "操作人", "时间"], roleChanges, (r) =>
        `<tr><td>${r.user_id}</td><td><span class="tag">${ROLE_NAMES[r.old_role] || esc(r.old_role)}</span></td>
         <td><span class="tag green">${ROLE_NAMES[r.new_role] || esc(r.new_role)}</span></td>
         <td>${r.changed_by}</td><td>${esc(r.at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>
    <div class="panel"><h3>系统参数配置（键值集中管理）</h3>
      <form class="inline" id="param-form">
        <input name="key" placeholder="参数键（如 portal.verify_lock_seconds）" required style="min-width:240px">
        <input name="value" placeholder="参数值" required>
        <input name="description" placeholder="说明" style="min-width:180px">
        <button>保存</button></form>
      <p class="msg" id="param-msg"></p>
      ${table(["键", "值", "说明", "更新时间"], params, (p) =>
        `<tr><td><span class="tag">${esc(p.key)}</span></td><td>${esc(p.value)}</td><td>${esc(p.description) || "—"}</td>
         <td>${esc(p.updated_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>`;
  $("#user-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/users", { method: "POST", body: JSON.stringify({
        username: f.get("username"), password: f.get("password"), full_name: f.get("full_name"),
        role: f.get("role"), org_id: f.get("org_id") ? Number(f.get("org_id")) : null }) });
      route();
    } catch (err) { setMsg("#user-msg", err.message, false); }
  };
  $("#pwd-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/auth/change-password", { method: "POST", body: JSON.stringify({
        current_password: f.get("current_password"), new_password: f.get("new_password") }) });
      setMsg("#pwd-msg", "密码已修改");
      e.target.reset();
    } catch (err) { setMsg("#pwd-msg", err.message, false); }
  };
  $("#param-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/params", formJson(e.target), "#param-msg"); };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.chrole;
    if (!id) return;
    const keys = Object.keys(ROLE_NAMES);
    const pick = prompt(`新角色（${keys.map((k, i) => `${i + 1}=${ROLE_NAMES[k]}`).join("，")}）输入序号`);
    const role = keys[Number(pick) - 1]; if (!role) return;
    try {
      await api(`/api/users/${id}/role`, { method: "PATCH", body: JSON.stringify({ role }) });
      route();
    } catch (err) { setMsg("#user-msg", err.message, false); }
  };
}

async function renderAudit() {
  $("#page-desc").textContent = "全部写操作留痕（等保三级安全审计），仅管理员可查";
  const draw = async (username = "") => {
    const logs = await api(`/api/audit?limit=200${username ? `&username=${encodeURIComponent(username)}` : ""}`);
    $("#audit-table").innerHTML = table(["时间", "用户", "操作", "接口", "结果"], logs, (l) =>
      `<tr><td>${esc(l.at.replace("T", " ").slice(0, 19))}</td><td>${esc(l.username)}</td>
       <td><span class="tag">${esc(l.method)}</span></td><td>${esc(l.path)}</td>
       <td><span class="tag ${l.status_code < 400 ? "green" : "red"}">${l.status_code}</span></td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="panel">
      <form class="inline" id="audit-search"><input name="username" placeholder="按用户名过滤"><button>查询</button></form>
      <div id="audit-table"></div></div>`;
  await draw();
  $("#audit-search").onsubmit = async (e) => { e.preventDefault(); await draw(new FormData(e.target).get("username")); };
}

/* ---------- 通用小工具：表单序列化 + 动作分派 ---------- */
function formJson(form, numFields = []) {
  const f = new FormData(form), out = {};
  for (const [k, v] of f.entries()) {
    if (v === "") continue;
    out[k] = numFields.includes(k) ? Number(v) : v;
  }
  return out;
}

async function postAction(path, body, msgSel) {
  try { await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined }); route(); }
  catch (err) { setMsg(msgSel, err.message, false); }
}

/* ---------- 附件通用控件：multipart 上传 / 鉴权下载 / 按 owner 列表 ---------- */

async function uploadAttachment(ownerType, ownerId, fileInput) {
  const file = fileInput.files[0];
  if (!file) throw new Error("请选择文件（图片或PDF，≤10MB）");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("owner_type", ownerType);
  fd.append("owner_id", ownerId);
  const resp = await fetch("/api/attachments", {
    method: "POST", headers: { Authorization: `Bearer ${token}` }, body: fd });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `上传失败(${resp.status})`);
  return data;
}

async function downloadAttachment(id, filename) {
  const resp = await fetch(`/api/attachments/${id}`, { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) throw new Error(`下载失败(${resp.status})`);
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || `attachment-${id}`;
  a.click();
  URL.revokeObjectURL(url);
}

async function drawAttachments(ownerType, ownerId, containerSel, msgSel) {
  const list = await api(`/api/attachments?owner_type=${ownerType}&owner_id=${ownerId}`);
  const el = $(containerSel);
  el.innerHTML = table(["ID", "文件名", "类型", "大小", "上传时间", "操作"], list, (a) =>
    `<tr><td>${a.id}</td><td>${esc(a.filename)}</td><td><span class="tag">${esc(a.content_type)}</span></td>
     <td>${(a.size / 1024).toFixed(1)} KB</td><td>${esc(a.created_at.slice(0, 16).replace("T", " "))}</td>
     <td><button class="btn secondary" data-attdl="${a.id}" data-fn="${esc(a.filename)}">下载</button></td></tr>`);
  el.onclick = async (e) => {
    const { attdl, fn } = e.target.dataset;
    if (!attdl) return;
    try { await downloadAttachment(attdl, fn); }
    catch (err) { setMsg(msgSel, err.message, false); }
  };
}

async function renderEmergency() {
  $("#page-desc").textContent = "呼救调度→转运（生命体征回传）→到院→收治，上车即入院";
  const cases = await api("/api/emergency/cases");
  const ES = { dispatched: ["已调度", "orange"], en_route: ["转运中", "orange"], arrived: ["已到院", ""], admitted: ["已收治", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>呼救登记</h3>
      <form class="inline" id="em-form">
        <input name="location" placeholder="事发地点" required><input name="symptom" placeholder="主诉">
        <input name="ambulance_no" placeholder="车牌"><input name="dest_org_id" type="number" placeholder="目标医院ID">
        <input name="patient_id" type="number" placeholder="患者ID(可空)"><button>调度</button>
      </form><p class="msg" id="em-msg"></p></div>
    <div class="panel">${table(["ID", "地点", "主诉", "车辆", "状态", "操作"], cases, (c) => {
      const [t, col] = ES[c.status] || [c.status, ""];
      return `<tr><td>${c.id}</td><td>${esc(c.location)}</td><td>${esc(c.symptom)}</td><td>${esc(c.ambulance_no)}</td>
        <td><span class="tag ${col}">${t}</span></td>
        <td>${c.status !== "admitted" ? `<button class="btn secondary" data-adv="${c.id}">流转</button>
          <button class="btn secondary" data-vital="${c.id}">回传体征</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#em-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id", "patient_id"]), "#em-msg"); };
  $("#page-body").onclick = async (e) => {
    const { adv, vital } = e.target.dataset;
    if (adv) return postAction(`/api/emergency/cases/${adv}/advance`, null, "#em-msg");
    if (vital) {
      const hr = prompt("心率"); if (hr === null) return;
      return postAction(`/api/emergency/cases/${vital}/vitals`, { heart_rate: Number(hr) || null, note: prompt("备注") || "" }, "#em-msg");
    }
  };
}

async function renderTelemedicine() {
  $("#page-desc").textContent = "在线咨询、复诊续方（续方须关联已过审处方）";
  const consults = await api("/api/telemedicine/consults");
  const TS = { open: ["待回复", "orange"], replied: ["已回复", "green"], closed: ["已结束", ""] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>发起咨询</h3>
      <form class="inline" id="tm-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="consult_type"><option value="consult">在线咨询</option><option value="repeat_rx">复诊续方</option></select>
        <input name="question" placeholder="咨询内容" required style="min-width:220px"><button>提交</button>
      </form><p class="msg" id="tm-msg"></p></div>
    <div class="panel">${table(["ID", "患者", "类型", "内容", "回复", "关联处方", "状态", "操作"], consults, (c) => {
      const [t, col] = TS[c.status] || [c.status, ""];
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.consult_type === "repeat_rx" ? "续方" : "咨询"}</td>
        <td>${esc(c.question)}</td><td>${esc(c.reply) || "—"}</td><td>${c.prescription_id ?? "—"}</td>
        <td><span class="tag ${col}">${t}</span></td>
        <td>${c.status === "open" ? `<button class="btn secondary" data-reply="${c.id}">回复</button>`
          : c.status === "replied" ? `<button class="btn secondary" data-close="${c.id}">结束</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#tm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/telemedicine/consults", formJson(e.target, ["patient_id", "org_id"]), "#tm-msg"); };
  $("#page-body").onclick = (e) => {
    const { reply, close } = e.target.dataset;
    if (reply) {
      const text = prompt("回复内容"); if (!text) return;
      const rxid = prompt("关联处方ID（续方时填写，可空）");
      return postAction(`/api/telemedicine/consults/${reply}/reply`, { reply: text, doctor_name: prompt("医师姓名") || "医师", prescription_id: rxid ? Number(rxid) : null }, "#tm-msg");
    }
    if (close) return postAction(`/api/telemedicine/consults/${close}/close`, null, "#tm-msg");
  };
}

async function renderTcm() {
  $("#page-desc").textContent = "智能辅诊（辨证推荐）、共享中药房追溯、适宜技术库";
  const [orders, techniques] = await Promise.all([api("/api/tcm/dispense-orders"), api("/api/tcm/techniques")]);
  const DS = { ordered: "已下单", dispensed: "已调配", decocted: "已煎煮", delivering: "配送中", delivered: "已送达" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>智能辨证</h3>
      <form class="inline" id="tcm-diag"><input name="symptoms" placeholder="症状（逗号分隔，如：乏力,气短）" required style="min-width:280px"><button>辨证</button></form>
      <div id="tcm-diag-result"></div></div>
    <div class="panel"><h3>共享中药房下单</h3>
      <form class="inline" id="tcm-order">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="from_org_id" type="number" placeholder="机构ID" required>
        <input name="herbs" placeholder="处方饮片" required style="min-width:220px"><input name="doses" type="number" value="7" min="1" style="min-width:60px">
        <select name="decoct"><option value="true">代煎</option><option value="false">免煎</option></select><button>下单</button>
      </form><p class="msg" id="tcm-msg"></p>
      ${table(["ID", "患者", "饮片", "剂数", "状态", "操作"], orders, (o) =>
        `<tr><td>${o.id}</td><td>${o.patient_id}</td><td>${esc(o.herbs)}</td><td>${o.doses}</td>
         <td><span class="tag ${o.status === "delivered" ? "green" : "orange"}">${DS[o.status]}</span></td>
         <td>${o.status !== "delivered" ? `<button class="btn secondary" data-adv="${o.id}">流转</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>适宜技术库</h3>
      ${table(["名称", "分类", "适应症"], techniques, (t) =>
        `<tr><td>${esc(t.name)}</td><td>${esc(t.category)}</td><td>${esc(t.indication)}</td></tr>`)}</div>`;
  $("#tcm-diag").onsubmit = async (e) => {
    e.preventDefault();
    const symptoms = new FormData(e.target).get("symptoms").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const result = await api("/api/tcm/assist-diagnosis", { method: "POST", body: JSON.stringify({ symptoms }) });
    $("#tcm-diag-result").innerHTML = `<pre class="json">${esc(JSON.stringify(result.recommendations, null, 2))}</pre>`;
  };
  $("#tcm-order").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "from_org_id", "doses"]);
    body.decoct = body.decoct === "true";
    postAction("/api/tcm/dispense-orders", body, "#tcm-msg");
  };
  $("#page-body").onclick = (e) => { if (e.target.dataset.adv) postAction(`/api/tcm/dispense-orders/${e.target.dataset.adv}/advance`, null, "#tcm-msg"); };
}

async function renderMedication() {
  $("#page-desc").textContent = "缺药登记流转、供应风险研判、全县用药地图、居民用药画像";
  const [shortages, stats, risk] = await Promise.all([
    api("/api/medication/shortages"), api("/api/medication/usage-stats"), api("/api/medication/supply-risk")]);
  const SS = { registered: ["已登记", "orange"], purchasing: ["采购中", "orange"], delivered: ["已配送", "green"] };
  $("#page-body").innerHTML = `
    ${risk.total ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 药品供应风险评估（${risk.total}）</h3>${
      table(["药品编码", "药品", "库存告警机构数", "未结案缺药登记", "风险等级"], risk.risks, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${esc(r.drug_name) || "—"}</td><td>${r.low_stock_orgs}</td><td>${r.open_shortages}</td>
         <td><span class="tag ${r.risk_level === "high" ? "red" : "orange"}">${r.risk_level === "high" ? "高" : "中"}</span></td></tr>`)}</div>` : ""}`;
  $("#page-body").innerHTML += `
    <div class="panel"><h3>缺药登记</h3>
      <form class="inline" id="short-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>登记</button>
      </form><p class="msg" id="short-msg"></p>
      ${table(["ID", "机构", "药品", "数量", "状态", "操作"], shortages, (s) => {
        const [t, col] = SS[s.status];
        return `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${esc(s.drug_name)}</td><td>${s.quantity}</td>
          <td><span class="tag ${col}">${t}</span></td>
          <td>${s.status !== "delivered" ? `<button class="btn secondary" data-adv="${s.id}">流转</button>` : "—"}</td></tr>`;
      })}</div>
    <div class="panel"><h3>用药画像查询</h3>
      <form class="inline" id="prof-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="prof-result"></div></div>
    <div class="panel"><h3>全县用药地图（品种排名）</h3>
      ${stats.length ? barChart(stats.slice(0, 8).map((s) => [s.drug_name, s.rx_count]), { unit: " 方" }) : "暂无数据"}</div>`;
  $("#short-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/medication/shortages", formJson(e.target, ["org_id", "quantity"]), "#short-msg"); };
  $("#prof-form").onsubmit = async (e) => {
    e.preventDefault();
    const profile = await api(`/api/medication/profile/${new FormData(e.target).get("patient_id")}`);
    $("#prof-result").innerHTML = `${profile.polypharmacy_warning ? '<p class="msg err">⚠ 多重用药风险</p>' : ""}<pre class="json">${esc(JSON.stringify(profile, null, 2))}</pre>`;
  };
  $("#page-body").onclick = (e) => { if (e.target.dataset.adv) postAction(`/api/medication/shortages/${e.target.dataset.adv}/advance`, null, "#short-msg"); };
}

async function renderInsurance() {
  $("#page-desc").textContent = "结算记录、转诊证明、特殊病种申报、双通道药品申报、基金监测";
  const [fund, settlements, apps, dualApps] = await Promise.all([
    api("/api/insurance/fund-stats"), api("/api/insurance/settlements"),
    api("/api/insurance/special-diseases"), api("/api/insurance/dual-channel")]);
  const canReviewDual = ["director", "admin"].includes(currentRole());
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">医保基金支出总额</div><div class="value">${fund.insurance_pay_total}</div></div>
      <div class="card"><div class="label">县域内结算占比</div><div class="value">${fund.local_ratio_pct}%</div></div>
      <div class="card"><div class="label">基层支出占比</div><div class="value">${fund.grassroots_ratio_pct}%</div></div></div>
    <div class="panel"><h3>结算登记</h3>
      <form class="inline" id="ins-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="settle_type"><option value="local">本地</option><option value="remote">异地</option></select>
        <input name="total_amount" type="number" step="any" placeholder="总额" required><input name="insurance_pay" type="number" step="any" placeholder="医保支付" required>
        <input name="self_pay" type="number" step="any" placeholder="自付" required><button>登记</button>
      </form>
      <h3 style="margin-top:12px">转诊证明 / 特病申报</h3>
      <form class="inline" id="cert-form"><input name="referral_id" type="number" placeholder="转诊记录ID" required><button>签发证明</button></form>
      <form class="inline" id="spec-form"><input name="patient_id" type="number" placeholder="患者ID" required><input name="disease_name" placeholder="病种" required><button>特病申报</button></form>
      <p class="msg" id="ins-msg"></p></div>
    <div class="panel"><h3>特病申报队列</h3>${table(["ID", "患者", "病种", "状态", "操作"], apps, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.disease_name)}</td>
       <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${a.status}</span></td>
       <td>${a.status === "applied" ? `<button class="btn secondary" data-ok="${a.id}">批准</button><button class="btn danger" data-no="${a.id}">驳回</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>双通道药品申报（医师/经办申报 → 管理层审核）</h3>
      <form class="inline" id="dual-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="reason" placeholder="申报理由" style="min-width:180px">
        <button>申报</button></form>
      ${table(["ID", "患者", "药品", "理由", "状态", "审核意见", "操作"], dualApps, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.drug_name)}</td><td>${esc(a.reason) || "—"}</td>
         <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${a.status === "approved" ? "已批准" : a.status === "rejected" ? "已驳回" : "待审核"}</span></td>
         <td>${esc(a.review_comment) || "—"}</td>
         <td>${a.status === "pending" && canReviewDual
           ? `<button class="btn secondary" data-dualok="${a.id}">批准</button><button class="btn danger" data-dualno="${a.id}">驳回</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>结算记录</h3>${table(["ID", "患者", "机构", "类型", "总额", "医保付", "自付"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${s.org_id}</td><td>${s.settle_type === "local" ? "本地" : "异地"}</td>
       <td>${s.total_amount}</td><td>${s.insurance_pay}</td><td>${s.self_pay}</td></tr>`)}</div>`;
  $("#ins-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/settlements", formJson(e.target, ["patient_id", "org_id", "total_amount", "insurance_pay", "self_pay"]), "#ins-msg"); };
  $("#cert-form").onsubmit = async (e) => {
    e.preventDefault();
    try { const c = await api(`/api/insurance/referral-certs/${new FormData(e.target).get("referral_id")}`, { method: "POST" }); setMsg("#ins-msg", `证明号：${c.cert_no}`); }
    catch (err) { setMsg("#ins-msg", err.message, false); }
  };
  $("#spec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/special-diseases", formJson(e.target, ["patient_id"]), "#ins-msg"); };
  $("#dual-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/dual-channel", formJson(e.target, ["patient_id"]), "#ins-msg"); };
  $("#page-body").onclick = (e) => {
    const { ok, no, dualok, dualno } = e.target.dataset;
    if (ok) postAction(`/api/insurance/special-diseases/${ok}/review?approve=true`, null, "#ins-msg");
    if (no) postAction(`/api/insurance/special-diseases/${no}/review?approve=false`, null, "#ins-msg");
    if (dualok) postAction(`/api/insurance/dual-channel/${dualok}/review?approve=true&comment=${encodeURIComponent(prompt("审核意见") || "")}`, null, "#ins-msg");
    if (dualno) postAction(`/api/insurance/dual-channel/${dualno}/review?approve=false&comment=${encodeURIComponent(prompt("驳回理由") || "")}`, null, "#ins-msg");
  };
}

async function renderEducation() {
  $("#page-desc").textContent = "课程管理、培训考核（60分合格）、个人学分；直播申请与排期审核";
  const [courses, mine, lives] = await Promise.all([
    api("/api/education/courses"), api("/api/education/my-records"), api("/api/education/live-sessions")]);
  const role = currentRole();
  const LS = { pending: ["待审核", "orange"], approved: ["已排期", ""], rejected: ["已驳回", "red"], finished: ["已结束", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>新建课程（管理员）</h3>
      <form class="inline" id="course-form">
        <input name="title" placeholder="课程名" required style="min-width:220px">
        <select name="course_type"><option value="vod">点播</option><option value="live">直播</option></select>
        <select name="category"><option value="clinical">临床医学</option><option value="tcm">中医适宜技术</option><option value="public_health">公共卫生</option></select>
        <input name="speaker" placeholder="讲者"><button>创建</button>
      </form><p class="msg" id="edu-msg"></p></div>
    <div class="panel"><h3>课程列表</h3>${table(["ID", "课程", "形式", "类别", "讲者", "操作"], courses, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.title)}</td><td>${c.course_type === "live" ? "直播" : "点播"}</td><td>${esc(c.category)}</td><td>${esc(c.speaker)}</td>
       <td><button class="btn secondary" data-exam="${c.id}">提交考核</button></td></tr>`)}</div>
    <div class="panel"><h3>我的学习记录</h3>${table(["课程", "成绩", "结果"], mine, (r) =>
      `<tr><td>${esc(r.title)}</td><td>${r.score}</td><td><span class="tag ${r.passed ? "green" : "red"}">${r.passed ? "合格" : "未合格"}</span></td></tr>`)}</div>
    <div class="panel"><h3>直播管理（申请 → 管理层排期审核 → 结束；音视频通道为对接项）</h3>
      <form class="inline" id="live-form">
        <input name="title" placeholder="直播主题" required style="min-width:220px">
        <input name="speaker" placeholder="主讲人">
        <input name="planned_at" placeholder="计划时间（如 2026-09-01 19:00）">
        <button>申请直播</button></form>
      ${table(["ID", "主题", "主讲", "计划时间", "状态", "审核意见", "操作"], lives, (s) => {
        const [text, color] = LS[s.status] || [s.status, ""];
        const actions = s.status === "pending" && ["director", "admin"].includes(role)
          ? `<button class="btn secondary" data-liveok="${s.id}">排期</button>
             <button class="btn danger" data-liveno="${s.id}">驳回</button>`
          : s.status === "approved" && ["director", "operator", "admin"].includes(role)
          ? `<button class="btn secondary" data-livefin="${s.id}">结束</button>` : "—";
        return `<tr><td>${s.id}</td><td>${esc(s.title)}</td><td>${esc(s.speaker) || "—"}</td><td>${esc(s.planned_at) || "—"}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${esc(s.review_comment) || "—"}</td><td>${actions}</td></tr>`;
      })}</div>`;
  $("#course-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/courses", formJson(e.target), "#edu-msg"); };
  $("#live-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/live-sessions", formJson(e.target), "#edu-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.liveok) return postAction(`/api/education/live-sessions/${d.liveok}/review?approve=true&comment=${encodeURIComponent(prompt("审核意见") || "同意排期")}`, null, "#edu-msg");
    if (d.liveno) return postAction(`/api/education/live-sessions/${d.liveno}/review?approve=false&comment=${encodeURIComponent(prompt("驳回理由") || "")}`, null, "#edu-msg");
    if (d.livefin) return postAction(`/api/education/live-sessions/${d.livefin}/finish`, null, "#edu-msg");
    const id = d.exam;
    if (!id) return;
    const score = prompt("考核得分（0-100）"); if (score === null) return;
    postAction(`/api/education/courses/${id}/exam`, { score: Number(score) }, "#edu-msg");
  };
}

async function renderEldercare() {
  $("#page-desc").textContent = "自理能力评估（Barthel自动分级）、失能老人清单、健康预警（重度失能专案+年度复评到期）";
  const [assessments, disabled, alerts] = await Promise.all([
    api("/api/eldercare/assessments"), api("/api/eldercare/disabled"), api("/api/eldercare/alerts")]);
  $("#page-body").innerHTML = `
    ${alerts.total ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 老年健康预警（${alerts.total}）</h3>${
      table(["患者", "预警类型", "提示", "末次评估"], alerts.alerts, (a) =>
        `<tr><td>${a.patient_id}</td>
         <td><span class="tag ${a.alert_type === "severe_disability" ? "red" : "orange"}">${a.alert_type === "severe_disability" ? "重度失能专案" : "复评到期"}</span></td>
         <td>${esc(a.message)}</td><td>${esc(a.assessed_date) || "—"}</td></tr>`)}</div>` : ""}`;
  $("#page-body").innerHTML += `
    <div class="panel"><h3>新评估</h3>
      <form class="inline" id="eld-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="adl_score" type="number" min="0" max="100" placeholder="ADL(0-100)" required>
        <input name="cognitive_score" type="number" min="0" max="30" placeholder="认知(0-30)"><input name="tcm_constitution" placeholder="体质">
        <input name="assessed_date" placeholder="评估日期 YYYY-MM-DD"><button>评估</button>
      </form><p class="msg" id="eld-msg"></p></div>
    ${disabled.length ? `<div class="panel"><h3>⚠ 失能老人清单（${disabled.length}）</h3>${table(["患者", "分级", "ADL"], disabled, (d) =>
      `<tr><td>${d.patient_id}</td><td><span class="tag red">${esc(d.care_level)}</span></td><td>${d.adl_score}</td></tr>`)}</div>` : ""}
    <div class="panel">${table(["ID", "患者", "ADL", "认知", "分级", "日期"], assessments, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${a.adl_score}</td><td>${a.cognitive_score}</td>
       <td><span class="tag ${a.care_level === "能力完好" ? "green" : "red"}">${esc(a.care_level)}</span></td><td>${esc(a.assessed_date)}</td></tr>`)}</div>`;
  $("#eld-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/eldercare/assessments", formJson(e.target, ["patient_id", "adl_score", "cognitive_score"]), "#eld-msg"); };
}

async function renderMaternal() {
  $("#page-desc").textContent = "孕产妇建册、产检、分娩登记、产后结案；儿童保健、新筛与高危儿；妇女保健四类记录";
  const [records, children, highRisk, womenHealth] = await Promise.all([
    api("/api/maternal/records"), api("/api/maternal/children"),
    api("/api/maternal/children/high-risk"), api("/api/maternal/women-health")]);
  const MS = { registered: "孕期管理", delivered: "已分娩", closed: "已结案" };
  const WH_TYPES = { premarital: "婚前保健", preconception: "孕前保健", gynecology: "妇女病检查", contraception: "避孕节育" };
  const SCREEN_ITEMS = { metabolic: "遗传代谢病", hearing: "听力", chd: "先心病" };
  const hrIds = new Set(highRisk.map((c) => c.id));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>孕产妇建册 / 儿童建档</h3>
      <form class="inline" id="mat-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="lmp" placeholder="末次月经 YYYY-MM-DD">
        <input name="edc" placeholder="预产期 YYYY-MM-DD"><button>建册</button></form>
      <form class="inline" id="child-form">
        <input name="name" placeholder="儿童姓名" required><select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="birth_date" placeholder="出生日期 YYYY-MM-DD" required><input name="guardian_patient_id" type="number" placeholder="监护人患者ID"><button>建档</button></form>
      <p class="msg" id="mat-msg"></p></div>
    <div class="panel"><h3>孕产妇档案</h3>${table(["ID", "患者", "预产期", "孕/产次", "高危", "状态", "操作"], records, (r) =>
      `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${esc(r.edc)}</td><td>G${r.gravidity}P${r.parity}</td>
       <td>${r.high_risk ? `<span class="tag red">高危</span> ${esc(r.risk_factors)}` : '<span class="tag green">正常</span>'}</td>
       <td><span class="tag">${MS[r.status]}</span></td>
       <td>${r.status !== "closed" ? `<button class="btn secondary" data-visit="${r.id}">记录访视</button>
         ${r.status === "registered" ? `<button class="btn secondary" data-delivery="${r.id}">分娩登记</button>` : ""}
         ${r.status === "delivered" ? `<button class="btn secondary" data-close="${r.id}">结案</button>` : ""}` : "—"}</td></tr>`)}</div>
    ${highRisk.length ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 高危儿专案清单（${highRisk.length}）</h3>${
      table(["ID", "姓名", "出生日期", "高危原因"], highRisk, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.birth_date)}</td><td><span class="tag red">${esc(c.risk_note)}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>儿童档案（新筛异常自动纳入高危儿）</h3>${table(["ID", "姓名", "性别", "出生日期", "高危", "操作"], children, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.birth_date)}</td>
       <td>${hrIds.has(c.id) ? '<span class="tag red">高危</span>' : '<span class="tag green">—</span>'}</td>
       <td><button class="btn secondary" data-cvisit="${c.id}">记录访视</button>
           <button class="btn secondary" data-screen="${c.id}">新筛登记</button>
           <button class="btn" data-shist="${c.id}">筛查史</button>
           <button class="btn ${hrIds.has(c.id) ? "" : "danger"}" data-hrtoggle="${c.id}" data-cur="${hrIds.has(c.id)}">${hrIds.has(c.id) ? "解除高危" : "标记高危"}</button></td></tr>`)}</div>
    <div class="panel hidden" id="screen-panel"><h3>新生儿筛查史</h3><div id="screen-list"></div></div>
    <div class="panel"><h3>妇女保健记录（婚前/孕前/妇女病/避孕节育）</h3>
      <form class="inline" id="wh-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="record_type">${Object.entries(WH_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="exam_date" placeholder="检查日期 YYYY-MM-DD">
        <input name="result" placeholder="检查结果">
        <input name="advice" placeholder="指导意见">
        <button>登记</button></form>
      ${table(["ID", "患者", "类型", "日期", "结果", "指导"], womenHealth, (w) =>
        `<tr><td>${w.id}</td><td>${w.patient_id}</td><td><span class="tag">${WH_TYPES[w.record_type] || esc(w.record_type)}</span></td>
         <td>${esc(w.exam_date) || "—"}</td><td>${esc(w.result) || "—"}</td><td>${esc(w.advice) || "—"}</td></tr>`)}</div>`;
  $("#mat-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/records", formJson(e.target, ["patient_id"]), "#mat-msg"); };
  $("#child-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/children", formJson(e.target, ["guardian_patient_id"]), "#mat-msg"); };
  $("#wh-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/women-health", formJson(e.target, ["patient_id"]), "#mat-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.visit) {
      const type = prompt("访视类型：prenatal(产检)/postpartum(产后)", "prenatal"); if (!type) return;
      return postAction(`/api/maternal/records/${d.visit}/visits`, { visit_type: type, bp: prompt("血压(如120/80，可空)") || "", visit_date: prompt("日期 YYYY-MM-DD") || "" }, "#mat-msg");
    }
    if (d.delivery) {
      const orgId = prompt("分娩机构ID"); if (!orgId) return;
      const deliveryDate = prompt("分娩日期 YYYY-MM-DD"); if (!deliveryDate) return;
      const mode = prompt("分娩方式：natural(顺产)/cesarean(剖宫产)", "natural") || "natural";
      return postAction(`/api/maternal/records/${d.delivery}/delivery`,
        { org_id: Number(orgId), delivery_date: deliveryDate, delivery_mode: mode, outcome: prompt("分娩结局") || "" }, "#mat-msg");
    }
    if (d.close) return postAction(`/api/maternal/records/${d.close}/close`, null, "#mat-msg");
    if (d.cvisit) return postAction(`/api/maternal/children/${d.cvisit}/visits`, { visit_type: "checkup", visit_date: prompt("日期 YYYY-MM-DD") || "" }, "#mat-msg");
    if (d.screen) {
      const keys = Object.keys(SCREEN_ITEMS);
      const pick = prompt(`筛查项目（${keys.map((k, i) => `${i + 1}=${SCREEN_ITEMS[k]}`).join("，")}）输入序号`);
      const item = keys[Number(pick) - 1]; if (!item) return;
      const abnormal = confirm("结果是否异常？（确定=异常，将自动纳入高危儿）");
      return postAction(`/api/maternal/children/${d.screen}/screenings`,
        { item, result: abnormal ? "abnormal" : "normal", screen_date: prompt("筛查日期 YYYY-MM-DD") || "" }, "#mat-msg");
    }
    if (d.shist) {
      try {
        const list = await api(`/api/maternal/children/${d.shist}/screenings`);
        $("#screen-panel").classList.remove("hidden");
        $("#screen-list").innerHTML = table(["ID", "项目", "结果", "日期", "备注"], list, (s) =>
          `<tr><td>${s.id}</td><td>${SCREEN_ITEMS[s.item] || esc(s.item)}</td>
           <td><span class="tag ${s.result === "abnormal" ? "red" : "green"}">${s.result === "abnormal" ? "异常" : "正常"}</span></td>
           <td>${esc(s.screen_date) || "—"}</td><td>${esc(s.note) || "—"}</td></tr>`);
      } catch (err) { setMsg("#mat-msg", err.message, false); }
      return;
    }
    if (d.hrtoggle) {
      const toHigh = d.cur !== "true";
      const note = toHigh ? (prompt("高危原因") || "人工标记") : "";
      return postAction(`/api/maternal/children/${d.hrtoggle}/high-risk`, { high_risk: toHigh, risk_note: note }, "#mat-msg");
    }
  };
}

async function renderVaccination() {
  $("#page-desc").textContent = "接种前综合评估（禁忌硬拦截）、接种登记、禁忌管理";
  $("#page-body").innerHTML = `
    <div class="panel"><h3>接种前评估</h3>
      <form class="inline" id="vac-check"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="vaccine_code" placeholder="疫苗编码" required><button>评估</button></form>
      <div id="vac-check-result"></div></div>
    <div class="panel"><h3>接种登记 / 禁忌登记</h3>
      <form class="inline" id="vac-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="vaccine_name" placeholder="疫苗名称" required><input name="dose_no" type="number" value="1" min="1" style="min-width:60px">
        <input name="vaccinated_date" placeholder="接种日期"><input name="org_id" type="number" placeholder="接种机构ID" required><button>登记接种</button></form>
      <form class="inline" id="contra-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="reason" placeholder="禁忌原因" required><button class="btn danger">登记禁忌</button></form>
      <p class="msg" id="vac-msg"></p></div>
    <div class="panel"><h3>接种史查询</h3>
      <form class="inline" id="vac-hist"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="vac-hist-result"></div></div>`;
  $("#vac-check").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const r = await api(`/api/vaccination/pre-check?patient_id=${f.get("patient_id")}&vaccine_code=${encodeURIComponent(f.get("vaccine_code"))}`);
    $("#vac-check-result").innerHTML = r.allowed
      ? `<p class="msg ok">可以接种，本次为第 ${r.next_dose_no} 剂</p>`
      : `<p class="msg err">禁止接种：${esc(r.contraindications.join("；"))}</p>`;
  };
  $("#vac-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/records", formJson(e.target, ["patient_id", "dose_no", "org_id"]), "#vac-msg"); };
  $("#contra-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/contraindications", formJson(e.target, ["patient_id"]), "#vac-msg"); };
  $("#vac-hist").onsubmit = async (e) => {
    e.preventDefault();
    const records = await api(`/api/vaccination/records?patient_id=${new FormData(e.target).get("patient_id")}`);
    $("#vac-hist-result").innerHTML = table(["疫苗", "剂次", "日期", "机构"], records, (r) =>
      `<tr><td>${esc(r.vaccine_name)}</td><td>第${r.dose_no}剂</td><td>${esc(r.vaccinated_date)}</td><td>${r.org_id}</td></tr>`);
  };
}

async function renderPublicHealth() {
  $("#page-desc").textContent = "应急事件指挥（I-IV级）、诊间医防提醒、五域卫生监测";
  const [events, monitors] = await Promise.all([api("/api/publichealth/events"), api("/api/publichealth/monitors")]);
  const DM = { nutrition: "营养", environment: "环境", occupational: "职业", radiation: "放射", school: "学校" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>事件立案</h3>
      <form class="inline" id="ev-form">
        <input name="title" placeholder="事件名称" required style="min-width:220px">
        <select name="level"><option>IV</option><option>III</option><option>II</option><option>I</option></select>
        <input name="disease_name" placeholder="相关病种"><button>立案</button></form>
      <h3 style="margin-top:12px">诊间医防提醒</h3>
      <form class="inline" id="rem-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询提醒</button></form>
      <div id="rem-result"></div><p class="msg" id="ph-msg"></p></div>
    <div class="panel"><h3>事件列表</h3>${table(["ID", "事件", "级别", "病种", "状态", "操作"], events, (ev) =>
      `<tr><td>${ev.id}</td><td>${esc(ev.title)}</td><td><span class="tag ${ev.level === "I" || ev.level === "II" ? "red" : "orange"}">${ev.level}级</span></td>
       <td>${esc(ev.disease_name)}</td><td><span class="tag ${ev.status === "active" ? "red" : "green"}">${ev.status === "active" ? "处置中" : "已结案"}</span></td>
       <td>${ev.status === "active" ? `<button class="btn secondary" data-act="${ev.id}">处置记录</button><button class="btn secondary" data-close="${ev.id}">结案</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>卫生监测（营养/环境/职业/放射/学校）</h3>
      <form class="inline" id="mon-form">
        <select name="domain">${Object.entries(DM).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="org_id" type="number" placeholder="机构ID" required><input name="indicator" placeholder="监测指标" required>
        <input name="value" type="number" step="any" placeholder="监测值" required><input name="threshold" type="number" step="any" placeholder="阈值" required>
        <input name="record_date" placeholder="日期"><button>登记</button></form>
      ${table(["领域", "指标", "值/阈值", "状态"], monitors, (m) =>
        `<tr><td>${DM[m.domain]}</td><td>${esc(m.indicator)}</td><td>${m.value} / ${m.threshold}</td>
         <td>${m.exceeded ? '<span class="tag red">超标</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}</div>`;
  $("#ev-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/events", formJson(e.target), "#ph-msg"); };
  $("#mon-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/monitors", formJson(e.target, ["org_id", "value", "threshold"]), "#ph-msg"); };
  $("#rem-form").onsubmit = async (e) => {
    e.preventDefault();
    const r = await api(`/api/publichealth/reminders/${new FormData(e.target).get("patient_id")}`);
    $("#rem-result").innerHTML = r.reminders.length
      ? `<ul style="margin:8px 0 0 18px;font-size:13px">${r.reminders.map((x) => `<li>${esc(x.detail)}</li>`).join("")}</ul>`
      : '<p class="msg ok">无待办提醒</p>';
  };
  $("#page-body").onclick = (e) => {
    const { act, close } = e.target.dataset;
    if (act) {
      const action = prompt("处置动作"); if (!action) return;
      return postAction(`/api/publichealth/events/${act}/actions`, { action, actor: prompt("执行人") || "" }, "#ph-msg");
    }
    if (close) return postAction(`/api/publichealth/events/${close}/close`, null, "#ph-msg");
  };
}

async function renderHrFinance() {
  $("#page-desc").textContent = "人力资源（科室库/变动/合同/薪酬）、派驻下沉、财务集中核算与预算执行、物资出入库";
  const role = currentRole();
  const isDirector = ["director", "admin"].includes(role);
  const [employees, secStats, finance, assets, departments, expiringContracts] = await Promise.all([
    api("/api/mgmt/employees"), api("/api/mgmt/secondments/stats"), api("/api/mgmt/finance/summary"),
    api("/api/mgmt/assets"), api("/api/mgmt/departments"), api("/api/mgmt/staff-contracts/expiring?days=60")]);
  const payroll = isDirector ? await api("/api/mgmt/payroll").catch(() => null) : null;
  const EST = { active: ["在岗", "green"], seconded: ["派驻中", "orange"], left: ["离职", ""] };
  const CHG_TYPES = { hire: "入职", regularize: "转正", transfer: "调动", leave: "离职" };
  const MV_TYPES = { inbound: "入库", issue: "领用", return: "归还", scrap: "报废" };
  const deptNames = Object.fromEntries(departments.map((d) => [d.id, d.name]));
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">在派人数</div><div class="value">${secStats.active_secondments}</div></div>
      <div class="card"><div class="label">医共体收入合计</div><div class="value">${finance.consolidated.income}</div></div>
      <div class="card"><div class="label">医共体结余</div><div class="value">${finance.consolidated.balance}</div></div>
      ${expiringContracts.length ? `<div class="card"><div class="label">60天内到期合同</div><div class="value warn">${expiringContracts.length}</div></div>` : ""}</div>
    <div class="panel"><h3>员工 / 派驻 / 财务 / 物资录入</h3>
      <form class="inline" id="emp-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="name" placeholder="姓名" required>
        <input name="title" placeholder="职称"><input name="position" placeholder="岗位"><button>登记员工</button></form>
      <form class="inline" id="sec-form"><input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="to_org_id" type="number" placeholder="派驻机构ID" required><input name="start_date" placeholder="开始日期 YYYY-MM-DD" required><button>派驻下沉</button></form>
      <form class="inline" id="fin-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="period" placeholder="期间 YYYY-MM" required>
        <select name="category"><option value="income">收入</option><option value="expense">支出</option></select>
        <input name="item" placeholder="科目"><input name="amount" type="number" step="any" placeholder="金额" required><button>记账</button></form>
      <form class="inline" id="asset-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="code" placeholder="物资编码" required>
        <input name="name" placeholder="名称" required><select name="category"><option value="office">办公用品</option><option value="equipment">非医疗设备</option></select>
        <input name="quantity" type="number" value="1" min="1" style="min-width:60px"><button>物资建档</button></form>
      <p class="msg" id="hrf-msg"></p></div>
    <div class="panel"><h3>科室信息基础库（机构内编码唯一，员工跨机构挂接拦截）</h3>
      <form class="inline" id="dept-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="code" placeholder="科室编码" required><input name="name" placeholder="科室名称" required>
        <select name="category"><option value="clinical">临床</option><option value="medtech">医技</option><option value="admin">行政</option></select>
        <button>科室建档</button></form>
      ${table(["ID", "机构", "编码", "名称", "类别"], departments, (d) =>
        `<tr><td>${d.id}</td><td>${d.org_id}</td><td><span class="tag">${esc(d.code)}</span></td><td>${esc(d.name)}</td><td>${esc(d.category)}</td></tr>`)}</div>
    <div class="panel"><h3>员工（变动留痕联动机构与状态）</h3>${table(["ID", "机构", "姓名", "职称", "科室", "状态", "操作"], employees, (em) => {
      const [t, col] = EST[em.status] || [em.status, ""];
      return `<tr><td>${em.id}</td><td>${em.org_id}</td><td>${esc(em.name)}</td><td>${esc(em.title)}</td>
        <td>${em.dept_id ? esc(deptNames[em.dept_id] || em.dept_id) : "—"}</td><td><span class="tag ${col}">${t}</span></td>
        <td><button class="btn secondary" data-empdept="${em.id}">挂科室</button>
            <button class="btn secondary" data-empchg="${em.id}">登记变动</button>
            <button class="btn" data-emphist="${em.id}">变动史</button>
            <button class="btn secondary" data-empct="${em.id}">签合同</button></td></tr>`;
    })}</div>
    <div class="panel hidden" id="empchg-panel"><h3>人员变动记录</h3><div id="empchg-list"></div></div>
    ${expiringContracts.length ? `<div class="panel" style="border-left:4px solid #b26a00"><h3>⚠ 合同到期提醒（60天内 ${expiringContracts.length} 份，续签管理）</h3>${
      table(["合同号", "员工", "止期"], expiringContracts, (c) =>
        `<tr><td><span class="tag">${esc(c.contract_no)}</span></td><td>${c.employee_id}</td><td><span class="tag orange">${esc(c.end_date)}</span></td></tr>`)}</div>` : ""}
    ${isDirector ? `<div class="panel"><h3>月度薪酬（管理层：基础 + 绩效×系数）</h3>
      <form class="inline" id="pay-form">
        <input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="period" placeholder="期间 YYYY-MM" required pattern="\\d{4}-\\d{2}">
        <input name="base_salary" type="number" step="any" placeholder="基础工资" required>
        <input name="perf_bonus" type="number" step="any" placeholder="绩效奖金" value="0">
        <input name="perf_coefficient" type="number" step="any" placeholder="绩效系数" value="1.0">
        <button>录入</button></form>
      ${payroll ? `<p style="font-size:13px">合计发放：<b>${payroll.total_amount}</b> 元</p>${
        table(["ID", "员工", "期间", "基础", "绩效", "系数", "实发"], payroll.records, (r) =>
          `<tr><td>${r.id}</td><td>${r.employee_id}</td><td>${esc(r.period)}</td><td>${r.base_salary}</td>
           <td>${r.perf_bonus}</td><td>${r.perf_coefficient}</td><td><b>${r.total}</b></td></tr>`)}` : ""}</div>
    <div class="panel"><h3>预算编制与执行（管理层）</h3>
      <form class="inline" id="bud-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="year" placeholder="年度 YYYY" required pattern="\\d{4}">
        <select name="category"><option value="income">收入预算</option><option value="expense">支出预算</option></select>
        <input name="amount" type="number" step="any" placeholder="预算额" required>
        <button>编制/调整</button></form>
      <form class="inline" id="bud-exec-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="year" placeholder="年度 YYYY" required pattern="\\d{4}">
        <button>查执行率</button></form>
      <div id="bud-exec"></div></div>` : ""}
    <div class="panel"><h3>各单位收支（全部期间）</h3>${table(["机构", "收入", "支出", "结余"], finance.orgs, (o) =>
      `<tr><td>${o.org_id}</td><td>${o.income}</td><td>${o.expense}</td><td>${o.balance}</td></tr>`)}</div>
    <div class="panel"><h3>物资（出入库全程留痕）</h3>${table(["ID", "编码", "名称", "机构", "数量", "状态", "操作"], assets, (a) =>
      `<tr><td>${a.id}</td><td>${esc(a.code)}</td><td>${esc(a.name)}</td><td>${a.org_id}</td><td>${a.quantity}</td>
       <td><span class="tag ${a.status === "scrapped" ? "red" : ""}">${a.status === "scrapped" ? "已报废" : a.status}</span></td>
       <td>${a.status !== "scrapped" ? `<button class="btn secondary" data-assetmv="${a.id}">出入库</button>` : ""}
           <button class="btn" data-assethist="${a.id}">记录</button></td></tr>`)}</div>
    <div class="panel hidden" id="assetmv-panel"><h3>物资出入库记录</h3><div id="assetmv-list"></div></div>`;
  $("#emp-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/employees", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  $("#sec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/secondments", formJson(e.target, ["employee_id", "to_org_id"]), "#hrf-msg"); };
  $("#fin-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/finance", formJson(e.target, ["org_id", "amount"]), "#hrf-msg"); };
  $("#asset-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/assets", formJson(e.target, ["org_id", "quantity"]), "#hrf-msg"); };
  $("#dept-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/departments", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  const payForm = $("#pay-form");
  if (payForm) payForm.onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/payroll", formJson(e.target, ["employee_id", "base_salary", "perf_bonus", "perf_coefficient"]), "#hrf-msg"); };
  const budForm = $("#bud-form");
  if (budForm) budForm.onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/budgets", formJson(e.target, ["org_id", "amount"]), "#hrf-msg"); };
  const budExec = $("#bud-exec-form");
  if (budExec) budExec.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api(`/api/mgmt/budgets/execution?org_id=${f.get("org_id")}&year=${f.get("year")}`);
      $("#bud-exec").innerHTML = table(["类别", "预算", "实际", "执行率"], [
        ["收入", r.income], ["支出", r.expense]], ([label, d]) =>
        `<tr><td>${label}</td><td>${d.budget}</td><td>${d.actual}</td>
         <td>${d.execution_pct === null ? "—" : `<span class="tag ${d.execution_pct > 100 ? "red" : "green"}">${d.execution_pct}%</span>`}</td></tr>`);
    } catch (err) { setMsg("#hrf-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.empdept) {
        const deptId = prompt("科室ID（须与员工同机构）"); if (!deptId) return;
        await api(`/api/mgmt/employees/${d.empdept}/department?dept_id=${Number(deptId)}`, { method: "POST" });
        route();
      }
      if (d.empchg) {
        const keys = Object.keys(CHG_TYPES);
        const pick = prompt(`变动类型（${keys.map((k, i) => `${i + 1}=${CHG_TYPES[k]}`).join("，")}）输入序号`);
        const type = keys[Number(pick) - 1]; if (!type) return;
        const body = { change_type: type, detail: prompt("变动说明") || "", effective_date: prompt("生效日期 YYYY-MM-DD") || "" };
        if (type === "transfer") {
          const toOrg = prompt("调入机构ID"); if (!toOrg) return;
          body.to_org_id = Number(toOrg);
        }
        return postAction(`/api/mgmt/employees/${d.empchg}/changes`, body, "#hrf-msg");
      }
      if (d.emphist) {
        const changes = await api(`/api/mgmt/employees/${d.emphist}/changes`);
        $("#empchg-panel").classList.remove("hidden");
        $("#empchg-list").innerHTML = table(["ID", "类型", "调入机构", "说明", "生效日期"], changes, (c) =>
          `<tr><td>${c.id}</td><td><span class="tag">${CHG_TYPES[c.change_type] || esc(c.change_type)}</span></td>
           <td>${c.to_org_id ?? "—"}</td><td>${esc(c.detail) || "—"}</td><td>${esc(c.effective_date) || "—"}</td></tr>`);
      }
      if (d.empct) {
        const no = prompt("合同编号"); if (!no) return;
        const start = prompt("起期 YYYY-MM-DD"); if (!start) return;
        const end = prompt("止期 YYYY-MM-DD"); if (!end) return;
        return postAction("/api/mgmt/staff-contracts",
          { employee_id: Number(d.empct), contract_no: no, start_date: start, end_date: end }, "#hrf-msg");
      }
      if (d.assetmv) {
        const keys = Object.keys(MV_TYPES);
        const pick = prompt(`动作（${keys.map((k, i) => `${i + 1}=${MV_TYPES[k]}`).join("，")}）输入序号`);
        const type = keys[Number(pick) - 1]; if (!type) return;
        const qty = prompt("数量"); if (!qty) return;
        return postAction(`/api/mgmt/assets/${d.assetmv}/movements`,
          { movement_type: type, quantity: Number(qty), note: prompt("备注") || "" }, "#hrf-msg");
      }
      if (d.assethist) {
        const moves = await api(`/api/mgmt/assets/${d.assethist}/movements`);
        $("#assetmv-panel").classList.remove("hidden");
        $("#assetmv-list").innerHTML = table(["ID", "动作", "数量", "备注", "时间"], moves, (m) =>
          `<tr><td>${m.id}</td><td><span class="tag">${MV_TYPES[m.movement_type] || esc(m.movement_type)}</span></td>
           <td>${m.quantity}</td><td>${esc(m.note) || "—"}</td><td>${esc(m.at.slice(0, 16).replace("T", " "))}</td></tr>`);
      }
    } catch (err) { setMsg("#hrf-msg", err.message, false); }
  };
}

async function renderOaQc() {
  $("#page-desc").textContent = "行政公文（起草→发布）、共享中心排班与质控";
  const [docs, rosters, qc] = await Promise.all([api("/api/mgmt/docs"), api("/api/mgmt/rosters"), api("/api/mgmt/qc")]);
  const CN = { imaging: "影像", ecg: "心电", lab: "检验", pathology: "病理" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>公文起草 / 排班 / 质控登记</h3>
      <form class="inline" id="doc-form"><input name="title" placeholder="公文标题" required style="min-width:240px">
        <select name="doc_type"><option value="notice">通知</option><option value="policy">政策文件</option><option value="minutes">会议纪要</option></select>
        <input name="issuer" placeholder="发文单位"><button>起草</button></form>
      <form class="inline" id="roster-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="duty_date" placeholder="值班日期 YYYY-MM-DD" required><input name="shift" placeholder="班次" value="全天"><input name="doctor_name" placeholder="医师" required><button>排班</button></form>
      <form class="inline" id="qc-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="item" placeholder="质控项目" required><select name="result"><option value="pass">合格</option><option value="fail">不合格</option></select>
        <input name="note" placeholder="备注"><input name="record_date" placeholder="日期"><button>登记质控</button></form>
      <p class="msg" id="oa-msg"></p></div>
    <div class="panel"><h3>公文</h3>${table(["ID", "标题", "类型", "发文单位", "状态", "操作"], docs, (d) =>
      `<tr><td>${d.id}</td><td>${esc(d.title)}</td><td>${esc(d.doc_type)}</td><td>${esc(d.issuer)}</td>
       <td><span class="tag ${d.status === "published" ? "green" : "orange"}">${d.status === "published" ? "已发布" : "草稿"}</span></td>
       <td>${d.status === "draft" ? `<button class="btn secondary" data-pub="${d.id}">发布</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>排班</h3>${table(["中心", "日期", "班次", "医师"], rosters, (r) =>
      `<tr><td>${CN[r.center_type]}</td><td>${esc(r.duty_date)}</td><td>${esc(r.shift)}</td><td>${esc(r.doctor_name)}</td></tr>`)}</div>
    <div class="panel"><h3>质控记录</h3>${table(["中心", "项目", "结果", "备注"], qc, (q) =>
      `<tr><td>${CN[q.center_type]}</td><td>${esc(q.item)}</td>
       <td><span class="tag ${q.result === "pass" ? "green" : "red"}">${q.result === "pass" ? "合格" : "不合格"}</span></td><td>${esc(q.note)}</td></tr>`)}</div>`;
  $("#doc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/docs", formJson(e.target), "#oa-msg"); };
  $("#roster-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/rosters", formJson(e.target), "#oa-msg"); };
  $("#qc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/qc", formJson(e.target), "#oa-msg"); };
  $("#page-body").onclick = (e) => { if (e.target.dataset.pub) postAction(`/api/mgmt/docs/${e.target.dataset.pub}/publish`, null, "#oa-msg"); };
}

/* ---------------- 第四阶段新增页面 ---------------- */

const CRIT_STATUS = { notified: ["已通知", "orange"], acknowledged: ["已确认", ""], resolved: ["已处置", "green"], "": ["待回填", "orange"] };

async function renderCritical() {
  $("#page-desc").textContent = "危急值闭环：通知 → 医师确认接收 → 处置反馈；超时未确认催办";
  const [critical, unacked] = await Promise.all([
    api("/api/exams/critical"), api("/api/exams/critical/unacknowledged")]);
  $("#page-body").innerHTML = `
    ${unacked.length ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 超时未确认催办（${unacked.length}）</h3>${
      table(["报告ID", "申请单", "结论", "报告人", "报告时间"], unacked, (r) =>
        `<tr><td>${r.report_id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
         <td>${esc(r.reported_by)}</td><td>${esc(r.reported_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>危急值清单</h3><p class="msg" id="crit-msg"></p>${
      table(["报告ID", "申请单", "结论", "闭环状态", "操作"], critical, (r) => {
        const [text, color] = CRIT_STATUS[r.critical_status] || [r.critical_status, ""];
        const actions = (r.critical_status === "notified" || r.critical_status === "")
          ? `<button class="btn secondary" data-ack="${r.id}">确认接收</button>`
          : r.critical_status === "acknowledged"
          ? `<button class="btn secondary" data-resolve="${r.id}">处置反馈</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
          <td><span class="tag ${color}">${text}</span></td>
          <td>${actions} <button class="btn" data-trail="${r.id}">留痕</button></td></tr>`;
      })}</div>
    <div class="panel hidden" id="crit-trail-panel"><h3>处置留痕轨迹</h3><div id="crit-trail"></div></div>`;
  $("#page-body").onclick = async (e) => {
    const { ack, resolve, trail } = e.target.dataset;
    try {
      if (ack) { await api(`/api/exams/reports/${ack}/acknowledge`, { method: "POST" }); route(); }
      if (resolve) {
        const note = prompt("处置反馈说明（如：已复查、已调整治疗）") || "";
        await api(`/api/exams/reports/${resolve}/resolve`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (trail) {
        const actions = await api(`/api/exams/reports/${trail}/critical-actions`);
        $("#crit-trail-panel").classList.remove("hidden");
        $("#crit-trail").innerHTML = table(["动作", "操作人"], actions, (a) =>
          `<tr><td>${esc(a.action)}</td><td>${esc(a.actor)}</td></tr>`);
      }
    } catch (err) { setMsg("#crit-msg", err.message, false); }
  };
}

async function renderRecognition() {
  $("#page-desc").textContent = "互认项目目录（目录内 active 项目方可互认）、互认率统计与检查资源要素档案";
  const [items, stats, resources] = await Promise.all([
    api("/api/exams/recognition-items"), api("/api/exams/recognition-stats"), api("/api/exams/resources")]);
  const cards = [
    ["互认总次数", stats.recognized_total], ["已报告总数", stats.reported_total],
    ["互认率", stats.recognition_ratio_pct + "%"], ["节约检查次数", stats.saved_exams]];
  $("#page-body").innerHTML = `
    <div class="cards">${cards.map(([l, v]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    ${stats.by_item.length ? `<div class="panel"><h3>按项目互认次数</h3>${
      barChart(stats.by_item.slice(0, 10).map((i) => [i.item_name, i.recognized_count]), { unit: " 次" })}</div>` : ""}
    <div class="panel"><h3>目录维护（admin）</h3>
      <form class="inline" id="rec-form">
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="mutual_scope"><option value="county">县域互认</option><option value="city">市级互认</option></select>
        <button>加入目录</button>
      </form><p class="msg" id="rec-msg"></p>
      ${table(["编码", "名称", "中心", "范围", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.item_code)}</td><td>${esc(i.item_name)}</td><td>${CENTER_NAMES[i.center_type]}</td>
         <td>${i.mutual_scope === "city" ? "市级" : "县域"}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-toggle="${i.id}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button></td></tr>`)}</div>
    <div class="panel"><h3>检查资源要素档案（设备/价格/时长/注意事项，admin 建档）</h3>
      <form class="inline" id="res-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="item_name" placeholder="项目名称" required>
        <input name="device" placeholder="设备">
        <input name="price" type="number" step="any" placeholder="价格(元)">
        <input name="duration_min" type="number" min="1" placeholder="时长(分)">
        <input name="notes" placeholder="注意事项" style="min-width:160px">
        <button>建档</button></form>
      ${table(["ID", "机构", "中心", "项目", "设备", "价格", "时长", "注意事项"], resources, (r) =>
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${CENTER_NAMES[r.center_type]}</td><td>${esc(r.item_name)}</td>
         <td>${esc(r.device) || "—"}</td><td>${r.price} 元</td><td>${r.duration_min} 分</td><td>${esc(r.notes) || "—"}</td></tr>`)}</div>`;
  $("#rec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/exams/recognition-items", formJson(e.target), "#rec-msg"); };
  $("#res-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/exams/resources", formJson(e.target, ["org_id", "price", "duration_min"]), "#rec-msg"); };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.toggle;
    if (!id) return;
    try {
      await api(`/api/exams/recognition-items/${id}`, { method: "PATCH",
        body: JSON.stringify({ active: e.target.dataset.active !== "true" }) });
      route();
    } catch (err) { setMsg("#rec-msg", err.message, false); }
  };
}

async function renderInpatient() {
  $("#page-desc").textContent = "入院登记（床位原子占用）→ 转科/转床 → 医嘱 → 病案首页 → 出院（费用结清校验）";
  const [wards, beds, admissions, stats] = await Promise.all([
    api("/api/inpatient/wards"), api("/api/inpatient/beds"),
    api("/api/inpatient/admissions"), api("/api/inpatient/stats")]);
  const AS = { admitted: ["在院", "orange"], discharged: ["已出院", "green"] };
  const wardName = Object.fromEntries(wards.map((w) => [w.id, w.name]));
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="panel"><h3>床位效率</h3>${table(["机构", "床位", "占用", "使用率", "在院", "累计出院"], stats, (s) =>
      `<tr><td>${esc(s.org_name)}</td><td>${s.beds_total}</td><td>${s.beds_occupied}</td>
       <td>${s.occupancy_pct}%</td><td>${s.in_hospital}</td><td>${s.discharged_total}</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>病区/床位建档（admin）与入院登记</h3>
      <form class="inline" id="ward-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="病区名称" required><button>建病区</button></form>
      <form class="inline" id="bed-form"><input name="ward_id" type="number" placeholder="病区ID" required>
        <input name="bed_no" placeholder="床号" required><button>建床位</button></form>
      <form class="inline" id="adm-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="ward_id" type="number" placeholder="病区ID" required><input name="bed_id" type="number" placeholder="床位ID" required>
        <input name="doctor_name" placeholder="主管医师"><input name="diagnosis_name" placeholder="入院诊断"><button>入院登记</button></form>
      <p class="msg" id="inp-msg"></p></div>
    <div class="panel"><h3>床位（${beds.filter((b) => b.status === "free").length} 空闲 / ${beds.length}）</h3>${
      table(["ID", "病区", "床号", "状态"], beds, (b) =>
        `<tr><td>${b.id}</td><td>${esc(wardName[b.ward_id] || b.ward_id)}</td><td>${esc(b.bed_no)}</td>
         <td><span class="tag ${b.status === "free" ? "green" : "orange"}">${b.status === "free" ? "空闲" : "占用"}</span></td></tr>`)}</div>
    <div class="panel"><h3>住院记录</h3>${
      table(["ID", "患者", "病区/床位", "诊断", "状态", "操作"], admissions, (a) => {
        const [text, color] = AS[a.status] || [a.status, ""];
        const actions = a.status === "admitted"
          ? `<button class="btn secondary" data-transfer="${a.id}">转床</button>
             <button class="btn secondary" data-order="${a.id}">开医嘱</button>
             <button class="btn secondary" data-summary="${a.id}">病案首页</button>
             <button class="btn danger" data-discharge="${a.id}">出院</button>`
          : "—";
        return `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(wardName[a.ward_id] || a.ward_id)} / ${a.bed_id}</td>
          <td>${esc(a.diagnosis_name)}</td><td><span class="tag ${color}">${text}</span></td>
          <td>${actions} <button class="btn" data-orders="${a.id}">医嘱单</button></td></tr>`;
      })}</div>
    <div class="panel hidden" id="inp-orders-panel"><h3>医嘱单</h3><div id="inp-orders"></div></div>`;
  $("#ward-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/wards", formJson(e.target, ["org_id"]), "#inp-msg"); };
  $("#bed-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/beds", formJson(e.target, ["ward_id"]), "#inp-msg"); };
  $("#adm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/admissions", formJson(e.target, ["patient_id", "ward_id", "bed_id"]), "#inp-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.transfer) {
        const wardId = prompt("目标病区ID"), bedId = prompt("目标床位ID");
        if (!wardId || !bedId) return;
        await api(`/api/inpatient/admissions/${d.transfer}/transfer`, { method: "POST",
          body: JSON.stringify({ ward_id: Number(wardId), bed_id: Number(bedId) }) });
        route();
      }
      if (d.order) {
        const content = prompt("医嘱内容");
        if (!content) return;
        const isLong = confirm("长期医嘱？（确定=长期，取消=临时）");
        await api("/api/inpatient/orders", { method: "POST",
          body: JSON.stringify({ admission_id: Number(d.order), order_type: isLong ? "long" : "temp", content }) });
        route();
      }
      if (d.summary) {
        const diagnosis = prompt("出院诊断");
        if (!diagnosis) return;
        await api(`/api/inpatient/admissions/${d.summary}/case-summary`, { method: "POST",
          body: JSON.stringify({
            discharge_diagnosis: diagnosis, operation: prompt("手术名称（无则留空）") || "",
            total_cost: Number(prompt("总费用（元）") || 0), drug_cost: Number(prompt("其中药费（元）") || 0) }) });
        route();
      }
      if (d.discharge) { await api(`/api/inpatient/admissions/${d.discharge}/discharge`, { method: "POST" }); route(); }
      if (d.stopOrder) { await api(`/api/inpatient/orders/${d.stopOrder}/stop`, { method: "POST" }); route(); }
      if (d.orders) {
        const orders = await api(`/api/inpatient/orders?admission_id=${d.orders}`);
        $("#inp-orders-panel").classList.remove("hidden");
        $("#inp-orders").innerHTML = table(["ID", "类型", "内容", "状态", "开立", "操作"], orders, (o) =>
          `<tr><td>${o.id}</td><td>${o.order_type === "long" ? "长期" : "临时"}</td><td>${esc(o.content)}</td>
           <td><span class="tag ${o.status === "active" ? "orange" : "green"}">${o.status === "active" ? "执行中" : "已停止"}</span></td>
           <td>${esc(o.created_by_name)}</td>
           <td>${o.status === "active" ? `<button class="btn danger" data-stop-order="${o.id}">停止</button>` : "—"}</td></tr>`);
      }
    } catch (err) { setMsg("#inp-msg", err.message, false); }
  };
}

async function renderBilling() {
  $("#page-desc").textContent = "收费目录（价格公示）→ 计费明细（门诊按就诊/住院按住院单）→ 结算（医保分担）";
  const [items, settlements, stats] = await Promise.all([
    api("/api/billing/charge-items"), api("/api/billing/settlements"), api("/api/billing/stats")]);
  const BT = { outpatient: "门诊", inpatient: "住院" };
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="cards">${stats.map((s) =>
      `<div class="card"><div class="label">${BT[s.bill_type]}结算 ${s.count} 笔</div>
       <div class="value">${s.total_amount} 元</div>
       <div class="label">均次 ${s.avg_amount} 元 · 医保 ${s.insurance_ratio_pct}%</div></div>`).join("")}</div>` : ""}
    <div class="panel"><h3>收费项目目录（admin 维护）</h3>
      <form class="inline" id="ci-form"><input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="category"><option value="treatment">治疗处置</option><option value="drug">药品</option>
          <option value="exam">检查检验</option><option value="bed">床位</option><option value="other">其他</option></select>
        <input name="price" type="number" step="any" placeholder="单价(元)" required><button>加入目录</button></form>
      <p class="msg" id="bill-msg"></p>
      ${table(["编码", "名称", "类别", "单价", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td><td>${esc(i.category)}</td><td>${i.price}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-reprice="${i.id}">调价</button></td></tr>`)}</div>
    <div class="panel"><h3>计费与结算</h3>
      <form class="inline" id="bd-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="admission_id" type="number" placeholder="住院单ID(住院)"><input name="encounter_id" type="number" placeholder="就诊ID(门诊)">
        <input name="item_code" placeholder="收费编码" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>计费</button></form>
      <form class="inline" id="settle-form">
        <select name="bill_type"><option value="inpatient">住院结算</option><option value="outpatient">门诊结算</option></select>
        <input name="admission_id" type="number" placeholder="住院单ID"><input name="encounter_id" type="number" placeholder="就诊ID">
        <input name="insurance_pay" type="number" step="any" placeholder="医保支付(元)" value="0"><button>结算</button></form>
      <p style="font-size:12.5px;color:#8a939e">住院费用未结清不可出院；结算自动汇总未结清明细并联动医保结算记录</p></div>
    <div class="panel"><h3>结算单</h3>${table(["ID", "患者", "类型", "总额", "医保", "自付", "时间"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${BT[s.bill_type]}</td><td>${s.total_amount}</td>
       <td>${s.insurance_pay}</td><td>${s.self_pay}</td><td>${esc(s.created_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>`;
  $("#ci-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/charge-items", formJson(e.target, ["price"]), "#bill-msg"); };
  $("#bd-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/details", formJson(e.target, ["patient_id", "admission_id", "encounter_id", "quantity"]), "#bill-msg"); };
  $("#settle-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/settlements", formJson(e.target, ["admission_id", "encounter_id", "insurance_pay"]), "#bill-msg"); };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.reprice;
    if (!id) return;
    const price = prompt("新单价（元）");
    if (!price) return;
    try { await api(`/api/billing/charge-items/${id}`, { method: "PATCH", body: JSON.stringify({ price: Number(price) }) }); route(); }
    catch (err) { setMsg("#bill-msg", err.message, false); }
  };
}

async function renderQuality() {
  $("#page-desc").textContent = "不良事件上报（可匿名）→ 审核 → 整改；病历质控评分；院感上报核实";
  const [events, estats, qstats, infections] = await Promise.all([
    api("/api/quality/adverse-events"), api("/api/quality/adverse-events-stats"),
    api("/api/quality/record-qc-stats"), api("/api/quality/infection-reports")]);
  const AES = { reported: ["已上报", "orange"], reviewed: ["已审核", ""], rectified: ["已整改", "green"] };
  const AET = { medication: "用药", device: "器械", fall: "跌倒", pressure_sore: "压疮", transfusion: "输血", identification: "查对", other: "其他" };
  const SITE = { respiratory: "呼吸道", surgical_site: "手术部位", urinary: "泌尿道", bloodstream: "血流", gastrointestinal: "消化道", other: "其他" };
  const IST = { reported: ["待核实", "orange"], confirmed: ["已确认", "red"], excluded: ["已排除", "green"] };
  const cards = [
    ["不良事件", estats.total], ["整改闭环率", estats.closed_loop_pct + "%"],
    ["病历抽检", qstats.total], ["病历均分", qstats.avg_score], ["病历甲级率", qstats.grade_a_pct + "%"]];
  $("#page-body").innerHTML = `
    <div class="cards">${cards.map(([l, v]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    <div class="panel"><h3>不良事件上报</h3>
      <form class="inline" id="ae-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="event_type">${Object.entries(AET).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="level"><option value="IV">IV级(隐患)</option><option value="III">III级(无后果)</option>
          <option value="II">II级(不良后果)</option><option value="I">I级(警告)</option></select>
        <input name="description" placeholder="事件经过" required style="min-width:220px">
        <label style="font-size:13px"><input type="checkbox" name="anonymous" value="true"> 匿名</label><button>上报</button></form>
      <p class="msg" id="qa-msg"></p>
      ${table(["ID", "类型", "等级", "经过", "报告人", "状态", "操作"], events, (ev) => {
        const [text, color] = AES[ev.status] || [ev.status, ""];
        const actions = ev.status === "reported"
          ? `<button class="btn secondary" data-review="${ev.id}">审核</button>`
          : ev.status === "reviewed"
          ? `<button class="btn secondary" data-rectify="${ev.id}">登记整改</button>` : "—";
        return `<tr><td>${ev.id}</td><td>${AET[ev.event_type] || ev.event_type}</td><td>${ev.level}</td>
          <td>${esc(ev.description)}</td><td>${esc(ev.reporter_name) || "（匿名）"}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
      })}</div>
    <div class="panel"><h3>不良事件附件（现场照片/佐证PDF，≤10MB）</h3>
      <form class="inline" id="ae-att-form">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传</button></form>
      <form class="inline" id="ae-att-query">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <button>查附件</button></form>
      <p class="msg" id="ae-att-msg"></p><div id="ae-att-list"></div></div>
    <div class="panel"><h3>病历质控抽检</h3>
      <form class="inline" id="qc-rec-form">
        <select name="target_type"><option value="encounter">门急诊病历</option><option value="case_summary">病案首页</option></select>
        <input name="target_id" type="number" placeholder="对象ID" required>
        <input name="score" type="number" min="0" max="100" placeholder="评分0-100" required>
        <input name="defects" placeholder="缺陷项（分号分隔）" style="min-width:200px"><button>评分</button></form></div>
    <div class="panel"><h3>院感上报</h3>
      <form class="inline" id="inf-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="infection_site">${Object.entries(SITE).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="pathogen" placeholder="病原体"><input name="report_date" placeholder="日期 YYYY-MM-DD"><button>上报</button></form>
      ${table(["ID", "机构", "患者", "部位", "病原体", "状态", "操作"], infections, (r) => {
        const [text, color] = IST[r.status] || [r.status, ""];
        const actions = r.status === "reported"
          ? `<button class="btn secondary" data-verify="${r.id}" data-ok="true">确认</button>
             <button class="btn" data-verify="${r.id}" data-ok="false">排除</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${r.patient_id}</td><td>${SITE[r.infection_site]}</td>
          <td>${esc(r.pathogen)}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
      })}</div>`;
  $("#ae-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["org_id"]);
    body.anonymous = e.target.anonymous.checked;
    postAction("/api/quality/adverse-events", body, "#qa-msg");
  };
  $("#ae-att-form").onsubmit = async (e) => {
    e.preventDefault();
    const eventId = new FormData(e.target).get("event_id");
    try {
      await uploadAttachment("adverse_event", eventId, e.target.querySelector("input[type=file]"));
      setMsg("#ae-att-msg", "附件已上传");
      await drawAttachments("adverse_event", eventId, "#ae-att-list", "#ae-att-msg");
    } catch (err) { setMsg("#ae-att-msg", err.message, false); }
  };
  $("#ae-att-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawAttachments("adverse_event", new FormData(e.target).get("event_id"), "#ae-att-list", "#ae-att-msg"); }
    catch (err) { setMsg("#ae-att-msg", err.message, false); }
  };
  $("#qc-rec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/quality/record-qc", formJson(e.target, ["target_id", "score"]), "#qa-msg"); };
  $("#inf-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/quality/infection-reports", formJson(e.target, ["org_id", "patient_id"]), "#qa-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.review) {
        const note = prompt("审核意见");
        if (!note) return;
        await api(`/api/quality/adverse-events/${d.review}/review`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (d.rectify) {
        const note = prompt("整改措施");
        if (!note) return;
        await api(`/api/quality/adverse-events/${d.rectify}/rectify`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (d.verify) {
        await api(`/api/quality/infection-reports/${d.verify}/verify?confirmed=${d.ok}`, { method: "POST" });
        route();
      }
    } catch (err) { setMsg("#qa-msg", err.message, false); }
  };
}

async function renderPerfIndicators() {
  $("#page-desc").textContent = "绩效指标目录：权重调节与启停（调整后按比例归一化计分）";
  const indicators = await api("/api/performance/indicators");
  $("#page-body").innerHTML = `
    <div class="panel"><p class="msg" id="pi-msg"></p>${
      table(["指标", "键", "权重", "状态", "操作"], indicators, (i) =>
        `<tr><td>${esc(i.name)}</td><td>${esc(i.key)}</td><td>${i.weight}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-weight="${esc(i.key)}">调权重</button>
             <button class="btn" data-toggle-ind="${esc(i.key)}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button></td></tr>`)}</div>`;
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.weight) {
        const w = prompt("新权重（≥0，自动按比例归一化）");
        if (w === null || w === "") return;
        await api(`/api/performance/indicators/${d.weight}`, { method: "PATCH", body: JSON.stringify({ weight: Number(w) }) });
        route();
      }
      if (d.toggleInd) {
        await api(`/api/performance/indicators/${d.toggleInd}`, { method: "PATCH",
          body: JSON.stringify({ active: d.active !== "true" }) });
        route();
      }
    } catch (err) { setMsg("#pi-msg", err.message, false); }
  };
}

async function renderInfectiousDir() {
  $("#page-desc").textContent = "法定传染病目录（甲类2小时/乙丙类24小时报告时限）与迟报清单";
  const [diseases, late] = await Promise.all([
    api("/api/infectious/diseases"), api("/api/infectious/late-reports")]);
  const CAT = { A: ["甲类", "red"], B: ["乙类", "orange"], C: ["丙类", ""] };
  $("#page-body").innerHTML = `
    ${late.length ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 迟报清单（${late.length}）</h3>${
      table(["病例ID", "病种", "类别", "发病日期", "报告时间", "迟报"], late, (l) => {
        const [t, c] = CAT[l.category] || [l.category, ""];
        return `<tr><td>${l.case_id}</td><td>${esc(l.disease_name)}</td><td><span class="tag ${c}">${t}</span></td>
          <td>${esc(l.onset_date)}</td><td>${esc((l.reported_at || "").slice(0, 16).replace("T", " "))}</td>
          <td><span class="tag red">迟报 ${l.days_late} 天</span></td></tr>`;
      })}</div>` : '<div class="panel"><h3>迟报清单</h3><p style="color:#8a939e">无迟报病例</p></div>'}
    <div class="panel"><h3>法定传染病目录（${diseases.length}）</h3>${
      table(["编码", "名称", "类别", "报告时限"], diseases, (d) => {
        const [t, c] = CAT[d.category] || [d.category, ""];
        return `<tr><td>${esc(d.code)}</td><td>${esc(d.name)}</td>
          <td><span class="tag ${c}">${t}</span></td><td>${d.report_hours} 小时</td></tr>`;
      })}</div>`;
}

const MILESTONES = { onset: "发病", call: "呼救", depart: "出车", arrive_scene: "到达现场", arrive_hospital: "到达医院", treatment: "开始救治" };
const CHANNELS = { "": "普通", chest_pain: "胸痛", stroke: "卒中", trauma: "创伤" };

async function renderEmTimeline() {
  $("#page-desc").textContent = "急救绿道：通道建单 → 节点录入 → 时间轴时效展示";
  const cases = await api("/api/emergency/cases");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>绿道建单</h3>
      <form class="inline" id="gc-form"><input name="location" placeholder="事发地点" required>
        <input name="symptom" placeholder="主诉">
        <select name="channel_type">${Object.entries(CHANNELS).map(([v, t]) => `<option value="${v}">${t}通道</option>`).join("")}</select>
        <input name="dest_org_id" type="number" placeholder="目标医院ID"><button>建单</button></form>
      <p class="msg" id="gc-msg"></p></div>
    <div class="panel"><h3>急救事件</h3>${table(["ID", "地点", "主诉", "通道", "状态", "操作"], cases, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.location)}</td><td>${esc(c.symptom)}</td>
       <td><span class="tag ${c.channel_type ? "red" : ""}">${CHANNELS[c.channel_type] || c.channel_type}</span></td>
       <td>${esc(c.status)}</td>
       <td><button class="btn secondary" data-mile="${c.id}">录节点</button>
           <button class="btn" data-timeline="${c.id}">时间轴</button></td></tr>`)}</div>
    <div class="panel hidden" id="gc-tl-panel"><h3>绿道时间轴</h3><div id="gc-tl"></div></div>`;
  $("#gc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id"]), "#gc-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.mile) {
        const keys = Object.keys(MILESTONES);
        const pick = prompt(`节点（${keys.map((k, i) => `${i + 1}=${MILESTONES[k]}`).join("，")}）输入序号`);
        const key = keys[Number(pick) - 1];
        if (!key) return;
        const at = prompt("发生时刻（如 2026-08-11 14:30）");
        if (!at) return;
        await api(`/api/emergency/cases/${d.mile}/milestones`, { method: "POST",
          body: JSON.stringify({ milestone: key, occurred_at: at }) });
        route();
      }
      if (d.timeline) {
        const tl = await api(`/api/emergency/cases/${d.timeline}/timeline`);
        $("#gc-tl-panel").classList.remove("hidden");
        $("#gc-tl").innerHTML = `<p style="font-size:13px">通道：<span class="tag red">${CHANNELS[tl.channel_type] || "普通"}</span>
          已记录 ${tl.recorded_count}/6</p>` +
          table(["节点", "时刻", "状态"], tl.timeline, (m) =>
            `<tr><td>${esc(m.name)}</td><td>${esc(m.occurred_at || "—")}</td>
             <td><span class="tag ${m.recorded ? "green" : "orange"}">${m.recorded ? "已记录" : "缺失"}</span></td></tr>`);
      }
    } catch (err) { setMsg("#gc-msg", err.message, false); }
  };
}

async function renderDrgs() {
  $("#page-desc").textContent = "DRGs 简化分析：分组目录（关键词入组）、机构 CMI 与组均费用对比";
  const [groups, stats] = await Promise.all([api("/api/drgs/groups"), api("/api/drgs/stats")]);
  $("#page-body").innerHTML = `
    ${stats.orgs.length ? `<div class="panel"><h3>机构 CMI 对比（病例组合指数 = Σ权重 / 入组例数）</h3>${
      table(["机构", "出院病例", "入组", "入组率", "CMI", "均次费用"], stats.orgs, (o) =>
        `<tr><td>${esc(o.org_name)}</td><td>${o.cases}</td><td>${o.grouped}</td>
         <td>${o.grouped_pct}%</td><td><b>${o.cmi}</b></td><td>${o.avg_cost} 元</td></tr>`)}</div>` : ""}
    ${stats.groups.length ? `<div class="panel"><h3>组均费用</h3>${
      barChart(stats.groups.map((g) => [`${g.drg_code} ${g.drg_name}`, g.avg_cost]), { unit: " 元" })}</div>` : ""}
    <div class="panel"><h3>分组目录（admin 可调权）</h3><p class="msg" id="drg-msg"></p>${
      table(["编码", "名称", "基准权重", "关键词", "状态", "操作"], groups, (g) =>
        `<tr><td>${esc(g.code)}</td><td>${esc(g.name)}</td><td>${g.base_weight}</td><td>${esc(g.keywords)}</td>
         <td><span class="tag ${g.active ? "green" : "red"}">${g.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-drg-weight="${g.id}">调权</button></td></tr>`)}</div>`;
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.drgWeight;
    if (!id) return;
    const w = prompt("新基准权重（>0）");
    if (!w) return;
    try { await api(`/api/drgs/groups/${id}`, { method: "PATCH", body: JSON.stringify({ base_weight: Number(w) }) }); route(); }
    catch (err) { setMsg("#drg-msg", err.message, false); }
  };
}

/* ---------------- 终审轮新增页面 ---------------- */

const BLOOD_COMPONENTS = { rbc: "红细胞", plasma: "血浆", platelet: "血小板" };
const BLOOD_REQ_STATUS = { pending: ["待审批", "orange"], approved: ["已审批", ""], rejected: ["已驳回", "red"], issued: ["已发血", "green"] };

async function renderBlood() {
  $("#page-desc").textContent = "血库台账（经办登记）→ 用血申请（医师）→ 审批（管理层）→ 发血（经办，库存不足拦截）";
  const [stocks, requests] = await Promise.all([api("/api/blood/stocks"), api("/api/blood/requests")]);
  const role = currentRole();
  const typeOpts = ["A", "B", "AB", "O"].map((t) => `<option>${t}</option>`).join("");
  const compOpts = Object.entries(BLOOD_COMPONENTS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  $("#page-body").innerHTML = `
    ${["operator", "admin"].includes(role) ? `<div class="panel"><h3>血库入库登记（经办）</h3>
      <form class="inline" id="bs-form">
        <select name="blood_type">${typeOpts}</select>
        <select name="component">${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <button>入库</button></form></div>` : ""}
    ${["doctor", "admin"].includes(role) ? `<div class="panel"><h3>临床用血申请（医师）</h3>
      <form class="inline" id="br-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="用血机构ID" required>
        <select name="blood_type">${typeOpts}</select>
        <select name="component">${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <input name="reason" placeholder="用血原因">
        <button>申请</button></form></div>` : ""}
    <p class="msg" id="blood-msg"></p>
    <div class="panel"><h3>血液库存台账</h3>${table(["血型", "成分", "库存(ml)"], stocks, (s) =>
      `<tr><td><span class="tag">${esc(s.blood_type)}</span></td><td>${BLOOD_COMPONENTS[s.component] || esc(s.component)}</td><td>${s.quantity_ml}</td></tr>`)}</div>
    <div class="panel"><h3>用血申请队列</h3>${table(["ID", "患者", "机构", "血型/成分", "数量", "状态", "操作"], requests, (r) => {
      const [text, color] = BLOOD_REQ_STATUS[r.status] || [r.status, ""];
      const actions = r.status === "pending" && ["director", "admin"].includes(role)
        ? `<button class="btn secondary" data-brev="${r.id}" data-ok="true">批准</button>
           <button class="btn danger" data-brev="${r.id}" data-ok="false">驳回</button>`
        : r.status === "approved" && ["operator", "admin"].includes(role)
        ? `<button class="btn secondary" data-bissue="${r.id}">发血</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${r.org_id}</td>
        <td>${esc(r.blood_type)} / ${BLOOD_COMPONENTS[r.component] || esc(r.component)}</td><td>${r.quantity_ml}ml</td>
        <td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
    })}</div>`;
  const bs = $("#bs-form");
  if (bs) bs.onsubmit = (e) => { e.preventDefault(); postAction("/api/blood/stocks", formJson(e.target, ["quantity_ml"]), "#blood-msg"); };
  const br = $("#br-form");
  if (br) br.onsubmit = (e) => { e.preventDefault(); postAction("/api/blood/requests", formJson(e.target, ["patient_id", "org_id", "quantity_ml"]), "#blood-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.brev) return postAction(`/api/blood/requests/${d.brev}/review?approve=${d.ok}`, null, "#blood-msg");
    if (d.bissue) return postAction(`/api/blood/requests/${d.bissue}/issue`, null, "#blood-msg");
  };
}

const PO_STATUS = { pending: ["待审批", "orange"], approved: ["已审批", ""], rejected: ["已驳回", "red"], received: ["已验收", "green"] };

async function renderProcure() {
  $("#page-desc").textContent = "供应商建档 → 采购申请（经办/药师）→ 审批（管理层）→ 验收入库；存货盘点账实调整";
  const [suppliers, orders, takes] = await Promise.all([
    api("/api/pharmacy/suppliers"), api("/api/pharmacy/purchase-orders"), api("/api/pharmacy/stock-takes")]);
  const role = currentRole();
  const supNames = Object.fromEntries(suppliers.map((s) => [s.id, s.name]));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>供应商建档（管理层/经办）</h3>
      <form class="inline" id="sup-form">
        <input name="name" placeholder="供应商名称" required>
        <input name="contact" placeholder="联系方式">
        <input name="license_no" placeholder="许可证号">
        <button>建档</button></form>
      ${table(["ID", "名称", "联系方式", "许可证", "状态"], suppliers, (s) =>
        `<tr><td>${s.id}</td><td>${esc(s.name)}</td><td>${esc(s.contact)}</td><td>${esc(s.license_no)}</td>
         <td><span class="tag ${s.active ? "green" : "red"}">${s.active ? "在用" : "停用"}</span></td></tr>`)}</div>
    <div class="panel"><h3>采购申请（经办/药师）</h3>
      <form class="inline" id="po-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="supplier_id" type="number" placeholder="供应商ID" required>
        <select name="item_type"><option value="drug">药品</option><option value="material">物资耗材</option></select>
        <input name="item_code" placeholder="编码" required>
        <input name="item_name" placeholder="名称" required>
        <input name="quantity" type="number" min="1" placeholder="数量" required>
        <button>提交申请</button></form>
      <p class="msg" id="po-msg"></p>
      ${table(["ID", "机构", "供应商", "类型", "品目", "数量", "状态", "操作"], orders, (o) => {
        const [text, color] = PO_STATUS[o.status] || [o.status, ""];
        const actions = o.status === "pending" && ["director", "admin"].includes(role)
          ? `<button class="btn secondary" data-poap="${o.id}">批准</button>
             <button class="btn danger" data-poap="${o.id}" data-reject="1">驳回</button>`
          : o.status === "approved" && ["operator", "pharmacist", "admin"].includes(role)
          ? `<button class="btn secondary" data-porec="${o.id}">验收入库</button>` : "—";
        return `<tr><td>${o.id}</td><td>${o.org_id}</td><td>${esc(supNames[o.supplier_id] || o.supplier_id)}</td>
          <td>${o.item_type === "drug" ? "药品" : "物资"}</td><td>${esc(o.item_name)}（${esc(o.item_code)}）</td>
          <td>${o.quantity}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
      })}</div>
    <div class="panel"><h3>存货盘点（经办/药师，盘后账实相符）</h3>
      <form class="inline" id="st-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="drug_code" placeholder="药品编码" required>
        <input name="actual_qty" type="number" min="0" placeholder="实盘数量" required>
        <input name="note" placeholder="差异说明">
        <button>盘点</button></form>
      ${table(["ID", "机构", "药品编码", "账面", "实盘", "差异"], takes, (t) =>
        `<tr><td>${t.id}</td><td>${t.org_id}</td><td>${esc(t.drug_code)}</td><td>${t.book_qty}</td><td>${t.actual_qty}</td>
         <td><span class="tag ${t.diff === 0 ? "green" : "red"}">${t.diff > 0 ? "+" : ""}${t.diff}</span></td></tr>`)}</div>`;
  $("#sup-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/suppliers", formJson(e.target), "#po-msg"); };
  $("#po-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/purchase-orders", formJson(e.target, ["org_id", "supplier_id", "quantity"]), "#po-msg"); };
  $("#st-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/stock-takes", formJson(e.target, ["org_id", "actual_qty"]), "#po-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.poap) return postAction(`/api/pharmacy/purchase-orders/${d.poap}/approve${d.reject ? "?reject=true" : ""}`, null, "#po-msg");
    if (d.porec) return postAction(`/api/pharmacy/purchase-orders/${d.porec}/receive`, null, "#po-msg");
  };
}

const CERT_TYPES = { birth: "出生医学证明", death: "死亡医学证明", defect: "出生缺陷儿登记" };

async function renderCerts() {
  $("#page-desc").textContent = "出生/死亡医学证明签发与出生缺陷登记（限医师/公卫）；成人健康体检记录与异常清单";
  const [stats, checkups, abnormal] = await Promise.all([
    api("/api/certs/stats"), api("/api/checkups"), api("/api/checkups/abnormal")]);
  const draw = async (certType = "") => {
    const certs = await api(`/api/certs${certType ? `?cert_type=${certType}` : ""}`);
    $("#cert-table").innerHTML = table(["编号", "类型", "姓名", "性别", "日期", "诊断/说明", "机构"], certs, (c) =>
      `<tr><td><span class="tag">${esc(c.cert_no)}</span></td><td>${CERT_TYPES[c.cert_type] || esc(c.cert_type)}</td>
       <td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.event_date)}</td><td>${esc(c.detail) || "—"}</td><td>${c.org_id}</td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">出生证明</div><div class="value">${stats.birth || 0}</div></div>
      <div class="card"><div class="label">死亡证明</div><div class="value">${stats.death || 0}</div></div>
      <div class="card"><div class="label">缺陷登记</div><div class="value">${stats.defect || 0}</div></div></div>
    <div class="panel"><h3>证明签发（医师/公卫；死亡须关联患者并填死因，缺陷须填诊断）</h3>
      <form class="inline" id="cert-form">
        <select name="cert_type">${Object.entries(CERT_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="name" placeholder="姓名" required>
        <select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="event_date" placeholder="事件日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="detail" placeholder="死因/缺陷诊断" style="min-width:180px">
        <input name="org_id" type="number" placeholder="签发机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID(死亡必填)">
        <button>签发</button></form>
      <p class="msg" id="cert-msg"></p>
      <form class="inline" id="cert-filter">
        <select name="cert_type"><option value="">全部类型</option>${Object.entries(CERT_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <button>筛选</button></form>
      <div id="cert-table"></div></div>
    <div class="panel"><h3>成人健康体检登记（医师/公卫，异常项自动标记并入360档案）</h3>
      <form class="inline" id="chk-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="体检机构ID" required>
        <input name="package_name" placeholder="套餐（默认常规体检）">
        <input name="exam_date" placeholder="体检日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="summary" placeholder="体检结论" style="min-width:160px">
        <input name="abnormal_items" placeholder="异常项（有则填）" style="min-width:160px">
        <button>登记</button></form></div>
    ${abnormal.length ? `<div class="panel"><h3>⚠ 体检异常清单（${abnormal.length}，供慢病筛查建档衔接）</h3>${
      table(["体检ID", "患者", "日期", "异常项"], abnormal, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.exam_date)}</td><td><span class="tag red">${esc(a.abnormal_items)}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>体检记录</h3>${table(["ID", "患者", "套餐", "日期", "结论", "异常"], checkups, (c) =>
      `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${esc(c.package_name)}</td><td>${esc(c.exam_date)}</td>
       <td>${esc(c.summary) || "—"}</td><td>${c.has_abnormal ? `<span class="tag red">${esc(c.abnormal_items)}</span>` : '<span class="tag green">正常</span>'}</td></tr>`)}</div>`;
  await draw();
  $("#cert-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/certs", formJson(e.target, ["org_id", "patient_id"]), "#cert-msg"); };
  $("#cert-filter").onsubmit = async (e) => { e.preventDefault(); await draw(new FormData(e.target).get("cert_type")); };
  $("#chk-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/checkups", formJson(e.target, ["patient_id", "org_id"]), "#cert-msg"); };
}

const KB_CATEGORIES = { drug_policy: "药物政策", clinical_guideline: "临床指南", referral: "转诊知识", regulation: "质量制度规范", tcm_health: "中医养生" };

async function renderKnowledge() {
  $("#page-desc").textContent = "五分类统一知识库：发布（管理层/公卫）、检索、有效期管理与临期提醒";
  const expiring = await api("/api/knowledge/expiring?days=30");
  const canEdit = ["director", "public_health", "admin"].includes(currentRole());
  const catOpts = Object.entries(KB_CATEGORIES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  const draw = async (params = "") => {
    const entries = await api(`/api/knowledge${params}`);
    $("#kb-table").innerHTML = table(["ID", "分类", "标题", "有效期至", "状态", "操作"], entries, (k) =>
      `<tr><td>${k.id}</td><td><span class="tag">${esc(k.category_name)}</span></td><td>${esc(k.title)}</td>
       <td>${esc(k.expire_date) || "长期"}</td>
       <td>${k.expired ? '<span class="tag red">已过期</span>' : '<span class="tag green">有效</span>'}</td>
       <td>${canEdit ? `<button class="btn secondary" data-renew="${k.id}">续期</button>
            <button class="btn danger" data-deact="${k.id}">停用</button>` : "—"}</td></tr>`);
  };
  $("#page-body").innerHTML = `
    ${canEdit ? `<div class="panel"><h3>发布知识条目（管理层/公卫）</h3>
      <form class="inline" id="kb-form">
        <select name="category">${catOpts}</select>
        <input name="title" placeholder="标题" required style="min-width:240px">
        <input name="body" placeholder="正文/摘要" style="min-width:220px">
        <input name="expire_date" placeholder="有效期至 YYYY-MM-DD（可空）">
        <button>发布</button></form><p class="msg" id="kb-msg"></p></div>` : '<p class="msg" id="kb-msg"></p>'}
    ${expiring.length ? `<div class="panel" style="border-left:4px solid #b26a00"><h3>⚠ 30天内到期资料（${expiring.length}）</h3>${
      table(["ID", "分类", "标题", "到期日"], expiring, (k) =>
        `<tr><td>${k.id}</td><td>${KB_CATEGORIES[k.category] || esc(k.category)}</td><td>${esc(k.title)}</td>
         <td><span class="tag orange">${esc(k.expire_date)}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>知识检索</h3>
      <form class="inline" id="kb-search">
        <select name="category"><option value="">全部分类</option>${catOpts}</select>
        <input name="q" placeholder="标题关键字">
        <label style="font-size:13px"><input type="checkbox" name="include_expired"> 含过期</label>
        <button>检索</button></form>
      <div id="kb-table"></div></div>`;
  await draw();
  const kb = $("#kb-form");
  if (kb) kb.onsubmit = (e) => { e.preventDefault(); postAction("/api/knowledge", formJson(e.target), "#kb-msg"); };
  $("#kb-search").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const params = new URLSearchParams();
    if (f.get("category")) params.set("category", f.get("category"));
    if (f.get("q")) params.set("q", f.get("q"));
    if (f.get("include_expired")) params.set("include_expired", "true");
    const qs = params.toString();
    await draw(qs ? `?${qs}` : "");
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.renew) {
        const dateStr = prompt("新有效期 YYYY-MM-DD"); if (!dateStr) return;
        await api(`/api/knowledge/${d.renew}`, { method: "PATCH", body: JSON.stringify({ expire_date: dateStr }) });
        route();
      }
      if (d.deact) {
        await api(`/api/knowledge/${d.deact}`, { method: "PATCH", body: JSON.stringify({ active: false }) });
        route();
      }
    } catch (err) { setMsg("#kb-msg", err.message, false); }
  };
}

/* ---------------- 启动 ---------------- */

function buildNav() {
  $("#nav").innerHTML = PAGES.filter(pageAllowed).map((p) =>
    p.group
      ? `<div class="nav-group">${p.group}</div>`
      : `<a href="#${p.id}" data-page="${p.id}">${p.title}</a>`).join("");
}

function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  buildNav();
  startTodoPolling();
  route();
}

$("#todo-bell").onclick = (e) => {
  if (e.target.closest("#todo-panel")) return;
  $("#todo-panel").classList.toggle("hidden");
};

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/auth/login", { method: "POST", body: JSON.stringify({
      username: $("#login-username").value, password: $("#login-password").value }) });
    token = data.access_token;
    localStorage.setItem("medplat_token", token);
    localStorage.setItem("medplat_role", data.role);
    enterApp();
  } catch (err) { $("#login-error").textContent = err.message; }
};
$("#logout").onclick = logout;
window.addEventListener("hashchange", route);

if (token) enterApp();
else $("#login-view").classList.remove("hidden");
