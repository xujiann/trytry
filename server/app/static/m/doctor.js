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
  // 登录请求本身的 401 是「用户名或密码错误」这类，原样报后端的话、不走登出（P2-1225，与管理端 core.js 同一句）：原先一律
  // 当会话失效，口令敲错了登录框却写「登录已失效，请重新登录」
  if (resp.status === 401 && path !== "/api/auth/login") { logout(); throw new Error("登录已失效，请重新登录"); }
  if (!resp.ok) throw new Error(errorText(data.detail, `请求失败(${resp.status})`));
  // 要总数的清单带上 `withTotal`（P2-1771，与管理端 core.js 的 P2-1547、居民端 m.js 的 P2-1674 同一写法）：连同 X-Total-Count
  // 一起回 `{ rows, total }`，接口没发这个头时 total 为 null。缺省照旧只回响应体——全部调用点共用这个返回形状
  if (options.withTotal) {
    const total = resp.headers.get("X-Total-Count");
    return { rows: data, total: total === null ? null : Number(total) };
  }
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
    <button type="button" class="ghost-btn" data-cancel>取消</button>
    <p class="msg" data-card-msg></p>`;
  // 报错写在表单里（P2-1093，居民端 P2-1014 同一个写法）：onSubmit 抛错就写进这一行。原先各处把报错写到整页消息行——
  // 手机上列表 30～40 张卡片，消息行在屏幕外，提交失败时卡片上什么变化都没有
  const msg = form.querySelector("[data-card-msg]");
  form.onsubmit = async (e) => {
    e.preventDefault();
    msg.textContent = "";
    msg.className = "msg";
    try { await onSubmit(form.elements); }
    catch (err) { msg.textContent = err.message || String(err); msg.className = "msg err"; }
  };
  form.querySelector("[data-cancel]").onclick = () => form.remove();
  card.appendChild(form);
  const first = form.querySelector("textarea, input, select");
  if (first) first.focus();   // 纯确认的框（转诊撤回，P2-794）没有输入项
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

/* 退出时把上一位留在工作台上的都清掉、页签复位到待办（P2-1218）。原先只清 sessionStorage、藏起工作台：换人登录落回 hash
 * 指的页签，「速查」进页不取数（loadPatientTab 是空函数），后一位看到的就是前一位查的患者档案（姓名、卡号、诊断、危急值），
 * 卡号框里还是那个卡号，顶部还写着前一位（#who 只在待办取数成功时改写）；查房、手术、随访、慢专病取数失败时只改一行状态，
 * 前一位的病程、体征、名单照旧挂着，「我的患者」还按前一位筛（spdMe）。
 * 不整页重载：登录口令错也回 401、也进 logout()，重载会把登录框下的报错一起冲掉；登出请求发出不等，重载还会掐断它。 */
const WORKBENCH_BLOCKS = ["#who", "#todo-list", "#critical-list", "#exam-list", "#fu-chronic", "#fu-metrics",
  "#chronic-list", "#round-adm", "#round-status", "#round-notes", "#round-vitals", "#surgery-schedule",
  "#surgery-requests", "#spd-wb", "#spd-list", "#pt-result"].map((sel) => [sel, $(sel).innerHTML]);   // 页面刚载入时的样子

function clearWorkbench() {
  WORKBENCH_BLOCKS.forEach(([sel, html]) => { $(sel).innerHTML = html; });
  document.querySelectorAll("#workbench form").forEach((f) => f.reset());   // 卡号、病程、体征、随访填了没交的
  document.querySelectorAll("#workbench .msg").forEach((m) => { m.textContent = ""; m.className = "msg"; });
  $("#round-note").classList.add("hidden");
  $("#round-vital").classList.add("hidden");
  roundAdmissionId = 0;
  spdMe = null;
  spdCalendar = null;
  history.replaceState(null, "", location.pathname + location.search);   // 下一位落在待办：进页即取数，#who 跟着改写
}

function logout() {
  // 登录口令错同样回 401、同样走到这里：那时工作台上没有上一位，不清，也不动 hash（带页签的链接照旧落在那一页）
  const signedIn = isAuthed();
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
  if (signedIn) clearWorkbench();
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

/** 本人（工作台出参的 user）：「我的患者」按它筛 */
let spdMe = null;
/** 工作台的日历（出参的 calendar）：「今日随访 / 今日复诊」两段按它的 today 取（P2-1317） */
let spdCalendar = null;

/* 慢专病页签：工作台计数 + 下面的清单。取不到工作台时把原因写进本块（P2-1800，照 loadRound 的写法），清单照常取（loadSpdList
 * 自己兜错）。原先第一句 await 没人接：switchTab 不 await 也不 catch，500、断网、428 口令到期时留下一个未处理的 rejection，首次
 * 进页一片空白，同一次登录里再进则挂着上一次的待办、今日随访、待复核转诊几项数，看不出已经过期。
 * 回 null，或取不到工作台时的那个错误：动作之后的重画据此另写「刷新失败」（spdActionDone） */
async function loadSpdTab() {
  let wb;
  try {
    wb = await api("/api/spd/workbench/doctor-mobile");
  } catch (err) {
    $("#spd-wb").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    await loadSpdList();
    return err;
  }
  spdMe = wb.user;
  spdCalendar = wb.calendar;
  const roleText = (wb.user.member_roles || []).map((r) => ({
    doctor: "医生", nurse: "护士", rehab: "康复治疗师", case_manager: "个案管理师",
    village_doctor: "村医", expert: "专家",
  }[r] || r)).join("、") || "未加入慢专病团队";
  $("#spd-wb").innerHTML = `<div class="m-card">
    ${kv("当前身份", esc(roleText))}
    ${wb.user.is_village_doctor ? kv("辖区", esc(`${wb.user.township} ${wb.user.village}`)) : ""}
    ${kv("我的待办", `${wb.todo.open} 条（今日到期 ${wb.todo.due_today}）`)}
    ${kv("超期任务", wb.todo.overdue)}
    ${kv("今日随访", wb.calendar.followups)}
    ${kv("今日复诊", wb.calendar.revisits)}
    ${kv("待复核转诊", wb.referrals.pending_review)}
    ${kv("待接收转诊", wb.referrals.pending_accept)}
    ${kv("待承接下转", wb.referrals.pending_receive)}
    ${wb.user.is_village_doctor   // 与「我的患者」同一口径（P2-373）：村医看签约居民，其余看本人是责任医生的
      ? kv("签约居民", wb.patients.village) : kv("在管患者", wb.patients.mine)}
    ${kv("积分余额", wb.points.balance)}
  </div>`;
  await loadSpdList();
  return null;
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
        else if (activeDoctorSpd === "followup") await loadSpdTodayFollowups(box);
        else if (activeDoctorSpd === "revisit") await loadSpdTodayRevisits(box);
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
   已退回的「重新提交」，其余能办结；要佐证的任务多一个「上传佐证」——原先卡片上一律摆着接收与办结、没有上传入口，要佐证的
   任务在手机上点办结恒 422，接收点在已接收的任务上恒 409，待审核的点办结则绕过了审核（P2-84）。已退回的原先也只有「办结」、
   没有重新提交的入口：点一下就绕过了再审，随访日回写、计分照记（P2-1361，办结接口对已退回的 409）。 */
function spdTodoOps(t) {
  const b = (attr, label) => `<button type="button" class="ghost-btn" ${attr}="${t.id}">${label}</button>`;
  if (t.status === "submitted") return "";
  return (["pending", "overdue"].includes(t.status) ? b("data-spd-claim", "接收") : "")
    + (t.require_evidence ? b("data-spd-evidence", "上传佐证") : "")
    + (t.status === "rejected" ? b("data-spd-resubmit", "重新提交") : b("data-spd-done", "办结"));
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
      ? `已传 ${(t.evidence || []).length} 份` : `${t.status === "rejected" ? "重新提交" : "办结"}前须上传照片或报告`) : ""}
    ${spdTodoOps(t)}
  </div>`).join("") || '<p class="empty">暂无待办</p>';
  box.querySelectorAll("[data-spd-claim]").forEach((b) => b.addEventListener("click", async () => {
    await spdPost(`/api/spd/tasks/${b.dataset.spdClaim}/claim`);
  }));
  box.querySelectorAll("[data-spd-evidence]").forEach((b) => b.addEventListener("click", () => {
    // 佐证 = 挂在该任务名下的附件（owner_type=spd_task）。上传后由后端把附件编号追加进任务的佐证清单——与管理端「上传佐证」
    // 同一走法，办结时后端再核一遍。追加在服务端锁内做（P2-972）：原先页面取任务、带着取到的办理结果以草稿整体写回，
    // 交错上传丢佐证、改回别人刚保存的办理结果
    const input = document.createElement("input");
    input.type = "file";
    // 与附件白名单同一串（P2-1083，attachments.ALLOWED_CONTENT_TYPES）：原先 image/* 让手机「高效格式」的 HEIC、扫描仪的
    // TIFF / BMP 都选得上，传上去才 415，要凭证的任务卡在这里
    input.accept = "image/png,image/jpeg,image/gif,image/webp,application/pdf";
    input.onchange = async () => {
      if (!input.files[0]) return;
      const taskId = b.dataset.spdEvidence;
      let t;
      try {
        const att = await uploadAttachment("spd_task", taskId, input.files[0]);
        t = await api(`/api/spd/tasks/${taskId}/evidence`, { method: "POST",
          body: JSON.stringify({ attachment_id: att.id }) });
      } catch (err) {
        $("#spd-msg").textContent = err.message;
        return;
      }
      // 传上了就算传上了（P2-1800）：重画失败另写一句，不当成上传失败——原先报错盖掉回执，再传一次就再挂一份同样的附件
      await spdActionDone(`佐证已上传（共 ${(t.evidence || []).length} 份），办结时一并核验`);
    };
    input.click();
  }));
  // 办理结果在卡片里用文本域收，不再弹系统输入框：弹窗录不了多行、没有校验提示、
  // 粘不了长文本（功能完善规则 §1 第 7 项；存量登记 P2-38）。再点一次不重复插表单。
  // 走 cardForm（P2-1093）：「要求上传佐证」这类拒绝写在这张卡的表单里，不再写到屏幕外的整页消息行
  box.querySelectorAll("[data-spd-done]").forEach((b) => b.addEventListener("click", () =>
    cardForm(b.closest(".m-card"), "spd-done-form",
      '<textarea name="note" rows="2" placeholder="办理结果（可留空）"></textarea>', "确认办结",
      (f) => spdPost(`/api/spd/tasks/${b.dataset.spdDone}/complete`,
        { result: { note: f.note.value.trim() } }, true))));
  // 已退回的「重新提交」（P2-1361）：与管理端任务中心「提交」同一个接口、同一份取数（办理结果写进 result.note），提交即回到
  // 待审核、等审核人再审；要佐证的照样要（后端提交时核验，拒绝写在这张卡的表单里）。不给「保存草稿」：存草稿把它翻成办理中，
  // 卡片上又摆回「办结」
  box.querySelectorAll("[data-spd-resubmit]").forEach((b) => b.addEventListener("click", () =>
    cardForm(b.closest(".m-card"), "spd-resubmit-form",
      '<textarea name="note" rows="2" placeholder="按审核意见补办的结果（可留空）"></textarea>', "提交审核",
      (f) => spdPost(`/api/spd/tasks/${b.dataset.spdResubmit}/submit`,
        { result: { note: f.note.value.trim() } }, true))));
}

