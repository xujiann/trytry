/* 县域医共体 居民端移动版 H5

   身份体系：手机号验证码 / 微信网页授权登录 → 实名绑定 → 查档案。
   会话（G3，P1-23 收口）：登录后令牌只存 **HttpOnly Cookie**（JS 读不到、
   XSS 偷不走），本地只存非敏感的双提交 CSRF token（medplat_portal_csrf，
   其在 localStorage 的存在同时充当"已登录"标记）。401 即视为掉线，清本地
   状态并退回登录态（不做静默刷新，居民端会话本来就有 7 天）。
   迁移期兜底：旧版把令牌写 localStorage("medplat_portal_token")，仍保留
   **读取**，让升级前已登录的用户继续以 Header 模式工作；新登录不再写入，
   重新登录即切换 Cookie 模式。 */
"use strict";

const TOKEN_KEY = "medplat_portal_token";
// CSRF token 可入 localStorage：它防"跨站自动携带 Cookie"，不是凭据本身——
// 能读它的脚本已在同源内执行（XSS 场景），CSRF 防线本就不针对那种场景。
const CSRF_KEY = "medplat_portal_csrf";

const token = {
  get: () => localStorage.getItem(TOKEN_KEY) || "",   // 仅迁移兜底，登录不再写入
  clear: () => localStorage.removeItem(TOKEN_KEY),
};

