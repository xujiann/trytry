/* 县域医共体 医生移动工作台（块4）
   待办 / 危急值确认与处置 / 待审检查申请 / 慢病随访录入 / 患者档案速查
   复用居民端 m.css 风格，移动优先布局。
   会话（G3，P1-23 收口）：登录后令牌进 **HttpOnly Cookie**（与管理端共用
   业务侧 medplat_token / medplat_csrf，JS 读不到令牌）；sessionStorage 只存
   非敏感的用户名（兼作本页登录态标记，保留"关闭页面回登录页"的既有体验）。
   迁移期兜底：旧版把令牌写 sessionStorage("medplat_doctor_token")，仍保留
   读取并走 Header 模式，重新登录即切换 Cookie 模式。 */
"use strict";

const TOKEN_KEY = "medplat_doctor_token";
const USER_KEY = "medplat_doctor_user";
// 业务侧双提交 CSRF Cookie 名（非 HttpOnly，直接从 Cookie 读，不落 storage）
const CSRF_KEY = "medplat_csrf";

function token() { return sessionStorage.getItem(TOKEN_KEY) || ""; }  // 仅迁移兜底
function csrfToken() { return readCookie(CSRF_KEY); }
function isAuthed() { return Boolean(token()) || Boolean(sessionStorage.getItem(USER_KEY)); }

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token()) headers.Authorization = `Bearer ${token()}`;  // 迁移兜底：存量令牌走 Header
  else {
    const method = (options.method || "GET").toUpperCase();
    // Cookie 模式的写请求：双提交 CSRF（读请求服务端不强制）
    if (method !== "GET" && method !== "HEAD") headers["X-CSRF-Token"] = csrfToken();
  }
  const resp = await fetch(path, { ...options, credentials: "same-origin", headers });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401) { logout(); throw new Error("登录已失效，请重新登录"); }
  if (!resp.ok) throw new Error(errorText(data.detail, `请求失败(${resp.status})`));
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

/* 卡片内表单：系统输入框（window.prompt）的替代（P2-38）。原先的写法是「输入框返回值 || 空串」，
 * 在弹窗上点"取消"照样提交——危急值照样"闭环"、转诊照样退回；这里点"取消"就是放弃，
 * 提交才调 onSubmit(表单元素)。
 * 表单留到提交成功、列表重画时才随卡片消失：后端报错时填过的字还在，改了再交。
 * 再点一次同一个按钮不重复插表单；同一张卡换了个动作（先点"退回"又点"通过"），
 * 换成新动作的表单——否则开着的"确认退回"会让人以为点的是通过。 */
function cardForm(card, className, fieldsHtml, submitLabel, onSubmit) {
  if (!card) return;
  const open = card.querySelector(`.${className}`);
  if (open) {
    if (open.dataset.submitLabel === submitLabel) return;
    open.remove();
  }
  const form = document.createElement("form");
  form.className = className;
  form.dataset.submitLabel = submitLabel;
  form.innerHTML = `${fieldsHtml}
    <button type="submit" class="ghost-btn">${esc(submitLabel)}</button>
    <button type="button" class="ghost-btn" data-cancel>取消</button>`;
  form.onsubmit = (e) => { e.preventDefault(); onSubmit(form.elements); };
  form.querySelector("[data-cancel]").onclick = () => form.remove();
  card.appendChild(form);
  form.querySelector("textarea, input, select").focus();
}

/* 附件上传（multipart）：绕过 api()——它写死 JSON 的 Content-Type；会话口径与 api() 相同（存量 Header 令牌照带，
   Cookie 模式的写请求补双提交 CSRF）。 */
async function uploadAttachment(ownerType, ownerId, file) {
  const fd = new FormData();
  fd.append("file", file);
  fd.append("owner_type", ownerType);
  fd.append("owner_id", ownerId);
  const resp = await fetch("/api/attachments", {
    method: "POST", credentials: "same-origin",
    headers: token() ? { Authorization: `Bearer ${token()}` } : { "X-CSRF-Token": csrfToken() },
    body: fd });
  const data = await resp.json().catch(() => ({}));
  if (resp.status === 401) { logout(); throw new Error("登录已失效，请重新登录"); }
  if (!resp.ok) throw new Error(errorText(data.detail, `上传失败(${resp.status})`));
  return data;
}

/* ---------------- 登录 / 登出 ---------------- */

function showWorkbench(show) {
  $("#login-page").classList.toggle("hidden", show);
  $("#workbench").classList.toggle("hidden", !show);
  $("#tabbar").classList.toggle("hidden", !show);
  $("#btn-logout").classList.toggle("hidden", !show);
}

