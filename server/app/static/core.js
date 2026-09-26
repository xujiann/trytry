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
  if (!resp.ok) {
    // 状态码挂在错误上（居民端 m.js 的 api 早就这么做）：调用方要分得清
    // "参数被拒（422）"与"别的失败"，只拿文案去猜，后端改一句话就失灵。
    const err = new Error(errorText(data.detail, `请求失败(${resp.status})`));
    err.status = resp.status;
    throw err;
  }
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
 *
 * **`title === ""` 表示这个面板本来就没有标题**，此时不出 `<h3>`。这不是洁癖：
 * 仓库里有 16 处**裸面板**——外壳里直接就是一个 `table(…)`，没有 `<h3>`（表格自己带表头，
 * 再加个标题是重复）。组件若一律吐 `<h3></h3>`，迁移就成了**加一个空元素**
 * ——ADR-0009 的前提是"换外壳是 no-op"，加元素就不是 no-op 了。
 * 判据取**严格等于空串**而不是假值：`esc()` 会把 `undefined`/`null` 变成空串，
 * 所以 `panel(undefined, …)` 今天渲染成 `<h3></h3>`——一个空标题块，在页面上占着位、
 * 看得见。放宽成假值判断，它就会连 `<h3>` 一起消失，于是"这个面板本来就没标题"和
 * "标题算成了 undefined"两件事再也分不开。留着那个空 `<h3>`，反而是能被发现的失败。
 */
function panel(title, body, { accent = "" } = {}) {
  const style = accent ? ` style="border-left:4px solid ${esc(accent)}"` : "";
  const head = title === "" ? "" : `<h3>${esc(title)}</h3>`;
  return `<div class="panel"${style}>${head}${body}</div>`;
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

/** 存量选择：localStorage 里那个 id，只有在**这次真拉到的列表里还在**时才认，
    否则退回 `fallback`（默认 0 = 没有选择）。

    这些键跨会话、跨标签页持久，而它指向的东西会消失——住院记录会出院，
    基金池 / 协作分组 / 专病目录会被删或变得不可见。不校验有两种坏法：

    1. **静默指错人**：住院文书的 `<select>` 只列在院记录，存量 id 出院之后
       没有一个 option 带 selected，浏览器于是显示第一条；而下面四个面板与
       三个写入表单仍然指向那条旧记录——屏幕上写着甲，病程记录写进了乙。
    2. **永久卡死**：子资源取数（成员 / 预付批次 / 专病统计…）排在
       `#page-body` 赋值之前，一个 404 就被 `route()` 的 catch 换成一行错误，
       连"换一个"的那张列表都渲染不出来；而 id 在 localStorage 里不会自己
       消失，于是这一页对这个用户**每次进来都是同一行错误**。

    **这条规矩不是新发明的，管理端是三套前端里唯一没写的那个**：
    居民端 `m/m.js:334` 的家庭成员切换（"代管成员被解除后回落到本人"）、
    医师端 `m/doctor.js:599` 的查房住院记录（`admissions.some(...)` 后退到第一条）
    本来就是这么写的，两处都还把选中项留在内存里、不进存储。管理端这边只有专病页
    写对了一半（`programs.find((p) => p.id === picked)`），而那份校验只兜住了渲染、
    没兜住排在它前面的取数。 */
function pickedId(key, list, fallback = 0) {
  const stored = Number(localStorage.getItem(key) || 0);
  return list.some((item) => item.id === stored) ? stored : fallback;
}

/** 默认页 / 兜底页的 id。**按 id 取，不按下标取。**

    原先兜底写的是 `PAGES[1]`——它指到驾驶舱纯属排列的巧合：
    `PAGES[0]` 是 `{ group: "总览" }`，是**分组标题不是页面**（没有 id、
    没有 title、没有 render）。往注册表头部插一个分组（很正常的改动），
    `PAGES[1]` 就会指到一个分组项上，于是 `page.render()` 是 undefined
    直接抛错、`page.title` 也是 undefined——**整个管理端路由不起来**，
    而插分组的人完全想不到会碰这里。

    同一个意图在上一行本来就是按 id 写的（`|| HOME_PAGE_ID`），
    只有兜底这两处退回了下标。 */
const HOME_PAGE_ID = "dashboard";

/** 兜底页：先按 id 找首页，找不到就退到第一个**真的能渲染**的页面。

    第二重兜底是为了让「首页被改名/删掉」这种情况退化成「进了别的页」，
    而不是「拿到一个分组项然后抛错」——前者看得见，后者是白屏。 */
function homePage() {
  return PAGES.find((p) => p.id === HOME_PAGE_ID) || PAGES.find((p) => p.render);
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
      const id = location.hash.replace("#", "") || HOME_PAGE_ID;
      let page = PAGES.find((p) => p.id === id) || homePage();
      if (!pageAllowed(page)) page = homePage();
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
  // 局部变量叫 `drill` 而不是 `panel`：`panel()` 是面板组件（本文件上方），
  // 在会用到它的文件里再声明一个同名局部变量，迟早有人在这儿写下 `panel(...)`
  // 然后对着"panel is not a function"发呆。渲染驾驶舱那个函数里已经踩过一次。
  const drill = $("#drill-panel");
  if (!drill) return;
  drill.classList.remove("hidden");
  drill.innerHTML = "<div class='panel'>明细加载中…</div>";
  const limit = 20;
  const d = await api(`/api/metrics/drilldown?metric=${encodeURIComponent(metric)}&offset=${offset}&limit=${limit}`);
  const pager = [];
  if (offset > 0) pager.push(`<button class="btn secondary" data-drillpage="${Math.max(offset - limit, 0)}">上一页</button>`);
  if (offset + limit < d.total) pager.push(`<button class="btn secondary" data-drillpage="${offset + limit}">下一页</button>`);
  // 这个外壳**迁不了** `panel()`：标题里嵌着一个「关闭」按钮，而组件会把标题整段
  // `esc()` 掉——迁过去按钮就变成一段转义后的文本显示出来。理由记在
  // docs/adr/0009 第十一批，别当成"漏迁的"。
  drill.innerHTML = `<div class="panel" style="border-left:4px solid #0b6e6e">
    <h3>${esc(d.label)} 明细（${d.total}）　<button class="btn secondary" data-drillclose="1">关闭</button></h3>
    <p class="desc" style="font-size:12.5px">点击明细行跳转「${esc(d.page)}」业务页；口径与驾驶舱指标、预警横幅一致</p>
    ${table(d.columns, d.items, (row) =>
      `<tr data-drillgo="${esc(d.page)}" style="cursor:pointer">${
        d.fields.map((f) => `<td>${esc(row[f] ?? "—")}</td>`).join("")}</tr>`)}
    <div style="margin-top:8px">${pager.join(" ")}　<span style="font-size:12.5px;color:#5b6773">第 ${Math.floor(offset / limit) + 1} 页 / 共 ${Math.max(Math.ceil(d.total / limit), 1)} 页</span></div></div>`;
  drill.dataset.metric = metric;
  drill.dataset.offset = String(offset);
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
    // 以为是累计数（这正是口径变更前的行为）。
    // 周期这里**不再自己 esc()**：panel() 已经转义标题，套两层会把 `&` 变成 `&amp;amp;`。
    if (top.length) perfHtml = panel(`机构绩效评分（${perf.period} 年度，前8）`, barChart(top, { unit: " 分" }));
  } catch (e) { /* 绩效不可用不阻塞驾驶舱 */ }
  const [alerts, trends, drillables] = await Promise.all([
    api("/api/metrics/alerts"), api("/api/metrics/trends?months=6"),
    api("/api/metrics/drilldown-metrics")]);
  // 指标卡上绑了哪些下钻 key，从 cards 自己数——写死一个数字会随卡片增删悄悄过期
  const cardBound = new Set(cards.map((c) => c[3]).filter(Boolean));
  const alertBound = new Set(alerts.items.map((a) => a.type));
  const unbound = drillables.filter((x) => !cardBound.has(x.metric) && !alertBound.has(x.metric));
  const alertBanner = alerts.total
    ? panel(`⚠ 风险预警（${alerts.total}）`, `
       <p style="font-size:13.5px">${alerts.items.map((a) =>
        `<span class="tag red" style="margin-right:8px;cursor:pointer" data-drill="${esc(a.type)}">${esc(a.label)} ${a.count}</span>`).join("")}</p>`,
      { accent: "#c62828" })
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
     ${panel("近6月业务量趋势", `<div style="margin-bottom:6px">${legend}</div>${lineChart(trends.months, trends.series, trendColors)}`)}
     ${chronicItems.length ? panel("慢病分级分组", barChart(chronicItems, { color: "#b26a00", unit: " 人" })) : ""}
     ${panel(`可下钻指标目录（${drillables.length}）`, `
       <p class="desc">这份目录由后端 <code>METRIC_QUERIES</code> 生成，是下钻口径的唯一真源。
         上面的指标卡绑了 ${cardBound.size} 项，预警横幅此刻另外覆盖 ${alertBound.size} 项——
         ${unbound.length
           ? `剩下 <b>${unbound.length} 项在卡片与横幅上都没有入口</b>，只能从这里下钻。`
           : "目录里的每一项此刻都能从卡片或横幅点到。"}
         预警横幅那几项<b>只在有预警时才出现</b>，所以"此刻覆盖"不等于"一直覆盖"。</p>
       ${table(["指标", "名称", "当前计数", "业务页", "卡片入口"], drillables, (x) =>
         `<tr><td><span class="tag">${esc(x.metric)}</span></td><td>${esc(x.label)}</td>
          <td>${x.count}</td><td>${esc(x.page)}</td>
          <td>${cardBound.has(x.metric) ? "指标卡"
            : alertBound.has(x.metric) ? "预警横幅（当前有预警）"
            : '<span class="tag orange">无</span>'}
            <button class="btn sm" data-drill="${esc(x.metric)}">下钻</button></td></tr>`)}`)}
     ${perfHtml}`;
  $("#page-body").onclick = async (e) => {
    const hit = e.target.closest("[data-drill],[data-drillgo],[data-drillpage],[data-drillclose]");
    if (!hit) return;
    // 这个局部变量原名 `panel`，正好遮住同名的面板组件函数——上面的模板已经在用
    // `panel()` 了，同一个函数里两个 `panel` 是给后来人挖坑，就近改名。
    const drill = $("#drill-panel");
    try {
      if (hit.dataset.drillclose) return drill.classList.add("hidden");
      if (hit.dataset.drillgo) return nav(hit.dataset.drillgo);
      if (hit.dataset.drillpage) return await openDrilldown(drill.dataset.metric, Number(hit.dataset.drillpage));
      await openDrilldown(hit.dataset.drill, 0);
    } catch (err) { drill.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
}

// available 是 bool，没有后端文案可取；映成状态码再走 statusTag，与本页其余状态列同写法
const EXPERT_STATUS = { on: ["可排班", "green"], off: ["暂停排班", "red"] };

async function renderConsultations() {
  $("#page-desc").textContent = "申请 → 受理 → 出具意见 → 评价 → 计费；专家库与运行统计";
  const [consultations, experts, stats] = await Promise.all([
    api("/api/consultations"), api("/api/consultations/experts"), api("/api/consultations/stats"),
  ]);
  const CS = { applied: ["已申请", "orange"], accepted: ["已受理", ""], completed: ["已完成", "green"], declined: ["已拒绝", "red"] };
  // 专家建档后端是 require_admin，不是 admin 就别摆那个表单
  const canExpert = currentRole() === "admin";
  const onDuty = experts.filter((x) => x.available);
  $("#page-body").innerHTML = `
    ${panel("会诊申请", `
      <form class="inline" id="cons-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="申请机构ID" required>
        <input name="to_org_id" type="number" placeholder="受邀机构ID" required>
        <input name="question" placeholder="会诊问题" required style="min-width:240px">
        <button>提交</button>
      </form><p class="msg" id="cons-msg"></p>`)}
    ${panel("运行统计", `
      <div class="cards">
        <div class="card"><div class="label">申请总量</div><div class="value">${stats.total}</div></div>
        <div class="card"><div class="label">完成率</div><div class="value">${
          stats.completion_rate_pct === null ? "—" : `${stats.completion_rate_pct}%`}</div></div>
        <div class="card"><div class="label">评分均值</div><div class="value">${
          stats.rating.avg === null ? "—" : stats.rating.avg}</div>
          <div class="label">已评 ${stats.rating.rated_count} / 未评 ${stats.rating.unrated_count}</div></div>
        <div class="card"><div class="label">已计费金额</div><div class="value">${stats.fee.total_amount}</div>
          <div class="label">已计费 ${stats.fee.settled_count} / 未计费 ${
            stats.fee.unsettled_count}</div></div></div>
      ${table(["状态", "件数"], Object.entries(stats.by_status), ([code, n]) =>
        `<tr><td>${statusTag(CS, code)}</td><td>${n}</td></tr>`)}
      <p class="desc">${esc(stats.caliber)}</p>`)}
    ${panel("", table(["ID", "患者", "申请→受邀", "问题", "专家", "意见", "评价", "状态", "操作"], consultations, (c) => {
      const actions = c.status === "applied"
        ? `<button class="btn secondary" data-act="accept" data-id="${c.id}">受理</button>
           <button class="btn danger" data-act="decline" data-id="${c.id}">拒绝</button>`
        : c.status === "accepted"
        ? `<button class="btn secondary" data-act="complete" data-id="${c.id}">出意见</button>`
        : c.status === "completed"
        ? `${c.rating ? "" : `<button class="btn secondary" data-act="rate" data-id="${c.id}">评价</button> `
          }<button class="btn" data-act="fee" data-id="${c.id}">计费</button>` : "—";
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.from_org_id} → ${c.to_org_id}</td>
        <td>${esc(c.question)}</td><td>${esc(c.expert_name) || "—"}</td><td>${esc(c.opinion) || "—"}</td>
        <td>${c.rating ? "★".repeat(c.rating) : "—"}</td><td>${statusTag(CS, c.status)}</td><td>${actions}</td></tr>`;
    }) + `<p class="desc">计费只对已完成的会诊开放（拒绝与未受理的没有发生服务）。
      <b>行上看不到"这单计没计费"</b>——会诊列表的出参不含 fee 字段，
      按已计费/未计费的件数看上方统计；再计一次是覆盖，不是追加。</p>`)}
    ${panel("会诊专家库（受理时从这里选人，不再手打姓名）", `
      ${canExpert ? `<form class="inline" id="expert-form">
        <input name="name" placeholder="专家姓名" required>
        <input name="org_id" type="number" placeholder="所属机构ID" required>
        <input name="specialty" placeholder="专业方向">
        <select name="available"><option value="1">可排班</option><option value="0">暂停排班</option></select>
        <button>建档</button></form>` : ""}
      ${table(["ID", "姓名", "机构", "专业方向", "排班状态"], experts, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.name)}</td><td>${x.org_id}</td>
         <td>${esc(x.specialty) || "—"}</td>
         <td>${statusTag(EXPERT_STATUS, x.available ? "on" : "off")}</td></tr>`)}
      <p class="desc">受理时的专家下拉只列<b>可排班</b>的（当前 ${onDuty.length} 人）；
        专家库为空时退回手工输入，不至于卡住受理。</p>`)}`;
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
  const expertForm = $("#expert-form");
  if (expertForm) expertForm.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/consultations/experts", { method: "POST", body: JSON.stringify({
        name: f.get("name"), org_id: Number(f.get("org_id")),
        specialty: f.get("specialty"), available: f.get("available") === "1" }) });
      route();
    } catch (err) { setMsg("#cons-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { act, id } = e.target.dataset;
    if (!act || !id) return;
    try {
      if (act === "accept") {
        // 有专家库就从库里选，没有才退回手打——手打进来的名字对不上任何一条专家记录，
        // 后端 accept 只收字符串不校验，于是统计里"谁接得多"永远是一笔糊涂账
        const picked = await spdModal("受理会诊", [onDuty.length
          ? { name: "expert_name", label: "受理专家（只列可排班的）", type: "select",
              options: onDuty.map((x) => ({ value: x.name, label: `${x.name}${x.specialty ? `（${x.specialty}）` : ""}` })) }
          : { name: "expert_name", label: "受理专家姓名（专家库为空，先手工填）", type: "text", required: true }]);
        if (!picked || !picked.expert_name) return;
        await api(`/api/consultations/${id}/accept`, { method: "POST",
          body: JSON.stringify({ expert_name: picked.expert_name }) });
      } else if (act === "decline") {
        await api(`/api/consultations/${id}/decline`, { method: "POST" });
      } else if (act === "complete") {
        const picked = await spdModal("出具会诊意见", [
          { name: "opinion", label: "会诊意见（后端上限 2048 字）", type: "textarea" }]);
        if (!picked || !picked.opinion) return;
        await api(`/api/consultations/${id}/complete`, { method: "POST",
          body: JSON.stringify({ opinion: picked.opinion }) });
      } else if (act === "rate") {
        const picked = await spdModal("会诊评价", [
          { name: "rating", label: "评分", type: "select", value: "5",
            options: [5, 4, 3, 2, 1].map((n) => ({ value: n, label: `${"★".repeat(n)}（${n} 分）` })) }]);
        if (!picked) return;
        await api(`/api/consultations/${id}/rate`, { method: "POST",
          body: JSON.stringify({ rating: Number(picked.rating) }) });
      } else if (act === "fee") {
        const picked = await spdModal("会诊计费", [
          // 必填：数字框留空会被读成 0，照样标成「已计费」——标签自己说着 0 与未计费是两回事（本院内部会诊计 0 元就明确填 0）
          { name: "fee", label: "费用（元；0 与「未计费」是两回事，0 也会标成已计费）", type: "number", required: true },
          { name: "fee_note", label: "计费说明", type: "text" }]);
        if (!picked) return;
        // 不在这里 setMsg：下面紧接着 route() 会整页重画，写了也当场被冲掉。
        // 计费的回馈看上方统计卡（已计费件数与金额会跟着变）
        await api(`/api/consultations/${id}/fee`, { method: "POST",
          body: JSON.stringify({ fee: picked.fee, fee_note: picked.fee_note }) });
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
    ${panel("签约", `
      <form class="inline" id="ct-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="doctor_name" placeholder="家庭医生" required>
        <select name="package">${Object.entries(PKG).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="signed_date" placeholder="签约日期 YYYY-MM-DD">
        <button>签约</button>
      </form><p class="msg" id="ct-msg"></p>`)}
    ${panel("", table(["ID", "患者", "机构", "医生", "服务包", "状态", "操作"], contracts, (c) =>
      `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.org_id}</td><td>${esc(c.doctor_name)}</td>
       <td><span class="tag">${esc(PKG[c.package] || c.package)}</span></td>
       <td><span class="tag ${c.status === "active" ? "green" : "red"}">${c.status === "active" ? "履约中" : "已解约"}</span></td>
       <td>${c.status === "active"
         ? `<button class="btn secondary" data-svc="${c.id}">记录履约</button>
            <button class="btn danger" data-term="${c.id}">解约</button>` : "—"}</td></tr>`))}`;
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
        // P2-38：原先两连问——履约类型要手打英文代码（打错被后端 422 拒回），备注框点取消照样记。
        // 合成一个表单：类型从下拉里选，取消就是不记。
        const form = await spdModal("记录履约", [
          { name: "service_type", label: "履约类型", type: "select", value: "followup",
            options: Object.entries(SVC).map(([value, label]) => ({ value, label })) },
          { name: "note", label: "备注", type: "textarea" },
        ]);
        if (!form) return;
        await api(`/api/contracts/${svc}/services`, { method: "POST", body: JSON.stringify(form) });
        setMsg("#ct-msg", "履约已记录", true);
      }
      if (term) {
        // 解约原先点一下就生效、没有任何确认：一次误点就把一户的家医签约解掉了，页面上也没有恢复入口。
        if (!await spdModal("解约", [], { intro: "解约后该签约不再记录履约；如需恢复，须重新签约。" })) return;
        await api(`/api/contracts/${term}/terminate`, { method: "POST" }); route();
      }
    } catch (err) { setMsg("#ct-msg", err.message, false); }
  };
  await drawHomeVisits();  // 块4⑨ 上门服务调度
}

