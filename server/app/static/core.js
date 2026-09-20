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

/* 会话与鉴权（G3，P1-23 收口）：登录后令牌只存 HttpOnly Cookie（JS 读不到、
 * XSS 偷不走），localStorage 只存非敏感的 role 与 CSRF token。
 * - 迁移期兜底：旧版把 JWT 写进 localStorage("medplat_token")，这里**仍读取**
 *   存量值并继续走 Header 模式，让升级前已登录的用户不被强制掉线；重新登录
 *   （或登出）即切换 Cookie 模式并清掉旧存量。
 * - CSRF token 存 localStorage 没有令牌那样的失窃风险：它防的是"跨站请求自动
 *   携带 Cookie"，本身不是凭据——能读它的脚本已在同源页面内执行，那是 XSS
 *   场景，CSRF 防线本就不针对它。 */
let token = localStorage.getItem("medplat_token") || "";
const CSRF_KEY = "medplat_csrf";

function csrfToken() { return readCookie(CSRF_KEY) || localStorage.getItem(CSRF_KEY) || ""; }
function isAuthed() { return Boolean(token) || Boolean(localStorage.getItem("medplat_role")); }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;  // 迁移兜底：存量令牌仍走 Header
  const method = (options.method || "GET").toUpperCase();
  // Cookie 模式的写请求：双提交 CSRF（读请求服务端不强制）
  if (!token && method !== "GET" && method !== "HEAD") headers["X-CSRF-Token"] = csrfToken();
  const resp = await fetch(path, { ...options, credentials: "same-origin", headers });
  if (resp.status === 401) { logout(); throw new Error("登录已过期"); }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `请求失败(${resp.status})`);
  return data;
}

function logout() {
  // 先请后端拉黑令牌并清 HttpOnly Cookie（直接 fetch 而不走 api()：
  // api() 的 401 分支会调回本函数）；网络失败/本就未登录时照样本地退出。
  fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken(), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
  }).catch(() => {});
  token = "";
  localStorage.removeItem("medplat_token");
  localStorage.removeItem("medplat_role");
  localStorage.removeItem(CSRF_KEY);
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