function logout() {
  // 先请后端拉黑令牌并清 HttpOnly Cookie（直接 fetch 而不走 api()：
  // api() 的 401 分支会调回本函数）；失败时照样本地退出
  fetch("/api/auth/logout", {
    method: "POST",
    credentials: "same-origin",
    headers: { "X-CSRF-Token": csrfToken(), ...(token() ? { Authorization: `Bearer ${token()}` } : {}) },
  }).catch(() => {});
  sessionStorage.removeItem(TOKEN_KEY);
  sessionStorage.removeItem(USER_KEY);
  showWorkbench(false);
}

$("#btn-logout").addEventListener("click", logout);

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    // X-Token-Transport: cookie —— 声明走 Cookie 会话：令牌进 HttpOnly Cookie（G3）
    await api("/api/auth/login", {
      method: "POST",
      headers: { "X-Token-Transport": "cookie" },
      body: JSON.stringify({ username: $("#lg-user").value.trim(), password: $("#lg-pass").value }),
    });
    // P1-23：不再把 access_token 写入 sessionStorage；旧存量一并清掉（切换 Cookie 模式）
    sessionStorage.removeItem(TOKEN_KEY);
    sessionStorage.setItem(USER_KEY, $("#lg-user").value.trim());
    $("#lg-pass").value = "";
    showWorkbench(true);
    switchTab(currentTab());
  } catch (err) {
    $("#login-error").textContent = err.message;
  }
});


/* ---------------- 慢专病（基层医护健康管理端） ----------------
 *
 * 角色（村医 / 卫生院 / 县级 / 管理者）由**团队成员身份**推出来，不是账号角色：
 * 同一个人在县医院是医师、在专病团队里是专家，两者要同时成立。
 * 服务端 `/api/spd/workbench/doctor-mobile` 已经把这件事算好，这里只负责画。
 */

let activeDoctorSpd = "todo";

document.querySelectorAll("[data-dspd]").forEach((btn) => {
  btn.addEventListener("click", () => {
    activeDoctorSpd = btn.dataset.dspd;
    document.querySelectorAll("[data-dspd]").forEach((b) => b.classList.toggle("active", b === btn));
    loadSpdList();
  });
});

async function loadSpdTab() {
  const wb = await api("/api/spd/workbench/doctor-mobile");
  const roleText = (wb.user.member_roles || []).map((r) => ({
    doctor: "医生", nurse: "护士", rehab: "康复治疗师", case_manager: "个案管理师",
    village_doctor: "村医", expert: "专家",
  }[r] || r)).join("、") || "未加入慢专病团队";
  $("#spd-wb").innerHTML = `<div class="m-card">
    ${kv("当前身份", esc(roleText))}
    ${wb.user.is_village_doctor ? kv("辖区", esc(`${wb.user.township} ${wb.user.village}`)) : ""}
    ${kv("我的待办", `${wb.todo.open} 条（今日到期 ${wb.calendar.tasks}）`)}
    ${kv("超期任务", wb.todo.overdue)}
    ${kv("今日随访", wb.calendar.followups)}
    ${kv("今日复诊", wb.calendar.revisits)}
    ${kv("待复核转诊", wb.referrals.pending_review)}
    ${kv("待接收转诊", wb.referrals.pending_accept)}
    ${kv("待承接下转", wb.referrals.pending_receive)}
    ${kv("在管患者", wb.patients.mine)}
    ${kv("积分余额", wb.points.balance)}
  </div>`;
  await loadSpdList();
}

/* 渲染串行化：与 core.js route()、m.js loadSpd() 同一套（序号 + 互斥 +
 * 收尾补画）。触发路径：动作按钮（接收/审核）成功后重画列表 vs 用户同时
 * 切 [data-dspd] 页签——两个并发渲染写同一个 #spd-list，后完成者盖掉前者。 */
let dspdSeq = 0;
let dspdRendering = false;

async function loadSpdList() {
  dspdSeq += 1;
  if (dspdRendering) return;  // 已有渲染在跑，它收尾时会按最新页签补画
  dspdRendering = true;
  try {
    for (;;) {
      const seq = dspdSeq;
      const box = $("#spd-list");
      box.innerHTML = '<p class="empty">加载中…</p>';
      try {
        if (activeDoctorSpd === "todo") await loadSpdTodo(box);
        else if (activeDoctorSpd === "referral") await loadSpdReferral(box);
        else if (activeDoctorSpd === "patient") await loadSpdPatients(box);
        else await loadSpdPerf(box);
      } catch (err) {
        box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
      }
      if (seq === dspdSeq) break;  // 期间没有新的渲染请求，收工
    }
  } finally {
    dspdRendering = false;
  }
}

