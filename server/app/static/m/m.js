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
  renderArchiveTab();
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
  await loadArchive();
}

async function loadArchive() {
  const box = $("#archive-result");
  box.innerHTML = '<p class="empty">加载中…</p>';
  try {
    const data = await authApi("/api/portal/me/archive");
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
  switchTab(fromWeChat ? "archive" : (["edu", "archive", "survey"].includes(initTab) ? initTab : "edu"));
})();