/* 今日随访 / 今日复诊（P2-1317）：工作台报「今日随访 N / 今日复诊 N」，原先这一页只有待办、转诊、患者、积分四段，全文件不调
 * 随访记录与复诊清单——是哪几位在手机上无处可看。两段与那两个数同一句：本人（执行人 / 复诊医生）、计划日是工作台的「今天」
 * （日历出参的 today，服务端的业务日，不取手机本地日期——跨时区的手机按本地取会差一天）、还没做完的（open_only）。 */
async function spdCalendarDay() {
  return (spdCalendar || (await api("/api/spd/workbench/doctor-mobile")).calendar).today;
}

async function loadSpdTodayFollowups(box) {
  const day = encodeURIComponent(await spdCalendarDay());
  const rows = await api(`/api/spd/followup-records?mine=true&open_only=true&date_from=${day}&date_to=${day}&limit=100`);
  box.innerHTML = rows.map((r) => `<div class="m-card">
    ${kv("患者", esc(r.patient_name || r.patient_id))}
    ${kv("场景", esc(r.scene_name))}
    ${kv("计划日期", esc(r.planned_at))}
    ${kv("状态", esc(r.status_name))}
  </div>`).join("") || '<p class="empty">今天没有要做的随访</p>';
  if (rows.length === 100) box.insertAdjacentHTML("beforeend", '<p class="hint">只列出前 100 条，其余请到管理端随访看板查</p>');
}

async function loadSpdTodayRevisits(box) {
  const day = encodeURIComponent(await spdCalendarDay());
  const rows = await api(`/api/spd/revisits?mine=true&open_only=true&date_from=${day}&date_to=${day}&limit=100`);
  box.innerHTML = rows.map((r) => `<div class="m-card">
    ${kv("患者", esc(r.patient_name || r.patient_id))}
    ${kv("病种", esc(r.program_code || "—"))}
    ${kv("科室", esc(r.dept || "—"))}
    ${kv("复查项目", esc(r.items || "—"))}
  </div>`).join("") || '<p class="empty">今天没有排定的复诊</p>';
  if (rows.length === 100) box.insertAdjacentHTML("beforeend", '<p class="hint">只列出前 100 条，其余请到管理端复诊看板查</p>');
}

/** 转诊卡片按后端出参 `actions` 给按钮（P2-794）：原先按状态给（P2-101，免得点错一个就 409），可推进权还看机构——
 *  审核只有当前机构的直接上级、登记到院 / 承接随访只有当前持有机构、撤回只有发起人。村医自己发起的单子卡片上有
 *  「通过 / 退回」、县医院接收之后有「登记到院」，点了都是 403；发起人又没有撤回。`actions` 由后端按同一判据现算。 */
