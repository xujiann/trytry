/* 县域医共体 居民端移动版 H5

   身份体系：手机号验证码 / 微信网页授权登录 → 实名绑定 → 查档案。
   令牌存 localStorage，所有 /api/portal/me/* 请求自动带上；401 即视为掉线，
   清本地令牌并退回登录态（不做静默刷新，居民端会话本来就有 7 天）。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const TOKEN_KEY = "medplat_portal_token";

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

const token = {
  get: () => localStorage.getItem(TOKEN_KEY) || "",
  set: (v) => localStorage.setItem(TOKEN_KEY, v),
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const resp = await fetch(path, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

/** 带令牌的请求；令牌失效时清理本地状态并回到登录页。 */
async function authApi(path, options = {}) {
  const t = token.get();
  if (!t) throw new Error("请先登录");
  try {
    return await api(path, { ...options, headers: { Authorization: `Bearer ${t}`, ...(options.headers || {}) } });
  } catch (err) {
    if (/401|登录状态无效|请先登录|已退出登录|账户不存在/.test(err.message)) {
      token.clear();
      renderArchiveTab();
    }
    throw err;
  }
}

/* ---------------- 标签页切换 ---------------- */

function switchTab(tab) {
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.add("hidden"));
  $(`#tab-${tab}`).classList.remove("hidden");
  document.querySelectorAll(".tab-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  window.scrollTo(0, 0);
  if (tab === "archive") renderArchiveTab();
  if (tab === "service") renderServiceTab();
  if (tab === "survey") renderSurveyTab();
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    switchTab(btn.dataset.tab);
    history.replaceState(null, "", `#${btn.dataset.tab}`);
  });
});

/* ---------------- 健康宣教 ---------------- */

async function loadArticles() {
  const box = $("#edu-list");
  try {
    const items = await api("/api/portal/health-articles");
    if (!items.length) {
      box.innerHTML = '<p class="empty">暂无宣教内容</p>';
      return;
    }
    box.innerHTML = items.map((a) => `
      <div class="article">
        <h3>${esc(a.title)}</h3>
        <span class="cat">${esc(a.category || "健康科普")}</span>
        <p class="clamp">${esc(a.content)}</p>
        <a class="more">展开全文</a>
      </div>`).join("");
    box.querySelectorAll(".more").forEach((link) => {
      link.addEventListener("click", () => {
        const p = link.previousElementSibling;
        const expanded = !p.classList.contains("clamp");
        p.classList.toggle("clamp", expanded);
        link.textContent = expanded ? "展开全文" : "收起";
      });
    });
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

/* ---------------- 登录：手机号验证码 ---------------- */

let cooldownTimer = null;

function startCooldown(seconds) {
  const btn = $("#btn-send-code");
  let left = seconds;
  btn.disabled = true;
  clearInterval(cooldownTimer);
  const tick = () => {
    btn.textContent = left > 0 ? `${left}秒后重发` : "获取验证码";
    if (left <= 0) {
      btn.disabled = false;
      clearInterval(cooldownTimer);
    }
    left -= 1;
  };
  tick();
  cooldownTimer = setInterval(tick, 1000);
}

function setMsg(id, text, ok) {
  const el = $(id);
  el.textContent = text;
  el.className = `msg ${ok ? "ok" : "err"}`;
}

$("#btn-send-code").addEventListener("click", async () => {
  const phone = $("#in-phone").value.trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) {
    setMsg("#login-msg", "请输入正确的11位手机号", false);
    return;
  }
  try {
    const data = await api("/api/portal/auth/sms/code", {
      method: "POST",
      body: JSON.stringify({ phone, purpose: "login" }),
    });
    startCooldown(data.cooldown_seconds || 60);
    // debug_code 只在 console 短信通道 + 非生产环境下返回，便于演示与联调
    if (data.debug_code) {
      $("#in-code").value = data.debug_code;
      setMsg("#login-msg", `演示环境验证码：${data.debug_code}（已自动填入）`, true);
    } else {
      setMsg("#login-msg", "验证码已发送，请查收短信", true);
    }
  } catch (err) {
    setMsg("#login-msg", err.message, false);
  }
});

$("#sms-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const data = await api("/api/portal/auth/sms/login", {
      method: "POST",
      body: JSON.stringify({ phone: $("#in-phone").value.trim(), code: $("#in-code").value.trim() }),
    });
    token.set(data.access_token);
    $("#in-code").value = "";
    renderArchiveTab();
  } catch (err) {
    setMsg("#login-msg", err.message, false);
  }
});