function csrfToken() { return readCookie(CSRF_KEY) || localStorage.getItem(CSRF_KEY) || ""; }
function isAuthed() { return Boolean(token.get()) || Boolean(localStorage.getItem(CSRF_KEY)); }
/** 登录成功：令牌已在 HttpOnly Cookie 里，本地只记 CSRF（兼作登录态标记）。 */
function markLoggedIn() {
  token.clear();
  localStorage.setItem(CSRF_KEY, readCookie(CSRF_KEY));
}
function clearAuth() {
  token.clear();
  localStorage.removeItem(CSRF_KEY);
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const resp = await fetch(path, { ...options, credentials: "same-origin", headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

/** 登录态请求：Cookie 会话自动携带（写请求补双提交 CSRF 头）；
    存量 localStorage 令牌走 Header 兜底。失效时清理本地状态并回到登录页。 */
async function authApi(path, options = {}) {
  if (!isAuthed()) throw new Error("请先登录");
  const t = token.get();
  const auth = {};
  if (t) auth.Authorization = `Bearer ${t}`;
  else {
    const method = (options.method || "GET").toUpperCase();
    if (method !== "GET" && method !== "HEAD") auth["X-CSRF-Token"] = csrfToken();
  }
  try {
    return await api(path, { ...options, headers: { ...auth, ...(options.headers || {}) } });
  } catch (err) {
    if (/401|登录状态无效|请先登录|已退出登录|账户不存在/.test(err.message)) {
      clearAuth();
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
  if (tab === "notify") renderNotifyTab();
  if (tab === "spd") renderSpdTab();
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
    await api("/api/portal/auth/sms/login", {
      method: "POST",
      headers: { "X-Token-Transport": "cookie" },  // 声明 Cookie 会话：令牌进 HttpOnly Cookie
      body: JSON.stringify({ phone: $("#in-phone").value.trim(), code: $("#in-code").value.trim() }),
    });
    markLoggedIn();
    $("#in-code").value = "";
    renderArchiveTab();
    refreshNotifyDot();
  } catch (err) {
    setMsg("#login-msg", err.message, false);
  }
});

/* ---------------- 登录：微信 ---------------- */

async function wechatLogin(code) {
  await api("/api/portal/auth/wechat/login", {
    method: "POST",
    headers: { "X-Token-Transport": "cookie" },  // 声明 Cookie 会话：令牌进 HttpOnly Cookie
    body: JSON.stringify({ code }),
  });
  markLoggedIn();
  renderArchiveTab();
  refreshNotifyDot();
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
  clearAuth();
  viewingPatientId = null;
  renderArchiveTab();
  renderServiceTab();
  renderSurveyTab();
  renderNotifyTab();
  refreshNotifyDot();
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
  if (!isAuthed()) {
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

// 转诊状态文案不再由前端维护：两套子域的状态码同名不同义（平台 accepted="已接收"
// 是对方接了那一次转诊，慢专病 accepted="县级医院已接收"是链路走到了县级），
// 前端各存一张表迟早与后端说法对不上。现统一用聚合接口返回的 status_label。
// 这里只保留"算不算走完了"的判断，用于给状态标上绿/橙。
const REFERRAL_DONE = new Set(["completed", "closed"]);
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
  if (isAuthed()) {
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

const SOURCE_NAMES = { platform: "双向转诊", spd: "慢专病转诊" };

// 两个页面共用一份卡片：此前"我的转诊"与"慢专病转诊"各写各的渲染与各自的文案表，
// 同一张单子在两处读起来不一样。注意 kv() 只转义键、不转义值，值一律先 esc()。
function referralCard(r, opts = {}) {
  const orgs = (r.from_org || r.to_org)
    ? kv("转出", esc(r.from_org || "—")) + kv("转入", esc(r.to_org || "—"))
    : "";
  const detail = r.detail_path
    ? `<button type="button" class="ghost-btn" data-ref-detail="${esc(r.detail_path)}">查看全过程</button>`
    : "";
  return `<div class="m-card">
    ${kv("方向", r.direction === "up" ? "上转" : "下转")}
    ${orgs}
    ${kv("事由", esc(r.reason || "—"))}
    ${kv("状态", `<span class="tag ${REFERRAL_DONE.has(r.status) ? "green" : "orange"}">${esc(r.status_label || r.status)}</span>`)}
    ${kv("日期", esc(r.date))}
    ${opts.showSource ? kv("来源", esc(SOURCE_NAMES[r.source] || r.source)) : ""}
    ${detail}
  </div>`;
}

// "查看全过程"：链接由后端给出（已带 patient_id，代管家属才点得开）
function bindReferralDetails(box) {
  box.querySelectorAll("[data-ref-detail]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      // 单独 catch：这是列表渲染之后才触发的异步，不在 loadService/loadSpd 的
      // try 覆盖范围内。不接住的话 404/403/断网只会是一个"点了没反应"的死按钮。
      try {
        const d = await authApi(btn.dataset.refDetail);
        alert((d.steps || []).map((s) =>
          `${s.created_at.slice(0, 16).replace("T", " ")} ${s.step}${s.opinion ? "：" + s.opinion : ""}`
        ).join("\n") || "暂无环节记录");
      } catch (err) {
        alert(`查看失败：${err.message}`);
      }
    });
  });
}

async function renderReferrals(box) {
  // 走聚合接口：平台转诊与慢专病转诊并成一份，居民不必再猜该点哪个入口
  const rows = await authApi(`/api/portal/me/referrals/all${svcQuery()}`);
  box.innerHTML = rows.length
    ? rows.map((r) => referralCard(r, { showSource: true })).join("")
    : '<p class="empty">暂无转诊记录</p>';
  bindReferralDetails(box);
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
  if (isAuthed()) {
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

/* ---------------- 价格公示（浙#55，免登录） ---------------- */

/* 展开才拉数据：绝大多数人开 App 是来看宣教的，不该为价格表付一次请求。 */
let priceLoaded = false;

async function loadPriceList(keyword = "") {
  const box = $("#price-list");
  box.innerHTML = '<p class="hint">加载中…</p>';
  try {
    const rows = await api(`/api/portal/price-list${
      keyword ? `?keyword=${encodeURIComponent(keyword)}` : ""}`);
    if (!rows.length) {
      box.innerHTML = '<p class="empty">没有匹配的项目</p>';
      return;
    }
    box.innerHTML = rows.slice(0, 200).map((r) => `
      <div class="price-row">
        <span class="n">${esc(r.name)}<span class="c">${esc(r.category_name)}</span>
          ${r.effective_date ? `<span class="d">${esc(r.effective_date)} 起执行</span>` : ""}</span>
        <span class="p">${r.price} 元</span>
      </div>`).join("");
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

$("#price-fold").addEventListener("toggle", () => {
  if ($("#price-fold").open && !priceLoaded) {
    priceLoaded = true;
    loadPriceList();
  }
});

let priceTimer = null;
$("#price-search").addEventListener("input", (e) => {
  // 输入停下来再查：逐字打一次请求，在县域机房的带宽上并不便宜
  clearTimeout(priceTimer);
  priceTimer = setTimeout(() => loadPriceList(e.target.value.trim()), 300);
});

/* ---------------- 站内消息 ---------------- */

const NOTIFY_LABELS = { exam_report: "检查报告", surgery: "手术安排", followup: "随访提醒" };

$("#btn-notify-login").addEventListener("click", () => {
  switchTab("archive");
  history.replaceState(null, "", "#archive");
});

/** 未读红点。未登录时静默跳过——登录页不该因为拉不到消息而报错。 */
async function refreshNotifyDot() {
  const dot = $("#notify-dot");
  if (!isAuthed()) return dot.classList.add("hidden");
  try {
    const { unread } = await authApi("/api/portal/me/notifications/unread-count");
    dot.classList.toggle("hidden", unread === 0);
  } catch (err) {
    dot.classList.add("hidden");
  }
}

async function renderNotifyTab() {
  const box = $("#notify-box");
  const guard = $("#notify-guard");
  if (!isAuthed()) {
    guard.classList.remove("hidden");
    box.innerHTML = "";
    return;
  }
  guard.classList.add("hidden");
  box.innerHTML = '<div class="m-card"><p class="hint">加载中…</p></div>';
  let items;
  try {
    items = await authApi("/api/portal/me/notifications?limit=50");
  } catch (err) {
    box.innerHTML = `<div class="m-card"><p class="msg err">${esc(err.message)}</p></div>`;
    return;
  }
  if (!items.length) {
    box.innerHTML = '<div class="m-card"><p class="empty">暂无消息</p></div>';
    return;
  }
  const unread = items.filter((n) => !n.read).length;
  box.innerHTML = `
    <div class="m-card">
      <p class="hint">共 ${items.length} 条，未读 ${unread} 条；点击任意一条即标记为已读。</p>
      ${items.map((n) => `
        <div class="notify-item ${n.read ? "" : "unread"}" data-nid="${n.id}">
          <div class="t">${esc(n.title)}</div>
          ${n.body ? `<div class="b">${esc(n.body)}</div>` : ""}
          <div class="m">${esc(NOTIFY_LABELS[n.category] || n.category)} · ${
            esc(n.created_at.slice(0, 16).replace("T", " "))}</div>
        </div>`).join("")}
    </div>`;
  box.onclick = async (e) => {
    const row = e.target.closest(".notify-item");
    if (!row || !row.classList.contains("unread")) return;
    row.classList.remove("unread");  // 先反馈再落库，网络慢时不至于点了没反应
    try {
      await authApi(`/api/portal/me/notifications/${row.dataset.nid}/read`, { method: "POST" });
    } catch (err) {
      row.classList.add("unread");
    }
    refreshNotifyDot();
  };
  refreshNotifyDot();
}

/* ---------------- 启动 ---------------- */

const TABS = ["edu", "archive", "service", "spd", "notify", "survey"];

/* 扫码进入：#scale=<qr_token> 直达慢专病自查并预选该量表（P2-3 二维码入口）。
 * 令牌在这里只记下来，预选发生在 renderSpdScreen——量表停用/令牌失效时
 * 页面自然回落到量表列表，码不用重印。 */
let scaleTokenFromQr = "";

(async function start() {
  loadArticles();
  const fromWeChat = await consumeWeChatRedirect();
  const hash = location.hash || "#edu";
  if (hash.startsWith("#scale=")) {
    scaleTokenFromQr = hash.slice("#scale=".length);
    activeSpd = "screen";
    switchTab("spd");
    refreshNotifyDot();
    setInterval(refreshNotifyDot, 300000);
    return;
  }
  const initTab = hash.replace("#", "");
  switchTab(fromWeChat ? "archive" : (TABS.includes(initTab) ? initTab : "edu"));
  refreshNotifyDot();
  // 5 分钟一次即可：居民端不是值班台，红点晚几分钟出现没有代价，
  // 而后台常驻页签每 30 秒打一次接口纯属浪费。
  setInterval(refreshNotifyDot, 300000);
})();


/* ---------------- 慢专病（自主服务与健康管理端） ----------------
 *
 * 与「在线服务」分开一个页签，而不是塞成它的第八个分段：慢专病是**长期**关系
 * （签约、路径、随访、指标趋势），在线服务是**一次性**事务（约号、看账单）。
 * 混在一起，长期管理的入口会被一堆一次性事务淹没。
 */

let activeSpd = "home";

document.querySelectorAll("[data-spd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    activeSpd = btn.dataset.spd;
    document.querySelectorAll("[data-spd]").forEach((b) => b.classList.toggle("active", b === btn));
    loadSpd();
  });
});

const spdLoginBtn = $("#btn-spd-login");
if (spdLoginBtn) {
  spdLoginBtn.addEventListener("click", () => {
    switchTab("archive");
    history.replaceState(null, "", "#archive");
  });
}

async function renderSpdTab() {
  let bound = false;
  if (isAuthed()) {
    try { bound = (await authApi("/api/portal/me")).bound; } catch (err) { bound = false; }
  }
  $("#spd-guard").classList.toggle("hidden", bound);
  $("#spd-body").classList.toggle("hidden", !bound);
  if (bound) await loadSpd();
}

/* 与「我的档案」的成员切换保持一致：为家人查看时带上 patient_id */
function spdQuery(extra) {
  const params = new URLSearchParams(extra || {});
  if (viewingPatientId !== null) params.set("patient_id", viewingPatientId);
  const qs = params.toString();
  return qs ? `?${qs}` : "";
}

const SPD_RISK_TAGS = {
  low: ["低危", "green"], mid: ["中危", "orange"],
  high: ["高危", "red"], very_high: ["极高危", "red"],
};
const SPD_LEVEL_TAGS = { normal: ["正常", "green"], high: ["偏高", "red"], low: ["偏低", "orange"] };

function spdTagOf(map, key) {
  const [text, cls] = map[key] || [key || "—", ""];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

async function loadSpd() {
  const box = $("#spd-result");
  box.innerHTML = '<p class="empty">加载中…</p>';
  try {
    if (activeSpd === "home") return await renderSpdHome(box);
    if (activeSpd === "measure") return await renderSpdMeasure(box);
    if (activeSpd === "task") return await renderSpdTasks(box);
    if (activeSpd === "followup") return await renderSpdFollowups(box);
    if (activeSpd === "plan") return await renderSpdPlans(box);
    if (activeSpd === "referral") return await renderSpdReferrals(box);
    return await renderSpdScreen(box);
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

async function renderSpdHome(box) {
  const home = await authApi(`/api/portal/spd/home${spdQuery()}`);
  if (!home.enrolled) {
    box.innerHTML = `<div class="m-card"><p class="hint">您当前没有签约的慢专病管理。
      可在「自查」中完成高危筛查后申请专病服务。</p></div>`;
    return;
  }
  const metrics = Object.entries(home.latest_metrics || {}).map(([key, m]) => kv(
    { bp_sys: "收缩压", bp_dia: "舒张压", glucose_fasting: "空腹血糖",
      bmi: "体质指数", spo2: "血氧饱和度" }[key] || key,
    `${esc(m.value)}${esc(m.unit)} ${spdTagOf(SPD_LEVEL_TAGS, m.level)}`)).join("");
  const programs = home.programs.map((p) => `<div class="m-card">
    ${kv("管理病种", esc(p.program_name || p.program_code))}
    ${kv("当前阶段", esc(p.stage || "—"))}
    ${kv("风险等级", spdTagOf(SPD_RISK_TAGS, p.risk_level))}
    ${kv("签约团队", esc(p.team_name || "—"))}
    ${kv("下次随访", esc(p.next_followup_at || "—"))}</div>`).join("");
  const packages = home.packages.map((p) => `<div class="m-card">
    ${kv("服务包", esc(p.name))}
    ${kv("完成进度", `${p.used}/${p.total}（${p.progress}%）`)}
    ${kv("有效期至", esc(p.period_end || "—"))}</div>`).join("");
  box.innerHTML = `
    <div class="m-card">
      ${kv("待办任务", home.todo.tasks)}
      ${kv("待随访", home.todo.followups)}
      ${kv("待复诊", home.todo.revisits)}
      ${kv("干预方案", home.todo.interventions)}
      ${kv("未读宣教", home.todo.unread_edu)}
    </div>
    ${metrics ? `<div class="m-card"><h3>最新指标</h3>${metrics}</div>` : ""}
    ${programs}
    ${packages}`;
}

async function renderSpdMeasure(box) {
  const rows = await authApi(`/api/portal/spd/measurements${spdQuery({ limit: 30 })}`);
  const list = rows.map((r) => `<div class="m-card">
    ${kv("项目", esc(r.metric))}
    ${kv("数值", `${esc(r.value)}${esc(r.unit)} ${spdTagOf(SPD_LEVEL_TAGS, r.level)}`)}
    ${kv("来源", r.source === "device" ? "设备采集" : "手工记录")}
    ${kv("时间", esc(r.measured_at.replace("T", " ").slice(0, 16)))}</div>`).join("")
    || '<p class="empty">还没有记录，先添加一条吧</p>';
  box.innerHTML = `
    <form id="spd-measure-form" class="m-card">
      <p class="hint">记录血压、血糖、体重等居家监测数据，系统会按管理目标判定是否达标</p>
      <select id="spd-metric">
        <option value="bp_sys">收缩压(mmHg)</option>
        <option value="bp_dia">舒张压(mmHg)</option>
        <option value="glucose_fasting">空腹血糖(mmol/L)</option>
        <option value="bmi">体质指数</option>
        <option value="spo2">血氧饱和度(%)</option>
      </select>
      <input id="spd-value" type="number" step="any" placeholder="数值" required>
      <input id="spd-program" placeholder="病种编码（可留空）">
      <button type="submit">保存</button>
      <p id="spd-measure-msg" class="msg"></p>
    </form>
    ${list}`;
  $("#spd-measure-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      const body = {
        metric: $("#spd-metric").value, value: Number($("#spd-value").value),
        program_code: $("#spd-program").value || "",
      };
      if (viewingPatientId !== null) body.patient_id = viewingPatientId;
      const r = await authApi("/api/portal/spd/measurements", {
        method: "POST", body: JSON.stringify(body) });
      $("#spd-measure-msg").textContent =
        r.level === "normal" ? "已保存，指标正常" : `已保存，指标${r.level === "high" ? "偏高" : "偏低"}，请关注`;
      await loadSpd();
    } catch (err) { $("#spd-measure-msg").textContent = err.message; }
  });
}

async function renderSpdTasks(box) {
  const rows = await authApi(`/api/portal/spd/tasks${spdQuery()}`);
  box.innerHTML = rows.map((t) => `<div class="m-card">
    ${kv("任务", esc(t.title))}
    ${kv("截止", esc(t.due_date || "—"))}
    ${kv("状态", esc({ pending: "待办", claimed: "待办", doing: "办理中",
      submitted: "已提交待审核", done: "已完成", overdue: "已超期" }[t.status] || t.status))}
    ${t.review_note ? kv("审核意见", esc(t.review_note)) : ""}
    ${["pending", "claimed", "doing", "overdue"].includes(t.status)
      ? `<button type="button" class="ghost-btn" data-spd-task="${t.id}">填报并提交</button>` : ""}
    </div>`).join("") || '<p class="empty">暂无健康任务</p>';
  box.querySelectorAll("[data-spd-task]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const note = prompt("填写完成情况（如：已服药、已测量血压）") || "";
      const body = { result: { note } };
      if (viewingPatientId !== null) body.patient_id = viewingPatientId;
      try {
        await authApi(`/api/portal/spd/tasks/${btn.dataset.spdTask}/submit`, {
          method: "POST", body: JSON.stringify(body) });
        await loadSpd();
      } catch (err) { alert(err.message); }
    });
  });
}

async function renderSpdFollowups(box) {
  const rows = await authApi(`/api/portal/spd/followups${spdQuery()}`);
  box.innerHTML = rows.map((f) => `<div class="m-card">
    ${kv("随访类型", esc({ inpatient: "出院随访", outpatient: "门诊随访",
      surgery: "术后随访", checkup: "体检随访" }[f.scene] || f.scene))}
    ${kv("计划时间", esc(f.planned_at))}
    ${kv("状态", esc({ planned: "待随访", done: "已完成",
      unreachable: "未联系上", removed: "已移除" }[f.status] || f.status))}
    ${f.result ? kv("医生反馈", esc(f.result)) : ""}
    ${f.status === "planned"
      ? `<button type="button" class="ghost-btn" data-spd-self="${f.id}">线上自助随访</button>` : ""}
    </div>`).join("") || '<p class="empty">暂无随访计划</p>';
  box.querySelectorAll("[data-spd-self]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const body = { answers: { recovery: prompt("总体恢复情况（良好/一般/较差）") || "良好" } };
      if (viewingPatientId !== null) body.patient_id = viewingPatientId;
      try {
        const r = await authApi(`/api/portal/spd/followups/${btn.dataset.spdSelf}/self-answer`, {
          method: "POST", body: JSON.stringify(body) });
        alert(r.abnormal_level && r.abnormal_level !== "none"
          ? `已提交。系统判定为${r.abnormal_level}异常，${r.action || "医护将尽快联系您"}`
          : "已提交，感谢配合");
        await loadSpd();
      } catch (err) { alert(err.message); }
    });
  });
}