function table(cols, rows, renderRow) {
  const head = cols.map((c) => `<th>${esc(c)}</th>`).join("");
  const body = rows.length
    ? rows.map(renderRow).join("")
    : `<tr><td colspan="${cols.length}" style="color:#8a939e">暂无数据</td></tr>`;
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

/**
 * 面板外壳：`<div class="panel"><h3>标题</h3>内容</div>`（ADR-0009 第二步）。
 *
 * **`title` 由组件转义，`body` 不转**——这条边界要说清楚，不然会给人虚假的安全感：
 * `body` 是调用方自己拼好的 HTML，组件没法替它转义，调用方内部该 `esc()` 的照旧。
 * 组件收掉的是"标题忘了转义"这一类，不是全部。
 *
 * 放在 core.js 而不是 shared.js：`.panel`/`.card`/`table()` 是**管理端**这一套
 * 前端的标记约定，居民端与医师端（`m/`）另有自己的一套，把它塞进三端共用的
 * shared.js 只会给那两端加一段永远不会被调用的代码。shared.js 只放三端真的都在用的
 * （`$`/`esc`）。
 *
 * `accent` 给左边框上色，既有页面用它区分警示／重点面板。
 */
function panel(title, body, { accent = "" } = {}) {
  const style = accent ? ` style="border-left:4px solid ${esc(accent)}"` : "";
  return `<div class="panel"${style}><h3>${esc(title)}</h3>${body}</div>`;
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
// 转诊状态**文案取自后端** `status_label`（权威在 routers/referrals.STATUS_LABELS）。
// 这里只留配色——配色是展示，文案是口径；口径在前后端各存一份，改一处漏一处只是时间问题。
const REF_STATUS_COLOR = { pending: "orange", accepted: "", completed: "green", rejected: "red" };

function nav(pageId) {
  location.hash = pageId;
}

/* 路由串行化：同一时刻只允许一次页面渲染在跑。
 *
 * 每个页面的 render() 都是异步的（先拉接口再写 DOM）。放任并发会出一种没有任何
 * 报错的错乱：点开一个慢页面、没等它画完又点了另一页，**慢的那次后完成，就把
 * 已经画好的新页面盖掉**——标题是新页的，内容是旧页的。端到端用例里登录后立刻
 * 切页能稳定复现：驾驶舱的内容盖在"手术麻醉"的标题下面。
 *
 * 修法不是"渲染完发现自己过期就重画"——试过，会死循环：两次并发渲染各自完成时
 * 都判定自己过期，互相触发新的渲染，页面永远停在"加载中…"。
 *
 * 改成串行 + 待办位：有渲染在跑时，新的路由请求只更新序号就返回；正在跑的那次
 * 收尾时发现序号变了，就再画一轮。**最多同时一次渲染**，最后一次请求必定被画出，
 * 也不需要给每个 render 加取消逻辑。
 */
let routeSeq = 0;
let routing = false;

async function route() {
  if (!isAuthed()) return;
  routeSeq += 1;
  if (routing) return;  // 已有渲染在跑，它收尾时会处理最新这次
  routing = true;
  try {
    for (;;) {
      const seq = routeSeq;
      const id = location.hash.replace("#", "") || "dashboard";
      let page = PAGES.find((p) => p.id === id) || PAGES[1];
      if (!pageAllowed(page)) page = PAGES[1];
      document.querySelectorAll("#nav a").forEach((a) =>
        a.classList.toggle("active", a.dataset.page === page.id));
      $("#main").innerHTML = `<h2>${esc(page.title)}</h2><div class="desc" id="page-desc"></div><div id="page-body">加载中…</div>`;
      try { await page.render(); }
      catch (e) { $("#page-body").innerHTML = `<p class="msg err">${esc(e.message)}</p>`; }
      if (seq === routeSeq) break;  // 期间没有新的路由请求，收工
    }
  } finally {
    routing = false;
  }
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
  // 月份标签来自后端、格式固定（YYYY-MM），今天不含特殊字符——但图表组件是
  // 三套前端共用的渲染出口，"这个入参恰好安全"不是组件该依赖的前提。
  // 同文件的 barChart 早就 esc(label) 了，这里对齐（CLAUDE.md §8）。
  months.forEach((mo, i) => { svg += `<text x="${x(i)}" y="${h - 6}" font-size="10.5" fill="#5b6773" text-anchor="middle">${esc(String(mo).slice(2))}</text>`; });
  // `max` 是本函数自己算出来的数字，`esc()` 对它是恒等——照样包上，是为了让
  // "<text> 里的插值一律过 esc" 这条规则**没有例外**。带例外清单的规则，
  // 后来人得先判断自己算不算例外，判断错了就是漏转义。
  svg += `<text x="4" y="${y(max) + 4}" font-size="10.5" fill="#5b6773">${esc(max)}</text><text x="4" y="${y(0) + 4}" font-size="10.5" fill="#5b6773">0</text>`;
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
    // 分数是**周期口径**（缺省当年），标题必须带上周期——不标的话读的人会
    // 以为是累计数（这正是口径变更前的行为）
    if (top.length) perfHtml = `<div class="panel"><h3>机构绩效评分（${esc(perf.period)} 年度，前8）</h3>${barChart(top, { unit: " 分" })}</div>`;
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
    `<span style="font-size:12.5px;margin-right:14px"><span style="display:inline-block;width:10px;height:10px;background:${trendColors[i]};border-radius:2px;margin-right:4px"></span>${esc(trendNames[k] || k)}</span>`).join("");
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
  const [consultations, experts, stats] = await Promise.all([
    api("/api/consultations"), api("/api/consultations/experts"), api("/api/consultations/stats")]);
  const CS = { applied: ["已申请", "orange"], accepted: ["已受理", ""], completed: ["已完成", "green"], declined: ["已拒绝", "red"] };
  const availableExperts = experts.filter((x) => x.available);
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
      const actions = c.status === "applied"
        ? `<button class="btn secondary" data-act="accept" data-id="${c.id}">受理</button>
           <button class="btn danger" data-act="decline" data-id="${c.id}">拒绝</button>`
        : c.status === "accepted"
        ? `<button class="btn secondary" data-act="complete" data-id="${c.id}">出意见</button>`
        : c.status === "completed"
        // 已完成的会诊，评价与计费是两件独立的事：没评价就给评价按钮，
        // 没计费就给计费按钮，两个都缺就两个都给。
        ? `${c.rating ? "" : `<button class="btn secondary" data-act="rate" data-id="${c.id}">评价</button>`}
           ${c.fee_settled ? "" : `<button class="btn secondary" data-act="fee" data-id="${c.id}">计费</button>`}` || "—"
        : "—";
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.from_org_id} → ${c.to_org_id}</td>
        <td>${esc(c.question)}</td><td>${esc(c.expert_name) || "—"}</td><td>${esc(c.opinion) || "—"}</td>
        <td>${c.rating ? "★".repeat(c.rating) : "—"}</td>
        <td>${c.fee_settled ? `${c.fee} 元${c.fee_note ? `（${esc(c.fee_note)}）` : ""}` : "未计费"}</td>
        <td>${statusTag(CS, c.status)}</td><td>${actions}</td></tr>`;
    })}</div>
    <div class="panel"><h3>会诊统计</h3>
      <p>累计 ${stats.total} 例，完成率 ${stats.completion_rate_pct === null ? "—" : `${stats.completion_rate_pct}%`}；
        各状态：${Object.entries(stats.by_status).map(([k, v]) => `${esc(CS[k] ? CS[k][0] : k)} ${v}`).join("，") || "—"}</p>
      <p>评价：已评 ${stats.rating.rated_count} 例，均分 ${stats.rating.avg === null ? "暂无" : stats.rating.avg}，
        <b>未评价 ${stats.rating.unrated_count} 例（单列，不当 0 分并入均值）</b></p>
      <p>费用：已计费 ${stats.fee.settled_count} 例，合计 ${stats.fee.total_amount} 元，
        <b>已完成但未计费 ${stats.fee.unsettled_count} 例</b>（可能是院内会诊不计费，也可能是漏计）</p>
      <p style="color:#888">${esc(stats.caliber)}</p></div>
    <div class="panel"><h3>会诊专家库</h3>
      <form class="inline" id="expert-form">
        <input name="name" placeholder="专家姓名" required>
        <input name="org_id" type="number" placeholder="所属机构ID" required>
        <input name="specialty" placeholder="专长" style="min-width:180px">
        <label><input type="checkbox" name="available" checked> 可受理</label>
        <button>入库</button></form>
      <p class="msg" id="expert-msg"></p>
      ${table(["ID", "姓名", "机构", "专长", "状态"], experts, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.name)}</td><td>${x.org_id}</td><td>${esc(x.specialty) || "—"}</td>
         <td><span class="tag ${x.available ? "green" : "red"}">${x.available ? "可受理" : "暂不可约"}</span></td></tr>`)}</div>`;
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
  $("#expert-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/consultations/experts", { method: "POST", body: JSON.stringify({
        name: f.get("name"), org_id: Number(f.get("org_id")),
        specialty: f.get("specialty") || "", available: f.get("available") === "on" }) });
      route();
    } catch (err) { setMsg("#expert-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { act, id } = e.target.dataset;
    if (!act || !id) return;
    try {
      if (act === "fee") {
        // 计费只对已完成的会诊（后端对其余一律 409）。fee=0 是一次真实的
        // "本次不计费"记录，不是"没填"——所以空串取消、"0" 照样提交。
        const fee = prompt("会诊费用（元，院内会诊可填 0，会记为已计费）");
        if (fee === null || fee === "") return;
        await api(`/api/consultations/${id}/fee`, { method: "POST", body: JSON.stringify({
          fee: Number(fee), fee_note: prompt("计费说明（可空）") || "" }) });
      } else if (act === "accept") {
        const roster = availableExperts.length
          ? `\n可受理专家：${availableExperts.map((x) => `${x.name}${x.specialty ? `（${x.specialty}）` : ""}`).join("、")}`
          : "\n（专家库里还没有可受理的专家，可先在下方入库）";
        const expert = prompt(`受理专家姓名${roster}`); if (!expert) return;
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

// 与后端 appointments.BLACKLIST_DOMAINS 同口径（路径保留 /appointments/blacklist
// 不改——已有对接方在用，为泛化去破坏外部契约不值当）
const BLACKLIST_DOMAINS = { appointment: "预约爽约", shortage: "缺药登记后不取药" };

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
      <h3 style="margin-top:14px">批量排班（模板 × 日期区间）</h3>
      <form class="inline" id="batch-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type">${Object.entries(RT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="resource_name" placeholder="资源名称" required>
        <input name="employee_id" type="number" placeholder="医师ID（可空）" style="min-width:130px">
        <input name="slot_time" placeholder="时段（如09:00-10:00）">
        <input name="capacity" type="number" value="5" min="1" style="min-width:70px">
        <input name="date_from" placeholder="起 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="date_to" placeholder="止 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="skip_dates" placeholder="跳过日期，逗号分隔（节假日/停诊）" style="min-width:230px">
        <label><input type="checkbox" name="skip_weekends"> 跳周末</label>
        <button>批量生成</button>
      </form>
      <h3 style="margin-top:14px">预约</h3>
      <form class="inline" id="book-form">
        <input name="slot_id" type="number" placeholder="号源ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <button>预约</button>
      </form><p class="msg" id="apt-msg"></p></div>
    <div class="panel"><h3>便捷寻医</h3>
      <p style="margin-bottom:8px">按姓名/科室/职称找医师并带出可约号源。<b>跨机构不设限</b>——
        在卫生院帮患者约县医院的号正是它的用途；没号的医师也一并列出并标注，
        只给有号的会让居民以为这位医师不存在。</p>
      <form class="inline" id="doc-form">
        <input name="keyword" placeholder="姓名/科室/职称">
        <input name="org_id" type="number" placeholder="限定机构ID（可空）" style="min-width:160px">
        <input name="from_date" placeholder="起始日期 YYYY-MM-DD（可空）" style="min-width:200px">
        <button>查找</button></form>
      <div id="doc-list"></div></div>
    <div class="panel"><h3>服务黑名单</h3>
      <form class="inline" id="bl-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="domain">${Object.entries(BLACKLIST_DOMAINS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="reason" placeholder="事由" style="min-width:200px">
        <button>加入</button></form>
      <form class="inline" id="bl-query">
        <select name="domain"><option value="">全部业务域</option>${Object.entries(BLACKLIST_DOMAINS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <button>刷新</button></form>
      <div id="bl-list"></div></div>
    <div class="panel"><h3>号源</h3>${table(["ID", "机构", "类型", "资源", "日期/时段", "已约/容量"], slots, (s) =>
      `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${RT[s.resource_type]}</td><td>${esc(s.resource_name)}</td>
       <td>${esc(s.slot_date)} ${esc(s.slot_time)}</td>
       <td><span class="tag ${s.booked >= s.capacity ? "red" : "green"}">${s.booked}/${s.capacity}</span></td></tr>`)}</div>
    <div class="panel"><h3>预约记录</h3>${table(["ID", "号源", "患者", "状态", "操作"], appointments, (a) => {
      return `<tr><td>${a.id}</td><td>${a.slot_id}</td><td>${a.patient_id}</td>
        <td>${statusTag(AS, a.status)}</td>
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
  $("#batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const tpl = {
      resource_type: f.get("resource_type"), resource_name: f.get("resource_name"),
      slot_time: f.get("slot_time") || "", capacity: Number(f.get("capacity")),
      employee_id: f.get("employee_id") ? Number(f.get("employee_id")) : null,
    };
    try {
      const r = await api("/api/appointments/slots/batch", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), templates: [tpl],
        date_from: f.get("date_from"), date_to: f.get("date_to"),
        skip_dates: (f.get("skip_dates") || "").split(",").map((d) => d.trim()).filter(Boolean),
        skip_weekends: f.get("skip_weekends") === "on" }) });
      // 后端幂等：已有号源的日期跳过而不是报错（开办期常要补生成某几天），
      // 所以 skipped 不是失败数——照实说明，免得有人以为生成漏了。
      setMsg("#apt-msg", `已生成 ${r.created} 个号源，跳过 ${r.skipped} 个（已存在或在跳过日期内）`, true);
      route();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  const drawDoctors = async (qs) => {
    const docs = await api(`/api/appointments/doctors${qs}`);
    $("#doc-list").innerHTML = table(["医师", "职称", "科室/岗位", "所属机构", "可约号", "最近号源"], docs, (d) =>
      `<tr><td>${esc(d.name)}</td><td>${esc(d.title) || "—"}</td><td>${esc(d.position) || "—"}</td>
       <td>${esc(d.org_name) || d.org_id}</td>
       <td><span class="tag ${d.bookable ? "green" : "red"}">${d.bookable ? `${d.available_slots} 个` : "暂无号"}</span></td>
       <td>${d.next_slots.map((s) => `${esc(s.slot_date)} ${esc(s.slot_time)}（${esc(s.resource_name)}，余${s.remaining}，号源#${s.slot_id}）`).join("<br>") || "—"}</td></tr>`);
  };
  $("#doc-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const q = new URLSearchParams();
    for (const k of ["keyword", "org_id", "from_date"]) if (f.get(k)) q.set(k, f.get(k));
    try { await drawDoctors(q.toString() ? `?${q}` : ""); }
    catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  const drawBlacklist = async (domain = "") => {
    const rows = await api(`/api/appointments/blacklist${domain ? `?domain=${domain}` : ""}`);
    $("#bl-list").innerHTML = table(["ID", "业务域", "患者ID", "事由", "操作"], rows, (b) =>
      `<tr><td>${b.id}</td><td><span class="tag">${esc(b.domain_name)}</span></td><td>${b.patient_id}</td>
       <td>${esc(b.reason) || "—"}</td>
       <td><button class="btn secondary" data-blrm="${b.patient_id}" data-bldomain="${esc(b.domain)}">移出</button></td></tr>`);
  };
  $("#bl-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/appointments/blacklist", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), domain: f.get("domain"), reason: f.get("reason") || "" }) });
      setMsg("#apt-msg", "已加入黑名单", true);
      await drawBlacklist();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  $("#bl-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawBlacklist(new FormData(e.target).get("domain")); }
    catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  await drawBlacklist();
  $("#page-body").onclick = async (e) => {
    const { cancel, fulfill, blrm, bldomain } = e.target.dataset;
    try {
      if (cancel) { await api(`/api/appointments/${cancel}/cancel`, { method: "POST" }); route(); }
      if (fulfill) { await api(`/api/appointments/${fulfill}/fulfill`, { method: "POST" }); route(); }
      if (blrm) {
        // 删除按 patient_id + domain 定位，两个都得带：同一个患者可能同时在
        // 「预约爽约」和「缺药不取」两个域里，漏掉 domain 会删错那条。
        await api(`/api/appointments/blacklist/${blrm}?domain=${bldomain}`, { method: "DELETE" });
        setMsg("#apt-msg", "已移出黑名单", true);
        await drawBlacklist();
      }
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
}

async function downloadCsv(path, filename, msgSel) {
  try {
    // GET 导出：Cookie 模式凭 HttpOnly Cookie（credentials），迁移兜底仍带 Header
    const resp = await fetch(path, {
      credentials: "same-origin",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
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
  $("#page-desc").textContent = "按机构自动汇算：转诊结案、共享诊断、慢病随访、处方合格、家医履约；监测指标上报导出";
  const [data, monitoring] = await Promise.all([
    api("/api/performance/orgs"), api("/api/reports/monitoring").catch(() => null)]);
  // 口径变更后分数只统计考核周期内的业务量，页面必须说清是哪一期
  $("#page-desc").textContent =
    `${$("#page-desc").textContent}｜当前评分周期：${data.period}`;
  $("#page-body").innerHTML = `
    <div class="panel"><h3>上报报表导出（管理层）</h3>
      <p style="margin-bottom:8px">
        <button class="btn secondary" id="exp-monitor">监测指标CSV（14项）</button>
        <button class="btn secondary" id="exp-ops">运营月报CSV（业务量累计／绩效分当年）</button>
        <button class="btn" id="exp-ops-period">按月导出运营月报</button></p>
      <p class="msg" id="rpt-msg"></p>
      ${monitoring ? table(["#", "指标名", "口径", "当期值", "数据来源"], monitoring.indicators, (i) =>
        `<tr><td>${i.no}</td><td>${esc(i.name)}</td><td style="font-size:12.5px;color:#5b6773">${esc(i.caliber)}</td>
         <td><b>${esc(i.value)}</b> ${esc(i.unit)}</td><td><span class="tag">${esc(i.source)}</span></td></tr>`) : ""}</div>
    <div class="panel"><h3>机构评分排名</h3>
      <p class="desc">本页是<b>考核口径</b>：指标与权重来自指标目录，分数只统计
        当前评分周期（${esc(data.period)}）内的业务量。「决策分析」页的
        「期末综合绩效报告」走的是自定义公式，<b>两者不可比</b>。</p>
      ${data.scorecards.length ? barChart(data.scorecards.map((c) => [c.org_name, c.score]), { unit: " 分" }) : "暂无数据"}</div>
    <div class="panel">${table(["排名", "机构", "层级", "总分", "转诊结案", "共享诊断(申请/出报告)", "慢病随访", "处方合格(可审)", "家医履约"],
      data.scorecards, (c, i) => {
        const d = c.detail;
        return `<tr><td>${data.scorecards.indexOf(c) + 1}</td><td>${esc(c.org_name)}</td><td>${esc(LEVELS[c.level] || c.level)}</td>
          <td><b>${c.score}</b></td>
          <td>${d.referral_completion.completed}/${d.referral_completion.total}</td>
          <td>${d.remote_exams}<span style="color:#8a939e">（${d.remote_exams_requested}/${d.remote_exams_provided}）</span></td>
          <td>${d.chronic_followup.followed}/${d.chronic_followup.total}</td>
          <td>${d.rx_pass.passed}/${d.rx_pass.total}<span style="color:#8a939e">（可审 ${d.rx_pass.rule_covered}）</span></td>
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
  const [batches, requests] = await Promise.all([
    api("/api/cssd/batches"), api("/api/cssd/requests")]);
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
      const next = { sterilizing: "标记已灭菌", sterile: "发放", dispatched: "回收" }[b.status];
      return `<tr><td>${b.id}</td><td><span class="tag">${esc(b.batch_no)}</span></td><td>${esc(b.item_name)}</td>
        <td>${b.quantity}</td><td>${b.dispatched_to_org_id ?? "—"}</td>
        <td>${statusTag(BS, b.status)}</td>
        <td>${next ? `<button class="btn secondary" data-adv="${b.id}" data-next="${b.status}">${next}</button>` : "—"}</td></tr>`;
    })}</div>
    <div class="panel"><h3>科室物品申领</h3>
      <form class="inline" id="cssd-req-form">
        <input name="org_id" type="number" placeholder="申领机构ID" required>
        <input name="item_name" placeholder="器械名称" required>
        <input name="quantity" type="number" value="1" min="1" style="min-width:80px">
        <button>提交申领</button></form>
      <p class="msg" id="cssd-req-msg"></p>
      ${table(["ID", "申领机构", "器械", "数量", "状态", "响应批次", "操作"], requests, (r) =>
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${esc(r.item_name)}</td><td>${r.quantity}</td>
         <td><span class="tag ${r.status === "fulfilled" ? "green" : "orange"}">${r.status === "fulfilled" ? "已响应" : "待响应"}</span></td>
         <td>${r.batch_id ?? "—"}</td>
         <td>${r.status === "requested"
           ? `<button class="btn secondary" data-cssdfill="${r.id}">以已灭菌批次响应</button>` : "—"}</td></tr>`)}
      <p style="margin-top:6px;color:#888">只有<b>已灭菌或已发放</b>的批次能用来响应申领——拿灭菌中的批次去响应，
        等于把还没灭完菌的器械算成已经给出去了。</p></div>`;
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
  $("#cssd-req-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/cssd/requests", formJson(e.target, ["org_id", "quantity"]), "#cssd-req-msg");
  };
  $("#page-body").onclick = async (e) => {
    const { adv, next, cssdfill } = e.target.dataset;
    if (cssdfill) {
      const batchId = prompt("用哪个批次响应？填批次ID（须为已灭菌或已发放）");
      if (!batchId) return;
      try {
        // batch_id 走 query 而不是请求体：后端签名就是这么收的
        await api(`/api/cssd/requests/${cssdfill}/fulfill?batch_id=${Number(batchId)}`, { method: "POST" });
        setMsg("#cssd-req-msg", "申领已响应", true);
        return route();
      } catch (err) { return setMsg("#cssd-req-msg", err.message, false); }
    }
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
  $("#page-desc").textContent = "点位建档→收集→暂存→交接全过程监管，扫码追溯与转运人工作量，超2天未交接自动预警";
  const [wastes, alerts, locations, hstats] = await Promise.all([
    api("/api/medwaste"), api("/api/medwaste/alerts"),
    api("/api/medwaste/locations?include_inactive=true"), api("/api/medwaste/handler-stats")]);
  const alertIds = new Set(alerts.map((w) => w.id));
  const WT = { infectious: "感染性", sharp: "损伤性", pathological: "病理性", pharmaceutical: "药物性", chemical: "化学性" };
  const WS = { collected: ["已收集", "orange"], stored: ["已暂存", "orange"], handed_over: ["已交接", "green"] };
  // 点位类型的中文名，**展示一律用服务端折算好的 `location_type_name`**
  // （后端 docstring 明写"前端不该自己维护第二份 source→产生点 的映射"）。
  // 下面这张表只给**录入下拉**用——它回答的是"能选哪些值"，与展示口径不是一回事。
  const LOC_TYPE_OPTIONS = { source: "产生点", storage: "暂存间" };
  const locName = Object.fromEntries(locations.map((l) => [l.id, l.name]));
  const sources = locations.filter((l) => l.active && l.location_type === "source");
  const storages = locations.filter((l) => l.active && l.location_type === "storage");
  const locLabel = (id) => (id ? esc(locName[id] || id) : "—");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>点位建档（产生点 / 暂存间）</h3>
      <form class="inline" id="loc-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="点位名称（如 内科病区 / 一号暂存间）" required>
        <select name="location_type">${Object.entries(LOC_TYPE_OPTIONS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="manager_name" placeholder="负责人">
        <button>建档</button>
      </form><p class="msg" id="loc-msg"></p>
      ${table(["ID", "机构", "名称", "类型", "负责人", "状态", "操作"], locations, (l) =>
        `<tr><td>${l.id}</td><td>${l.org_id}</td><td>${esc(l.name)}</td>
         <td>${esc(l.location_type_name)}</td><td>${esc(l.manager_name) || "—"}</td>
         <td><span class="tag ${l.active ? "green" : "red"}">${l.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-locedit="${l.id}">改档</button>
         ${l.active
            ? `<button class="btn danger" data-locoff="${l.id}">停用</button>`
            : `<button class="btn secondary" data-locon="${l.id}">启用</button>`}</td></tr>`)}
      <p class="desc">改档只改名称与负责人（归属与类型不可改，改了会让历史医废的语义变掉）；停用只改状态、不删行：点位会撤并，但“这包医废当年从哪个科室出来”必须永远查得到。</p></div>
    <div class="panel"><h3>收集登记</h3>
      <form class="inline" id="waste-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="waste_type">${Object.entries(WT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="weight_kg" type="number" step="any" placeholder="重量(kg)" required>
        <input name="collected_date" placeholder="收集日期 YYYY-MM-DD" required>
        <select name="source_location_id"><option value="">不指定产生点</option>${
          sources.map((l) => `<option value="${l.id}">${esc(l.name)}</option>`).join("")}</select>
        <button>登记</button>
      </form><p class="msg" id="waste-msg"></p>
      <p class="desc">追溯码按 MW-日期-序号 由平台生成，不接受人工填写——两包医废共用一个码，追溯就全废了。</p></div>
    <div class="panel"><h3>扫码追溯</h3>
      <form class="inline" id="trace-form">
        <input name="trace_code" placeholder="追溯码，如 MW-20260101-0001" required style="min-width:220px">
        <button>查询</button></form>
      <p class="msg" id="trace-msg"></p><div id="trace-result"></div></div>
    ${alerts.length ? `<div class="panel"><h3>⚠ 滞留预警（${alerts.length}）</h3><p class="desc">收集超过2天仍未交接</p></div>` : ""}
    <div class="panel">${table(["ID", "机构", "追溯码", "类别", "重量", "收集日期", "产生点", "暂存间", "转运人", "状态", "操作"], wastes, (w) => {
      const actions = w.status === "collected"
        ? `<button class="btn secondary" data-store="${w.id}">入暂存</button>
           <button class="btn secondary" data-hand="${w.id}">交接</button>`
        : w.status === "stored" ? `<button class="btn secondary" data-hand="${w.id}">交接</button>` : "—";
      return `<tr><td>${w.id}</td><td>${w.org_id}</td><td><span class="tag">${esc(w.trace_code)}</span></td>
        <td>${WT[w.waste_type]}</td><td>${w.weight_kg}kg</td>
        <td>${esc(w.collected_date)}${alertIds.has(w.id) ? ' <span class="tag red">滞留</span>' : ""}</td>
        <td>${locLabel(w.source_location_id)}</td><td>${locLabel(w.storage_location_id)}</td>
        <td>${esc(w.handler_name) || "—"}</td><td>${statusTag(WS, w.status)}</td>
        <td>${actions}</td></tr>`;
    })}</div>
    <div class="panel"><h3>转运人员工作量</h3>
      ${table(["员工ID", "姓名", "交接次数", "重量合计"], hstats.handlers, (h) =>
        `<tr><td>${h.employee_id}</td><td>${esc(h.name)}</td><td>${h.count}</td><td>${h.weight_kg}kg</td></tr>`)}
      <p class="desc">未挂员工档案的交接单列：${hstats.unlinked_records.count} 次 / ${hstats.unlinked_records.weight_kg}kg，
        不摊到任何人头上——只填了名字的历史记录，重名就会张冠李戴。</p></div>`;
  $("#loc-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/medwaste/locations", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), name: f.get("name"),
        location_type: f.get("location_type"), manager_name: f.get("manager_name") || "" }) });
      route();
    } catch (err) { setMsg("#loc-msg", err.message, false); }
  };
  $("#waste-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { org_id: Number(f.get("org_id")), waste_type: f.get("waste_type"),
      weight_kg: Number(f.get("weight_kg")), collected_date: f.get("collected_date") };
    if (f.get("source_location_id")) body.source_location_id = Number(f.get("source_location_id"));
    try {
      await api("/api/medwaste", { method: "POST", body: JSON.stringify(body) });
      route();
    } catch (err) { setMsg("#waste-msg", err.message, false); }
  };
  $("#trace-form").onsubmit = async (e) => {
    e.preventDefault();
    const code = new FormData(e.target).get("trace_code");
    try {
      const w = await api(`/api/medwaste/trace/${encodeURIComponent(code)}`);
      $("#trace-result").innerHTML = `
        <p style="font-size:13px">追溯码 <span class="tag">${esc(w.trace_code)}</span>
          ${esc(w.waste_type_name)} ${esc(w.weight_kg)}kg · 机构 ${w.org_id} · ${statusTag(WS, w.status)}</p>
        ${table(["环节", "时间", "地点 / 经手人"], w.timeline, (s) =>
          `<tr><td>${esc(s.step)}</td><td>${esc(s.at)}</td><td>${esc(s.location) || "—"}</td></tr>`)}`;
      setMsg("#trace-msg", "", true);
    } catch (err) { $("#trace-result").innerHTML = ""; setMsg("#trace-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { hand, store, locoff, locon, locedit } = e.target.dataset;
    try {
      if (locedit) {
        // 只改名字与负责人：点位归属与类型不可改，改了会让历史医废记录的语义变掉
        const cur = locations.find((l) => String(l.id) === locedit) || {};
        const name = prompt("点位名称", cur.name || "");
        if (name === null) return;
        const manager = prompt("负责人（可留空）", cur.manager_name || "");
        if (manager === null) return;
        await api(`/api/medwaste/locations/${locedit}`, { method: "PATCH",
          body: JSON.stringify({ name, manager_name: manager }) });
        return route();
      }
      if (locoff) { await api(`/api/medwaste/locations/${locoff}`, { method: "DELETE" }); return route(); }
      if (locon) { await api(`/api/medwaste/locations/${locon}/reactivate`, { method: "POST" }); return route(); }
      if (store) {
        const hint = storages.map((l) => `${l.id}=${l.name}`).join("，") || "本机构尚未建暂存间";
        const locId = prompt(`暂存间点位ID（${hint}）`);
        if (!locId) return;
        await api(`/api/medwaste/${store}/store`, { method: "POST",
          body: JSON.stringify({ storage_location_id: Number(locId) }) });
        return route();
      }
      if (hand) {
        // handler_name 是后端必填（schemas.WasteHandover 的 min_length=1），即便挂了
        // 员工档案也得给——所以先问姓名再问档案ID，别做成"二选一"让人撞 422。
        // 挂上档案才进得了工作量统计（挂档案时姓名以档案为准，由后端覆盖）。
        const handler = prompt("转运人员姓名"); if (!handler) return;
        const employeeId = prompt("转运人员的员工档案ID（可留空；填了才计入工作量统计）");
        const body = { handler_name: handler };
        if (employeeId) body.handler_employee_id = Number(employeeId);
        await api(`/api/medwaste/${hand}/handover`, { method: "POST", body: JSON.stringify(body) });
        return route();
      }
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

// 样本物流：与后端 exams._SAMPLE_FLOW 同口径（""→采样→冷链转运→中心核收）
const SAMPLE_FLOW = { "": "collected", collected: "in_transit", in_transit: "received" };
const SAMPLE_STATUS = { collected: "已采样", in_transit: "冷链转运中", received: "中心已核收" };

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
    <div class="panel"><h3>申请单</h3>${table(["ID", "患者", "中心", "项目", "状态", "样本", "操作"], requests, (r) => {
      let actions = r.status === "pending"
        ? `<button class="btn secondary" data-claim="${r.id}">领取</button>`
        : r.status === "diagnosing"
        ? `<button class="btn secondary" data-report="${r.id}">出报告</button>` : "";
      // 样本物流只在检验类申请上有环节（后端对非 lab 一律 422），且出报告后不再推进
      const nextSample = SAMPLE_FLOW[r.sample_status || ""];
      if (r.center_type === "lab" && nextSample && ["pending", "diagnosing"].includes(r.status)) {
        actions += ` <button class="btn secondary" data-sample="${r.id}">${SAMPLE_STATUS[nextSample]}</button>`;
      }
      actions += ` <button class="btn secondary" data-printreq="${r.id}">打印申请单</button>`;
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${CENTER_NAMES[r.center_type]}</td>
        <td>${esc(r.item_name)}</td><td>${statusTag(EXAM_STATUS, r.status)}</td>
        <td>${r.center_type === "lab" ? (SAMPLE_STATUS[r.sample_status] || "未采样") : "—"}</td>
        <td>${actions}</td></tr>`;
    })}</div>
    <div class="panel"><h3>报告修订（限医师，前值留痕）</h3>
      <p style="margin-bottom:8px">修订会把<b>前结论/前所见/前危急标记</b>写进修订历史；
        危急值联动：改后仍为危急值则闭环状态复位为"已通知"须重新确认，解除危急标记则闭环状态清空——两种变化都留痕。</p>
      <form class="inline" id="amend-form">
        <input name="report_id" type="number" placeholder="报告ID" required style="min-width:110px">
        <input name="conclusion" placeholder="新结论" required style="min-width:220px">
        <input name="finding" placeholder="新所见（可空）" style="min-width:200px">
        <select name="critical"><option value="">危急标记不变</option><option value="true">置为危急值</option><option value="false">解除危急标记</option></select>
        <input name="reason" placeholder="修订理由" style="min-width:180px">
        <button>提交修订</button></form>
      <form class="inline" id="rev-query"><input name="report_id" type="number" placeholder="报告ID" required><button>查修订历史</button></form>
      <p class="msg" id="amend-msg"></p><div id="rev-list"></div></div>
    <div class="panel"><h3>报告模板</h3>
      <form class="inline" id="tpl-form">
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="name" placeholder="模板名" required>
        <input name="content" placeholder="模板正文" style="min-width:300px">
        <button>新建模板</button></form>
      <form class="inline" id="tpl-query">
        <select name="center_type"><option value="">全部中心</option>${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <button>刷新</button></form>
      <p class="msg" id="tpl-msg"></p><div id="tpl-list"></div></div>
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
  const drawTemplates = async (centerType = "") => {
    const list = await api(`/api/exams/templates${centerType ? `?center_type=${centerType}` : ""}`);
    $("#tpl-list").innerHTML = table(["ID", "中心", "模板名", "正文"], list, (t) =>
      `<tr><td>${t.id}</td><td>${CENTER_NAMES[t.center_type] || esc(t.center_type)}</td>
       <td>${esc(t.name)}</td><td>${esc(t.content) || "—"}</td></tr>`);
  };
  $("#tpl-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/exams/templates", { method: "POST", body: JSON.stringify({
        center_type: f.get("center_type"), name: f.get("name"), content: f.get("content") || "" }) });
      setMsg("#tpl-msg", "模板已创建", true);
      await drawTemplates();
    } catch (err) { setMsg("#tpl-msg", err.message, false); }
  };
  $("#tpl-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawTemplates(new FormData(e.target).get("center_type")); }
    catch (err) { setMsg("#tpl-msg", err.message, false); }
  };
  await drawTemplates();
  const drawRevisions = async (reportId) => {
    const revs = await api(`/api/exams/reports/${reportId}/revisions`);
    $("#rev-list").innerHTML = `<h4>报告 ${esc(String(reportId))} 修订历史（${revs.length} 次）</h4>` +
      table(["时间", "修订人", "前结论", "前所见", "前危急", "理由"], revs, (r) =>
        `<tr><td>${esc(r.at.replace("T", " ").slice(0, 19))}</td><td>${r.revised_by}</td>
         <td>${esc(r.prev_conclusion) || "—"}</td><td>${esc(r.prev_finding) || "—"}</td>
         <td>${r.prev_critical ? '<span class="tag red">是</span>' : "否"}</td>
         <td>${esc(r.reason) || "—"}</td></tr>`);
  };
  $("#amend-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { conclusion: f.get("conclusion"), reason: f.get("reason") || "" };
    if (f.get("finding")) body.finding = f.get("finding");
    // 三态：不选＝不动危急标记（null），选了才带值——传 false 与不传是两回事，
    // 前者会解除危急标记并清空闭环状态，后者保持原样。
    if (f.get("critical")) body.critical = f.get("critical") === "true";
    try {
      const r = await api(`/api/exams/reports/${f.get("report_id")}`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg("#amend-msg", `已修订：${r.critical ? `危急值，闭环状态 ${r.critical_status || "待确认"}` : "非危急值"}`, true);
      await drawRevisions(f.get("report_id"));
    } catch (err) { setMsg("#amend-msg", err.message, false); }
  };
  $("#rev-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawRevisions(new FormData(e.target).get("report_id")); }
    catch (err) { setMsg("#amend-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const claim = e.target.dataset.claim, report = e.target.dataset.report;
    const { printreq, printreport, sample } = e.target.dataset;
    try {
      if (printreq) return await openPrintPage(`/api/print/exam-requests/${printreq}`);
      if (printreport) return await openPrintPage(`/api/print/exam-reports/${printreport}`);
      if (sample) {
        const r = await api(`/api/exams/${sample}/sample/advance`, { method: "POST" });
        setMsg("#exam-msg", `样本状态：${esc(SAMPLE_STATUS[r.sample_status] || r.sample_status)}`, true);
        return route();
      }
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
      const text = r.status_label || r.status;
      const color = REF_STATUS_COLOR[r.status] || "";
      const actions = r.status === "pending"
        ? `<button class="btn secondary" data-status="accepted" data-id="${r.id}">接诊</button>
           <button class="btn danger" data-status="rejected" data-id="${r.id}">退回</button>`
        : r.status === "accepted"
        ? `<button class="btn secondary" data-status="completed" data-id="${r.id}">结案</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${r.direction === "up" ? "上转" : "下转"}</td>
        <td>${r.from_org_id} → ${r.to_org_id}</td><td>${esc(r.reason)}</td>
        <td><span class="tag ${esc(color)}">${esc(text)}</span></td>
        <td>${actions} <button class="btn" data-printref="${r.id}">打印转诊单</button></td></tr>`;
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
    const { status, id, printref } = e.target.dataset;
    try {
      // 转诊单：服务端渲染的 A4 版式（转出/转入机构、方向、事由、状态），
      // 与报告/处方同一条打印链路（openPrintPage 带令牌取回再唤起打印）
      if (printref) return await openPrintPage(`/api/print/referrals/${printref}`);
      if (!status || !id) return;
      await api(`/api/referrals/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) });
      route();
    } catch (err) { setMsg("#ref-msg", err.message, false); }
  };
}

async function renderRx() {
  $("#page-desc").textContent = "“系统+药师”双重审方，每方必审；事后处方点评（药师）与合理率监管";
  const [prescriptions, rules, cstats, creviews] = await Promise.all([
    api("/api/prescriptions"), api("/api/prescriptions/rules?include_inactive=true"),
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
      <form class="inline" id="rule-import-form">
        <textarea name="rows" rows="3" style="width:100%" placeholder='批量导入（每行一条：药品编码,日剂量上限,单位  例：D001,2000,mg）'></textarea>
        <button class="secondary">批量导入</button></form>
      <p style="margin-bottom:8px">导入按 <b>drug_code 已存在则整条更新、不存在则新建</b>；
        一条撞车整批回滚，返回里分开报「新建 / 更新」两个数。</p>
      <p class="msg" id="rule-msg"></p>
      ${table(["药品编码", "日剂量上限", "相互作用", "禁忌诊断", "特殊人群", "肝肾功能提示", "状态", "操作"], rules, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${r.max_daily_dose}${esc(r.dose_unit)}</td>
         <td>${esc(r.interactions) || "—"}</td><td>${esc(r.contraindicated_diagnoses) || "—"}</td>
         <td>${esc(r.special_groups) || "—"}</td><td>${esc(r.renal_hepatic_note) || "—"}</td>
         <td><span class="tag ${r.active ? "green" : "red"}">${r.active ? "生效中" : "已停用"}</span></td>
         <td>${r.active
           ? `<button class="btn danger" data-ruleoff="${esc(r.drug_code)}">停用</button>`
           : `<button class="btn secondary" data-ruleon="${esc(r.drug_code)}">重新启用</button>`}</td></tr>`)}
      <p style="margin-top:6px;color:#888">停用<b>不删行</b>：规则改过什么、什么时候不再生效，处方点评复核时要回溯得到。</p></div>
    <div class="panel"><h3>处方队列</h3>${table(["ID", "患者", "诊断", "状态", "审方意见", "操作"], prescriptions, (p) => {
      let actions = p.status === "pending_review"
        ? `<button class="btn secondary" data-approve="1" data-id="${p.id}">通过</button>
           <button class="btn danger" data-approve="0" data-id="${p.id}">退回</button>` : "";
      if (canComment && !commented.has(p.id)) actions += ` <button class="btn" data-rxcomment="${p.id}">点评</button>`;
      actions += ` <button class="btn secondary" data-printrx="${p.id}">打印</button>`;
      return `<tr><td>${p.id}</td><td>${p.patient_id}</td><td>${esc(p.diagnosis_name)}</td>
        <td>${statusTag(RX_STATUS, p.status)}</td><td>${esc(p.review_comment) || "—"}</td><td>${actions || "—"}</td></tr>`;
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
  $("#rule-import-form").onsubmit = async (e) => {
    e.preventDefault();
    const rows = (new FormData(e.target).get("rows") || "").split("\n")
      .map((l) => l.trim()).filter(Boolean)
      .map((l) => {
        const [drug_code, dose, unit] = l.split(",").map((x) => (x || "").trim());
        return { drug_code, max_daily_dose: Number(dose), dose_unit: unit || "mg" };
      });
    const bad = rows.filter((r) => !r.drug_code || !(r.max_daily_dose > 0));
    // 后端一条撞车整批回滚，返回是 500 与"一条都没进"。格式错在前端就拦下来，
    // 别让人把一批 200 行提上去再从 500 里倒推是哪一行写错了。
    if (bad.length) return setMsg("#rule-msg", `有 ${bad.length} 行格式不对（编码为空或剂量上限非正数），整批未提交`, false);
    if (!rows.length) return setMsg("#rule-msg", "没有可导入的行", false);
    try {
      const r = await api("/api/prescriptions/rules/import", { method: "POST", body: JSON.stringify(rows) });
      setMsg("#rule-msg", `导入完成：新建 ${r.imported} 条，更新 ${r.updated} 条`, true);
      route();
    } catch (err) { setMsg("#rule-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { approve, id, rxcomment, printrx, ruleoff, ruleon } = e.target.dataset;
    if (ruleoff) {
      if (!confirm(`停用 ${ruleoff} 的审方规则？停用后开这个药不再受该规则拦截。规则行保留，可随时重新启用。`)) return;
      try {
        await api(`/api/prescriptions/rules/${encodeURIComponent(ruleoff)}`, { method: "DELETE" });
        setMsg("#rule-msg", "规则已停用（行保留）", true);
        return route();
      } catch (err) { return setMsg("#rule-msg", err.message, false); }
    }
    if (ruleon) {
      try {
        await api(`/api/prescriptions/rules/${encodeURIComponent(ruleon)}/reactivate`, { method: "POST" });
        setMsg("#rule-msg", "规则已重新启用", true);
        return route();
      } catch (err) { return setMsg("#rule-msg", err.message, false); }
    }
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
  $("#page-desc").textContent = "库存管理、批号效期、西药发药、县乡村余缺调拨、缺药预警";
  const [stocks, alerts, expiring, dispenses, purchase] = await Promise.all([
    api("/api/pharmacy/stocks"), api("/api/pharmacy/alerts"),
    api("/api/pharmacy/batches/expiring"), api("/api/dispense"),
    api("/api/pharmacy/purchase-suggestions"),
  ]);
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
      <h3 style="margin-top:14px">按批次入库（批号效期台账）</h3>
      <form class="inline" id="batch-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="batch_no" placeholder="批号" required>
        <input name="expire_date" placeholder="效期 YYYY-MM-DD" required>
        <input name="quantity" type="number" placeholder="数量" required min="1">
        <button>批次入库</button>
      </form>
      <h3 style="margin-top:14px">发药（凭审方通过处方，FEFO 先到效期先出）</h3>
      <form class="inline" id="dispense-form">
        <input name="prescription_id" type="number" placeholder="处方ID" required>
        <button>发药</button>
      </form>
      <h3 style="margin-top:14px">退药冲销</h3>
      <form class="inline" id="reverse-form">
        <input name="dispense_id" type="number" placeholder="发药记录ID" required>
        <input name="reason" placeholder="冲销原因" required>
        <button>退药</button>
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
         <td>${alertIds.has(s.id) ? '<span class="tag red">缺药</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}
      <h3 style="margin-top:14px">近效期批次（90 天）</h3>
      ${table(["机构ID", "药品", "批号", "效期", "余量", "剩余天数", "操作"], expiring, (b) =>
        `<tr><td>${b.org_id}</td><td>${esc(b.drug_name)}（${esc(b.drug_code)}）</td><td>${esc(b.batch_no)}</td>
         <td>${esc(b.expire_date)}</td><td>${b.remaining}</td>
         <td>${b.expired ? '<span class="tag red">已过期</span>' : `${b.remaining_days} 天`}</td>
         <td><button class="btn secondary" data-btrace="${b.id}">流向反查</button>
             <button class="btn danger" data-brecall="${b.id}">召回</button></td></tr>`)}
      <h3 style="margin-top:14px">批次召回与流向反查</h3>
      <p style="margin-bottom:8px">召回后该批次不得再发药、不得再入库，<b>余量同事务退出可用汇总</b>
        （记到批次的 blocked_quantity 上——药还在库房，只是发不出去）。
        只翻状态不动汇总的话，召回的药会一直被算成有货，缺药预警与采购建议长期少报。</p>
      <form class="inline" id="batch-op-form">
        <input name="batch_id" type="number" placeholder="批次ID" required>
        <button class="secondary">按批次ID反查流向</button></form>
      <p class="msg" id="recall-msg"></p><div id="batch-trace"></div>
      <h3 style="margin-top:14px">采购建议（近30天用量 − 全网当前库存，缺口为正的品种）</h3>
      <p style="margin-bottom:8px">用量按处方明细「日剂量 × 天数」汇总，退回的处方不计入。</p>
      ${table(["药品", "近30天用量", "当前库存", "建议采购量"], purchase, (s) =>
        `<tr><td>${esc(s.drug_name)}（${esc(s.drug_code)}）</td><td>${s.usage_30d}</td>
         <td>${s.current_stock}</td><td><span class="tag red">${s.suggested_quantity}</span></td></tr>`)}
      <h3 style="margin-top:14px">发药记录</h3>
      ${table(["ID", "处方ID", "状态", "明细（批号×数量）"], dispenses, (d) =>
        `<tr><td>${d.id}</td><td>${d.prescription_id}</td>
         <td>${d.status === "reversed" ? '<span class="tag red">已冲销</span>' : '<span class="tag green">已发药</span>'}</td>
         <td>${d.items.map((i) => `${esc(i.drug_name)} ${esc(i.batch_no)}×${i.quantity}`).join("，")}</td></tr>`)}</div>`;
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
  $("#batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/pharmacy/batches", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), drug_code: f.get("drug_code"), drug_name: f.get("drug_name"),
        batch_no: f.get("batch_no"), expire_date: f.get("expire_date"), quantity: Number(f.get("quantity")) }) });
      route();
    } catch (err) { setMsg("#pharm-msg", err.message, false); }
  };
  $("#dispense-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/dispense", { method: "POST",
        body: JSON.stringify({ prescription_id: Number(f.get("prescription_id")) }) });
      route();
    } catch (err) { setMsg("#pharm-msg", err.message, false); }
  };
  $("#reverse-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api(`/api/dispense/${Number(f.get("dispense_id"))}/reverse`, { method: "POST",
        body: JSON.stringify({ reason: f.get("reason") }) });
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
  const drawTrace = async (batchId) => {
    const t = await api(`/api/pharmacy/batches/${batchId}/dispenses`);
    $("#batch-trace").innerHTML = `
      <h4>批次 #${t.batch_id}｜${esc(t.drug_name)}（${esc(t.drug_code)}）批号 ${esc(t.batch_no)}，效期 ${esc(t.expire_date)}
        <span class="tag ${t.status === "recalled" ? "red" : "green"}">${t.status === "recalled" ? "已召回" : esc(t.status)}</span></h4>
      <p>仍在外面的量（不含已冲销）：<b>${t.total_dispensed}</b></p>
      ${table(["发药记录", "处方", "患者", "数量", "状态", "发药时间"], t.dispenses, (d) =>
        `<tr><td>${d.dispense_id}</td><td>${d.prescription_id}</td>
         <td>${esc(d.patient_name) || d.patient_id}</td><td>${d.quantity}</td>
         <td>${d.status === "reversed" ? '<span class="tag">已冲销</span>' : '<span class="tag red">在患者手上</span>'}</td>
         <td>${esc(d.dispensed_at.replace("T", " ").slice(0, 19))}</td></tr>`)}`;
  };
  $("#batch-op-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawTrace(new FormData(e.target).get("batch_id")); }
    catch (err) { setMsg("#recall-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { btrace, brecall } = e.target.dataset;
    try {
      if (btrace) return await drawTrace(btrace);
      if (brecall) {
        // 召回前先把流向摆出来：召回本身只是不让它再发出去，已经在患者手上的
        // 那部分要靠这张名单去联系。先召回再想起来查，名单还在，但人已经晚了一步。
        await drawTrace(brecall);
        const reason = prompt("召回原因（必填，会写进批次台账）");
        if (!reason) return;
        if (!confirm(`召回批次 ${brecall}？召回后不可再发药、不可再入库，余量立即退出可用汇总。\n上方已列出该批次的流向名单，需要另行联系已发出的患者。`)) return;
        await api(`/api/pharmacy/batches/${brecall}/recall`, { method: "POST", body: JSON.stringify({ reason }) });
        setMsg("#recall-msg", "批次已召回，余量已退出可用汇总", true);
        route();
      }
    } catch (err) { setMsg("#recall-msg", err.message, false); }
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