/* ---------------- 登录：微信 ---------------- */

async function wechatLogin(code) {
  const data = await api("/api/portal/auth/wechat/login", {
    method: "POST",
    body: JSON.stringify({ code }),
  });
  token.set(data.access_token);
  renderArchiveTab();
}

$("#btn-wechat").addEventListener("click", async () => {
  try {
    const auth = await api("/api/portal/auth/wechat/authorize");
    if (auth.mock_code) {
      // mock 通道：无需跳转微信，就地完成一次授权（演示站/本地联调）
      await wechatLogin(auth.mock_code);
      return;
    }
    location.href = auth.authorize_url;
  } catch (err) {
    setMsg("#login-msg", err.message, false);
  }
});

/** 微信授权回跳：地址栏带 code 时自动完成登录，随后清掉 query 防重复提交。 */
async function consumeWeChatRedirect() {
  const params = new URLSearchParams(location.search);
  const code = params.get("code");
  if (!code) return false;
  history.replaceState(null, "", location.pathname + location.hash);
  try {
    await wechatLogin(code);
    return true;
  } catch (err) {
    setMsg("#login-msg", err.message, false);
    return false;
  }
}

/* ---------------- 实名绑定 ---------------- */

$("#bind-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await authApi("/api/portal/auth/realname", {
      method: "POST",
      body: JSON.stringify({ name: $("#in-name").value.trim(), id_card: $("#in-idcard").value.trim() }),
    });
    $("#in-idcard").value = "";
    renderArchiveTab();
  } catch (err) {
    setMsg("#bind-msg", err.message, false);
  }
});

$("#btn-logout").addEventListener("click", async () => {
  try {
    await authApi("/api/portal/auth/logout", { method: "POST" });
  } catch (err) {
    // 令牌本就失效时忽略，本地照样退出
  }
  token.clear();
  viewingPatientId = null;
  renderArchiveTab();
  renderServiceTab();
  renderSurveyTab();
});

/* ---------------- 我的档案 ---------------- */

const CHRONIC_NAMES = { hypertension: "高血压", diabetes: "2型糖尿病", copd: "慢阻肺" };
const LEVEL_TAGS = { 1: ["控制良好", "green"], 2: ["需干预", "orange"], 3: ["高危", "red"] };

function kv(k, v) {
  return `<div class="kv"><span class="k">${esc(k)}</span><span>${v}</span></div>`;
}

function showPane(name) {
  ["login", "bind", "archive"].forEach((p) =>
    $(`#pane-${p}`).classList.toggle("hidden", p !== name));
  $("#btn-logout").classList.toggle("hidden", name === "login");
}

/** 依据登录/绑定状态渲染「我的档案」页，是该页唯一的状态入口。 */
async function renderArchiveTab() {
  if (!token.get()) {
    showPane("login");
    return;
  }
  let me;
  try {
    me = await authApi("/api/portal/me");
  } catch (err) {
    showPane("login");
    return;
  }
  if (!me.bound) {
    showPane("bind");
    setMsg("#bind-msg", "", true);
    return;
  }
  showPane("archive");
  $("#account-bar").innerHTML = `
    <span class="who">${esc(me.name)}</span>
    <span class="sub">健康卡号 ${esc(me.ehc_no)}${me.phone ? " · " + esc(me.phone) : ""}</span>`;
  await renderFamily();
  await loadArchive();
}

/* ---------------- 家庭成员代管 ---------------- */

// 当前正在查看谁的档案：null=本人，否则为代管成员的 patient_id
let viewingPatientId = null;

const RELATION_NAMES = { self: "本人", child: "子女", parent: "父母", spouse: "配偶", other: "家人" };