function spdReferralOps(r) {
  const on = (op) => (r.actions || []).includes(op);
  const ops = [];
  if (on("review")) {
    ops.push(`<button type="button" class="ghost-btn" data-spd-pass="${r.id}">通过</button>`,
      `<button type="button" class="ghost-btn" data-spd-reject="${r.id}">退回</button>`);
  }
  if (on("arrive")) ops.push(`<button type="button" class="ghost-btn" data-spd-arrive="${r.id}">登记到院</button>`);
  if (on("recv")) ops.push(`<button type="button" class="ghost-btn" data-spd-recv="${r.id}">承接随访</button>`);
  if (on("withdraw")) ops.push(`<button type="button" class="ghost-btn" data-spd-withdraw="${r.id}">撤回</button>`);
  return ops.join("\n    ");
}

/** 转诊卡片上的「触发依据」与「转诊资料」（P2-1609）：审核卡片原先只有理由，审核人看不到规则开单命中了哪几条、交了
 *  什么资料（清单出参早就带着这两项）。依据的中文名取 /api/spd/meta（`meta` 取不到时照原样印编码），资料链接只给
 *  http(s) 画——与管理端、居民端同一处帮手（shared.js）；有才出这一行，与任务卡片上「审核意见」一类可选行同一写法。 */
function spdReferralBasis(r, meta) {
  const basis = referralEvidenceLines(r.trigger_evidence, meta).map(esc).join("；");
  const materials = referralMaterialsHtml(r.materials);
  return (basis ? kv("触发依据", basis) : "") + (materials ? kv("转诊资料", materials) : "");
}

/** 发起上转的卡片内表单（P2-795）：村医手册写的是「在『转诊办理』里发起，写清理由」，原先这一页只有在途单的
 *  审核 / 到院 / 承接按钮，发起要回电脑上的管理端。患者从本人名下的在管档案里选（病种随档案带上），目标机构从
 *  县级机构里选（可空：不写目标的由审核环节定），理由必填。`prefill` 给「重新发起」用。 */
/* 患者下拉的选项：本人名下在管的档案。「重新发起」带来的那位不在这一页里时（名下超过 100 份、他建档早），照退回单上的
   患者与病种补一项（P2-824）——原先预填落空，要在 100 项里自己找，找不到就发不了 */
function spdReferralPatientOptions(patients, pick, prefill) {
  const opts = patients.map((e) => ({ value: `${e.patient_id}|${e.program_code}`, name: e.patient_name || String(e.patient_id),
    program: e.program_code }));
  if (prefill.patient_id && !opts.some((o) => o.value === pick)) {
    opts.unshift({ value: pick, name: prefill.patient_name || String(prefill.patient_id), program: prefill.program_code || "" });
  }
  return `<option value="">选择患者（本人名下在管）</option>` + opts.map((o) =>
    `<option value="${esc(o.value)}"${o.value === pick ? " selected" : ""}>${esc(o.name)}（${esc(o.program)}）</option>`).join("");
}