/* 待办卡片的动作按状态给（与管理端 `spdTaskActions` 同一口径）：待接收 / 已超期的能接收，待审核的等审核人在管理端审，
   其余能办结；要佐证的任务多一个「上传佐证」——原先卡片上一律摆着接收与办结、没有上传入口，要佐证的任务在手机上点办结
   恒 422，接收点在已接收的任务上恒 409，待审核的点办结则绕过了审核（P2-84）。 */
function spdTodoOps(t) {
  const b = (attr, label) => `<button type="button" class="ghost-btn" ${attr}="${t.id}">${label}</button>`;
  if (t.status === "submitted") return "";
  return (["pending", "overdue"].includes(t.status) ? b("data-spd-claim", "接收") : "")
    + (t.require_evidence ? b("data-spd-evidence", "上传佐证") : "")
    + b("data-spd-done", "办结");
}

async function loadSpdTodo(box) {
  const rows = await api("/api/spd/tasks?mine=true&open_only=true&limit=30");
  box.innerHTML = rows.map((t) => `<div class="m-card">
    ${kv("任务", esc(t.title))}
    ${kv("患者", esc(t.patient_name || t.patient_id))}
    ${kv("类型", esc({ path: "路径节点", followup: "随访", intervention: "干预",
      assess: "评估", revisit: "复诊", referral: "转诊", report: "上报",
      recall: "召回", edu: "宣教", screen: "筛查复核" }[t.task_type] || t.task_type))}
    ${kv("截止", esc(t.due_date || "—"))}
    ${kv("状态", esc({ pending: "待接收", claimed: "已接收", doing: "办理中",
      submitted: "待审核", done: "已完成", rejected: "已退回", overdue: "已超期",
      cancelled: "已取消" }[t.status] || t.status))}
    ${t.review_note ? kv("审核意见", esc(t.review_note)) : ""}
    ${t.require_evidence ? kv("佐证", (t.evidence || []).length
      ? `已传 ${(t.evidence || []).length} 份` : "办结前须上传照片或报告") : ""}
    ${spdTodoOps(t)}
  </div>`).join("") || '<p class="empty">暂无待办</p>';
  box.querySelectorAll("[data-spd-claim]").forEach((b) => b.addEventListener("click", async () => {
    await spdPost(`/api/spd/tasks/${b.dataset.spdClaim}/claim`);
  }));
  box.querySelectorAll("[data-spd-evidence]").forEach((b) => b.addEventListener("click", () => {
    // 佐证 = 挂在该任务名下的附件（owner_type=spd_task）。上传后以「保存草稿」把附件编号写进任务的佐证清单
    // （要带上已有的，后端整体替换）——与管理端「上传佐证」同一走法，办结时后端再核一遍
    const input = document.createElement("input");
    input.type = "file";
    input.accept = "image/*,.pdf";
    input.onchange = async () => {
      if (!input.files[0]) return;
      const taskId = b.dataset.spdEvidence;
      try {
        const att = await uploadAttachment("spd_task", taskId, input.files[0]);
        const t = await api(`/api/spd/tasks/${taskId}`);
        const ids = [...(t.evidence || []).map(Number).filter(Boolean), att.id];
        await api(`/api/spd/tasks/${taskId}/submit`, { method: "POST",
          body: JSON.stringify({ result: t.result || {}, evidence: ids, draft: true }) });
        $("#spd-msg").textContent = `佐证已上传（共 ${ids.length} 份），办结时一并核验`;
        await loadSpdTab();
      } catch (err) {
        $("#spd-msg").textContent = err.message;
      }
    };
    input.click();
  }));
  box.querySelectorAll("[data-spd-done]").forEach((b) => b.addEventListener("click", () => {
    // 办理结果在卡片里用文本域收，不再弹系统输入框：弹窗录不了多行、没有校验提示、
    // 粘不了长文本（功能完善规则 §1 第 7 项；存量登记 P2-38）。再点一次不重复插表单。
    const card = b.closest(".m-card");
    if (!card || card.querySelector(".spd-done-form")) return;
    const form = document.createElement("form");
    form.className = "spd-done-form";
    form.innerHTML = `<textarea name="note" rows="2" placeholder="办理结果（可留空）"></textarea>
      <button type="submit" class="ghost-btn">确认办结</button>
      <button type="button" class="ghost-btn" data-cancel>取消</button>`;
    form.onsubmit = async (e) => {
      e.preventDefault();
      await spdPost(`/api/spd/tasks/${b.dataset.spdDone}/complete`,
        { result: { note: form.note.value.trim() } });
    };
    form.querySelector("[data-cancel]").onclick = () => form.remove();
    card.appendChild(form);
  }));
}

/** 转诊卡片按状态给按钮（与后端的状态前置同一口径）：审核只对待审核的，登记到院只对已接收的，承接随访只对已下转的。
 *  原先四个按钮一律摆着，点错一个就是 409「只有已接收的转诊单可登记到院」——与 P2-84 的待办卡片同一个毛病。 */
