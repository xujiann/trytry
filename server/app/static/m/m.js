/* 县域医共体 居民端移动版 H5 */
"use strict";

const $ = (sel) => document.querySelector(sel);

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  const resp = await fetch(path, { ...options, headers });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

/* ---------------- 标签页切换 ---------------- */

function switchTab(tab) {
  document.querySelectorAll(".tab-page").forEach((p) => p.classList.add("hidden"));
  $(`#tab-${tab}`).classList.remove("hidden");
  document.querySelectorAll(".tab-btn").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === tab));
  window.scrollTo(0, 0);
}

document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", (e) => {
    e.preventDefault();
    location.hash = btn.dataset.tab;
    switchTab(btn.dataset.tab);
  });
});

/* ---------------- 健康宣教（无需登录） ---------------- */

const CATEGORY_NAMES = { general: "健康常识", chronic: "慢病防治", maternal: "妇幼保健", tcm: "中医养生" };

async function loadArticles() {
  const box = $("#edu-list");
  try {
    const articles = await api("/api/portal/health-articles");
    if (!articles.length) {
      box.innerHTML = '<p class="empty">暂无宣教文章</p>';
      return;
    }
    box.innerHTML = articles.map((a) => `
      <div class="article">
        <span class="cat">${esc(CATEGORY_NAMES[a.category] || a.category)}</span>
        <h3>${esc(a.title)}</h3>
        <p class="clamp">${esc(a.content)}</p>
        <a class="more" href="javascript:void(0)">展开全文</a>
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

/* ---------------- 我的档案（双因子核验） ---------------- */

const CHRONIC_NAMES = { hypertension: "高血压", diabetes: "2型糖尿病", copd: "慢阻肺" };
const LEVEL_TAGS = { 1: ["控制良好", "green"], 2: ["需干预", "orange"], 3: ["高危", "red"] };

function kv(k, v) {
  return `<div class="kv"><span class="k">${esc(k)}</span><span>${v}</span></div>`;
}

$("#archive-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#archive-error").textContent = "";
  const ehc = $("#in-ehc").value.trim();
  const idCard = $("#in-idcard").value.trim();
  try {
    // 走 POST：身份证号放 body，避免经 URL 进入代理日志与浏览器历史
    const data = await api("/api/portal/my-archive", {
      method: "POST",
      body: JSON.stringify({ ehc_no: ehc, id_card: idCard }),
    });
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
    $("#archive-result").innerHTML = `
      <div class="m-card">${kv("姓名", esc(data.name))}${kv("健康卡号", esc(data.ehc_no))}</div>
      <div class="sec-title">慢病管理（${data.chronic_care.length}）</div>
      ${chronic || '<p class="empty">无慢病在管记录</p>'}
      <div class="sec-title">就诊记录（${data.encounters.length}）</div>
      ${encounters || '<p class="empty">无就诊记录</p>'}
      <div class="sec-title">检查检验报告（${data.exam_reports.length}）</div>
      ${reports || '<p class="empty">无报告</p>'}`;
  } catch (err) {
    $("#archive-result").innerHTML = "";
    $("#archive-error").textContent = err.message;
  }
});

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

$("#survey-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const msg = $("#survey-msg");
  msg.textContent = "";
  try {
    await api("/api/portal/surveys", {
      method: "POST",
      body: JSON.stringify({
        ehc_no: $("#sv-ehc").value.trim(),
        id_card: $("#sv-idcard").value.trim(),
        target_type: $("#sv-type").value,
        score: Number(starBox.dataset.score),
        comment: $("#sv-comment").value.trim(),
      }),
    });
    msg.textContent = "评价已提交，感谢您的反馈！";
    msg.className = "msg ok";
    $("#sv-comment").value = "";
  } catch (err) {
    msg.textContent = err.message;
    msg.className = "msg err";
  }
});

/* ---------------- 启动 ---------------- */

const initTab = (location.hash || "#edu").replace("#", "");
switchTab(["edu", "archive", "survey"].includes(initTab) ? initTab : "edu");
loadArticles();