async function renderFamily() {
  const box = $("#family-switch");
  let members = [];
  try {
    members = await authApi("/api/portal/me/family");
  } catch (err) {
    box.innerHTML = "";
    return;
  }
  // 代管成员被解除后，当前查看对象可能已失效，回落到本人
  if (viewingPatientId !== null && !members.some((m) => m.patient_id === viewingPatientId)) {
    viewingPatientId = null;
  }
  box.innerHTML = members.map((m) => {
    const active = (viewingPatientId === null && m.is_self) || viewingPatientId === m.patient_id;
    const del = m.is_self ? "" : `<i class="x" data-member="${m.member_id}">×</i>`;
    return `<span class="chip${active ? " on" : ""}" data-pid="${m.patient_id}" data-self="${m.is_self}">
      ${esc(m.name)}<em>${esc(RELATION_NAMES[m.relation] || m.relation)}</em>${del}</span>`;
  }).join("");
  box.querySelectorAll(".chip").forEach((chip) => {
    chip.addEventListener("click", async (e) => {
      if (e.target.classList.contains("x")) {
        if (!confirm("解除该成员的代管关系？")) return;
        await authApi(`/api/portal/me/family/${e.target.dataset.member}`, { method: "DELETE" });
        await renderFamily();
        await loadArchive();
        return;
      }
      viewingPatientId = chip.dataset.self === "true" ? null : Number(chip.dataset.pid);
      await renderFamily();
      await loadArchive();
    });
  });
}

/** 代管成员自己留了手机号时后端返 428，此时展开验证码输入行。 */
$("#fm-send-code").addEventListener("click", async () => {
  const phone = prompt("请输入该成员在档案中登记的手机号");
  if (!phone) return;
  try {
    const data = await api("/api/portal/auth/sms/code", {
      method: "POST",
      body: JSON.stringify({ phone: phone.trim(), purpose: "bind" }),
    });
    if (data.debug_code) $("#fm-code").value = data.debug_code;
    setMsg("#family-msg", data.debug_code ? `演示环境验证码：${data.debug_code}` : "验证码已发送", true);
  } catch (err) {
    setMsg("#family-msg", err.message, false);
  }
});

$("#family-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await authApi("/api/portal/me/family", {
      method: "POST",
      body: JSON.stringify({
        name: $("#fm-name").value.trim(),
        id_card: $("#fm-idcard").value.trim(),
        relation: $("#fm-relation").value,
        code: $("#fm-code").value.trim(),
      }),
    });
    $("#fm-name").value = "";
    $("#fm-idcard").value = "";
    $("#fm-code").value = "";
    $("#fm-code-row").classList.add("hidden");
    $("#family-add").open = false;
    setMsg("#family-msg", "", true);
    await renderFamily();
  } catch (err) {
    // 428：该成员自己留了手机号，需要那个号码的验证码
    if (/登记手机号/.test(err.message)) $("#fm-code-row").classList.remove("hidden");
    setMsg("#family-msg", err.message, false);
  }
});

async function loadArchive() {
  const box = $("#archive-result");
  box.innerHTML = '<p class="empty">加载中…</p>';
  const query = viewingPatientId === null ? "" : `?patient_id=${viewingPatientId}`;
  try {
    const data = await authApi(`/api/portal/me/archive${query}`);
    const chronic = data.chronic_care.map((c) => {
      const [label, color] = LEVEL_TAGS[c.level] || ["未分级", ""];
      return `<div class="m-card">
        ${kv("病种", esc(CHRONIC_NAMES[c.disease] || c.disease))}
        ${kv("分级", `<span class="tag ${color}">${esc(label)}</span>`)}
        ${kv("下次随访", esc(c.next_followup_due || "待安排"))}
        ${c.guidance_points ? kv("指导要点", esc(c.guidance_points)) : ""}
      </div>`;
    }).join("");
    const encounters = data.encounters.map((en) => `<div class="m-card">
      ${kv("诊断", esc(en.diagnosis_name || "—"))}
      ${kv("类型", esc(en.encounter_type === "inpatient" ? "住院" : "门诊"))}
      ${en.summary ? kv("摘要", esc(en.summary)) : ""}
    </div>`).join("");
    const reports = data.exam_reports.map((r) => `<div class="m-card">
      ${kv("结论", esc(r.conclusion))}
      ${r.critical ? kv("危急值", '<span class="tag red">是，请尽快就医复诊</span>') : ""}
    </div>`).join("");
    box.innerHTML = `
      <div class="sec-title">慢病管理（${data.chronic_care.length}）</div>
      ${chronic || '<p class="empty">无慢病在管记录</p>'}
      <div class="sec-title">就诊记录（${data.encounters.length}）</div>
      ${encounters || '<p class="empty">无就诊记录</p>'}
      <div class="sec-title">检查检验报告（${data.exam_reports.length}）</div>
      ${reports || '<p class="empty">无报告</p>'}`;
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

/* ---------------- 在线服务：预约 / 签约 / 账单 / 转诊 ---------------- */

let activeService = "appointment";

const REFERRAL_STATUS = { pending: "待接收", accepted: "已接收", completed: "已完成", rejected: "已退回" };
const APPT_STATUS = { booked: "待就诊", fulfilled: "已就诊", cancelled: "已取消" };
const PACKAGES = { basic: "基础包", standard: "标准包", premium: "个性包" };
const SERVICE_TYPES = { visit: "上门服务", consult: "健康咨询", followup: "随访", referral: "转诊协助" };

document.querySelectorAll(".seg-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    activeService = btn.dataset.svc;
    document.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
    loadService();
  });
});

