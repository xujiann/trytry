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
  $("#app-view").classList.add("hidden");
  $("#login-view").classList.remove("hidden");
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
  { id: "referrals", title: "双向转诊", render: renderReferrals },
  { id: "rx", title: "集中审方", render: renderRx },
  { id: "pharmacy", title: "中心药房", render: renderPharmacy },
  { group: "医防融合" },
  { id: "chronic", title: "慢病管理", render: renderChronic },
  { id: "infectious", title: "传染病预警", render: renderInfectious },
  { group: "便民惠民" },
  { id: "archive", title: "患者360视图", render: renderArchive },
];

const CENTER_NAMES = { imaging: "影像", ecg: "心电", lab: "检验", pathology: "病理" };
const ORG_TYPES = { lead_hospital: "牵头医院", township: "乡镇卫生院", village: "村卫生室", public_health: "公卫机构" };
const LEVELS = { county: "县级", township: "乡级", village: "村级" };
const DISEASES = { hypertension: "高血压", diabetes: "2型糖尿病", copd: "慢阻肺" };
const RX_STATUS = { auto_passed: ["系统审通过", "green"], pending_review: ["待药师审", "orange"], approved: ["药师审通过", "green"], rejected: ["已退回", "red"] };
const EXAM_STATUS = { pending: ["待诊断", "orange"], diagnosing: ["诊断中", ""], reported: ["已报告", "green"], recognized: ["已互认", "green"] };
const REF_STATUS = { pending: ["待接诊", "orange"], accepted: ["已接诊", ""], completed: ["已结案", "green"], rejected: ["已退回", "red"] };

function nav(pageId) {
  location.hash = pageId;
}

async function route() {
  if (!token) return;
  const id = location.hash.replace("#", "") || "dashboard";
  const page = PAGES.find((p) => p.id === id) || PAGES[1];
  document.querySelectorAll("#nav a").forEach((a) =>
    a.classList.toggle("active", a.dataset.page === page.id));
  $("#main").innerHTML = `<h2>${esc(page.title)}</h2><div class="desc" id="page-desc"></div><div id="page-body">加载中…</div>`;
  try { await page.render(); }
  catch (e) { $("#page-body").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; }
}

/* ---------------- 各页面 ---------------- */

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
  $("#page-body").innerHTML =
    `<div class="cards">${cards.map(([label, value, warn]) =>
      `<div class="card"><div class="label">${esc(label)}</div><div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}</div>
     <div class="panel"><h3>慢病分级分组</h3><pre class="json">${esc(JSON.stringify(m.chronic_management.by_level, null, 2))}</pre></div>`;
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
      <div id="patient-table"></div></div>`;
  await draw();
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
    })}</div>`;
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
  $("#page-desc").textContent = "“系统+药师”双重审方，每方必审";
  const [prescriptions, rules] = await Promise.all([api("/api/prescriptions"), api("/api/prescriptions/rules")]);
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
      ${table(["药品编码", "日剂量上限"], rules, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${r.max_daily_dose}${esc(r.dose_unit)}</td></tr>`)}</div>
    <div class="panel"><h3>处方队列</h3>${table(["ID", "患者", "诊断", "状态", "审方意见", "操作"], prescriptions, (p) => {
      const [text, color] = RX_STATUS[p.status] || [p.status, ""];
      const actions = p.status === "pending_review"
        ? `<button class="btn secondary" data-approve="1" data-id="${p.id}">通过</button>
           <button class="btn danger" data-approve="0" data-id="${p.id}">退回</button>` : "—";
      return `<tr><td>${p.id}</td><td>${p.patient_id}</td><td>${esc(p.diagnosis_name)}</td>
        <td><span class="tag ${color}">${text}</span></td><td>${esc(p.review_comment) || "—"}</td><td>${actions}</td></tr>`;
    })}</div>`;
  $("#rx-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const p = await api("/api/prescriptions", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), org_id: Number(f.get("org_id")),
        diagnosis_name: f.get("diagnosis_name"),
        items: [{ drug_code: f.get("drug_code"), drug_name: f.get("drug_name"),
          daily_dose: Number(f.get("daily_dose")), days: Number(f.get("days")) }] }) });
      setMsg("#rx-msg", p.status === "auto_passed" ? "系统审通过" : `转入药师审核：${p.review_comment}`, p.status === "auto_passed");
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
    const { approve, id } = e.target.dataset;
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
  $("#page-desc").textContent = "建档、随访即时智能分级，3级建议上转；膳食运动指导要点自动嵌入";
  const [chronicList, overdue] = await Promise.all([api("/api/chronic"), api("/api/chronic/overdue")]);
  const overdueIds = new Set(overdue.map((c) => c.id));
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
        <input name="next_due" placeholder="下次随访 YYYY-MM-DD">
        <button>提交随访</button>
      </form><p class="msg" id="chronic-msg"></p></div>
    <div class="panel"><h3>在管名单${overdue.length ? `（<span style="color:#c62828">${overdue.length} 人随访超期</span>）` : ""}</h3>
      ${table(["档案ID", "患者", "病种", "分级", "下次随访", "随访状态"], chronicList, (c) =>
        `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${DISEASES[c.disease]}</td>
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
    try {
      const result = await api(`/api/chronic/${f.get("chronic_id")}/followups`, { method: "POST",
        body: JSON.stringify({ sbp: num("sbp"), dbp: num("dbp"), glucose: num("glucose"), next_due: f.get("next_due") }) });
      alert(`分级：${result.level} 级${result.refer_up_suggested ? "（建议上转！）" : ""}\n指导要点：${result.guidance_points}`);
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

/* ---------------- 启动 ---------------- */

function buildNav() {
  $("#nav").innerHTML = PAGES.map((p) =>
    p.group
      ? `<div class="nav-group">${p.group}</div>`
      : `<a href="#${p.id}" data-page="${p.id}">${p.title}</a>`).join("");
}

function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  buildNav();
  route();
}

$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/auth/login", { method: "POST", body: JSON.stringify({
      username: $("#login-username").value, password: $("#login-password").value }) });
    token = data.access_token;
    localStorage.setItem("medplat_token", token);
    enterApp();
  } catch (err) { $("#login-error").textContent = err.message; }
};
$("#logout").onclick = logout;
window.addEventListener("hashchange", route);

if (token) enterApp();
else $("#login-view").classList.remove("hidden");
