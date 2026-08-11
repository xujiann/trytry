/* 县域医共体 医生移动工作台（块4）
   待办 / 危急值确认与处置 / 待审检查申请 / 慢病随访录入 / 患者档案速查
   复用居民端 m.css 风格，移动优先布局；令牌存 sessionStorage（关闭页面即失效）。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const TOKEN_KEY = "medplat_doctor_token";
const USER_KEY = "medplat_doctor_user";

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;
  const resp = await fetch(path, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401) { logout(); throw new Error("登录已失效，请重新登录"); }
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

function setMsg(sel, text, ok) {
  const el = $(sel);
  if (!el) return;
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

function kv(k, v) {
  return `<div class="kv"><span class="k">${esc(k)}</span><span>${v}</span></div>`;
}

function card(inner, ops = "") {
  return `<div class="m-card">${inner}${ops ? `<div class="ops">${ops}</div>` : ""}</div>`;
}

/* ---------------- 登录 / 登出 ---------------- */

function showWorkbench(show) {
  $("#login-page").classList.toggle("hidden", show);
  $("#workbench").classList.toggle("hidden", !show);
  $("#tabbar").classList.toggle("hidden", !show);
  $("#btn-logout").classList.toggle("hidden", !show);
}

function logout() {
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  showWorkbench(false);
}

$("#btn-logout").addEventListener("click", logout);

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    const data = await api("/api/auth/login", {
      method: "POST",
      body: JSON.stringify({ username: $("#lg-user").value.trim(), password: $("#lg-pass").value }),
    });
    sessionStorage.setItem(TOKEN_KEY, data.access_token);
    sessionStorage.setItem(USER_KEY, $("#lg-user").value.trim());
    $("#lg-pass").value = "";
    showWorkbench(true);
    switchTab(currentTab());
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});

/* ---------------- 标签页 ---------------- */

const TABS = { todo: loadTodos, critical: loadCritical, exam: loadExams, chronic: loadChronic, patient: loadPatientTab };

function currentTab() {
  const tab = (location.hash || "#todo").replace("#", "");
  return tab in TABS ? tab : "todo";
}

function switchTab(tab) {
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.add("hidden"));
  $(`#tab-${tab}`).classList.remove("hidden");
  document.querySelectorAll(".tab-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  window.scrollTo(0, 0);
  if (token()) TABS[tab]();
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    location.hash = btn.dataset.tab;
    switchTab(btn.dataset.tab);
  });
});

/* ---------------- 待办（复用 /api/todos） ---------------- */

const ROLE_NAMES = { admin: "平台管理员", director: "管理层", doctor: "医师", pharmacist: "药师", public_health: "公卫人员", operator: "经办人员" };