$("#btn-service-login").addEventListener("click", () => {
  switchTab("archive");
  history.replaceState(null, "", "#archive");
});

async function renderServiceTab() {
  let bound = false;
  if (token.get()) {
    try {
      bound = (await authApi("/api/portal/me")).bound;
    } catch (err) {
      bound = false;
    }
  }
  $("#service-guard").classList.toggle("hidden", bound);
  $("#service-body").classList.toggle("hidden", !bound);
  if (bound) await loadService();
}

/** 查询串：非本人视角时带上 patient_id，与「我的档案」的成员切换保持一致。 */
function svcQuery() {
  return viewingPatientId === null ? "" : `?patient_id=${viewingPatientId}`;
}

async function loadService() {
  const box = $("#service-result");
  box.innerHTML = '<p class="empty">加载中…</p>';
  try {
    if (activeService === "appointment") return await renderAppointments(box);
    if (activeService === "contract") return await renderContracts(box);
    if (activeService === "inpatient") return await renderInpatient(box);
    if (activeService === "surgery") return await renderSurgeries(box);
    if (activeService === "bill") return await renderBills(box);
    return await renderReferrals(box);
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

async function renderAppointments(box) {
  const [mine, slots] = await Promise.all([
    authApi("/api/portal/me/appointments"),
    authApi("/api/portal/me/slots"),
  ]);
  const list = mine.map((a) => `<div class="m-card">
    ${kv("就诊人", esc(a.patient_name))}
    ${kv("机构", esc(a.org_name))}
    ${kv("资源", esc(a.resource_name))}
    ${kv("时间", `${esc(a.slot_date)} ${esc(a.slot_time)}`)}
    ${kv("状态", `<span class="tag ${a.status === "booked" ? "green" : ""}">${esc(APPT_STATUS[a.status] || a.status)}</span>`)}
    ${a.status === "booked" ? `<button class="cancel-appt" data-id="${a.id}">取消预约</button>` : ""}
  </div>`).join("");
  const available = slots.map((s) => `<div class="m-card">
    ${kv("机构", esc(s.org_name))}
    ${kv("资源", esc(s.resource_name))}
    ${kv("时间", `${esc(s.slot_date)} ${esc(s.slot_time)}`)}
    ${kv("余号", String(s.remaining))}
    <button class="book-slot" data-id="${s.id}">为${viewingPatientId === null ? "本人" : "该成员"}预约</button>
  </div>`).join("");
  box.innerHTML = `
    <div class="sec-title">我的预约（${mine.length}）</div>
    ${list || '<p class="empty">暂无预约</p>'}
    <div class="sec-title">可约号源（${slots.length}）</div>
    ${available || '<p class="empty">暂无可约号源</p>'}`;
  box.querySelectorAll(".book-slot").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await authApi("/api/portal/me/appointments", {
          method: "POST",
          body: JSON.stringify({ slot_id: Number(btn.dataset.id), patient_id: viewingPatientId }),
        });
        await loadService();
      } catch (err) {
        btn.disabled = false;
        alert(err.message);
      }
    });
  });
  box.querySelectorAll(".cancel-appt").forEach((btn) => {
    btn.addEventListener("click", async () => {
      if (!confirm("确认取消该预约？")) return;
      try {
        await authApi(`/api/portal/me/appointments/${btn.dataset.id}/cancel`, { method: "POST" });
        await loadService();
      } catch (err) {
        alert(err.message);
      }
    });
  });
}