async function openSpdReferralForm(card, patients, prefill = {}, mineQuery = "") {
  let counties;
  try { counties = await api("/api/organizations?level=county"); }
  catch (err) { $("#spd-msg").textContent = err.message; return; }
  const pick = `${prefill.patient_id || ""}|${prefill.program_code || ""}`;
  cardForm(card, "spd-ref-new-form", `
    <input name="kw" placeholder="按姓名 / 证件号找患者（名下超过 100 份时用）">
    <button type="button" class="ghost-btn" data-spd-ref-find>找</button>
    <select name="enrollment" required>${spdReferralPatientOptions(patients, pick, prefill)}</select>
    <select name="target_org_id"><option value="">目标机构（可空）</option>${counties.map((o) =>
      `<option value="${o.id}"${o.id === prefill.target_org_id ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
    <textarea name="reason" rows="2" placeholder="转诊理由（血压 / 血糖控制情况、预警症状）" required>${esc(prefill.reason || "")}</textarea>`,
    "提交上转", (f) => {
      const [patientId, programCode] = f.enrollment.value.split("|");
      if (!patientId || !f.reason.value.trim()) throw new Error("请选择患者并写清转诊理由");
      return spdPost("/api/spd/referrals", { patient_id: Number(patientId), program_code: programCode,
        direction: "up", target_org_id: f.target_org_id.value ? Number(f.target_org_id.value) : null,
        reason: f.reason.value.trim() }, true);
    });
  // 按姓名 / 证件号在本人名下在管的档案里找（P2-824）：下拉只装得下前 100 份，接口早就收 keyword
  const form = card.querySelector(".spd-ref-new-form");
  if (!form) return;
  const find = async () => {
    const kw = form.elements.kw.value.trim();
    try {
      const found = await api(`/api/spd/enrollments?limit=100&${mineQuery}${kw ? `&keyword=${encodeURIComponent(kw)}` : ""}`);
      form.elements.enrollment.innerHTML = spdReferralPatientOptions(found, pick, prefill);
      $("#spd-msg").textContent = found.length ? "" : "本人名下在管的档案里没有匹配的";
    } catch (err) { $("#spd-msg").textContent = err.message; }
  };
  form.querySelector("[data-spd-ref-find]").onclick = find;
  form.elements.kw.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); find(); } };   // 回车是找，不是提交
}

async function loadSpdReferral(box) {
  const me = spdMe || (await api("/api/spd/workbench/doctor-mobile")).user;
  const mine = me.is_village_doctor ? `village_doctor_id=${me.id}` : `doctor_user_id=${me.id}`;
  // 本人发起、被退回的单子单列（P2-795）：手册写「看退回理由，补材料后重新发起」，原先这一页只取在途的，
  // 退回的单子（已结束）从清单里消失，退回意见在手机上看不到
  // 本人发起的由接口按发起人筛（P2-824）：原先取机构最新 50 张退回单、在这里按发起人挑，同机构别人的退回单一多，本人的
  // 整段被挤掉
  // 规则字段与比较符的中文名（P2-1609，卡片上的触发依据用）：取不到不挡卡片，依据照原样印编码
  const [rows, myRejected, patients, meta] = await Promise.all([
    api("/api/spd/referrals?open_only=true&limit=30"), api("/api/spd/referrals?status=rejected&mine=true&limit=10"),
    api(`/api/spd/enrollments?limit=100&${mine}`), api("/api/spd/meta").catch(() => null)]);
  box.innerHTML = `<div class="m-card" id="spd-ref-new-card">
      <button type="button" class="ghost-btn" data-spd-ref-new>发起上转</button>
    </div>` + (rows.map((r) => `<div class="m-card">
    ${kv("患者", esc(r.patient_name))}
    ${kv("方向", r.direction === "up" ? "上转" : "下转")}
    ${kv("当前环节", esc({ submitted: "待卫生院审核", station_reviewed: "待卫生院审核(存量)",
      township_reviewed: "待县级接收", accepted: "已接收待到院",
      arrived: "已到院", down_referred: "待承接随访" }[r.status] || r.status))}
    ${kv("理由", esc(r.reason || "—"))}
    ${spdReferralBasis(r, meta)}
    ${spdReferralOps(r)}
  </div>`).join("") || '<p class="empty">暂无在途转诊</p>') + (myRejected.length ? `
    <p class="hint">我发起的、被退回的（最近 ${myRejected.length} 张）</p>` + myRejected.map((r) => `<div class="m-card">
    ${kv("患者", esc(r.patient_name))}
    ${kv("理由", esc(r.reason || "—"))}
    ${kv("退回时间", esc((r.closed_at || "").replace("T", " ").slice(0, 16) || "—"))}
    <div data-spd-reject-note="${r.id}"></div>
    <button type="button" class="ghost-btn" data-spd-reject-view="${r.id}">看退回意见</button>
    <button type="button" class="ghost-btn" data-spd-ref-again="${r.id}">重新发起</button>
  </div>`).join("") : "");
  box.querySelector("[data-spd-ref-new]").addEventListener("click", () =>
    openSpdReferralForm(box.querySelector("#spd-ref-new-card"), patients, {}, mine));
  box.querySelectorAll("[data-spd-reject-view]").forEach((b) => b.addEventListener("click", async () => {
    const note = b.closest(".m-card").querySelector("[data-spd-reject-note]");
    try {
      const detail = await api(`/api/spd/referrals/${b.dataset.spdRejectView}`);
      const step = [...(detail.steps || [])].reverse().find((x) => x.action === "reject");
      note.innerHTML = kv("退回意见", esc((step && step.opinion) || "（审核人没写意见）"));
    } catch (err) { note.innerHTML = kv("退回意见", esc(err.message)); }
  }));
  box.querySelectorAll("[data-spd-ref-again]").forEach((b) => b.addEventListener("click", () => {
    const r = myRejected.find((x) => String(x.id) === b.dataset.spdRefAgain);
    openSpdReferralForm(b.closest(".m-card"), patients, r, mine);
  }));
  const bind = (attr, path, body) => box.querySelectorAll(`[${attr}]`).forEach((b) =>
    b.addEventListener("click", () => spdPost(path(b), body ? body() : null)));
  // 通过 / 退回的意见在卡片里填（与管理端同一口径：意见可空）。原先 prompt 点"取消"照样通过、
  // 照样退回——想反悔的人反而把单子退了回去，还没有理由。
  const review = (attr, action, placeholder, submitLabel) =>
    box.querySelectorAll(`[${attr}]`).forEach((b) => b.addEventListener("click", () =>
      cardForm(b.closest(".m-card"), "spd-review-form",
        `<textarea name="opinion" rows="2" placeholder="${placeholder}"></textarea>`, submitLabel,
        (f) => spdPost(`/api/spd/referrals/${b.getAttribute(attr)}/review`,
          { action, opinion: f.opinion.value.trim() }, true))));
  review("data-spd-pass", "pass", "审核意见（可空）", "确认通过");
  review("data-spd-reject", "reject", "退回理由", "确认退回");
  bind("data-spd-arrive", (b) => `/api/spd/referrals/${b.dataset.spdArrive}/arrive`,
    () => ({ effective_visit: true }));
  bind("data-spd-recv", (b) => `/api/spd/referrals/${b.dataset.spdRecv}/receive-followup`,
    () => ({ opinion: "已接收随访" }));
  // 撤回（P2-794）：发起人本人、上级审核之前。撤回不可逆（后端没有反向动作），先在卡片里确认一下
  box.querySelectorAll("[data-spd-withdraw]").forEach((b) => b.addEventListener("click", () =>
    cardForm(b.closest(".m-card"), "spd-withdraw-form", '<p class="hint">撤回后这张转诊单结束，要转诊需重新发起。</p>',
      "确认撤回", () => spdPost(`/api/spd/referrals/${b.dataset.spdWithdraw}/withdraw`, null, true))));
}

async function loadSpdPatients(box) {
  // 只列本人名下的（P2-373）：原先不带筛选，列的是可见机构最新 30 份在管档案——多半是别的医生的患者。
  // 村医按签约村医筛，其余按责任医生筛，与上方「签约居民 / 在管患者」计数同一口径
  const me = spdMe || (await api("/api/spd/workbench/doctor-mobile")).user;
  const mine = me.is_village_doctor ? `village_doctor_id=${me.id}` : `doctor_user_id=${me.id}`;
  const rows = await api(`/api/spd/enrollments?limit=30&${mine}`);
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
  // 签到 / 兑换的结果里有要给人看的数字（加了几分、核销码），不能走 spdPost 那句「操作成功」；成功之后的重画同样交给
  // spdActionDone（P2-1800）：重画失败不盖掉核销码
  const act = async (path, body, okText) => {
    let r;
    try {
      r = await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
    } catch (err) { $("#spd-msg").textContent = err.message; return; }
    await spdActionDone(okText(r));
  };
  box.querySelectorAll("[data-spd-signin]").forEach((b) => b.addEventListener("click", () =>
    act("/api/spd/point-accounts/signin", null, (r) => `签到成功：+${r.points} 分，余额 ${r.balance}`)));
  box.querySelectorAll("[data-spd-redeem]").forEach((b) => b.addEventListener("click", () =>
    act("/api/spd/redeems", { goods_id: Number(b.dataset.spdRedeem) },
      (r) => `兑换成功，核销码 ${r.verify_code}（到点位出示），余额 ${r.balance}`)));
}

/** 动作已成功之后（P2-1800）：回执先写上再重画；重画取不到工作台时回执照留、另写一句「已办理，刷新失败：…」——动作已经成了，
 *  不能当成没成。原先重画的报错抛回动作：卡片表单写「请求失败」、填的理由还留着，整页消息行的回执也被盖掉，看着像没交上，医生
 *  再交一次就重复开在途上转单、重复挂附件。spdPost、上传佐证、签到 / 兑换共用这一段 */
async function spdActionDone(okText) {
  $("#spd-msg").textContent = okText;
  const failed = await loadSpdTab();
  if (failed) $("#spd-msg").textContent = `${okText}；已办理，刷新失败：${failed.message}`;
}

/* inline=true：卡片内表单提交用，失败时把错抛给 cardForm、写进那张卡的表单里（P2-1093），不写整页消息行。
 * 只有动作本身的失败算失败（P2-1800）：成功之后的重画交给 spdActionDone，不抛回卡片表单 */
async function spdPost(path, body, inline = false) {
  try {
    await api(path, { method: "POST", body: body ? JSON.stringify(body) : undefined });
  } catch (err) {
    if (inline) throw err;
    $("#spd-msg").textContent = err.message;
    return;
  }
  await spdActionDone("操作成功");
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
    // 角标取未读总数（P2-374）：原先是这一页的条数，积压过 20 条也只显示 20
    const [data, notices, unread] = await Promise.all([
      api("/api/todos"), api("/api/notifications?unread_only=true&limit=20"), api("/api/notifications/unread-count")]);
    $("#who").innerHTML = `<span>${esc(sessionStorage.getItem(USER_KEY) || "")}</span>
      <span class="role">${esc(ROLE_NAMES[data.role] || data.role)} · 待办 ${data.total}</span>`;
    const noticeBlock = notices.length ? `<div class="todo-group">
      <div class="head"><span>未读消息</span><span class="badge warn">${unread.unread}</span></div>
      ${unread.unread > notices.length ? `<p class="hint">仅显示最近 ${notices.length} 条</p>` : ""}
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
          .filter(([k]) => k !== "id" && k !== "org_id")   // 机构显示名称（org_name），不显示编号
          .map(([k, v]) => kv(FIELD_NAMES[k] || k, esc(TODO_VALUES[k] ? TODO_VALUES[k](v) : v)))
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
      } catch (err) {
        btn.disabled = false;
        return;
      }
      // 「未读消息」角标跟着改（P2-1312）：原先只移除卡片，角标照挂标记前的数。照取数时的同一个接口重取未读总数（角标是
      // 未读总数、不是这一屏的条数——P2-374）；取不到就按刚标的这一条减一
      const badge = btn.closest(".todo-group").querySelector(".badge");
      btn.closest(".notice").remove();
      let unread;
      try { ({ unread } = await api("/api/notifications/unread-count")); }
      catch (err) { unread = Math.max(0, Number(badge.textContent) - 1); }
      badge.textContent = unread;
      badge.className = `badge ${unread ? "warn" : "zero"}`;
    });
  });
}