async function renderSpdPlans(box) {
  const rows = await authApi(`/api/portal/spd/interventions${spdQuery()}`);
  box.innerHTML = rows.map((p) => `<div class="m-card">
    ${kv("干预目标", esc(p.goal || "—"))}
    ${kv("方案内容", esc(p.content))}
    ${p.measures ? kv("具体措施", esc(p.measures)) : ""}
    ${kv("执行频次", esc(p.frequency || "—"))}
    ${kv("下次执行", esc(p.next_at || "—"))}
    ${p.read ? "" : `<button type="button" class="ghost-btn" data-spd-read="${p.id}">标记已读并反馈</button>`}
    </div>`).join("") || '<p class="empty">暂无干预方案</p>';
  box.querySelectorAll("[data-spd-read]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const body = { feedback: prompt("执行情况反馈（可留空）") || "" };
      if (viewingPatientId !== null) body.patient_id = viewingPatientId;
      try {
        await authApi(`/api/portal/spd/interventions/${btn.dataset.spdRead}/feedback`, {
          method: "POST", body: JSON.stringify(body) });
        await loadSpd();
      } catch (err) { alert(err.message); }
    });
  });
}

// 慢专病页是主列表的**作用域视图**：同样走聚合接口，用 source 参数收窄到本子域。
// 这样文案、状态与排序与"我的转诊"天生一致——不是两份数据，是同一份的一段。
// 收窄交给服务端做：条数上限是合并之后才截的，客户端自己 filter 会在平台转诊
// 足够多且更新时把慢专病的单子整段挤掉，页面显示"暂无"而其实有在办的。
async function renderSpdReferrals(box) {
  const rows = await authApi(`/api/portal/me/referrals/all${spdQuery({ source: "spd" })}`);
  box.innerHTML = rows.length
    ? rows.map((r) => referralCard(r)).join("")
    : '<p class="empty">暂无转诊记录</p>';
  bindReferralDetails(box);
}