function spdReferralOps(r) {
  const ops = [];
  if (["submitted", "station_reviewed", "township_reviewed"].includes(r.status)) {
    ops.push(`<button type="button" class="ghost-btn" data-spd-pass="${r.id}">通过</button>`,
      `<button type="button" class="ghost-btn" data-spd-reject="${r.id}">退回</button>`);
  }
  if (r.status === "accepted") ops.push(`<button type="button" class="ghost-btn" data-spd-arrive="${r.id}">登记到院</button>`);
  if (r.status === "down_referred") ops.push(`<button type="button" class="ghost-btn" data-spd-recv="${r.id}">承接随访</button>`);
  return ops.join("\n    ");
}

async function loadSpdReferral(box) {
  const rows = await api("/api/spd/referrals?open_only=true&limit=30");
  box.innerHTML = rows.map((r) => `<div class="m-card">
    ${kv("患者", esc(r.patient_name))}
    ${kv("方向", r.direction === "up" ? "上转" : "下转")}
    ${kv("当前环节", esc({ submitted: "待卫生院审核", station_reviewed: "待卫生院审核(存量)",
      township_reviewed: "待县级接收", accepted: "已接收待到院",
      arrived: "已到院", down_referred: "待承接随访" }[r.status] || r.status))}
    ${kv("理由", esc(r.reason || "—"))}
    ${spdReferralOps(r)}
  </div>`).join("") || '<p class="empty">暂无在途转诊</p>';
  const bind = (attr, path, body) => box.querySelectorAll(`[${attr}]`).forEach((b) =>
    b.addEventListener("click", () => spdPost(path(b), body ? body() : null)));
  // 通过 / 退回的意见在卡片里填（与管理端同一口径：意见可空）。原先 prompt 点"取消"照样通过、
  // 照样退回——想反悔的人反而把单子退了回去，还没有理由。
  const review = (attr, action, placeholder, submitLabel) =>
    box.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", () =>
      cardForm(b.closest(".m-card"), "spd-review-form",
        `<textarea name="opinion" rows="2" placeholder="${placeholder}"></textarea>`, submitLabel,
        (f) => spdPost(`/api/spd/referrals/${b.getAttribute(attr)}/review`,
          { action, opinion: f.opinion.value.trim() }))));
  review("data-spd-pass", "pass", "审核意见（可空）", "确认通过");
  review("data-spd-reject", "reject", "退回理由", "确认退回");
  bind("data-spd-arrive", (b) => `/api/spd/referrals/${b.dataset.spdArrive}/arrive`,
    () => ({ effective_visit: true }));
  bind("data-spd-recv", (b) => `/api/spd/referrals/${b.dataset.spdRecv}/receive-followup`,
    () => ({ opinion: "已接收随访" }));
}

async function loadSpdPatients(box) {
  const rows = await api("/api/spd/enrollments?limit=30");
  box.innerHTML = rows.map((e) => `<div class="m-card">
    ${kv("患者", esc(e.patient_name || e.patient_id))}
    ${kv("病种", esc(e.program_code))}
    ${kv("风险", esc({ low: "低危", mid: "中危", high: "高危", very_high: "极高危" }[e.risk_level] || e.risk_level))}
    ${kv("阶段", esc(e.stage || "—"))}
    ${kv("下次随访", esc(e.next_followup_at || "—"))}
  </div>`).join("") || '<p class="empty">暂无在管患者</p>';
}

const REDEEM_STATUS_NAMES = { pending: "待核销", verified: "已核销", cancelled: "已取消" };