async function renderContracts(box) {
  const rows = await authApi(`/api/portal/me/contract${svcQuery()}`);
  box.innerHTML = rows.length ? rows.map((c) => `<div class="m-card">
    ${kv("签约机构", esc(c.org_name))}
    ${kv("家庭医生", esc(c.doctor_name))}
    ${kv("服务包", esc(PACKAGES[c.package] || c.package))}
    ${kv("签约日期", esc(c.signed_date || "—"))}
    ${kv("状态", `<span class="tag ${c.status === "active" ? "green" : ""}">${c.status === "active" ? "履约中" : "已解约"}</span>`)}
    ${c.services.length ? `<div class="sec-title">履约记录（${c.services.length}）</div>` +
      c.services.map((s) => kv(esc(SERVICE_TYPES[s.service_type] || s.service_type),
        `${esc(s.note || "—")}<br><small>${esc(s.date)}</small>`)).join("") : ""}
  </div>`).join("") : '<p class="empty">暂无签约记录</p>';
}

const SURGERY_STATUS_TEXT = {
  requested: ["待审批", "orange"], approved: ["已审批，待排期", "orange"],
  scheduled: ["已排期", "green"], completed: ["已完成", ""], cancelled: ["已取消", ""],
};
const URGENCY_TEXT = { elective: "择期", urgent: "限期", emergency: "急诊" };
const CATEGORY_TEXT = { bed: "床位费", drug: "药费", exam: "检查费", treat: "治疗费",
  material: "材料费", other: "其他" };

async function renderInpatient(box) {
  const rows = await authApi(`/api/portal/me/admissions${svcQuery()}`);
  if (!rows.length) { box.innerHTML = '<p class="empty">暂无住院记录</p>'; return; }
  box.innerHTML = rows.map((a) => `<div class="m-card">
    ${kv("医院", esc(a.org_name))}
    ${kv("科室床位", `${esc(a.ward_name)} ${esc(a.bed_no)}`)}
    ${kv("诊断", esc(a.diagnosis_name || "—"))}
    ${kv("主管医师", esc(a.doctor_name || "—"))}
    ${kv("入院日期", esc(a.admitted_date))}
    ${a.discharged_date ? kv("出院日期", esc(a.discharged_date)) : ""}
    ${kv("住院天数", `${a.days} 天`)}
    ${kv("状态", `<span class="tag ${a.status === "admitted" ? "orange" : "green"}">${
      a.status === "admitted" ? "在院" : "已出院"}</span>`)}
    ${kv("费用", a.settled
      ? '<span class="tag green">已结清</span>'
      : '<span class="tag orange">有未结清费用</span>')}
    <button class="bill-detail" data-adm="${a.id}">查看费用清单</button>
  </div>`).join("") + '<div id="adm-bill"></div>';
  box.querySelectorAll(".bill-detail").forEach((btn) => {
    btn.addEventListener("click", async () => {
      try {
        const bill = await authApi(`/api/portal/me/admissions/${btn.dataset.adm}/bill`);
        const cats = Object.entries(bill.by_category);
        $("#adm-bill").innerHTML = `
          <div class="sec-title">费用清单（住院号 ${bill.admission_id}）</div>
          <div class="m-card">
            ${kv("费用合计", `<b>¥${bill.total_amount.toFixed(2)}</b>`)}
            ${cats.map(([k, v]) => kv(CATEGORY_TEXT[k] || k, `¥${v.toFixed(2)}`)).join("")}
          </div>
          ${bill.settlements.length ? `<div class="m-card">
            ${bill.settlements.map((s) => `${kv("结算日期", esc(s.date))}
              ${kv("总额", `¥${s.total_amount.toFixed(2)}`)}
              ${kv("医保支付", `¥${s.insurance_pay.toFixed(2)}`)}
              ${kv("个人自付", `<b>¥${s.self_pay.toFixed(2)}</b>`)}`).join("<hr>")}
          </div>` : '<p class="empty">尚未结算</p>'}
          <div class="sec-title">明细（${bill.items.length}）</div>
          ${bill.items.map((i) => `<div class="m-card">
            ${kv(esc(i.item_name), `¥${i.amount.toFixed(2)}`)}
            ${kv("单价×数量", `¥${i.unit_price.toFixed(2)} × ${i.quantity}`)}
            ${kv("日期", esc(i.date))}
          </div>`).join("")}`;
        $("#adm-bill").scrollIntoView({ behavior: "smooth", block: "start" });
      } catch (err) { alert(err.message); }
    });
  });
}