async function renderSpdScreen(box) {
  const scales = await authApi("/api/portal/spd/scales");
  if (!scales.length) {
    box.innerHTML = '<p class="empty">暂无可用的自查问卷</p>';
    return;
  }
  const applies = await authApi(`/api/portal/spd/service-applies${spdQuery()}`);
  box.innerHTML = `
    <div class="m-card">
      <p class="hint">完成高危自查后可申请专病管理服务，由基层医生复核后纳入管理</p>
      <select id="spd-scale">${scales.map((s) =>
        `<option value="${esc(s.code)}" data-program="${esc(s.program_code)}">${esc(s.name)}</option>`).join("")}</select>
      <div id="spd-scale-items"></div>
      <button type="button" id="spd-screen-submit">提交自查</button>
      <p id="spd-screen-msg" class="msg"></p>
    </div>
    ${applies.map((a) => `<div class="m-card">
      ${kv("申请病种", esc(a.program_code))}
      ${kv("状态", esc({ pending: "待受理", accepted: "已受理", rejected: "未通过" }[a.status] || a.status))}
      ${a.handle_note ? kv("处理意见", esc(a.handle_note)) : ""}</div>`).join("")}`;

  const drawItems = () => {
    const scale = scales.find((s) => s.code === $("#spd-scale").value);
    $("#spd-scale-items").innerHTML = (scale.items || []).map((item) => `
      <div class="kv"><span class="k">${esc(item.title)}</span>
        <select data-q="${esc(item.key)}">${(item.options || []).map((o) =>
          `<option value="${esc(o.label)}">${esc(o.label)}</option>`).join("")}</select></div>`).join("");
  };
  if (scaleTokenFromQr) {
    // 扫码进来的：按令牌预选对应量表；令牌失效就静默回落到列表首项
    try {
      const byToken = await api(`/api/portal/spd/scales/by-token/${scaleTokenFromQr}`);
      if (scales.some((s) => s.code === byToken.code)) $("#spd-scale").value = byToken.code;
    } catch (err) { /* 码失效不打断自查流程 */ }
    scaleTokenFromQr = "";
  }
  drawItems();
  $("#spd-scale").addEventListener("change", drawItems);
  $("#spd-screen-submit").addEventListener("click", async () => {
    const scale = scales.find((s) => s.code === $("#spd-scale").value);
    const answers = {};
    document.querySelectorAll("[data-q]").forEach((sel) => { answers[sel.dataset.q] = sel.value; });
    const body = { program_code: scale.program_code, scale_code: scale.code, answers };
    if (viewingPatientId !== null) body.patient_id = viewingPatientId;
    try {
      const r = await authApi("/api/portal/spd/screenings", {
        method: "POST", body: JSON.stringify(body) });
      $("#spd-screen-msg").textContent =
        `风险等级：${{ low: "低危", mid: "中危", high: "高危" }[r.risk_level] || r.risk_level}。${r.advice}`;
      if (r.can_apply && confirm("检测到中高风险，是否申请专病管理服务？")) {
        const applyBody = { program_code: scale.program_code, screening_id: r.id };
        if (viewingPatientId !== null) applyBody.patient_id = viewingPatientId;
        await authApi("/api/portal/spd/service-applies", {
          method: "POST", body: JSON.stringify(applyBody) });
        await loadSpd();
      }
    } catch (err) { $("#spd-screen-msg").textContent = err.message; }
  });
}