async function loadSpdPerf(box) {
  const [points, wb, goods, redeems] = await Promise.all([
    api("/api/spd/point-accounts/me"), api("/api/spd/workbench/doctor-mobile"),
    api("/api/spd/goods"), api("/api/spd/redeems?mine=true&limit=20"),
  ]);
  const perf = wb.performance;
  box.innerHTML = `<div class="m-card">
      ${kv("积分余额", points.balance)}
      ${kv("累计获得", points.earned)}
      ${kv("累计兑换", points.used)}
      <button type="button" class="ghost-btn" data-spd-signin>每日签到</button>
    </div>
    ${perf ? `<div class="m-card">
      ${kv("考核周期", esc(perf.period))}
      ${kv("综合得分", perf.total_score)}
      ${kv("排名", perf.rank)}
      ${(perf.detail || []).map((d) =>
        kv(esc(d.indicator_name || d.indicator_code),
           `${d.score ?? "—"} 分${d.deduction ? `（扣 ${d.deduction}）` : ""}`)).join("")}
    </div>` : '<p class="empty">暂无考核结果</p>'}
    ${goods.map((g) => `<div class="m-card">
      ${kv("商品", esc(g.name))}
      ${kv("所需积分", g.points)}
      ${kv("库存", g.stock)}
      ${g.stock > 0 && points.balance >= g.points
        ? `<button type="button" class="ghost-btn" data-spd-redeem="${g.id}">兑换</button>`
        : `<p class="empty">${g.stock > 0 ? "积分不足" : "暂无库存"}</p>`}
    </div>`).join("") || '<p class="empty">暂无可兑换商品</p>'}
    ${redeems.map((r) => `<div class="m-card">
      ${kv("兑换", esc(r.goods_name))}
      ${kv("核销码", esc(r.verify_code))}
      ${kv("状态", esc(REDEEM_STATUS_NAMES[r.status] || r.status))}
      ${kv("时间", esc(r.created_at.replace("T", " ").slice(0, 16)))}</div>`).join("")}
    ${(points.records || []).slice(0, 20).map((r) => `<div class="m-card">
      ${kv("积分", `${r.direction === "in" ? "+" : "-"}${r.points}（余额 ${r.balance_after}）`)}
      ${kv("来源", esc(r.note))}
      ${kv("时间", esc(r.created_at.replace("T", " ").slice(0, 16)))}</div>`).join("")}`;
  // 签到 / 兑换的结果里有要给人看的数字（加了几分、核销码），不能走 spdPost 那句「操作成功」
  const act = async (path, body, okText) => {
    try {
      const r = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
      $("#spd-msg").textContent = okText(r);
      await loadSpdTab();
    } catch (err) { $("#spd-msg").textContent = err.message; }
  };
  box.querySelectorAll("[data-spd-signin]").forEach((b) => b.addEventListener("click", () =>
    act("/api/spd/point-accounts/signin", null, (r) => `签到成功：+${r.points} 分，余额 ${r.balance}`)));
  box.querySelectorAll("[data-spd-redeem]").forEach((b) => b.addEventListener("click", () =>
    act("/api/spd/redeems", { goods_id: Number(b.dataset.spdRedeem) },
      (r) => `兑换成功，核销码 ${r.verify_code}（到点位出示），余额 ${r.balance}`)));
}

async function spdPost(path, body) {
  try {
    await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    $("#spd-msg").textContent = "操作成功";
    await loadSpdTab();
  } catch (err) {
    $("#spd-msg").textContent = err.message;
  }
}

/* ---------------- 标签页 ---------------- */

const TABS = { todo: loadTodos, critical: loadCritical, exam: loadExams, round: loadRound,
  surgery: loadSurgery, chronic: loadChronic, spd: loadSpdTab, patient: loadPatientTab };

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
  if (isAuthed()) TABS[tab]();
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
    // 站内消息与待办并到同一屏：医生查房时不会为了看消息切第二个页签。
    const [data, notices] = await Promise.all([
      api("/api/todos"), api("/api/notifications?unread_only=true&limit=20")]);
    $("#who").innerHTML = `<span>${esc(sessionStorage.getItem(USER_KEY) || "")}</span>
      <span class="role">${esc(ROLE_NAMES[data.role] || data.role)} · 待办 ${data.total}</span>`;
    const noticeBlock = notices.length ? `<div class="todo-group">
      <div class="head"><span>未读消息</span><span class="badge warn">${notices.length}</span></div>
      ${notices.map((n) => `<div class="m-card notice">
        ${kv("标题", esc(n.title))}${kv("内容", esc(n.body || "—"))}
        ${kv("时间", esc(n.created_at.slice(0, 16).replace("T", " ")))}
        <div class="ops"><button class="ghost" data-ntread="${n.id}">标记已读</button></div></div>`).join("")}
    </div>` : "";
    if (!data.items.length) {
      box.innerHTML = noticeBlock || '<p class="empty">当前角色无待办事项</p>';
      bindNoticeRead(box);
      return;
    }
    box.innerHTML = noticeBlock + data.items.map((item) => {
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
    bindNoticeRead(box);
  } catch (err) {
    box.innerHTML = `<p class="empty">${esc(err.message)}</p>`;
  }
}