async function renderAppointments() {
  $("#page-desc").textContent = "智能导诊 + 机构发布分时段号源，一站式预约挂号/检查/检验";
  const [slots, appointments, blacklist] = await Promise.all([
    api("/api/appointments/slots"), api("/api/appointments"), api("/api/appointments/blacklist")]);
  const RT = { outpatient: "门诊", exam: "检查", lab: "检验" };
  const AS = { booked: ["已预约", "green"], cancelled: ["已取消", "red"], fulfilled: ["已就诊", ""] };
  $("#page-body").innerHTML = `
    ${panel("智能导诊台（功能指引 ⑨）", `
      <p class="desc">分诊工位用：录症状出科室建议，急症症状直接提示走急诊——导诊完就地预约</p>
      <form class="inline" id="triage-form">
        <input name="symptoms" placeholder="症状，逗号分隔（如：胸痛,心悸）" required style="min-width:260px">
        <button>导诊</button>
      </form><p class="msg" id="triage-msg"></p>
      <div id="triage-result"></div>`)}
    ${panel("发布号源", `
      <form class="inline" id="slot-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type">${Object.entries(RT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="resource_name" placeholder="资源名称（如CT室上午）" required>
        <input name="slot_date" placeholder="日期 YYYY-MM-DD" required>
        <input name="slot_time" placeholder="时段（如09:00-10:00）">
        <input name="capacity" type="number" value="5" min="1" style="min-width:70px">
        <button>发布</button>
      </form>
      <p class="desc" style="margin-top:12px">批量排班：一条模板 × 一段日期，已有号源的日期自动跳过（幂等，补生成可重跑）</p>
      <form class="inline" id="slot-batch-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type">${Object.entries(RT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="resource_name" placeholder="资源名称" required>
        <input name="employee_id" type="number" placeholder="医师ID（可选）" style="width:130px">
        <input name="slot_time" placeholder="时段 09:00-10:00" style="width:150px">
        <input name="capacity" type="number" value="5" min="1" style="width:80px">
        <input name="date_from" placeholder="起 YYYY-MM-DD" required>
        <input name="date_to" placeholder="止 YYYY-MM-DD" required>
        <input name="skip_dates" placeholder="跳过日期，逗号分隔" style="width:180px">
        <label style="font-size:13px"><input type="checkbox" name="skip_weekends" value="true"> 跳过周末</label>
        <button class="secondary">批量生成</button>
      </form>
      <h3 style="margin-top:14px">预约</h3>
      <form class="inline" id="book-form">
        <input name="slot_id" type="number" placeholder="号源ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <button>预约</button>
      </form><p class="msg" id="apt-msg"></p>`)}
    ${panel("号源", table(["ID", "机构", "类型", "资源", "日期/时段", "已约/容量"], slots, (s) =>
      `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${esc(RT[s.resource_type] || s.resource_type)}</td><td>${esc(s.resource_name)}</td>
       <td>${esc(s.slot_date)} ${esc(s.slot_time)}</td>
       <td><span class="tag ${s.booked >= s.capacity ? "red" : "green"}">${s.booked}/${s.capacity}</span></td></tr>`))}
    ${panel("便捷寻医（指引⑨）", `
      <p class="desc">按姓名 / 科室 / 职称找医师并带出近期可约号源。<b>没号的医师也在列</b>并标注——
        只给有号的，居民会以为这位医师不存在。跨机构可查：这是面向居民的寻医目录，不是管理数据</p>
      <form class="inline" id="doctor-form">
        <input name="keyword" placeholder="姓名 / 科室 / 职称" style="min-width:200px">
        <input name="org_id" type="number" placeholder="机构ID（可选）" style="width:140px">
        <input name="from_date" placeholder="起始日期 YYYY-MM-DD（默认今天）" style="width:230px">
        <button>寻医</button>
      </form><p class="msg" id="doctor-msg"></p>
      <div id="doctor-result"></div>`)}
    ${panel("服务黑名单（⑫ 爽约 / 缺药不取）", `
      <form class="inline" id="bl-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="domain"><option value="appointment">预约爽约</option><option value="shortage">缺药登记后不取药</option></select>
        <input name="reason" placeholder="原因" style="min-width:200px">
        <button>加入黑名单</button>
      </form><p class="msg" id="bl-msg"></p>
      ${table(["ID", "患者", "业务域", "原因", "操作"], blacklist, (b) =>
        `<tr><td>${b.id}</td><td>${b.patient_id}</td><td>${esc(b.domain_name)}</td><td>${esc(b.reason) || "—"}</td>
         <td><button class="btn secondary" data-blout="${b.patient_id}" data-domain="${esc(b.domain)}">移出</button></td></tr>`)}`)}
    ${panel("预约记录", table(["ID", "号源", "患者", "状态", "操作"], appointments, (a) => {
      return `<tr><td>${a.id}</td><td>${a.slot_id}</td><td>${a.patient_id}</td>
        <td>${statusTag(AS, a.status)}</td>
        <td>${a.status === "booked"
          ? `<button class="btn secondary" data-fulfill="${a.id}">核销</button>
             <button class="btn danger" data-cancel="${a.id}">取消</button>` : "—"}</td></tr>`;
    }))}`;
  $("#triage-form").onsubmit = async (e) => {
    e.preventDefault();
    const symptoms = String(new FormData(e.target).get("symptoms") || "")
      .split(/[，,、\s]+/).filter(Boolean);
    if (!symptoms.length) return;
    try {
      const r = await api("/api/triage/suggest", {
        method: "POST", body: JSON.stringify(symptoms) });
      $("#triage-result").innerHTML = `
        ${r.emergency_hint
          ? '<p class="msg err">⚠ 首选建议命中急症症状，请引导走急诊通道</p>' : ""}
        ${table(["建议科室", "命中症状", "急症"], r.recommendations, (x) =>
          `<tr><td>${esc(x.department)}</td>
           <td>${x.matched.length ? x.matched.map((s) => esc(s)).join("、") : "—"}</td>
           <td>${x.urgent ? '<span class="tag red">急症</span>' : "—"}</td></tr>`)}`;
      setMsg("#triage-msg", "");
    } catch (err) { setMsg("#triage-msg", err.message, false); }
  };
  $("#slot-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/appointments/slots", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), resource_type: f.get("resource_type"),
        resource_name: f.get("resource_name"), slot_date: f.get("slot_date"),
        // 清空 `capacity` 会送 0（`Number("")` 是 0 不是 NaN），而后端是
        // `Field(default=1, ge=1)`——0 违反 ge=1，用户拿到的是一句 422 而不是默认值。
        // 与 `#og-form`「空字符串要去掉」、`#pay-form` 的 `if (f.get("amount"))` 同一写法。
        slot_time: f.get("slot_time"),
        ...(f.get("capacity") ? { capacity: Number(f.get("capacity")) } : {}) }) });
      route();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  $("#slot-batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const template = {
      resource_type: f.get("resource_type"), resource_name: f.get("resource_name"),
      slot_time: f.get("slot_time") || "",
      // 与上面单条发布同一个写法：清空 capacity 会送 0，而后端是 ge=1
      ...(f.get("capacity") ? { capacity: Number(f.get("capacity")) } : {}),
      ...(f.get("employee_id") ? { employee_id: Number(f.get("employee_id")) } : {}),
    };
    try {
      const r = await api("/api/appointments/slots/batch", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), templates: [template],
        date_from: f.get("date_from"), date_to: f.get("date_to"),
        skip_dates: String(f.get("skip_dates") || "").split(/[，,\s]+/).filter(Boolean),
        skip_weekends: f.get("skip_weekends") === "true" }) });
      // 跳过数要说出来：幂等跳过与"什么都没生成"看起来一样，不报出来没人知道是补生成生效了
      setMsg("#apt-msg", `批量生成 ${r.created} 个号源，跳过已有 ${r.skipped} 个`);
      route();
    } catch (err) { setMsg("#apt-msg", err.message, false); }
  };
  $("#doctor-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const q = new URLSearchParams();
    ["keyword", "org_id", "from_date"].forEach((k) => { if (f.get(k)) q.set(k, f.get(k)); });
    try {
      const rows = await api(`/api/appointments/doctors?${q.toString()}`);
      $("#doctor-result").innerHTML = table(
        ["医师", "职称", "岗位", "机构", "可约号源", "近期号源"], rows, (d) =>
        `<tr><td>${esc(d.name)}</td><td>${esc(d.title) || "—"}</td><td>${esc(d.position) || "—"}</td>
         <td>${esc(d.org_name) || d.org_id}</td>
         <td>${d.bookable ? `<span class="tag green">${d.available_slots}</span>` : '<span class="tag">暂无号</span>'}</td>
         <td>${(d.next_slots || []).map((s) =>
            `${esc(s.slot_date)} ${esc(s.slot_time) || ""} ${esc(s.resource_name)}（余 ${s.remaining}，号源 ${s.slot_id}）`)
            .join("<br>") || "—"}</td></tr>`);
      setMsg("#doctor-msg", "");
    } catch (err) { $("#doctor-result").innerHTML = ""; setMsg("#doctor-msg", err.message, false); }
  };
  $("#bl-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/appointments/blacklist", formJson(e.target, ["patient_id"]), "#bl-msg");
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
    const { cancel, fulfill, blout, domain } = e.target.dataset;
    try {
      if (cancel) {
        // P2-43：原先点一下就生效——号源随即释放，居民那边的预约就没了
        if (!await spdModal("取消预约", [], { intro: "点「确定」作废这次预约，号源立即释放给他人，不能恢复；点「取消」保留。" })) return;
        await api(`/api/appointments/${cancel}/cancel`, { method: "POST" }); route();
      }
      if (fulfill) { await api(`/api/appointments/${fulfill}/fulfill`, { method: "POST" }); route(); }
      if (blout) {
        // domain 必须带上：后端按 (domain, patient_id) 定位，缺省是 appointment，
        // 移「缺药不取」那条时不带就会 404（或误删另一个业务域的那条）
        await api(`/api/appointments/blacklist/${blout}?domain=${encodeURIComponent(domain)}`, { method: "DELETE" });
        route();
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
    if (!resp.ok) {
      // 失败时后端回的是 JSON detail（如导出月份写错的 422）：照 api() 的口径报人话，别只给状态码
      let detail = null;
      try { detail = (await resp.json()).detail; } catch (e) { detail = null; }
      throw new Error(errorText(detail, `导出失败(${resp.status})`));
    }
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
    ${panel("上报报表导出（管理层）", `
      <p style="margin-bottom:8px">
        <button class="btn secondary" id="exp-monitor">监测指标CSV（14项）</button>
        <button class="btn secondary" id="exp-ops">运营月报CSV（业务量累计／绩效分当年）</button>
        <button class="btn" id="exp-ops-period">按月导出运营月报</button></p>
      <p class="msg" id="rpt-msg"></p>
      ${monitoring ? table(["#", "指标名", "口径", "当期值", "数据来源"], monitoring.indicators, (i) =>
        `<tr><td>${i.no}</td><td>${esc(i.name)}</td><td style="font-size:12.5px;color:#5b6773">${esc(i.caliber)}</td>
         <td><b>${esc(i.value)}</b> ${esc(i.unit)}</td><td><span class="tag">${esc(i.source)}</span></td></tr>`) : ""}`)}
    ${panel("机构评分排名", `
      <p class="desc">本页是<b>考核口径</b>：指标与权重来自指标目录，分数只统计
        当前评分周期（${esc(data.period)}）内的业务量。「决策分析」页的
        「期末综合绩效报告」走的是自定义公式，<b>两者不可比</b>。</p>
      ${data.scorecards.length ? barChart(data.scorecards.map((c) => [c.org_name, c.score]), { unit: " 分" }) : "暂无数据"}`)}
    ${panel("", table(["排名", "机构", "层级", "总分", "转诊结案", "共享诊断(申请/出报告)", "慢病随访", "处方合格(可审)", "家医履约"],
      data.scorecards, (c, i) => {
        const d = c.detail;
        return `<tr><td>${data.scorecards.indexOf(c) + 1}</td><td>${esc(c.org_name)}</td><td>${esc(LEVELS[c.level] || c.level)}</td>
          <td><b>${c.score}</b></td>
          <td>${d.referral_completion.completed}/${d.referral_completion.total}</td>
          <td>${d.remote_exams}<span style="color:#8a939e">（${d.remote_exams_requested}/${d.remote_exams_provided}）</span></td>
          <td>${d.chronic_followup.followed}/${d.chronic_followup.total}</td>
          <td>${d.rx_pass.passed}/${d.rx_pass.total}<span style="color:#8a939e">（可审 ${d.rx_pass.rule_covered}）</span></td>
          <td>${d.contract_services}</td></tr>`;
      }))}`;
  $("#exp-monitor").onclick = () => downloadCsv("/api/reports/monitoring/export", "monitoring_indicators.csv", "#rpt-msg");
  $("#exp-ops").onclick = () => downloadCsv("/api/reports/operations/export", "operations_report_all.csv", "#rpt-msg");
  $("#exp-ops-period").onclick = async () => {
    // P2-38：弹窗换成页内表单；月份的形状与日历由后端 require_month 判，写错报人话（downloadCsv 取 detail）
    const form = await spdModal("按月导出运营报表", [
      { name: "period", label: "导出月份", required: true, placeholder: "YYYY-MM，如 2026-07" }]);
    if (!form) return;
    downloadCsv(`/api/reports/operations/export?period=${encodeURIComponent(form.period)}`,
      `operations_report_${form.period}.csv`, "#rpt-msg");
  };
  await drawImprovementTasks();  // 块4㉟ 绩效自评改进
}

async function renderCssd() {
  $("#page-desc").textContent =
    "器械批次：灭菌中 → 已灭菌 → 已发放 → 已回收，全程追溯；基层物品申领与中心响应";
  const [batches, requests, orgs] = await Promise.all([
    api("/api/cssd/batches"), api("/api/cssd/requests?limit=200"), api("/api/organizations"),
  ]);
  const BS = { sterilizing: ["灭菌中", "orange"], sterile: ["已灭菌", ""], dispatched: ["已发放", "green"], recycled: ["已回收", "green"] };
  // 取值真源是 models/assets.py:CssdRequest.status 的列注释
  const RS = { requested: ["已申领", "orange"], fulfilled: ["已发放", "green"] };
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  // 后端只接受已完成灭菌的批次（sterile / dispatched），其余 409——按状态筛，别摆了等报错
  const usable = batches.filter((b) => ["sterile", "dispatched"].includes(b.status));
  $("#page-body").innerHTML = `
    ${panel("新建批次", `
      <form class="inline" id="batch-form">
        <input name="batch_no" placeholder="批次号" required>
        <input name="center_org_id" type="number" placeholder="消毒中心机构ID" required>
        <input name="item_name" placeholder="器械名称" required>
        <input name="quantity" type="number" placeholder="数量" required min="1">
        <button>创建</button>
      </form><p class="msg" id="cssd-msg"></p>`)}
    ${panel("", table(["ID", "批次号", "器械", "数量", "接收机构", "状态", "操作"], batches, (b) => {
      const next = { sterilizing: "标记已灭菌", sterile: "发放", dispatched: "回收" }[b.status];
      return `<tr><td>${b.id}</td><td><span class="tag">${esc(b.batch_no)}</span></td><td>${esc(b.item_name)}</td>
        <td>${b.quantity}</td><td>${b.dispatched_to_org_id ?? "—"}</td>
        <td>${statusTag(BS, b.status)}</td>
        <td>${next ? `<button class="btn secondary" data-adv="${b.id}" data-next="${esc(b.status)}">${next}</button>` : "—"}</td></tr>`;
    }))}
    ${panel("基层物品申领与中心响应", `
      <form class="inline" id="creq-form">
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">申领机构：${esc(o.name)}</option>`).join("")}</select>
        <input name="item_name" placeholder="物品名称" required>
        <input name="quantity" type="number" value="1" min="1" style="min-width:80px">
        <button>申领</button>
      </form>
      ${table(["ID", "申领机构", "物品", "数量", "状态", "响应批次", "操作"], requests, (r) =>
        `<tr><td>${r.id}</td><td>${esc(orgNames[r.org_id] || r.org_id)}</td>
         <td>${esc(r.item_name)}</td><td>${r.quantity}</td>
         <td>${statusTag(RS, r.status)}</td><td>${r.batch_id ?? "—"}</td>
         <td>${r.status === "requested"
           ? `<button class="btn secondary" data-creqful="${r.id}">以批次响应</button>` : "—"}</td></tr>`)}
      <p class="desc">响应时只能选<b>已完成灭菌</b>的批次（已灭菌或已发放，当前 ${usable.length} 个）——
        灭菌中的批次后端直接 409。申领一经响应即定批次，<b>没有反向端点</b>。</p>
      <p class="msg" id="creq-msg"></p>`)}`;
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
  $("#creq-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/cssd/requests", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), item_name: f.get("item_name"),
        quantity: Number(f.get("quantity")) }) });
      route();
    } catch (err) { setMsg("#creq-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { adv, next, creqful } = e.target.dataset;
    if (creqful) {
      if (!usable.length) return setMsg("#creq-msg", "没有已完成灭菌的批次可响应", false);
      try {
        const picked = await spdModal(`响应申领 ${creqful}`, [
          { name: "batch_id", label: "以哪一批响应（只列已完成灭菌的）", type: "select",
            options: usable.map((b) => ({ value: b.id, label: `${b.batch_no}｜${b.item_name}×${b.quantity}` })) }]);
        if (!picked) return;
        await api(`/api/cssd/requests/${creqful}/fulfill?batch_id=${Number(picked.batch_id)}`, { method: "POST" });
        return route();
      } catch (err) { return setMsg("#creq-msg", err.message, false); }
    }
    if (!adv) return;
    try {
      let qs = "";
      if (next === "sterile") {
        // 原先是输机构ID的弹窗——这一步是"发给谁"，选机构比默写数字靠谱
        const picked = await spdModal("发放批次", [
          { name: "dispatched_to_org_id", label: "接收机构", type: "select",
            options: orgs.map((o) => ({ value: o.id, label: o.name })) }]);
        if (!picked) return;
        qs = `?dispatched_to_org_id=${Number(picked.dispatched_to_org_id)}`;
      }
      await api(`/api/cssd/batches/${adv}/advance${qs}`, { method: "POST" });
      route();
    } catch (err) { setMsg("#cssd-msg", err.message, false); }
  };
  await drawCssdCosts();  // 块4⑥ 消毒供应成本核算
}