// 缺药行按可发量判（P2-1250）：`quantity` 是账面汇总、`dispensable` 是此刻能发多少——批次过了效期两者就不等，
// 只印「库存 100 / 阈值 20」看不出为什么缺药
const FIELD_NAMES = {
  diagnosis_name: "诊断", review_comment: "审方意见", center_type: "中心", item_name: "项目",
  status: "状态", conclusion: "结论", critical_status: "危急值状态", request_id: "申请单",
  drug_name: "药品", quantity: "账面库存", threshold: "阈值", dispensable: "可发", org_name: "机构",
};
/** 待办行里的取值按键翻译（P2-371）：原先原样打印——「中心 imaging」「状态 pending」「危急值状态 notified」。
 *  取值表用本文件现成的（检查申请页、危急值页同一套）；带「状态」的只有待诊断申请行 */
const TODO_VALUES = {
  center_type: (v) => CENTER_NAMES[v] || v,
  status: (v) => (EXAM_STATUS[v] || [v])[0],
  critical_status: (v) => (CRITICAL_TAGS[v] || [v])[0],
};

/* ---------------- 危急值确认与处置 ---------------- */

const CRITICAL_TAGS = {
  notified: ["待确认", "red"], "": ["待确认", "red"],
  acknowledged: ["已接收，待处置", "orange"], resolved: ["已闭环", "green"],
};