/** 标记已读后就地移除卡片，不整屏重刷——医生手上可能正在看别的分组。 */
function bindNoticeRead(box) {
  box.querySelectorAll("[data-ntread]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try {
        await api(`/api/notifications/${btn.dataset.ntread}/read`, { method: "POST" });
        btn.closest(".notice").remove();
      } catch (err) {
        btn.disabled = false;
      }
    });
  });
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
      const pending = ["notified", ""].includes(r.critical_status);
      const ops = pending
        ? `<button data-ack="${r.id}">确认接收</button>`
        : r.critical_status === "acknowledged"
          ? `<button data-resolve="${r.id}">处置反馈</button>` : "";
      return card(
        kv("报告编号", esc(r.id)) + kv("申请单", esc(r.request_id)) +
        kv("结论", esc(r.conclusion)) + kv("状态", statusTag(CRITICAL_TAGS, r.critical_status)),
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
      // 原先 prompt 点"取消"照样提交：危急值就此"闭环完成"，处置措施一个字没有。
      return cardForm(e.target.closest(".m-card"), "crit-resolve-form",
        '<textarea name="note" rows="2" placeholder="处置措施（如：已联系患者并调整治疗方案）"></textarea>',
        "提交处置反馈", async (f) => {
          try {
            await api(`/api/exams/reports/${resolve}/resolve`, { method: "POST",
              body: JSON.stringify({ note: f.note.value.trim() }) });
            setMsg("#critical-msg", "处置已反馈，危急值闭环完成", true);
            await loadCritical();
          } catch (err) { setMsg("#critical-msg", err.message, false); }
        });
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
      const ops = r.status === "pending"
        ? `<button data-claim="${r.id}">领取</button><button class="ghost" data-report="${r.id}">出报告</button>`
        : `<button data-report="${r.id}">出报告</button>`;
      return card(
        kv("申请单", esc(r.id)) + kv("中心", esc(CENTER_NAMES[r.center_type] || r.center_type)) +
        kv("项目", esc(r.item_name)) + kv("状态", statusTag(EXAM_STATUS, r.status)),
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
      // 与管理端「出报告」同一套字段（结论 + 所见 + 危急值）。原先结论之后跟一个
      // confirm「确定=是危急值」：想放弃时点"取消"，报告照样出具、还被记成**非危急值**。
      return cardForm(e.target.closest(".m-card"), "exam-report-form",
        `<textarea name="conclusion" rows="2" placeholder="诊断结论" required></textarea>
         <textarea name="finding" rows="2" placeholder="影像所见 / 检查所见（可空）"></textarea>
         <select name="critical"><option value="0">非危急值</option>
           <option value="1">危急值（进危急值闭环，通知申请机构）</option></select>`,
        "出具报告", async (f) => {
          const critical = f.critical.value === "1";
          try {
            await api(`/api/exams/${report}/report`, { method: "POST", body: JSON.stringify({
              conclusion: f.conclusion.value.trim(), finding: f.finding.value.trim(), critical }) });
            setMsg("#exam-msg", critical ? "报告已出具，危急值已通知申请机构" : "报告已出具", true);
            await loadExams();
          } catch (err) { setMsg("#exam-msg", err.message, false); }
        });
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

// Cookie 会话下 USER_KEY（sessionStorage）是本页登录态标记：关闭页面即回登录页，
// 保留旧版 sessionStorage 令牌时代的体验；Cookie 失效时首个 api() 401 统一登出
showWorkbench(isAuthed());

/* ============================================================================
 * 查房与手术（阶段二能力落到移动端）
 * 医生查房不会带电脑，住院文书、体征、手术排班这些恰恰都是移动场景。
 * ==========================================================================*/

const NOTE_TYPE_NAMES = { first: "首次病程", daily: "日常病程", ward_round: "上级查房",
  rescue: "抢救记录", consultation: "会诊记录", discharge: "出院记录" };
// 麻醉方式措辞与管理端 pages-mgmt.js 的 ANESTHESIA 一致
const ANESTHESIA_NAMES = { general: "全麻", spinal: "椎管内", local: "局麻", nerve_block: "神经阻滞" };
const SURGERY_STATUS_NAMES = { requested: ["待审批", "orange"], approved: ["已审批", ""],
  scheduled: ["已排班", "green"], completed: ["已完成", ""], cancelled: ["已取消", "red"] };

// 当前查房对象；切换患者后各区块都跟着刷新
let roundAdmissionId = 0;

async function loadRound() {
  const picker = $("#round-adm");
  let admissions = [];
  try {
    // 只取在院的（P2-154）：原先取「最新 200 条住院」再筛在院，住得久的患者被新入院的挤出去，查房选不到他
    admissions = (await api("/api/inpatient/admissions?status=admitted&limit=500"))
      .filter((a) => a.status === "admitted");
  } catch (err) {
    $("#round-status").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    return;
  }
  if (!admissions.length) {
    picker.innerHTML = "";
    $("#round-status").innerHTML = '<p class="empty">当前没有在院患者</p>';
    $("#round-note").classList.add("hidden");
    $("#round-vital").classList.add("hidden");
    $("#round-notes").innerHTML = "";
    $("#round-vitals").innerHTML = "";
    return;
  }
  if (!admissions.some((a) => a.id === roundAdmissionId)) roundAdmissionId = admissions[0].id;
  picker.innerHTML = admissions.map((a) =>
    `<option value="${a.id}" ${a.id === roundAdmissionId ? "selected" : ""}>
       ${esc(a.diagnosis_name || "住院")}（住院号 ${a.id}）</option>`).join("");
  $("#round-note").classList.remove("hidden");
  $("#round-vital").classList.remove("hidden");
  await refreshRoundDetail();
}

async function refreshRoundDetail() {
  const [completeness, notes, vitals] = await Promise.all([
    api(`/api/inpatient/admissions/${roundAdmissionId}/document-completeness`),
    api(`/api/inpatient/admissions/${roundAdmissionId}/progress-notes`),
    api(`/api/inpatient/admissions/${roundAdmissionId}/vitals`),
  ]);
  $("#round-status").innerHTML = `<div class="m-card">
    ${kv("文书完整性", completeness.complete
      ? '<span class="tag green">完整</span>'
      : `<span class="tag orange">${esc(completeness.missing.join("、"))}</span>`)}
    ${kv("病程记录", `${notes.length} 条`)}${kv("体征记录", `${vitals.length} 条`)}</div>`;

  $("#round-notes").innerHTML = `<div class="sec-title">病程记录（${notes.length}）</div>` + (
    notes.length
      ? notes.slice().reverse().map((n) => card(
          `${kv("类型", esc(NOTE_TYPE_NAMES[n.note_type] || n.note_type))}
           ${kv("时间", esc(n.recorded_at))}${kv("医师", esc(n.doctor_name))}
           <p class="note-body">${esc(n.content)}</p>`)).join("")
      : '<p class="empty">尚无病程记录</p>');

  // 体温单按时间倒序显示最近 8 次，移动端一屏看得完
  const recent = vitals.slice(-8).reverse();
  $("#round-vitals").innerHTML = `<div class="sec-title">体征（最近 ${recent.length} 次）</div>` + (
    recent.length
      ? recent.map((v) => card(
          `${kv("时刻", esc(v.measured_at))}
           ${kv("体温", v.temperature != null ? `${v.temperature} ℃` : "—")}
           ${kv("脉搏/呼吸", `${v.pulse ?? "—"} / ${v.respiration ?? "—"}`)}
           ${kv("血压", v.sbp != null || v.dbp != null ? `${v.sbp ?? "—"}/${v.dbp ?? "—"}` : "—")}`)).join("")
      : '<p class="empty">尚无体征记录</p>');
}

$("#round-pick").addEventListener("submit", async (e) => {
  e.preventDefault();
  roundAdmissionId = Number($("#round-adm").value);
  await refreshRoundDetail();
});

$("#round-note").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api(`/api/inpatient/admissions/${roundAdmissionId}/progress-notes`, {
      method: "POST",
      body: JSON.stringify({
        note_type: $("#round-note-type").value,
        content: $("#round-content").value.trim(),
      }),
    });
    $("#round-content").value = "";
    setMsg("#round-msg", "病程已记录", true);
    await refreshRoundDetail();
  } catch (err) { setMsg("#round-msg", err.message, false); }
});