const WASTE_TRACE_STEPS = { "收集": "green", "暂存": "orange", "交接": "" };

async function renderMedwaste() {
  $("#page-desc").textContent =
    "点位台账 → 收集 → 入暂存 → 交接全过程监管，超2天未交接自动预警；扫码追溯与转运工作量";
  const [wastes, alerts, locations, stats] = await Promise.all([
    api("/api/medwaste"), api("/api/medwaste/alerts"),
    api("/api/medwaste/locations?include_inactive=true"), api("/api/medwaste/handler-stats"),
  ]);
  const alertIds = new Set(alerts.map((w) => w.id));
  const WT = { infectious: "感染性", sharp: "损伤性", pathological: "病理性", pharmaceutical: "药物性", chemical: "化学性" };
  const WS = { collected: ["已收集", "orange"], stored: ["已暂存", "orange"], handed_over: ["已交接", "green"] };
  // 暂存间按机构分组：入暂存只能选本机构的暂存间（后端 422 拦跨机构）
  const storageOf = (orgId) => locations.filter((l) =>
    l.active && l.location_type === "storage" && l.org_id === orgId);
  // 产生点必填（模型注释「产生点必填」）：收集登记原先不送产生点，页面上收的每一袋医废追溯到的来源都是空的。
  // 机构取所选产生点的机构——后端本就要求点位属于该机构，再手输一遍只会多一种填错的方式
  const sources = locations.filter((l) => l.active && l.location_type === "source");
  $("#page-body").innerHTML = `
    ${panel("收集登记", `
      <form class="inline" id="waste-form">
        <select name="source_location_id" required><option value="">产生点（科室 / 病区）</option>${sources.map((l) =>
          `<option value="${l.id}">${esc(l.name)}（机构${l.org_id}）</option>`).join("")}</select>
        <select name="waste_type">${Object.entries(WT).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="weight_kg" type="number" step="any" placeholder="重量(kg)" required>
        <input name="collected_date" placeholder="收集日期 YYYY-MM-DD" required>
        <button>登记</button>
      </form><p class="msg" id="waste-msg"></p>
      ${sources.length ? "" : '<p class="desc">还没有在用的产生点：先在下方「点位台账」建一个产生点再登记收集</p>'}`)}
    ${alerts.length ? panel(`⚠ 滞留预警（${alerts.length}）`, `<p class="desc">收集超过2天仍未交接</p>`) : ""}
    ${panel("", `
      <form class="inline" id="trace-form">
        <input name="trace_code" placeholder="追溯码 MW-YYYYMMDD-序号" required style="min-width:220px">
        <button class="secondary">扫码追溯</button>
      </form><p class="msg" id="trace-msg"></p>
      <div id="trace-box"></div>
      ${table(["ID", "机构", "追溯码", "类别", "重量", "收集日期", "转运人", "状态", "操作"], wastes, (w) => {
      return `<tr><td>${w.id}</td><td>${w.org_id}</td><td><span class="tag">${esc(w.trace_code || "—")}</span></td>
        <td>${esc(WT[w.waste_type] || w.waste_type)}</td><td>${w.weight_kg}kg</td>
        <td>${esc(w.collected_date)}${alertIds.has(w.id) ? ' <span class="tag red">滞留</span>' : ""}</td>
        <td>${esc(w.handler_name) || "—"}</td><td>${statusTag(WS, w.status)}</td>
        <td>${w.status === "collected" ? `<button class="btn secondary" data-store="${w.id}" data-org="${w.org_id}">入暂存</button>` : ""}
            ${w.status !== "handed_over" ? `<button class="btn secondary" data-hand="${w.id}">交接</button>` : ""}
            ${w.status === "handed_over" ? "—" : ""}</td></tr>`;
    })}`)}
    ${panel("点位台账（产生点 / 暂存间）", `
      <p class="desc">停用不删行——科室撤并很常见，但历史医废的来源必须永远查得到</p>
      <form class="inline" id="loc-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="点位名称" required>
        <select name="location_type"><option value="source">产生点</option><option value="storage">暂存间</option></select>
        <input name="manager_name" placeholder="负责人">
        <button>新建点位</button>
      </form><p class="msg" id="loc-msg"></p>
      ${table(["ID", "机构", "名称", "类型", "负责人", "状态", "操作"], locations, (l) =>
        `<tr><td>${l.id}</td><td>${l.org_id}</td><td>${esc(l.name)}</td>
         <td>${esc(l.location_type_name)}</td><td>${esc(l.manager_name) || "—"}</td>
         <td>${l.active ? '<span class="tag green">在用</span>' : '<span class="tag">已停用</span>'}</td>
         <td><button class="btn secondary" data-loc-toggle="${l.id}" data-on="${l.active ? 0 : 1}">${l.active ? "停用" : "启用"}</button></td></tr>`)}`)}
    ${panel("转运人员工作量", `
      ${table(["员工ID", "姓名", "交接批次", "重量(kg)"], stats.handlers, (h) =>
        `<tr><td>${h.employee_id}</td><td>${esc(h.name)}</td><td>${h.count}</td><td>${h.weight_kg}</td></tr>`)}
      <p class="desc">未挂员工档案的交接单列 ${stats.unlinked_records.count} 批 /
        ${stats.unlinked_records.weight_kg} kg——只填了名字的历史记录不硬凑到某个人头上，名字重合就会张冠李戴</p>`)}`;
  $("#waste-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const source = sources.find((l) => l.id === Number(f.get("source_location_id")));
    if (!source) return setMsg("#waste-msg", "请选择产生点", false);
    try {
      await api("/api/medwaste", { method: "POST", body: JSON.stringify({
        org_id: source.org_id, source_location_id: source.id, waste_type: f.get("waste_type"),
        weight_kg: Number(f.get("weight_kg")), collected_date: f.get("collected_date") }) });
      route();
    } catch (err) { setMsg("#waste-msg", err.message, false); }
  };
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
  $("#trace-form").onsubmit = async (e) => {
    e.preventDefault();
    const code = new FormData(e.target).get("trace_code");
    try {
      const t = await api(`/api/medwaste/trace/${encodeURIComponent(code)}`);
      $("#trace-box").innerHTML = `<p class="desc">${esc(t.trace_code)} · ${esc(t.waste_type_name)} · ${t.weight_kg}kg · 机构 ${t.org_id}</p>
        ${table(["环节", "时间", "地点 / 经手人"], t.timeline, (s) =>
          `<tr><td><span class="tag ${WASTE_TRACE_STEPS[s.step] || ""}">${esc(s.step)}</span></td>
           <td>${esc(s.at.replace("T", " ").slice(0, 19))}</td><td>${esc(s.location) || "—"}</td></tr>`)}`;
      setMsg("#trace-msg", "");
    } catch (err) { $("#trace-box").innerHTML = ""; setMsg("#trace-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const hand = el("data-hand"), store = el("data-store"), locToggle = el("data-loc-toggle");
    if (store) {
      const options = storageOf(Number(store.dataset.org));
      if (!options.length) return setMsg("#waste-msg", "该机构还没有在用的暂存间，请先在下方点位台账里建一个", false);
      const form = await spdModal("入暂存间", [
        { name: "storage_location_id", label: "暂存间", type: "select",
          options: options.map((l) => ({ value: String(l.id), label: l.name })) },
      ]);
      if (!form) return;
      try {
        await api(`/api/medwaste/${store.dataset.store}/store`, { method: "POST",
          body: JSON.stringify({ storage_location_id: Number(form.storage_location_id) }) });
        route();
      } catch (err) { setMsg("#waste-msg", err.message, false); }
      return;
    }
    if (hand) {
      // 原先是系统输入框收转运人姓名（P2-38 存量弹窗录入）：挂了员工档案的该按 id 收，
      // 只填名字的那条路后端仍然留着（历史记录），所以两个字段都给、二选一
      const form = await spdModal("医废交接", [
        { name: "handler_employee_id", label: "转运人员工ID（优先，姓名由档案带出）", type: "number" },
        { name: "handler_name", label: "转运人姓名（没有员工档案时填）" },
      ]);
      if (!form) return;
      const body = form.handler_employee_id
        ? { handler_name: form.handler_name || "", handler_employee_id: form.handler_employee_id }
        : { handler_name: form.handler_name };
      if (!form.handler_employee_id && !form.handler_name) {
        return setMsg("#waste-msg", "请填员工ID或转运人姓名", false);
      }
      try {
        await api(`/api/medwaste/${hand.dataset.hand}/handover`, { method: "POST", body: JSON.stringify(body) });
        route();
      } catch (err) { setMsg("#waste-msg", err.message, false); }
      return;
    }
    if (locToggle) {
      const id = locToggle.dataset.locToggle;
      try {
        // 两条路径分开写而不是拼后缀：孤儿闸门按字面匹配，拼出来的地址它看不见
        // （全仓库字符串拼接 URL 为 0 处，判据据此收紧，别从这里开口子）
        if (locToggle.dataset.on === "1") {
          await api(`/api/medwaste/locations/${id}/reactivate`, { method: "POST" });
        } else {
          await api(`/api/medwaste/locations/${id}`, { method: "DELETE" });
        }
        route();
      } catch (err) { setMsg("#loc-msg", err.message, false); }
    }
  };
}

async function renderOrgs() {
  $("#page-desc").textContent = "县—乡—村三级医共体成员单位";
  const orgs = await api("/api/organizations");
  const options = orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  // 机构树体检是 require_admin：非管理员拿到 403，照驾驶舱绩效那处的先例**不阻塞本页**
  let healthHtml = "";
  try {
    const h = await api("/api/organizations/tree-health");
    healthHtml = panel(`机构树体检（${h.referral_ready ? "分级转诊口径已满足" : "有缺陷，会卡住分级审核"}）`, `
      <div class="cards">
        <div class="card"><div class="label">机构总数</div><div class="value">${h.total}</div></div>
        <div class="card"><div class="label">合法树根</div><div class="value">${h.roots}</div></div>
        <div class="card"><div class="label">最深链路</div><div class="value">${h.max_depth} 层</div></div>
        <div class="card"><div class="label">缺上级机构</div>
          <div class="value${h.orphans.length ? " warn" : ""}">${h.orphans.length}</div></div>
        <div class="card"><div class="label">层级错位</div>
          <div class="value${h.broken_chains.length ? " warn" : ""}">${h.broken_chains.length}</div></div>
      </div>
      <p class="desc">ADR-0004 起，转诊分级审核按 <code>parent_id</code> 逐级上收。
        <b>非顶层机构缺上级，它经手的转诊单就只有全域角色推得动，其余账号一律 403</b>——
        这张表的用处是在越权校验"咬人"之前先把树建好，而不是等基层报障。
        「最深链路」是纯信息项，不参与判定。</p>
      ${h.orphans.length ? `<h3 style="margin-top:12px">缺上级机构（会被 403）</h3>${
        table(["ID", "名称", "层级", "类型"], h.orphans, (o) =>
          `<tr><td>${o.id}</td><td>${esc(o.name)}</td><td>${esc(LEVELS[o.level] || o.level)}</td>
           <td>${esc(ORG_TYPES[o.org_type] || o.org_type)}</td></tr>`)}` : ""}
      ${h.broken_chains.length ? `<h3 style="margin-top:12px">层级错位</h3>${
        table(["ID", "名称", "层级", "实际上级层级", "应为", "链路"], h.broken_chains, (c) =>
          `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(LEVELS[c.level] || c.level)}</td>
           <td>${esc(LEVELS[c.parent_level] || c.parent_level)}</td>
           <td>${esc(c.expected_parent_levels.map((x) => LEVELS[x] || x).join(" / "))}</td>
           <td style="font-size:12px">${esc(c.chain.join(" → "))}</td></tr>`)}
        <p class="desc">判据是<b>层级相邻</b>而不是链路长度：市→县→乡→村四层是合法的市级牵头架构，
          而县→村室→村室 只有三层却已经错位——那张单子的「卫生院审核」会由一家村卫生室完成，
          环节名与实际处理机构对不上，闭环统计跟着失真。</p>` : ""}`);
  } catch (err) { /* 非管理员看不到体检，本页其余部分照常 */ }
  $("#page-body").innerHTML = `
    ${panel("新增机构", `
      <form class="inline" id="org-form">
        <input name="name" placeholder="机构名称" required>
        <select name="org_type">${Object.entries(ORG_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="level">${Object.entries(LEVELS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="parent_id"><option value="">无上级机构</option>${options}</select>
        <button>新增</button>
      </form><p class="msg" id="org-msg"></p>`)}
    ${healthHtml}
    ${panel("", table(["ID", "名称", "类型", "层级", "上级机构ID"], orgs, (o) =>
      `<tr><td>${o.id}</td><td>${esc(o.name)}</td><td>${ORG_TYPES[o.org_type] || esc(o.org_type)}</td>
       <td><span class="tag">${LEVELS[o.level] || esc(o.level)}</span></td><td>${o.parent_id ?? "—"}</td></tr>`))}`;
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
    ${panel("建档（重复身份证号幂等返回既有档案）", `
      <form class="inline" id="patient-form">
        <input name="name" placeholder="姓名" required>
        <input name="id_card" placeholder="身份证号" required minlength="15">
        <select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="phone" placeholder="电话">
        <button>建档</button>
      </form><p class="msg" id="patient-msg"></p>`)}
    ${panel("", `
      <form class="inline" id="patient-search"><input name="keyword" placeholder="姓名/身份证/健康卡号"><button>搜索</button></form>
      <div id="patient-table"></div>`)}
    ${panel("按电子健康卡号精确查", `
      <form class="inline" id="ehc-form">
        <input name="ehc_no" placeholder="电子健康卡号" required style="min-width:220px">
        <button>查档案</button></form>
      <p class="desc">上面那个搜索是<b>模糊匹配</b>（姓名/身份证/卡号都往里匹），
        窗口拿到一张卡时要的是<b>精确命中这一张</b>：卡号少打一位应当查不到，
        而不是模糊匹出一串同前缀的人来让人挑。
        两处都按角色脱敏（走 privacy 的 desensitize），没有全科室可见的明文。</p>
      <p class="msg" id="ehc-msg"></p>
      <div id="ehc-result"></div>`)}
    ${panel("档案调阅授权（医师/经办代录，患者知情）", `
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
      <p class="msg" id="auth-msg"></p><div id="auth-table"></div>`)}`;
  $("#ehc-form").onsubmit = async (e) => {
    e.preventDefault();
    const ehc = new FormData(e.target).get("ehc_no").trim();
    try {
      const p = await api(`/api/patients/${encodeURIComponent(ehc)}`);
      $("#ehc-result").innerHTML = table(
        ["ID", "电子健康卡号", "姓名", "身份证号", "性别", "电话"], [p], (x) =>
        `<tr><td>${x.id}</td><td><span class="tag">${esc(x.ehc_no)}</span></td><td>${esc(x.name)}</td>
         <td>${esc(x.id_card)}</td><td>${esc(x.gender)}</td><td>${esc(x.phone)}</td></tr>`);
      setMsg("#ehc-msg", "", true);
    } catch (err) {
      // 404 的文案是后端的「患者不存在」，原样给出来——这里最怕的是把"查不到"
      // 说成别的意思，窗口会以为系统坏了而不是卡号打错了
      $("#ehc-result").innerHTML = "";
      setMsg("#ehc-msg", err.message, false);
    }
  };
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
    // P2-43：原先点一下就生效；撤销后要恢复，得患者本人再来办一次授权
    if (!await spdModal("撤销调阅授权", [], {
      intro: "撤销后该机构不能再凭此授权调阅患者档案；如需恢复，须患者本人重新办理授权。" })) return;
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
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
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
    ${panel("", `
      <form class="inline" id="dict-form">
        <select id="dict-system">${Object.entries(systems).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <button>新增条目</button>
      </form><p class="msg" id="dict-msg"></p>
      <div id="dict-table"></div>
      <h3 style="margin-top:14px">批量导入</h3>
      <form id="dict-import">
        <textarea name="entries" rows="6" required
          style="width:100%;font-family:monospace;font-size:12px"
          placeholder='[{"code":"E11.9","name":"2型糖尿病"},{"code":"I10","name":"原发性高血压"}]'></textarea>
        <div class="inline" style="margin-top:8px"><button>导入到上方所选字典</button></div></form>
      <p class="desc">JSON 数组，每条至少 <code>code</code> 与 <code>name</code>；还可带
        spec / dosage_form / manufacturer / unit / insurance_code / national_code / extra。
        <b>已存在的编码会被跳过，而不是更新。</b>要改一条已有条目的名称，导入是不管用的——
        这一点与"导入"两个字给人的印象相反，所以写在这里。
        整批一次提交，撞车的那一条自己跳过，不会把整批带回滚。</p>
      <p class="msg" id="dict-import-msg"></p>`)}`;
  $("#dict-system").onchange = (e) => draw(e.target.value);
  $("#dict-import").onsubmit = async (e) => {
    e.preventDefault();
    const system = $("#dict-system").value;
    let entries;
    try { entries = JSON.parse(new FormData(e.target).get("entries")); }
    catch (err) { return setMsg("#dict-import-msg", `JSON 解析失败：${err.message}`, false); }
    if (!Array.isArray(entries) || !entries.length) {
      return setMsg("#dict-import-msg", "要一个非空的 JSON 数组", false);
    }
    try {
      const r = await api(`/api/dictionaries/${system}/import`,
        { method: "POST", body: JSON.stringify(entries) });
      setMsg("#dict-import-msg",
        `导入完成：新增 ${r.imported} 条，跳过（编码已存在）${r.skipped} 条`, true);
      await draw(system);
    } catch (err) { setMsg("#dict-import-msg", err.message, false); }
  };
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
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw("diagnosis");
}

/* 检验样本物流：后端 _SAMPLE_FLOW 的前端镜像。空 → 采样 → 转运 → 核收，核收即到头。
 * 键是当前状态、值是下一步的按钮文案；状态本身的中文名另表，两者别混。 */
const SAMPLE_NEXT = { "": "登记采样", collected: "发起转运", in_transit: "中心核收" };
const SAMPLE_STATUS = { "": "未采样", collected: "已采样", in_transit: "转运中", received: "已核收" };

async function renderExams() {
  $("#page-desc").textContent = "影像/心电/检验/病理：基层检查、上级诊断、结果互认、危急值管理";
  const [requests, critical, templates] = await Promise.all([
    api("/api/exams"), api("/api/exams/critical"), api("/api/exams/templates")]);
  $("#page-body").innerHTML = `
    ${panel("开单（先查互认）", `
      <form class="inline" id="exam-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="申请机构ID" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <input name="clinical_info" placeholder="临床信息">
        <button>提交申请</button>
      </form><p class="msg" id="exam-msg"></p>`)}
    ${critical.length ? panel(`⚠ 危急值（${critical.length}）`,
      table(["报告ID", "申请单", "结论", "操作"], critical, (r) =>
        `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
         <td><button class="btn secondary" data-printreport="${r.id}">打印报告</button>
             <button class="btn secondary" data-amend="${r.id}" data-conclusion="${esc(r.conclusion)}">修订</button>
             <button class="btn secondary" data-revs="${r.id}">修订史</button></td></tr>`)) : ""}
    ${panel("申请单", table(["ID", "患者", "中心", "项目", "状态", "样本", "操作"], requests, (r) => {
      let actions = r.status === "pending"
        ? `<button class="btn secondary" data-claim="${r.id}">领取</button>`
        : r.status === "diagnosing"
        ? `<button class="btn secondary" data-report="${r.id}">出报告</button>` : "";
      actions += ` <button class="btn secondary" data-printreq="${r.id}">打印申请单</button>`;
      // 样本物流只有检验类有，且只在出报告前走（后端两处分别 422 / 409）；已核收即到头
      const flow = r.center_type === "lab" && ["pending", "diagnosing"].includes(r.status)
        && SAMPLE_NEXT[r.sample_status || ""]
        ? ` <button class="btn secondary" data-sample="${r.id}">${SAMPLE_NEXT[r.sample_status || ""]}</button>` : "";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${esc(CENTER_NAMES[r.center_type] || r.center_type)}</td>
        <td>${esc(r.item_name)}</td><td>${statusTag(EXAM_STATUS, r.status)}</td>
        <td>${r.center_type === "lab" ? esc(SAMPLE_STATUS[r.sample_status || ""] || r.sample_status) : "—"}</td>
        <td>${actions}${flow}</td></tr>`;
    }))}
    ${panel("报告模板（管理员维护，出报告时照着写）", `
      <form class="inline" id="tpl-form">
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="name" placeholder="模板名称" required>
        <input name="content" placeholder="模板正文" style="min-width:260px">
        <button>新建模板</button>
      </form><p class="msg" id="tpl-msg"></p>
      ${table(["ID", "中心", "名称", "正文"], templates, (t) =>
        `<tr><td>${t.id}</td><td>${esc(CENTER_NAMES[t.center_type] || t.center_type)}</td><td>${esc(t.name)}</td>
         <td>${esc(t.content) || "—"}</td></tr>`)}`)}
    ${panel("报告修订与修订史（限医师；改前值逐条留痕）", `
      <p class="desc">危急值报告可直接在上方预警表里改；这里按报告 ID 找任意一份。
        修订会把改前的结论 / 所见 / 危急标记连同修订人与理由写进历史表；仍为危急值的会把闭环状态复位为「已通知」须重新确认</p>
      <form class="inline" id="rev-form">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <button class="secondary">查修订史</button>
      </form><p class="msg" id="rev-msg"></p>
      <div id="rev-box"></div>`)}
    ${panel("报告打印", `
      <form class="inline" id="exam-print-form">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <button>打印报告单</button></form>
      <p class="msg" id="exam-print-msg"></p>`)}
    ${panel("报告附件（影像截图/PDF，≤10MB，医师/经办上传）", `
      <form class="inline" id="exam-att-form">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传</button></form>
      <form class="inline" id="exam-att-query">
        <input name="report_id" type="number" placeholder="报告ID" required>
        <button>查附件</button></form>
      <p class="msg" id="exam-att-msg"></p><div id="exam-att-list"></div>`)}`;
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
      // 中心类型与申请机构一并送去预检（P2-145）：建单侧按它们判能不能互认，预检不带，就会弹出一份建单时 422 的「可互认」
      const check = await api(`/api/exams/recognition-check?patient_id=${patientId}&item_code=${encodeURIComponent(itemCode)}`
        + `&center_type=${encodeURIComponent(f.get("center_type"))}&from_org_id=${Number(f.get("from_org_id"))}`);
      let extra = {};
      if (check.recognizable) {
        // P2-38：原先是 confirm「确定=互认」——取消就是"不互认"，接着弹理由框，理由框再点取消
        // 照样开单（理由记"未填写"）：想放弃开单的人连点两次取消，反而开出一张重复检查。
        // 表单里互认与否是显式选择，取消就是不开单。
        const form = await spdModal("可互认：30 天内已有同项目报告", [
          { name: "decision", label: "处理方式", type: "select", value: "accept", options: [
            { value: "accept", label: "互认该结果，不再重复检查" },
            { value: "decline", label: "不互认，仍开新检查" }] },
          { name: "reason", label: "不互认理由（选「不互认」时填写，监管留痕）", type: "textarea" },
        ], { intro: `已有报告结论：${check.conclusion || "—"}` });
        if (!form) return;
        extra = form.decision === "accept"
          ? { accept_recognition_of: check.request_id }
          : { recognition_declined_reason: form.reason || "未填写" };
      }
      await api("/api/exams", { method: "POST", body: JSON.stringify({
        patient_id: patientId, from_org_id: Number(f.get("from_org_id")),
        center_type: f.get("center_type"), item_code: itemCode,
        item_name: f.get("item_name"), clinical_info: f.get("clinical_info"), ...extra }) });
      route();
    } catch (err) { setMsg("#exam-msg", err.message, false); }
  };
  const drawRevisions = async (reportId) => {
    const rows = await api(`/api/exams/reports/${reportId}/revisions`);
    $("#rev-box").innerHTML = table(["ID", "改前结论", "改前所见", "改前危急", "修订人", "理由", "时间"], rows, (r) =>
      `<tr><td>${r.id}</td><td>${esc(r.prev_conclusion) || "—"}</td><td>${esc(r.prev_finding) || "—"}</td>
       <td>${r.prev_critical ? '<span class="tag red">是</span>' : "否"}</td>
       <td>${esc(r.revised_by) || "—"}</td><td>${esc(r.reason) || "—"}</td>
       <td>${esc((r.at || "").replace("T", " ").slice(0, 19))}</td></tr>`);
  };
  $("#tpl-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/exams/templates", formJson(e.target), "#tpl-msg");
  };
  $("#rev-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await drawRevisions(new FormData(e.target).get("report_id"));
      setMsg("#rev-msg", "");
    } catch (err) { $("#rev-box").innerHTML = ""; setMsg("#rev-msg", err.message, false); }
  };
  $("#exam-print-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await openPrintPage(`/api/print/exam-reports/${new FormData(e.target).get("report_id")}`); }
    catch (err) { setMsg("#exam-print-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const claim = e.target.dataset.claim, report = e.target.dataset.report;
    const { printreq, printreport, sample, amend, revs } = e.target.dataset;
    try {
      if (printreq) return await openPrintPage(`/api/print/exam-requests/${printreq}`);
      if (printreport) return await openPrintPage(`/api/print/exam-reports/${printreport}`);
      if (sample) { await api(`/api/exams/${sample}/sample/advance`, { method: "POST" }); route(); return; }
      if (revs) {
        try { await drawRevisions(revs); setMsg("#rev-msg", ""); }
        catch (err) { setMsg("#rev-msg", err.message, false); }
        return;
      }
      if (amend) {
        const form = await spdModal("修订报告（改前值会连同理由留痕）", [
          { name: "conclusion", label: "新结论", value: e.target.dataset.conclusion, required: true },
          { name: "finding", label: "新所见（留空不改）" },
          { name: "critical", label: "危急值标记", type: "select", value: "keep",
            options: [{ value: "keep", label: "不改" }, { value: "1", label: "是危急值" }, { value: "0", label: "解除危急" }] },
          { name: "reason", label: "修订理由", type: "textarea" },
        ]);
        if (!form || !form.conclusion) return;
        const body = { conclusion: form.conclusion, reason: form.reason || "" };
        // finding/critical 是 `| None` 的可选项：不改就别送，送 null 会把所见清空
        if (form.finding) body.finding = form.finding;
        if (form.critical !== "keep") body.critical = form.critical === "1";
        const r = await api(`/api/exams/reports/${amend}`, { method: "PATCH", body: JSON.stringify(body) });
        setMsg("#exam-msg", `报告 ${r.id} 已修订${r.critical ? `（仍为危急值，闭环状态 ${r.critical_status_name}）` : "（非危急值）"}`);
        route();
        return;
      }
      if (claim) { await api(`/api/exams/${claim}/claim`, { method: "POST" }); route(); }
      if (report) {
        const form = await spdModal("出报告", [
          { name: "conclusion", label: "诊断结论", required: true },
          { name: "finding", label: "影像所见 / 检查所见" },
          { name: "critical", label: "是否危急值", type: "select", value: "0",
            options: [{ value: "0", label: "否" }, { value: "1", label: "是（进危急值闭环）" }] },
        ]);
        if (!form || !form.conclusion) return;
        await api(`/api/exams/${report}/report`, { method: "POST",
          body: JSON.stringify({ conclusion: form.conclusion, finding: form.finding || "",
                                 critical: form.critical === "1" }) });
        route();
      }
    } catch (err) { setMsg("#exam-msg", err.message, false); }
  };
}

async function renderReferrals() {
  $("#page-desc").textContent = "医共体内上转/下转：申请 → 接诊 → 结案";
  const referrals = await api("/api/referrals");
  $("#page-body").innerHTML = `
    ${panel("转诊申请", `
      <form class="inline" id="ref-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="from_org_id" type="number" placeholder="转出机构ID" required>
        <input name="to_org_id" type="number" placeholder="转入机构ID" required>
        <select name="direction"><option value="up">上转</option><option value="down">下转</option></select>
        <input name="reason" placeholder="转诊原因">
        <button>提交</button>
      </form><p class="msg" id="ref-msg"></p>`)}
    ${panel("", table(["ID", "患者", "方向", "转出→转入", "原因", "状态", "操作"], referrals, (r) => {
      const text = r.status_label || r.status;
      const color = REF_STATUS_COLOR[r.status] || "";
      const actions = r.status === "pending"
        ? `<button class="btn secondary" data-status="accepted" data-id="${r.id}">接诊</button>
           <button class="btn danger" data-status="rejected" data-id="${r.id}">退回</button>`
        : r.status === "accepted"
        ? `<button class="btn secondary" data-status="completed" data-id="${r.id}">结案</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${r.direction === "up" ? "上转" : "下转"}</td>
        <td>${r.from_org_id} → ${r.to_org_id}</td><td>${esc(r.reason)}</td>
        <td><span class="tag ${color}">${esc(text)}</span></td>
        <td>${actions} <button class="btn secondary" data-print-ref="${r.id}">打印转诊单</button></td></tr>`;
    }))}`;
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
    const { status, id, printRef } = e.target.dataset;
    if (printRef) {
      try { await openPrintPage(`/api/print/referrals/${printRef}`); }
      catch (err) { setMsg("#ref-msg", err.message, false); }
      return;
    }
    if (!status || !id) return;
    try { await api(`/api/referrals/${id}/status`, { method: "PATCH", body: JSON.stringify({ status }) }); route(); }
    catch (err) { setMsg("#ref-msg", err.message, false); }
  };
}

// active 是个 bool，没有后端文案可取；这里把它映成两个状态码再走 statusTag，
// 与本页其余状态列同一种写法（别在表格里直接写三元的中文）
const RULE_STATUS = { on: ["生效中", "green"], off: ["已停用", "red"] };

async function renderRx() {
  $("#page-desc").textContent = "“系统+药师”双重审方，每方必审；事后处方点评（药师）与合理率监管";
  const [prescriptions, rules, cstats, creviews] = await Promise.all([
    // 带上 include_inactive：不带的话停用的规则整行看不见，于是"这条规则怎么不生效了"
    // 在界面上无从查起，重新启用更无从谈起（后端 _active_rule 把停用一律当"未维护"）
    api("/api/prescriptions"), api("/api/prescriptions/rules?include_inactive=true"),
    api("/api/prescriptions/comment-stats"), api("/api/prescriptions/comment-reviews")]);
  const canComment = ["pharmacist", "admin"].includes(currentRole());
  // 规则的增删停启后端都是 require_admin：不是 admin 就别摆按钮，摆了只会点出 403
  const canRule = currentRole() === "admin";
  const commented = new Set(creviews.map((c) => c.prescription_id));
  $("#page-body").innerHTML = `
    ${panel("开方（单药演示）", `
      <form class="inline" id="rx-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="diagnosis_name" placeholder="诊断">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="daily_dose" type="number" step="any" placeholder="日剂量" required>
        <input name="days" type="number" value="7" min="1" style="min-width:70px">
        <button>提交处方</button>
      </form><p class="msg" id="rx-msg"></p>`)}
    ${panel("用药规则库", `
      <form class="inline" id="rule-form">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="max_daily_dose" type="number" step="any" placeholder="日剂量上限" required>
        <input name="dose_unit" placeholder="单位" value="mg" style="min-width:70px">
        <button>新增规则</button>
      </form>
      ${canRule ? `<details style="margin:8px 0">
        <summary style="cursor:pointer;font-size:13px">批量导入（同 drug_code 整条覆盖，不存在则新建）</summary>
        <form id="ruleimp-form">
          <textarea name="payload" rows="5" style="width:100%;font-family:monospace"
            placeholder='[{"drug_code":"AMOX","max_daily_dose":3000,"dose_unit":"mg","interactions":"","contraindicated_diagnoses":"","special_groups":"","renal_hepatic_note":"","review_points":"","antibiotic":true,"ddd":1500}]'></textarea>
          <p class="desc">一个 JSON 数组，每项一条规则；只有 drug_code 与 max_daily_dose 必填，其余留空走默认。
            <b>同 drug_code 的既有规则会被整条覆盖</b>（不是合并字段），回执报出新建与更新各几条。</p>
          <button class="btn">导入</button>
        </form></details>` : ""}
      ${table(["药品编码", "日剂量上限", "相互作用", "禁忌诊断", "特殊人群", "肝肾功能提示", "抗菌/DDD", "状态"]
          .concat(canRule ? ["操作"] : []), rules, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${r.max_daily_dose}${esc(r.dose_unit)}</td>
         <td>${esc(r.interactions) || "—"}</td><td>${esc(r.contraindicated_diagnoses) || "—"}</td>
         <td>${esc(r.special_groups) || "—"}</td><td>${esc(r.renal_hepatic_note) || "—"}</td>
         <td>${r.antibiotic ? `抗菌药物 / ${r.ddd ? `DDD ${r.ddd}` : '<span class="tag orange">DDD 未维护</span>'}` : "—"}</td>
         <td>${statusTag(RULE_STATUS, r.active ? "on" : "off")}</td>
         ${canRule ? `<td>${r.active
           ? `<button class="btn danger" data-ruleoff="${esc(r.drug_code)}">停用</button>`
           : `<button class="btn secondary" data-ruleon="${esc(r.drug_code)}">启用</button>`}</td>` : ""}</tr>`)}
      <p class="desc">停用不删行：规则改过什么、什么时候不再生效，处方点评复核时要回溯得到。
        <b>停用期间该药按"规则未维护"处理</b>——不是按上限 0 拦截，是根本不参与审方。</p>`)}
    ${panel("处方队列", table(["ID", "患者", "诊断", "状态", "审方意见", "操作"], prescriptions, (p) => {
      let actions = p.status === "pending_review"
        ? `<button class="btn secondary" data-approve="1" data-id="${p.id}">通过</button>
           <button class="btn danger" data-approve="0" data-id="${p.id}">退回</button>` : "";
      if (canComment && !commented.has(p.id)) actions += ` <button class="btn" data-rxcomment="${p.id}">点评</button>`;
      actions += ` <button class="btn secondary" data-printrx="${p.id}">打印</button>`;
      return `<tr><td>${p.id}</td><td>${p.patient_id}</td><td>${esc(p.diagnosis_name)}</td>
        <td>${statusTag(RX_STATUS, p.status)}</td><td>${esc(p.review_comment) || "—"}</td><td>${actions || "—"}</td></tr>`;
    }))}
    ${panel("处方点评（事后监管）", `
      <div class="cards">
        <div class="card"><div class="label">已点评处方</div><div class="value">${cstats.commented}</div></div>
        <div class="card"><div class="label">不合理处方</div><div class="value${cstats.unreasonable ? " warn" : ""}">${cstats.unreasonable}</div></div>
        <div class="card"><div class="label">点评合理率</div><div class="value">${cstats.reasonable_rate_pct}%</div></div></div>
      ${table(["处方ID", "结论", "问题类型", "点评意见", "时间"], creviews, (c) =>
        `<tr><td>${c.prescription_id}</td>
         <td><span class="tag ${c.grade === "reasonable" ? "green" : "red"}">${c.grade === "reasonable" ? "合理" : "不合理"}</span></td>
         <td>${esc(c.issues) || "—"}</td><td>${esc(c.comment) || "—"}</td><td>${esc(c.at.slice(0, 16).replace("T", " "))}</td></tr>`)}`)}`;
  $("#rx-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const p = await api("/api/prescriptions", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), org_id: Number(f.get("org_id")),
        diagnosis_name: f.get("diagnosis_name"),
        items: [{ drug_code: f.get("drug_code"), drug_name: f.get("drug_name"),
          // 同上：清空 `days` 送 0，后端 `Field(default=1, ge=1)` 直接 422。
          daily_dose: Number(f.get("daily_dose")),
          ...(f.get("days") ? { days: Number(f.get("days")) } : {}) }] }) });
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
  const impForm = $("#ruleimp-form");
  if (impForm) impForm.onsubmit = async (e) => {
    e.preventDefault();
    let rows;
    try {
      rows = JSON.parse(new FormData(e.target).get("payload"));
    } catch (err) {
      // 自己先说清楚是 JSON 没写对，别把一句 422 的字段路径丢给人
      return setMsg("#rx-msg", `JSON 解析失败：${err.message}`, false);
    }
    if (!Array.isArray(rows) || !rows.length) return setMsg("#rx-msg", "要一个非空的 JSON 数组", false);
    try {
      const r = await api("/api/prescriptions/rules/import", { method: "POST", body: JSON.stringify(rows) });
      setMsg("#rx-msg", `导入完成：新建 ${r.imported} 条，覆盖更新 ${r.updated} 条`, true);
      route();
    } catch (err) { setMsg("#rx-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { approve, id, rxcomment, printrx, ruleoff, ruleon } = e.target.dataset;
    if (ruleoff || ruleon) {
      // 两条路径分开写而不是拼动作：孤儿闸门按字面匹配，拼出来的地址它看不见
      try {
        if (ruleoff) {
          if (!confirm(`停用 ${ruleoff} 的规则后，该药此后一律按"规则未维护"通过审方。确认停用？`)) return;
          await api(`/api/prescriptions/rules/${encodeURIComponent(ruleoff)}`, { method: "DELETE" });
        } else {
          await api(`/api/prescriptions/rules/${encodeURIComponent(ruleon)}/reactivate`, { method: "POST" });
        }
        return route();
      } catch (err) { return setMsg("#rx-msg", err.message, false); }
    }
    if (printrx) {
      try { return await openPrintPage(`/api/print/prescriptions/${printrx}`); }
      catch (err) { return setMsg("#rx-msg", err.message, false); }
    }
    // P2-38：点评原先是 alert 看要点 → confirm「确定=合理，取消=不合理」→ 两连问——点"取消"想放弃，
    // 记下的却是"不合理"，这一路根本没有放弃的出口；审方的"药师意见"点取消照样提交通过/驳回。
    // 换成页内表单：要点放在表单上方照着填，取消就是放弃。
    if (rxcomment) {
      // 块2：点评规则化——先调阅该方药品的点评要点与肝肾提示，再作结论
      let intro;
      try {
        const rp = await api(`/api/prescriptions/${rxcomment}/review-points`);
        const lines = rp.items.map((i) => `${i.drug_name}（${i.drug_code}）${i.dose_exceeded ? "【日剂量超限】" : ""}\n  要点：${i.review_points || "规则库未维护"}\n  肝肾：${i.renal_hepatic_note || "—"}`);
        intro = `点评要点（规则覆盖 ${rp.rule_coverage_pct}%）：\n${lines.join("\n")}`;
      } catch (err) { intro = `点评要点取不到：${err.message}`; }
      const v = await spdModal(`处方点评（处方 ${rxcomment}）`, [
        { name: "grade", label: "结论", type: "select",
          options: [{ value: "reasonable", label: "合理" }, { value: "unreasonable", label: "不合理" }] },
        { name: "issues", label: "问题类型（不合理时与点评意见至少填一项，如：用法用量不适宜）" },
        { name: "comment", label: "点评意见", type: "textarea" },
      ], { intro });
      if (!v) return;
      return postAction(`/api/prescriptions/${rxcomment}/comment-review`, v, "#rx-msg");
    }
    if (approve === undefined || !id) return;
    const v = await spdModal(approve === "1" ? `审方通过（处方 ${id}）` : `审方驳回（处方 ${id}）`, [
      { name: "comment", label: approve === "1" ? "药师意见（可空）" : "驳回理由", type: "textarea" }]);
    if (!v) return;
    try {
      await api(`/api/prescriptions/${id}/review`, { method: "POST",
        body: JSON.stringify({ approve: approve === "1", comment: v.comment }) });
      route();
    } catch (err) { setMsg("#rx-msg", err.message, false); }
  };
}

async function renderPharmacy() {
  $("#page-desc").textContent =
    "库存管理、批号效期、西药发药、县乡村余缺调拨、缺药预警、批次召回与按批号反查、采购建议";
  const [stocks, alerts, expiring, dispenses, batches, suggestions] = await Promise.all([
    api("/api/pharmacy/stocks"), api("/api/pharmacy/alerts"),
    api("/api/pharmacy/batches/expiring"), api("/api/dispense"),
    api("/api/pharmacy/batches?limit=200"), api("/api/pharmacy/purchase-suggestions"),
  ]);
  const alertIds = new Set(alerts.map((a) => a.id));
  // 取值真源是 models/pharmacy.py:DrugBatch.status——只有这两个值，
  // 且它只表达"人决定召回"，过没过期是按效期现算的另一回事（见该列的注释）
  const BATCH_STATUS = { normal: ["正常", "green"], recalled: ["已召回", "red"] };
  const DISPENSE_STATUS = { dispensed: ["已发药", "green"], reversed: ["已冲销", "red"] };
  // 第二个面板的外壳**迁不了** `panel()`：它的标题里嵌着一个 `<span>`（缺药预警条数），
  // 而组件会把标题整段 `esc()` 掉，迁过去那个 span 会变成一段转义文本显示出来。
  // 同形状的还有慢病页与 openDrilldown，共 3 处，理由记在 docs/adr/0009 第十三批。
  $("#page-body").innerHTML = `
    ${panel("入库", `
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
      </form><p class="msg" id="pharm-msg"></p>`)}
    <div class="panel"><h3>库存${alerts.length ? `（<span style="color:#c62828">${alerts.length} 项缺药预警</span>）` : ""}</h3>
      ${table(["机构ID", "药品", "数量", "阈值", "状态"], stocks, (s) =>
        `<tr><td>${s.org_id}</td><td>${esc(s.drug_name)}（${esc(s.drug_code)}）</td><td>${s.quantity}</td><td>${s.threshold}</td>
         <td>${alertIds.has(s.id) ? '<span class="tag red">缺药</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}
      <h3 style="margin-top:14px">近效期批次（90 天）</h3>
      ${table(["机构ID", "药品", "批号", "效期", "余量", "剩余天数"], expiring, (b) =>
        `<tr><td>${b.org_id}</td><td>${esc(b.drug_name)}（${esc(b.drug_code)}）</td><td>${esc(b.batch_no)}</td>
         <td>${esc(b.expire_date)}</td><td>${b.remaining}</td>
         <td>${b.expired ? '<span class="tag red">已过期</span>' : `${b.remaining_days} 天`}</td></tr>`)}
      <h3 style="margin-top:14px">发药记录</h3>
      ${table(["ID", "处方ID", "状态", "明细（批号×数量）"], dispenses, (d) =>
        `<tr><td>${d.id}</td><td>${d.prescription_id}</td>
         <td>${d.status === "reversed" ? '<span class="tag red">已冲销</span>' : '<span class="tag green">已发药</span>'}</td>
         <td>${d.items.map((i) => `${esc(i.drug_name)} ${esc(i.batch_no)}×${i.quantity}`).join("，")}</td></tr>`)}</div>
    ${panel("批次台账（召回后不得再发药、不得再入库，余量同事务退出可用汇总）",
      table(["ID", "机构", "药品", "批号", "效期", "总量/已用", "可用", "不可发", "状态", "操作"], batches, (b) =>
        `<tr><td>${b.id}</td><td>${b.org_id}</td><td>${esc(b.drug_name)}（${esc(b.drug_code)}）</td>
         <td>${esc(b.batch_no)}</td><td>${esc(b.expire_date)}</td><td>${b.quantity} / ${b.used_quantity}</td>
         <td>${b.available}</td><td>${b.blocked_quantity}</td>
         <td>${statusTag(BATCH_STATUS, b.status)}${b.recall_reason
           ? `<br><span class="desc">${esc(b.recall_reason)}</span>` : ""}</td>
         <td>${b.status === "normal" ? `<button class="btn danger" data-recall="${b.id}">召回</button>` : ""}
             <button class="btn" data-trace="${b.id}">发给了谁</button></td></tr>`)
      + `<p class="desc">「发给了谁」是召回时唯一有用的那个查询：按批号反查这一批的发药去向，
         含已冲销的行（冲销的不计入"仍在外面"的量，但行还在）。</p>
         <p class="msg" id="batch-msg"></p>`)}
    <div class="panel hidden" id="trace-panel"><h3>按批号反查发药去向</h3><div id="trace-body"></div></div>
    ${panel("采购建议（近 30 天处方用量 − 当前全网库存，只列差值为正的品种；退回处方不计入用量）",
      table(["药品编码", "药品", "近 30 天用量", "当前库存", "建议采购量"], suggestions, (g) =>
        `<tr><td>${esc(g.drug_code)}</td><td>${esc(g.drug_name)}</td><td>${g.usage_30d}</td>
         <td>${g.current_stock}</td><td><b>${g.suggested_quantity}</b></td></tr>`))}`;
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
  $("#page-body").onclick = async (e) => {
    const { recall, trace } = e.target.dataset;
    try {
      if (recall) {
        const batch = batches.find((b) => b.id === Number(recall));
        const picked = await spdModal(`召回批次 ${batch ? batch.batch_no : recall}`, [
          { name: "reason", label: "召回原因（会随批次一起留存，后端必填）", type: "textarea" },
        ]);
        if (!picked || !picked.reason) return;
        const done = await api(`/api/pharmacy/batches/${recall}/recall`, {
          method: "POST", body: JSON.stringify({ reason: picked.reason }),
        });
        // 报出退出可用汇总的量：召回最要紧的后果是"账面上少了多少"，不是"状态翻了"
        setMsg("#batch-msg", `已召回，退出可用汇总 ${done.available} → 0，不可发余量 ${done.blocked_quantity}`, true);
      }
      if (trace) {
        const t = await api(`/api/pharmacy/batches/${trace}/dispenses`);
        $("#trace-panel").classList.remove("hidden");
        $("#trace-body").innerHTML = `
          <p class="desc">${esc(t.drug_name)}（${esc(t.drug_code)}）批号 ${esc(t.batch_no)}，
            效期 ${esc(t.expire_date)}，机构 ${t.org_id}，当前${statusTag(BATCH_STATUS, t.status)}；
            仍在外面（不含冲销）共 <b>${t.total_dispensed}</b>。</p>
          ${table(["发药ID", "处方ID", "患者", "数量", "状态", "发药时间"], t.dispenses, (r) =>
            `<tr><td>${r.dispense_id}</td><td>${r.prescription_id}</td>
             <td>${esc(r.patient_name) || "—"}（${r.patient_id}）</td><td>${r.quantity}</td>
             <td>${statusTag(DISPENSE_STATUS, r.status)}</td>
             <td>${esc(r.dispensed_at.slice(0, 16).replace("T", " "))}</td></tr>`)}`;
      }
    } catch (err) { setMsg("#batch-msg", err.message, false); }
  };
}

// active 是 bool；trend / risk_level 是后端的英文枚举，三处都要中文化（取值真源见 chronic.py）
const TYPE_STATUS = { on: ["启用", "green"], off: ["停用", "red"] };
const RISK_TREND = { rising: "上升", falling: "下降", stable: "平稳", insufficient_data: "数据不足" };
const RISK_LEVEL = { high: ["高危", "red"], medium: ["中危", "orange"], low: ["低危", "green"] };

async function renderChronic() {
  $("#page-desc").textContent = "病种目录驱动分级规则与随访周期，3级建议上转；膳食运动指导要点自动嵌入";
  const [chronicList, overdue, types] = await Promise.all([
    // 目录改成**取全部**（不带 active）：停用的病种也要能在目录里看到并重新启用，
    // 而且在管名单里那些挂着停用病种的档案，病种名也才查得到。
    // 建档下拉仍只列启用的——后端对停用病种直接 422。
    api("/api/chronic"), api("/api/chronic/overdue"), api("/api/chronic/disease-types"),
  ]);
  DISEASES = Object.fromEntries(types.map((t) => [t.code, t.name]));
  const activeTypes = types.filter((t) => t.active);
  const canType = currentRole() === "admin";
  const overdueIds = new Set(overdue.map((c) => c.id));
  // 各病种分级指标：随访录入时提示该病种应采集的指标与周期
  const typeRows = table(["ID", "病种", "编码", "分级指标", "随访周期", "状态"].concat(canType ? ["操作"] : []),
    types, (t) => {
      const keys = ((t.level_rules || {}).metrics || []).map((m) => `${m.name}(${m.key})`).join("、");
      return `<tr><td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(t.code)}</td>
        <td>${esc(keys) || "—"}</td><td>${t.followup_interval_days} 天</td>
        <td>${statusTag(TYPE_STATUS, t.active ? "on" : "off")}</td>
        ${canType ? `<td><button class="btn secondary" data-typeedit="${t.id}">编辑</button></td>` : ""}</tr>`;
    });
  // 第二个面板的外壳**迁不了** `panel()`：标题里嵌着一个 `<span>`（随访超期人数），
  // 形状同药房页，组件会把它整段 `esc()` 掉。理由记在 docs/adr/0009 第十三批。
  $("#page-body").innerHTML = `
    ${panel("慢病建档", `
      <form class="inline" id="chronic-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="disease">${activeTypes.map((t) => `<option value="${esc(t.code)}">${esc(t.name)}</option>`).join("")}</select>
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
        <input name="next_due" type="date" title="下次随访（留空按病种周期自动建议）">
        <button>提交随访</button>
      </form><p class="msg" id="chronic-msg"></p>
      <h3 style="margin-top:14px">病种目录（分级规则与随访周期的唯一数据源）</h3>
      ${canType ? `<form class="inline" id="chronic-type-form" style="margin-bottom:8px">
        <input name="code" placeholder="病种编码（英文）" required style="width:130px">
        <input name="name" placeholder="病种名称" required>
        <input name="followup_interval_days" type="number" value="90" placeholder="随访周期(天)" style="width:110px">
        <input name="guidance" placeholder="膳食运动指导要点" style="min-width:200px">
        <input name="level_rules" placeholder="分级规则 JSON（可留空，建好后可编辑）" style="min-width:220px">
        <button>新增病种</button>
      </form>` : ""}
      ${typeRows}
      <p class="desc">停用一个病种后<b>不能再按它建档</b>（后端 422），
        但已建的档案不受影响、仍按原规则随访——所以停用是"不再新增"，不是"作废存量"。
        建档下拉只列启用中的病种。</p>`)}
    <div class="panel"><h3>在管名单${overdue.length ? `（<span style="color:#c62828">${overdue.length} 人随访超期</span>）` : ""}</h3>
      ${table(["档案ID", "患者", "病种", "分级", "下次随访", "随访状态", "操作"], chronicList, (c) =>
        `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${esc(DISEASES[c.disease] || c.disease)}</td>
         <td><span class="tag ${c.level === 3 ? "red" : c.level === 2 ? "orange" : "green"}">${c.level} 级</span></td>
         <td>${esc(c.next_due) || "—"}</td>
         <td>${overdueIds.has(c.id) ? '<span class="tag red">超期</span>' : '<span class="tag green">正常</span>'}</td>
         <td><button class="btn" data-risk="${c.id}">风险评分</button></td></tr>`)}
      <div id="risk-box"></div></div>`;
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
  // 病种目录原先只能改不能建（P2-93 动词级孤儿）：建的接口一直在，新增一个病种只能靠接口调用方
  const typeForm = $("#chronic-type-form");
  if (typeForm) typeForm.onsubmit = async (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["followup_interval_days"]);
    if (body.level_rules) {
      try { body.level_rules = JSON.parse(body.level_rules); }
      catch (err) { return setMsg("#chronic-msg", `分级规则 JSON 解析失败：${err.message}`, false); }
    }
    return postAction("/api/chronic/disease-types", body, "#chronic-msg");
  };
  $("#fu-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const num = (k) => (f.get(k) ? Number(f.get(k)) : null);
    // "cat_score=22, mrs_score=3" → {cat_score: 22, mrs_score: 3}
    const metrics = {};
    // 全角逗号、顿号、全角等号也认（P1-137 前端同一族）：原先「cat_score=22，mrs_score=3」拆成一对、值是「22，mrs_score」，两项都被悄悄丢掉
    (f.get("metrics") || "").split(/[,，、]/).forEach((pair) => {
      const [k, v] = pair.split(/[=＝]/).map((s) => (s || "").trim());
      if (k && v !== undefined && v !== "" && !Number.isNaN(Number(v))) metrics[k] = Number(v);
    });
    try {
      const result = await api(`/api/chronic/${f.get("chronic_id")}/followups`, { method: "POST",
        body: JSON.stringify({ sbp: num("sbp"), dbp: num("dbp"), glucose: num("glucose"), metrics, next_due: f.get("next_due") }) });
      alert(`分级：${result.level} 级${result.refer_up_suggested ? "（建议上转！）" : ""}\n下次随访：${result.next_due}${result.next_due_suggested ? "（按病种周期自动建议）" : ""}\n指导要点：${result.guidance_points}`);
      route();
    } catch (err) { setMsg("#chronic-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { typeedit, risk } = e.target.dataset;
    try {
      if (typeedit) {
        const t = types.find((x) => x.id === Number(typeedit));
        const picked = await spdModal(`编辑病种 ${t ? t.code : typeedit}`, [
          { name: "name", label: "病种名称（留空不改）", type: "text", value: t ? t.name : "" },
          { name: "followup_interval_days", label: "随访周期（天，留空不改）", type: "number",
            value: t ? t.followup_interval_days : "" },
          { name: "active", label: "启停", type: "select", value: t && t.active ? "1" : "0",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
          { name: "guidance", label: "指导要点（留空不改）", type: "textarea", value: t ? t.guidance : "" },
          { name: "level_rules", label: "分级规则 JSON（留空不改；结构由目录数据决定，不要删自定义键）",
            type: "textarea", value: t ? JSON.stringify(t.level_rules || {}) : "" },
        ]);
        if (!picked) return;
        // 后端 exclude_unset + `if value is not None`：**不送的键就是不改**。
        // 所以留空的字段一律不放进 body，而不是送空串把人家的值清掉。
        const body = { active: picked.active === "1" };
        if (picked.name) body.name = picked.name;
        if (picked.followup_interval_days) body.followup_interval_days = picked.followup_interval_days;
        if (picked.guidance) body.guidance = picked.guidance;
        if (picked.level_rules) {
          try { body.level_rules = JSON.parse(picked.level_rules); }
          catch (err) { return setMsg("#chronic-msg", `分级规则 JSON 解析失败：${err.message}`, false); }
        }
        await api(`/api/chronic/disease-types/${typeedit}`, { method: "PATCH", body: JSON.stringify(body) });
        return route();
      }
      if (risk) {
        const r = await api(`/api/chronic/${risk}/risk`);
        $("#risk-box").innerHTML = `
          <div class="cards">
            <div class="card"><span class="k">档案</span><b>${r.chronic_id}（${
              esc(DISEASES[r.disease] || r.disease)}，${r.level} 级）</b></div>
            <div class="card"><span class="k">趋势指标</span><b>${esc(r.metric) || "—"}</b></div>
            <div class="card"><span class="k">最近三次</span><b>${
              r.recent_values.length ? r.recent_values.join(" → ") : "—"}</b></div>
            <div class="card"><span class="k">趋势</span><b>${esc(RISK_TREND[r.trend] || r.trend)}</b></div>
            <div class="card"><span class="k">评分</span><b>${r.score}</b></div>
            <div class="card"><span class="k">风险档</span><b>${statusTag(RISK_LEVEL, r.risk_level)}</b></div>
          </div>
          ${r.refer_up_suggested ? '<p class="msg err">评分达高危档，建议上转</p>' : ""}
          <p class="desc">评分 = 分级基础分（1级20 / 2级50 / 3级80）+ 趋势修正（变差 +15，好转 −10，平稳 0；越高越危的指标上升算变差，越低越危的——如用药依从性评分——下降算变差）；
            ≥70 高危、≥40 中危。<b>最近三次不足两次时趋势记「数据不足」、不作修正</b>——
            一次随访推不出趋势。</p>`;
      }
    } catch (err) { setMsg("#chronic-msg", err.message, false); }
  };
}