async function loadCritical() {
  const box = $("#critical-list");
  try {
    // 未处置的续页取全排在最前，再接缺省清单里最近已处置的、按 id 去重（P2-1711，同管理端危急值操作台）：缺省清单一页 100 条、
    // 不能翻页，未处置的一过 100 条，最早那条已确认的就点不到「处置反馈」
    const [open, recent] = await Promise.all([
      fetchAllPages(api, "/api/exams/critical?open=true"), api("/api/exams/critical")]);
    const openIds = new Set(open.map((r) => r.id));
    const reports = [...open, ...recent.filter((r) => !openIds.has(r.id))];
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
      // 所见照原样换行显示（P2-1363，与病程正文同一个样式）：LIS 回传的逐项结果只在所见里，卡片原先只列结论——
      // 村医在手机上确认接收时看不到是哪一项、多少
      // 报告时间（P2-1364）：卡片原先不出时间，何时出具只能查库
      return card(
        kv("报告编号", esc(r.id)) + kv("申请单", esc(r.request_id)) +
        kv("报告时间", esc(r.reported_at.slice(0, 16).replace("T", " "))) +
        kv("结论", esc(r.conclusion)) + kv("状态", statusTag(CRITICAL_TAGS, r.critical_status)) +
        (r.finding ? `<p class="note-body">${esc(r.finding)}</p>` : ""),
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
      // 说明必填（P2-1710）：原先留空照样提交、照样闭环；只填空格的 trim 后是空串，由后端 422、报错写在这张卡的表单里
      return cardForm(e.target.closest(".m-card"), "crit-resolve-form",
        '<textarea name="note" rows="2" placeholder="处置措施（如：已联系患者并调整治疗方案）" required></textarea>',
        "提交处置反馈", async (f) => {   // 失败时 cardForm 把报错写在这张卡的表单里（P2-1093）
          await api(`/api/exams/reports/${resolve}/resolve`, { method: "POST",
            body: JSON.stringify({ note: f.note.value.trim() }) });
          setMsg("#critical-msg", "处置已反馈，危急值闭环完成", true);
          await loadCritical();
        });
    }
    if (trace) {
      const actions = await api(`/api/exams/reports/${trace}/critical-actions`);
      // 每一步前面写时刻（P2-1364）：何时通知、何时确认、何时处置，原先只拼「操作人：动作」
      alert(actions.length
        ? actions.map((a) => `${a.at.slice(0, 16).replace("T", " ")} ${a.actor}：${a.action}`).join("\n")
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
    // 按状态续页取全（P2-1711，同管理端共享诊断页）：接口缺省一页 200 张，205 张待诊断时最早那批不在卡片里
    const [pending, diagnosing] = await Promise.all([
      fetchAllPages(api, "/api/exams?status=pending"), fetchAllPages(api, "/api/exams?status=diagnosing"),
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
      // 患者号与临床资料（检查目的）印上卡片、临床资料空的不印（P2-1367）：卡片原先只有申请单 / 中心 / 项目 / 状态，同一项目
      // 十几张单只能凭单号对，临床资料移动端无处可看。出报告的表单开在这张卡片里、就在这几行下面。患者姓名随 P2-681 待裁定
      return card(
        kv("申请单", esc(r.id)) + kv("患者号", esc(r.patient_id)) +
        kv("中心", esc(CENTER_NAMES[r.center_type] || r.center_type)) + kv("项目", esc(r.item_name)) +
        (r.clinical_info ? kv("临床资料", esc(r.clinical_info)) : "") + kv("状态", statusTag(EXAM_STATUS, r.status)),
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
        "出具报告", async (f) => {   // 失败时 cardForm 把报错写在这张卡的表单里（P2-1093）
          const critical = f.critical.value === "1";
          await api(`/api/exams/${report}/report`, { method: "POST", body: JSON.stringify({
            conclusion: f.conclusion.value.trim(), finding: f.finding.value.trim(), critical }) });
          setMsg("#exam-msg", critical ? "报告已出具，危急值已通知申请机构" : "报告已出具", true);
          await loadExams();
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
    // 病种目录取全部（含已停用的）（P2-458）：停用是「不再新增」，「已建的档案不受影响、仍按原规则随访」（慢病页原话）——原先只取启用中的，
    // 停用病种的档案一选上就画不出指标框、提示「该病种未配置分级指标」，医生只能登一条没有指标的随访
    const [types, list] = await Promise.all([
      api("/api/chronic/disease-types"), api("/api/chronic?limit=100"),
    ]);
    diseaseTypes = types;
    const byCode = Object.fromEntries(types.map((t) => [t.code, t]));
    // 重画时留住选中的那一份、选项写上患者号（P2-1011）：原先录完一条就重画下拉、不带 selected，悄悄跳回分级最高的第一条，
    // 指标框按那一份的病种重画——医生以为还是刚才那位，补一条就记进了别人的档案（同文件查房 loadRound 选中的还在就留着）
    const picked = $("#fu-chronic").value;
    $("#fu-chronic").innerHTML = list.length
      ? list.map((c) => `<option value="${c.id}" data-disease="${esc(c.disease)}"${String(c.id) === picked ? " selected" : ""}>
          档案${c.id} · 患者${esc(c.patient_id)} · ${esc((byCode[c.disease] || {}).name || c.disease)} · ${c.level}级</option>`).join("")
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
  // 同一指标挂了几条分级规则的只画一个框（P2-374）：种子糖尿病的空腹血糖挂着「偏高」与「低血糖」两条（P2-119），原先画两个框，
  // 两个框填的是同一个键，提交时后一个静默盖掉前一个——填了「空腹血糖」、没填「低血糖」那格，这次随访就没有血糖
  const seen = new Set();
  const metrics = (((type || {}).level_rules || {}).metrics || []).filter((m) => {
    if (seen.has(m.key)) return false;
    seen.add(m.key);
    return true;
  });
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
  // 本次指导原先录不了（P2-862）：随访记录的「指导」列恒为空
  const body = { metrics: {}, next_due: $("#fu-next").value.trim(), guidance: $("#fu-guidance").value.trim() };
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
    $("#fu-guidance").value = "";
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
    // 病种显示名称（P2-374）：与慢病随访页同一取法（病种目录按编码查），目录取不到就退回编码，不拖垮速查
    const [data, types] = await Promise.all([
      api(`/api/archive/${encodeURIComponent($("#pt-ehc").value.trim())}`),
      api("/api/chronic/disease-types").catch(() => [])]);
    const byCode = Object.fromEntries(types.map((t) => [t.code, t]));
    const p = data.patient || {};
    const chronic = (data.chronic_diseases || []).map((c) => card(
      kv("病种", esc((byCode[c.disease] || {}).name || c.disease)) + kv("分级", `${c.level} 级`) +
      kv("下次随访", esc(c.next_due || "待安排"))
    )).join("");
    // 截断要说出来（P2-374）：每段只显示最近 20 条，后端每段也最多给 section_limit 条（has_more 标着）——原先都不说
    const more = data.has_more || {};
    const cut = (rows, key) => (rows.length > 20 || more[key]
      ? `<p class="hint">仅显示最近 20 条（共 ${more[key] ? `${data.section_limit} 条以上` : `${rows.length} 条`}），完整记录请到电脑端查询</p>`
      : "");
    // 印出日期与检查项目（P2-1196）：原先只印诊断 / 类型 / 摘要、结论 / 危急值，分不清是哪天、哪项检查、危急值处置了没有。
    // 日期取 360 给的就诊 / 报告时刻，照本文件其他时间的写法截串；危急值处置状态用本文件危急值页那张 CRITICAL_TAGS
    const encounters = (data.encounters || []).slice(0, 20).map((en) => card(
      kv("就诊日期", esc(en.created_at.slice(0, 10))) +
      kv("诊断", esc(en.diagnosis_name || "—")) +
      kv("类型", esc(en.encounter_type === "inpatient" ? "住院" : "门诊")) +
      (en.summary ? kv("摘要", esc(en.summary)) : "")
    )).join("");
    const reports = (data.exam_reports || []).slice(0, 20).map((r) => card(
      kv("报告日期", esc(r.reported_at.slice(0, 10))) +
      kv("项目", esc(r.item_name || "—")) +
      kv("结论", esc(r.conclusion)) +
      (r.critical ? kv("危急值", '<span class="tag red">是</span>') +
        kv("危急值处置", statusTag(CRITICAL_TAGS, r.critical_status)) : "")
    )).join("");
    $("#pt-result").innerHTML = `
      <div class="m-card">${kv("姓名", esc(p.name))}${kv("健康卡号", esc(p.ehc_no))}${kv("性别", esc(p.gender || "—"))}</div>
      <div class="sec-title">慢病在管</div>${chronic || '<p class="empty">无</p>'}
      <div class="sec-title">就诊记录</div>${encounters || '<p class="empty">无</p>'}${cut(data.encounters || [], "encounters")}
      <div class="sec-title">检查检验报告</div>${reports || '<p class="empty">无</p>'}${cut(data.exam_reports || [], "exam_reports")}`;
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
/** 在院清单（loadRound 取到的那一份）：病程、体征的回执按住院号写明是谁（P2-1798） */
let roundAdmissions = [];

/** 查房回执写明是谁（P2-1798）：与下拉选项同一句「病区 床号 姓名」（P2-1335），取得到什么写什么；清单里找不到时写住院号 */
function roundWho(admissionId) {
  const a = roundAdmissions.find((x) => x.id === admissionId);
  return (a && [a.ward_name, a.bed_no, a.patient_name].filter(Boolean).join(" ")) || `住院号 ${admissionId}`;
}

async function loadRound() {
  const picker = $("#round-adm");
  let admissions = [];
  try {
    // 只取在院的（P2-154）：原先取「最新 200 条住院」再筛在院，住得久的患者被新入院的挤出去，查房选不到他。
    // 续页取全（P2-1333，shared.js）：一页最多 500 条，原先只取第一页，在院过 500 人时住得最久的那几位照样选不到
    admissions = (await fetchAllPages(api, "/api/inpatient/admissions?status=admitted"))
      .filter((a) => a.status === "admitted");
  } catch (err) {
    $("#round-status").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    return;
  }
  roundAdmissions = admissions;
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
  // 选项写「病区 床号 姓名」（P2-1335，与桌面住院临床文书同一句）：原先只有「诊断（住院号 N）」，同诊断的几位分不开，
  // 查房的病程、体征容易记到别人名下
  picker.innerHTML = admissions.map((a) =>
    `<option value="${a.id}" ${a.id === roundAdmissionId ? "selected" : ""}>
       ${esc(a.ward_name)} ${esc(a.bed_no)} ${esc(a.patient_name)} · ${esc(a.diagnosis_name || "住院")}（住院号 ${a.id}）</option>`).join("");
  $("#round-note").classList.remove("hidden");
  $("#round-vital").classList.remove("hidden");
  await refreshRoundDetail();
}

// 区块只画最后一次切换的那一位（P1-231）：先切甲、立刻改乙，甲那一批晚到会把甲的病程 / 体征画在乙名下
let roundSeq = 0;

async function refreshRoundDetail() {
  const seq = ++roundSeq;
  const admissionId = roundAdmissionId;
  let completeness, notesPage, vitalsPage;
  try {
    [completeness, notesPage, vitalsPage] = await Promise.all([
      api(`/api/inpatient/admissions/${admissionId}/document-completeness`),
      api(`/api/inpatient/admissions/${admissionId}/progress-notes`, { withTotal: true }),
      api(`/api/inpatient/admissions/${admissionId}/vitals`, { withTotal: true }),
    ]);
  } catch (err) {
    // 取不到就说清楚、清掉上一位的区块——原先报错没人接，屏幕上一直挂着上一位的病程 / 体征
    if (seq !== roundSeq) return;
    $("#round-status").innerHTML = `<p class="empty">${esc(err.message)}</p>`;
    $("#round-notes").innerHTML = "";
    $("#round-vitals").innerHTML = "";
    return;
  }
  if (seq !== roundSeq) return;
  // 条数读总数（P2-1771，同桌面住院临床文书）：病程一页 100 条、体征一页 500 次，原先印这一页的条数，过了一页恒为 100 / 500。
  // 列不全时写「已列 N / 共 M」，列全时与原先一字不差
  const notes = notesPage.rows, vitals = vitalsPage.rows;
  const listed = ({ rows, total }) => (total !== null && rows.length < total ? `已列 ${rows.length} / 共 ${total}` : `${rows.length}`);
  $("#round-status").innerHTML = `<div class="m-card">
    ${kv("文书完整性", completeness.complete
      ? '<span class="tag green">完整</span>'
      : `<span class="tag orange">${esc(completeness.missing.join("、"))}</span>`)}
    ${kv("病程记录", `${listed(notesPage)} 条`)}${kv("体征记录", `${listed(vitalsPage)} 条`)}</div>`;

  $("#round-notes").innerHTML = `<div class="sec-title">病程记录（${listed(notesPage)}）</div>` + (
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
           ${kv("血压", v.sbp != null || v.dbp != null ? `${v.sbp ?? "—"}/${v.dbp ?? "—"}` : "—")}
           ${kv("出入量", v.intake_ml != null || v.output_ml != null ? `${v.intake_ml ?? "—"} / ${v.output_ml ?? "—"} ml` : "—")}
           ${kv("体重", v.weight_kg != null ? `${v.weight_kg} kg` : "—")}`)).join("")
      : '<p class="empty">尚无体征记录</p>');
}

$("#round-pick").addEventListener("submit", async (e) => {
  e.preventDefault();
  roundAdmissionId = Number($("#round-adm").value);
  await refreshRoundDetail();
});
// 下拉一改就切换（P1-231）：原先只有点「切换患者」才改写入对象——下拉显示乙、没点切换，病程与体征照旧写进甲，
// 回执照样「病程已记录」。区块不写姓名，屏幕上看不出没切过去
$("#round-adm").addEventListener("change", async () => {
  roundAdmissionId = Number($("#round-adm").value);
  await refreshRoundDetail();
});

// 提交时记下是谁、交了什么（P2-1798）：弱网下提交在途，医生已切到下一床、开始写下一位——原先回包一到就按「此刻」清空输入、
// 写回执，下一位写了一半的被清空，「病程已记录 / 体征已录入」写在下一位的区块上方。现在请求发给提交那一刻的住院号；回包时还停在
// 这一位才照旧清空输入，已切走的只清还是这次交上去原样的格子（下一位填过的留着，上一位没动过的不能跟着下一位交上去）；回执写明
// 记在谁名下
$("#round-note").addEventListener("submit", async (e) => {
  e.preventDefault();
  const admissionId = roundAdmissionId, who = roundWho(admissionId);
  const sent = { "#round-content": $("#round-content").value, "#round-at": $("#round-at").value };
  const body = { note_type: $("#round-note-type").value, content: $("#round-content").value.trim() };
  // 记录时间（P2-1767，同桌面病程表单）：补记的照实填；留空不送，后端按此刻。日期时间控件送 `T` 分隔，换成空格再送
  // （同下面体征的测量时刻，P1-100）
  const recordedAt = $("#round-at").value.trim().replace("T", " ");
  if (recordedAt) body.recorded_at = recordedAt;
  try {
    await api(`/api/inpatient/admissions/${admissionId}/progress-notes`, {
      method: "POST",
      body: JSON.stringify(body),
    });
    // 记录时间一并清空（同体征录完清测量时刻，P1-231）：留着就成了下一条病程的记录时间
    if (roundAdmissionId === admissionId) {
      $("#round-content").value = "";
      $("#round-at").value = "";
    } else {
      Object.entries(sent).forEach(([s, v]) => { if ($(s).value === v) $(s).value = ""; });
    }
    setMsg("#round-msg", `病程已记录（${who}）`, true);
    await refreshRoundDetail();
  } catch (err) { setMsg("#round-msg", err.message, false); }
});

$("#round-vital").addEventListener("submit", async (e) => {
  e.preventDefault();
  const admissionId = roundAdmissionId, who = roundWho(admissionId);   // 同上（P2-1798）
  // 未测项留空 → 不进 body，落库为 null；填 0 会污染趋势曲线
  // 日期时间控件送 `T` 分隔，换成空格再送：与桌面端、服务端默认的写法一致（P1-100）
  const body = { measured_at: $("#rv-at").value.trim().replace("T", " ") };
  // 出入量、体重（P2-473）：接口与体温单模型一直有这三项，查房这里原先录不进、也看不见
  for (const [field, sel] of [["temperature", "#rv-temp"], ["pulse", "#rv-pulse"],
                              ["respiration", "#rv-resp"], ["sbp", "#rv-sbp"], ["dbp", "#rv-dbp"],
                              ["intake_ml", "#rv-in"], ["output_ml", "#rv-out"], ["weight_kg", "#rv-weight"]]) {
    const raw = $(sel).value.trim();
    if (raw !== "") body[field] = Number(raw);
  }
  const sent = Object.fromEntries(["#rv-at", "#rv-temp", "#rv-pulse", "#rv-resp", "#rv-sbp", "#rv-dbp", "#rv-in", "#rv-out",
    "#rv-weight"].map((s) => [s, $(s).value]));
  try {
    await api(`/api/inpatient/admissions/${admissionId}/vitals`, {
      method: "POST", body: JSON.stringify(body) });
    // 测量时刻一并清空（P1-231）：原先只清数值，换人后下一位带着上一位的测量时刻
    if (roundAdmissionId === admissionId) {
      ["#rv-at", "#rv-temp", "#rv-pulse", "#rv-resp", "#rv-sbp", "#rv-dbp", "#rv-in", "#rv-out", "#rv-weight"]
        .forEach((s) => { $(s).value = ""; });
    } else {
      Object.entries(sent).forEach(([s, v]) => { if ($(s).value === v) $(s).value = ""; });
    }
    setMsg("#round-vital-msg", `体征已录入（${who}）`, true);
    await refreshRoundDetail();
  } catch (err) { setMsg("#round-vital-msg", err.message, false); }   // 体征表单自己的消息行（P2-1093）
});

/* ---------------- 手术：排班与术中记录 ---------------- */

async function loadSurgery() {
  let schedules = [], requests = [];
  try {
    // 按状态取已排班的（P2-361）：原先取申请清单默认的最新 100 条再挑已排班——择期手术提前两周申请的，
    // 到手术日早已排在 100 条之外，术中记录填不了
    [schedules, requests] = await Promise.all([
      api("/api/surgery/schedules"), api("/api/surgery/requests?status=scheduled")]);
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
  // 这台的排班日期与时段跟着「填写术中记录」按钮走（P2-1116）：表单的手术起止时刻缺省取它。排班表只列今天及以后，
  // 更早的排班这里找不到，起止就留空（后端按排班日）
  const slots = Object.fromEntries(schedules.map((s) => [s.request_id, s]));
  $("#surgery-requests").innerHTML = `<div class="sec-title">待填术中记录（${pending.length}）</div>` + (
    pending.length
      ? pending.map((r) => {
          const slot = slots[r.id];
          const start = slot ? `${slot.scheduled_date} ${slot.start_time}` : "";
          const end = slot ? `${slot.scheduled_date} ${slot.end_time}` : "";
          return card(
            `${kv("术式", esc(r.surgery_name))}${kv("住院号", String(r.admission_id))}
             ${kv("状态", statusTag(SURGERY_STATUS_NAMES, r.status))}`,
            `<button class="op" data-record="${r.id}" data-name="${esc(r.surgery_name)}"
              data-surgeon="${esc(r.surgeon_name)}"
              data-anesthesia="${esc(r.anesthesia_type)}" data-incision="${esc(r.incision_level)}"
              data-start="${esc(start)}" data-end="${esc(end)}">填写术中记录</button>`);
        }).join("")
      : '<p class="empty">没有待填写的术中记录</p>');
}

$("#tab-surgery").addEventListener("click", (e) => {
  const id = e.target.dataset.record;
  if (!id) return;
  // P2-38 / P1-66：原先四连问、转归写死"好转"（质量指标的治愈率与死亡数取的正是它）。
  // 改成卡片内表单：转归可选，术前/术后诊断（诊断符合率的数据源）也能填。再点一次不重复插表单。
  // 走 cardForm（P2-1093）：出血量写错这类报错写在这张卡的表单里，不再写到两张列表下方的整页消息行
  cardForm(e.target.closest(".m-card"), "surg-record-form", `<input name="actual_surgery_name" placeholder="实际术式" required
      value="${esc(e.target.dataset.name || "")}">
    <input name="surgeon_name" placeholder="术者（留空取申请单上的拟施术者）" value="${esc(e.target.dataset.surgeon || "")}">
    <input name="assistants" placeholder="助手（可空）">
    <p class="hint">手术起止：缺省带出排班日期与时段，按实际改；留空按排班日</p>
    <input name="start_at" placeholder="开始时刻 YYYY-MM-DD HH:MM" value="${esc(e.target.dataset.start || "")}">
    <input name="end_at" placeholder="结束时刻 YYYY-MM-DD HH:MM" value="${esc(e.target.dataset.end || "")}">
    <input name="anesthetist_name" placeholder="麻醉医师">
    <select name="anesthesia_type">${Object.entries(ANESTHESIA_NAMES).map(([k, v]) =>
      `<option value="${k}"${k === e.target.dataset.anesthesia ? " selected" : ""}>${v}</option>`).join("")}</select>
    <select name="incision_level">${["I", "II", "III", "IV"].map((x) =>
      `<option value="${x}"${x === e.target.dataset.incision ? " selected" : ""}>${x} 类切口</option>`).join("")}</select>
    <textarea name="findings" rows="2" placeholder="术中所见"></textarea>
    <input name="complications" placeholder="并发症（无则留空）">
    <input name="blood_loss_ml" inputmode="numeric" placeholder="出血量 ml（可空）">
    <select name="outcome">${["治愈", "好转", "未愈", "死亡"].map((x) =>
      `<option${x === "好转" ? " selected" : ""}>${x}</option>`).join("")}</select>
    <input name="preop_diagnosis" placeholder="术前诊断（可空）">
    <input name="postop_diagnosis" placeholder="术后诊断（可空）">`, "提交术中记录", async (f) => {
    const blood = f.blood_loss_ml.value.trim();
    const died = f.outcome.value === "死亡";
    await api(`/api/surgery/requests/${id}/record`, {
      method: "POST",
      body: JSON.stringify({
        actual_surgery_name: f.actual_surgery_name.value.trim(),
        // 术者 / 助手原先不送（P2-1307）：术者恒取申请单上的拟施术者（缺省即申请人）——住院医提申请、外科医生主刀并录入，
        // 手术记录署的是住院医。缺省带出申请单上的、按实际改；留空照旧由后端取申请单上的
        surgeon_name: f.surgeon_name.value.trim(),
        assistants: f.assistants.value.trim(),
        // 手术起止时刻原先不送（P2-1116）：做手术那天（术后随访起算、手术质量指标归月）恒取排班日，顺延、提前的手术都
        // 跟着原排班日走。缺省带出排班的，按实际改；格式不对由后端报人话
        start_at: f.start_at.value.trim(),
        end_at: f.end_at.value.trim(),
        anesthetist_name: f.anesthetist_name.value.trim(),
        // 麻醉方式与切口等级原先不送（P2-179），后端缺省全麻、II 类——移动端记的每一台都记成全麻 II 类切口，
        // 手术量统计的切口 / 麻醉构成跟着失真。现在缺省带出申请时填的，可改
        anesthesia_type: f.anesthesia_type.value,
        incision_level: f.incision_level.value,
        findings: f.findings.value.trim(),
        // 并发症原先不录（P2-861）：手机上记的每一台都进「手术并发症发生率」的分母、进不了分子——管理端表单早有这一项
        complications: f.complications.value.trim(),
        // 留空记 0；写错的原样交给后端报人话，别让 Number() 把它悄悄变成 NaN → null
        blood_loss_ml: blood === "" ? 0 : (Number.isNaN(Number(blood)) ? blood : Number(blood)),
        outcome: f.outcome.value,
        preop_diagnosis: f.preop_diagnosis.value.trim(),
        postop_diagnosis: f.postop_diagnosis.value.trim(),
      }),
    });
    // 转归「死亡」后端不派术后随访（P2-499）：回执原先一律写「已自动派生」（P2-779）
    setMsg("#surgery-msg", died ? "术中记录已提交（转归死亡，不派生术后随访）"
      : "术中记录已提交，术后随访任务已自动派生", true);
    await loadSurgery();
  });
});

/* ---------------- 启动 ----------------
   放在文件最末：新增页签用到模块级状态（roundAdmissionId），启动调用若排在
   声明之前，就要靠"异步函数在首个 await 前挂起"这种脆弱假设才不触发 TDZ。 */

switchTab(currentTab());