$("#round-vital").addEventListener("submit", async (e) => {
  e.preventDefault();
  // 未测项留空 → 不进 body，落库为 null；填 0 会污染趋势曲线
  // 日期时间控件送 `T` 分隔，换成空格再送：与桌面端、服务端默认的写法一致（P1-100）
  const body = { measured_at: $("#rv-at").value.trim().replace("T", " ") };
  for (const [field, sel] of [["temperature", "#rv-temp"], ["pulse", "#rv-pulse"],
                              ["respiration", "#rv-resp"], ["sbp", "#rv-sbp"], ["dbp", "#rv-dbp"]]) {
    const raw = $(sel).value.trim();
    if (raw !== "") body[field] = Number(raw);
  }
  try {
    await api(`/api/inpatient/admissions/${roundAdmissionId}/vitals`, {
      method: "POST", body: JSON.stringify(body) });
    ["#rv-temp", "#rv-pulse", "#rv-resp", "#rv-sbp", "#rv-dbp"].forEach((s) => { $(s).value = ""; });
    setMsg("#round-msg", "体征已录入", true);
    await refreshRoundDetail();
  } catch (err) { setMsg("#round-msg", err.message, false); }
});

/* ---------------- 手术：排班与术中记录 ---------------- */

async function loadSurgery() {
  let schedules = [], requests = [];
  try {
    [schedules, requests] = await Promise.all([
      api("/api/surgery/schedules"), api("/api/surgery/requests")]);
  } catch (err) {
    $("#surgery-schedule").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    return;
  }
  $("#surgery-schedule").innerHTML = `<div class="sec-title">手术排班（${schedules.length}）</div>` + (
    schedules.length
      ? schedules.map((s) => card(
          `${kv("术式", esc(s.surgery_name))}${kv("日期", esc(s.scheduled_date))}
           ${kv("时段", `${esc(s.start_time)}-${esc(s.end_time)}`)}
           ${kv("手术间", esc(s.room_name))}${kv("术者", esc(s.surgeon_name))}`)).join("")
      : '<p class="empty">暂无排班</p>');

  // 只列还没写术中记录的，写完就从这里消失——这是医生真正要处理的部分
  const pending = requests.filter((r) => r.status === "scheduled");
  $("#surgery-requests").innerHTML = `<div class="sec-title">待填术中记录（${pending.length}）</div>` + (
    pending.length
      ? pending.map((r) => {
          return card(
            `${kv("术式", esc(r.surgery_name))}${kv("住院号", String(r.admission_id))}
             ${kv("状态", statusTag(SURGERY_STATUS_NAMES, r.status))}`,
            `<button class="op" data-record="${r.id}" data-name="${esc(r.surgery_name)}"
              data-anesthesia="${esc(r.anesthesia_type)}" data-incision="${esc(r.incision_level)}">填写术中记录</button>`);
        }).join("")
      : '<p class="empty">没有待填写的术中记录</p>');
}