async function loadTodos() {
  const box = $("#todo-list");
  try {
    const data = await api("/api/todos");
    $("#who").innerHTML = `<span>${esc(sessionStorage.getItem(USER_KEY) || "")}</span>
      <span class="role">${esc(ROLE_NAMES[data.role] || data.role)} · 待办 ${data.total}</span>`;
    if (!data.items.length) {
      box.innerHTML = '<p class="empty">当前角色无待办事项</p>';
      return;
    }
    box.innerHTML = data.items.map((item) => {
      const rows = item.list.slice(0, 20).map((row) => card(
        Object.entries(row)
          .filter(([k]) => k !== "id")
          .map(([k, v]) => kv(FIELD_NAMES[k] || k, esc(v)))
          .join("") || kv("编号", esc(row.id))
      )).join("");
      return `<div class="todo-group">
        <div class="head"><span>${esc(item.title)}</span>
          <span class="badge ${item.count ? "warn" : "zero"}">${item.count}</span></div>
        ${rows || '<p class="empty">无</p>'}
      </div>`;
    }).join("");
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

const FIELD_NAMES = {
  diagnosis_name: "诊断", review_comment: "审方意见", center_type: "中心", item_name: "项目",
  status: "状态", conclusion: "结论", critical_status: "危急值状态", request_id: "申请单",
  drug_name: "药品", quantity: "库存", threshold: "阈值", org_id: "机构",
};

/* ---------------- 危急值确认与处置 ---------------- */

const CRITICAL_TAGS = {
  notified: ["待确认", "red"], "": ["待确认", "red"],
  acknowledged: ["已接收，待处置", "orange"], resolved: ["已闭环", "green"],
};

async function loadCritical() {
  const box = $("#critical-list");
  try {
    const reports = await api("/api/exams/critical");
    if (!reports.length) {
      box.innerHTML = '<p class="empty">暂无危急值报告</p>';
      return;
    }
    box.innerHTML = reports.map((r) => {
      const [label, color] = CRITICAL_TAGS[r.critical_status] || [r.critical_status, ""];
      const pending = ["notified", ""].includes(r.critical_status);
      const ops = pending
        ? `<button data-ack="${r.id}">确认接收</button>`
        : r.critical_status === "acknowledged"
          ? `<button data-resolve="${r.id}">处置反馈</button>` : "";
      return card(
        kv("报告编号", esc(r.id)) + kv("申请单", esc(r.request_id)) +
        kv("结论", esc(r.conclusion)) + kv("状态", `<span class="tag ${color}">${esc(label)}</span>`),
        ops + `<button class="ghost" data-trace="${r.id}">处置轨迹</button>`
      );
    }).join("");
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

$("#critical-list").addEventListener("click", async (e) => {
  const { ack, resolve, trace } = e.target.dataset;
  try {
    if (ack) {
      await api(`/api/exams/reports/${ack}/acknowledge`, { method: "POST" });
      setMsg("#critical-msg", "已确认接收，请尽快处置并反馈", true);
      return loadCritical();
    }
    if (resolve) {
      const note = prompt("处置措施（如：已联系患者并调整治疗方案）") || "";
      await api(`/api/exams/reports/${resolve}/resolve`, { method: "POST", body: JSON.stringify({ note }) });
      setMsg("#critical-msg", "处置已反馈，危急值闭环完成", true);
      return loadCritical();
    }
    if (trace) {
      const actions = await api(`/api/exams/reports/${trace}/critical-actions`);
      alert(actions.length
        ? actions.map((a) => `${a.actor}：${a.action}`).join("\n")
        : "暂无处置轨迹");
    }
  } catch (err) {
    setMsg("#critical-msg", err.message, false);
  }
});

/* ---------------- 待审检查申请（领取 / 出报告） ---------------- */

const CENTER_NAMES = { imaging: "影像", ecg: "心电", lab: "检验", pathology: "病理" };
const EXAM_STATUS = { pending: ["待领取", "orange"], diagnosing: ["诊断中", ""] };

async function loadExams() {
  const box = $("#exam-list");
  try {
    const [pending, diagnosing] = await Promise.all([
      api("/api/exams?status=pending"), api("/api/exams?status=diagnosing"),
    ]);
    const rows = [...pending, ...diagnosing];
    if (!rows.length) {
      box.innerHTML = '<p class="empty">暂无待审检查申请</p>';
      return;
    }
    box.innerHTML = rows.map((r) => {
      const [label, color] = EXAM_STATUS[r.status] || [r.status, ""];
      const ops = r.status === "pending"
        ? `<button data-claim="${r.id}">领取</button><button class="ghost" data-report="${r.id}">出报告</button>`
        : `<button data-report="${r.id}">出报告</button>`;
      return card(
        kv("申请单", esc(r.id)) + kv("中心", esc(CENTER_NAMES[r.center_type] || r.center_type)) +
        kv("项目", esc(r.item_name)) + kv("状态", `<span class="tag ${color}">${esc(label)}</span>`),
        ops
      );
    }).join("");
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

$("#exam-list").addEventListener("click", async (e) => {
  const { claim, report } = e.target.dataset;
  try {
    if (claim) {
      await api(`/api/exams/${claim}/claim`, { method: "POST" });
      setMsg("#exam-msg", "已领取，请及时出具报告", true);
      return loadExams();
    }
    if (report) {
      const conclusion = prompt("报告结论");
      if (!conclusion) return;
      const critical = confirm("是否为危急值？（确定=是，将进入危急值闭环）");
      await api(`/api/exams/${report}/report`, {
        method: "POST", body: JSON.stringify({ conclusion, critical }),
      });
      setMsg("#exam-msg", critical ? "报告已出具，危急值已通知申请机构" : "报告已出具", true);
      loadExams();
    }
  } catch (err) {
    setMsg("#exam-msg", err.message, false);
  }
});

/* ---------------- 慢病随访录入（病种目录驱动指标） ---------------- */

let diseaseTypes = [];

async function loadChronic() {
  try {
    const [types, list] = await Promise.all([
      api("/api/chronic/disease-types?active=true"), api("/api/chronic?limit=100"),
    ]);
    diseaseTypes = types;
    const byCode = Object.fromEntries(types.map((t) => [t.code, t]));
    $("#fu-chronic").innerHTML = list.length
      ? list.map((c) => `<option value="${c.id}" data-disease="${esc(c.disease)}">
          档案${c.id} · ${esc((byCode[c.disease] || {}).name || c.disease)} · ${c.level}级</option>`).join("")
      : '<option value="">暂无在管档案</option>';
    renderMetricInputs();
    $("#chronic-list").innerHTML = list.length
      ? `<div class="sec-title">在管名单（${list.length}）</div>` + list.slice(0, 30).map((c) => card(
        kv("档案", esc(c.id)) + kv("病种", esc((byCode[c.disease] || {}).name || c.disease)) +
        kv("分级", `<span class="tag ${c.level === 3 ? "red" : c.level === 2 ? "orange" : "green"}">${c.level} 级</span>`) +
        kv("下次随访", esc(c.next_due || "待安排"))
      )).join("")
      : '<p class="empty">暂无在管慢病档案</p>';
  } catch (err) {
    $("#chronic-list").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

function renderMetricInputs() {
  const opt = $("#fu-chronic").selectedOptions[0];
  const type = diseaseTypes.find((t) => t.code === (opt ? opt.dataset.disease : ""));
  const metrics = ((type || {}).level_rules || {}).metrics || [];
  $("#fu-metrics").innerHTML = metrics.length
    ? `<div class="metric-row">${metrics.map((m) => `
        <label>${esc(m.name)}${m.unit ? `（${esc(m.unit)}）` : ""}
          <input type="number" step="any" data-key="${esc(m.key)}" placeholder="${esc(m.name)}">
        </label>`).join("")}</div>`
    : '<p class="hint">该病种未配置分级指标，可仅登记随访</p>';
}

$("#fu-chronic").addEventListener("change", renderMetricInputs);

$("#fu-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const chronicId = $("#fu-chronic").value;
  if (!chronicId) return setMsg("#fu-msg", "暂无可随访的在管档案", false);
  // 血压血糖走专用列，其余指标进通用 metrics JSON
  const body = { metrics: {}, next_due: $("#fu-next").value.trim() };
  $("#fu-metrics").querySelectorAll("input[data-key]").forEach((input) => {
    if (input.value === "") return;
    const key = input.dataset.key;
    if (["sbp", "dbp", "glucose"].includes(key)) body[key] = Number(input.value);
    else body.metrics[key] = Number(input.value);
  });
  try {
    const result = await api(`/api/chronic/${chronicId}/followups`, { method: "POST", body: JSON.stringify(body) });
    setMsg("#fu-msg",
      `已录入：分级 ${result.level} 级${result.refer_up_suggested ? "，建议上转评估" : ""}，下次随访 ${result.next_due}`,
      !result.refer_up_suggested);
    $("#fu-metrics").querySelectorAll("input").forEach((i) => { i.value = ""; });
    $("#fu-next").value = "";
    loadChronic();
  } catch (err) {
    setMsg("#fu-msg", err.message, false);
  }
});

/* ---------------- 患者档案速查 ---------------- */

function loadPatientTab() { /* 档案按需查询，进入标签页不自动请求 */ }

$("#pt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  setMsg("#pt-msg", "", true);
  try {
    const data = await api(`/api/archive/${encodeURIComponent($("#pt-ehc").value.trim())}`);
    const p = data.patient || {};
    const chronic = (data.chronic_diseases || []).map((c) => card(
      kv("病种", esc(c.disease)) + kv("分级", `${c.level} 级`) + kv("下次随访", esc(c.next_due || "待安排"))
    )).join("");
    const encounters = (data.encounters || []).slice(0, 20).map((en) => card(
      kv("诊断", esc(en.diagnosis_name || "—")) +
      kv("类型", esc(en.encounter_type === "inpatient" ? "住院" : "门诊")) +
      (en.summary ? kv("摘要", esc(en.summary)) : "")
    )).join("");
    const reports = (data.exam_reports || []).slice(0, 20).map((r) => card(
      kv("结论", esc(r.conclusion)) +
      (r.critical ? kv("危急值", '<span class="tag red">是</span>') : "")
    )).join("");
    $("#pt-result").innerHTML = `
      <div class="m-card">${kv("姓名", esc(p.name))}${kv("健康卡号", esc(p.ehc_no))}${kv("性别", esc(p.gender || "—"))}</div>
      <div class="sec-title">慢病在管</div>${chronic || '<p class="empty">无</p>'}
      <div class="sec-title">就诊记录</div>${encounters || '<p class="empty">无</p>'}
      <div class="sec-title">检查检验报告</div>${reports || '<p class="empty">无</p>'}`;
  } catch (err) {
    $("#pt-result").innerHTML = "";
    setMsg("#pt-msg", err.message, false);
  }
});

/* ---------------- 启动 ---------------- */

showWorkbench(Boolean(token()));
switchTab(currentTab());