async function renderSurgeries(box) {
  const rows = await authApi(`/api/portal/me/surgeries${svcQuery()}`);
  box.innerHTML = rows.length ? rows.map((s) => {
    const [text, color] = SURGERY_STATUS_TEXT[s.status] || [s.status, ""];
    return `<div class="m-card">
      ${kv("手术名称", esc(s.surgery_name))}
      ${kv("医院", esc(s.org_name))}
      ${kv("术者", esc(s.surgeon_name || "—"))}
      ${kv("类别", esc(URGENCY_TEXT[s.urgency] || s.urgency))}
      ${kv("状态", `<span class="tag ${color}">${text}</span>`)}
      ${s.scheduled_date
        ? kv("安排时间", `${esc(s.scheduled_date)} ${esc(s.scheduled_time)}`) +
          kv("手术间", esc(s.room_name))
        : kv("计划日期", esc(s.planned_date || "待安排"))}
    </div>`;
  }).join("") + '<p class="hint tips">手术记录等专业文书请向主管医师当面了解。</p>'
    : '<p class="empty">暂无手术安排</p>';
}

async function renderBills(box) {
  const rows = await authApi(`/api/portal/me/bills${svcQuery()}`);
  box.innerHTML = rows.length ? rows.map((b) => `<div class="m-card">
    ${kv("机构", esc(b.org_name))}
    ${kv("类型", b.bill_type === "inpatient" ? "住院结算" : "门诊结算")}
    ${kv("总金额", `¥${b.total_amount.toFixed(2)}`)}
    ${kv("医保支付", `¥${b.insurance_pay.toFixed(2)}`)}
    ${kv("个人自付", `<b>¥${b.self_pay.toFixed(2)}</b>`)}
    ${kv("支付状态", `<span class="tag ${b.paid ? "green" : "orange"}">${b.paid ? "已支付" : "待支付"}</span>`)}
    ${kv("日期", esc(b.date))}
  </div>`).join("") : '<p class="empty">暂无账单</p>';
}

async function renderReferrals(box) {
  const rows = await authApi(`/api/portal/me/referrals${svcQuery()}`);
  box.innerHTML = rows.length ? rows.map((r) => `<div class="m-card">
    ${kv("方向", r.direction === "up" ? "上转" : "下转")}
    ${kv("转出", esc(r.from_org))}
    ${kv("转入", esc(r.to_org))}
    ${kv("事由", esc(r.reason || "—"))}
    ${kv("状态", `<span class="tag ${r.status === "completed" ? "green" : "orange"}">${esc(REFERRAL_STATUS[r.status] || r.status)}</span>`)}
    ${kv("日期", esc(r.date))}
  </div>`).join("") : '<p class="empty">暂无转诊记录</p>';
}

/* ---------------- 满意度评价 ---------------- */

const starBox = $("#sv-stars");
function paintStars(score) {
  starBox.dataset.score = score;
  starBox.querySelectorAll("span").forEach((s) =>
    s.classList.toggle("on", Number(s.dataset.v) <= score));
}
starBox.addEventListener("click", (e) => {
  if (e.target.dataset.v) paintStars(Number(e.target.dataset.v));
});
paintStars(5);

$("#btn-goto-login").addEventListener("click", () => {
  switchTab("archive");
  history.replaceState(null, "", "#archive");
});

async function renderSurveyTab() {
  let bound = false;
  if (token.get()) {
    try {
      bound = (await authApi("/api/portal/me")).bound;
    } catch (err) {
      bound = false;
    }
  }
  $("#survey-guard").classList.toggle("hidden", bound);
  $("#survey-form").classList.toggle("hidden", !bound);
}

$("#survey-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await authApi("/api/portal/me/surveys", {
      method: "POST",
      body: JSON.stringify({
        target_type: $("#sv-type").value,
        score: Number(starBox.dataset.score),
        comment: $("#sv-comment").value.trim(),
      }),
    });
    setMsg("#survey-msg", "评价已提交，感谢您的反馈！", true);
    $("#sv-comment").value = "";
  } catch (err) {
    setMsg("#survey-msg", err.message, false);
  }
});

/* ---------------- 启动 ---------------- */

(async function start() {
  loadArticles();
  const fromWeChat = await consumeWeChatRedirect();
  const initTab = (location.hash || "#edu").replace("#", "");
  switchTab(fromWeChat ? "archive" : (["edu", "archive", "service", "survey"].includes(initTab) ? initTab : "edu"));
})();