$("#tab-surgery").addEventListener("click", (e) => {
  const id = e.target.dataset.record;
  if (!id) return;
  // P2-38 / P1-66：原先四连问、转归写死"好转"（质量指标的治愈率与死亡数取的正是它）。
  // 改成卡片内表单：转归可选，术前/术后诊断（诊断符合率的数据源）也能填。再点一次不重复插表单。
  const card = e.target.closest(".m-card");
  if (!card || card.querySelector(".surg-record-form")) return;
  const form = document.createElement("form");
  form.className = "surg-record-form";
  form.innerHTML = `<input name="actual_surgery_name" placeholder="实际术式" required
      value="${esc(e.target.dataset.name || "")}">
    <input name="anesthetist_name" placeholder="麻醉医师">
    <select name="anesthesia_type">${Object.entries(ANESTHESIA_NAMES).map(([k, v]) =>
      `<option value="${k}"${k === e.target.dataset.anesthesia ? " selected" : ""}>${v}</option>`).join("")}</select>
    <select name="incision_level">${["I", "II", "III", "IV"].map((x) =>
      `<option value="${x}"${x === e.target.dataset.incision ? " selected" : ""}>${x} 类切口</option>`).join("")}</select>
    <textarea name="findings" rows="2" placeholder="术中所见"></textarea>
    <input name="blood_loss_ml" inputmode="numeric" placeholder="出血量 ml（可空）">
    <select name="outcome">${["治愈", "好转", "未愈", "死亡"].map((x) =>
      `<option${x === "好转" ? " selected" : ""}>${x}</option>`).join("")}</select>
    <input name="preop_diagnosis" placeholder="术前诊断（可空）">
    <input name="postop_diagnosis" placeholder="术后诊断（可空）">
    <button type="submit" class="ghost-btn">提交术中记录</button>
    <button type="button" class="ghost-btn" data-cancel>取消</button>`;
  form.onsubmit = async (ev) => {
    ev.preventDefault();
    const f = form.elements;
    const blood = f.blood_loss_ml.value.trim();
    try {
      await api(`/api/surgery/requests/${id}/record`, {
        method: "POST",
        body: JSON.stringify({
          actual_surgery_name: f.actual_surgery_name.value.trim(),
          anesthetist_name: f.anesthetist_name.value.trim(),
          // 麻醉方式与切口等级原先不送（P2-179），后端缺省全麻、II 类——移动端记的每一台都记成全麻 II 类切口，
          // 手术量统计的切口 / 麻醉构成跟着失真。现在缺省带出申请时填的，可改
          anesthesia_type: f.anesthesia_type.value,
          incision_level: f.incision_level.value,
          findings: f.findings.value.trim(),
          // 留空记 0；写错的原样交给后端报人话，别让 Number() 把它悄悄变成 NaN → null
          blood_loss_ml: blood === "" ? 0 : (Number.isNaN(Number(blood)) ? blood : Number(blood)),
          outcome: f.outcome.value,
          preop_diagnosis: f.preop_diagnosis.value.trim(),
          postop_diagnosis: f.postop_diagnosis.value.trim(),
        }),
      });
      setMsg("#surgery-msg", "术中记录已提交，术后随访任务已自动派生", true);
      await loadSurgery();
    } catch (err) { setMsg("#surgery-msg", err.message, false); }
  };
  form.querySelector("[data-cancel]").onclick = () => form.remove();
  card.appendChild(form);
});

/* ---------------- 启动 ----------------
   放在文件最末：新增页签用到模块级状态（roundAdmissionId），启动调用若排在
   声明之前，就要靠"异步函数在首个 await 前挂起"这种脆弱假设才不触发 TDZ。 */

switchTab(currentTab());
