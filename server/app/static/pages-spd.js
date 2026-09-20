/* 管理端 · 全域慢专病全流程管理系统（十一端）。
 *
 * 一个端一个页面，与招标文件《服务内容及要求》的分端一一对应。页面共用
 * core.js 的 table / barChart / formJson / postAction 四件套，**不引入新依赖**
 * ——build-free 是既定约束，加一个页面文件即可，不要借机上打包器。
 *
 * 页面之间只共享下面这几个小工具，其余一律各自拉自己的接口：
 * 页面间共享状态会让"从 A 页跳到 B 页看到的是 A 页的筛选条件"这类问题冒出来。
 */

/* 各端共用的目录数据（病种/团队/量表/路径），首次访问任一慢专病页面时拉一次。
 * 缓存在内存而不是 localStorage：配置改了要能刷新页面就生效。 */
let SPD_CATALOG = null;

async function spdCatalog(force) {
  if (!SPD_CATALOG || force) SPD_CATALOG = await api("/api/spd/catalog");
  return SPD_CATALOG;
}

const SPD_RISK = {
  low: ["低危", "green"], mid: ["中危", "orange"],
  high: ["高危", "red"], very_high: ["极高危", "red"],
};
const SPD_TASK_STATUS = {
  pending: ["待接收", "orange"], claimed: ["已接收", ""], doing: ["办理中", ""],
  submitted: ["待审核", "orange"], done: ["已完成", "green"],
  rejected: ["已退回", "red"], overdue: ["已超期", "red"], cancelled: ["已取消", ""],
};
const SPD_TASK_TYPES = {
  path: "路径节点", followup: "随访", intervention: "干预", assess: "评估",
  revisit: "复诊", referral: "转诊", report: "上报", recall: "召回",
  edu: "宣教", screen: "筛查复核",
};
const SPD_REF_STATUS = {
  submitted: ["待卫生院审核", "orange"], station_reviewed: ["待卫生院审核(存量)", "orange"],
  township_reviewed: ["待县级接收", "orange"], accepted: ["已接收", ""],
  arrived: ["已到院", ""], down_referred: ["已下转", ""],
  closed: ["已闭环", "green"], rejected: ["已退回", "red"], withdrawn: ["已撤回", ""],
};

/* 慢专病的状态标签：与三端共用的 `statusTag()`（shared.js）同一份实现，
 * 这里只多一条自己的约定——**状态为空时显示 `—` 而不是空白**。
 * 合并时刻意没有把这条约定推给管理端（管理端历来显示空白），那是改字节不是去重。
 * 等价性由 `scripts/statustag_equiv.js` 证明，含一条前提扫描：
 * 传进来的映射表都不能用假值当键（否则 `key || "—"` 会改掉查表的那个键）。 */
function spdTag(map, key) {
  return statusTag(map, key || "—");
}

function spdCards(items) {
  return `<div class="cards">${items.map(([label, value, warn]) =>
    `<div class="card"><div class="label">${esc(label)}</div>
     <div class="value${warn ? " warn" : ""}">${esc(value ?? 0)}</div></div>`).join("")}</div>`;
}

function spdPairs(obj, names) {
  return Object.entries(obj || {}).map(([k, v]) => [(names || {})[k] || k || "未填", v]);
}

function spdProgramOptions(catalog, blank) {
  return (blank ? '<option value="">全部病种</option>' : "")
    + catalog.programs.map((p) => `<option value="${esc(p.code)}">${esc(p.name)}</option>`).join("");
}

/* ============================================================
 * 共用交互组件（P2-2）：模态表单 + 规则编辑器。
 * build-free 约束不变——纯 DOM，无任何组件库。
 * ==========================================================*/

/* prompt() 的替代：Promise 化的浮层表单，一次拿齐多个字段。
 * fields: [{name, label, type: text|number|textarea|select, options, value, placeholder, required}]
 * 确定 resolve(值对象)；取消 / Esc / 点遮罩 resolve(null)——调用方判 null 直接返回，
 * 与 prompt 返回 null 的习惯一致，改造调用点时不用改控制流。 */
function spdModal(title, fields) {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.style.cssText = "position:fixed;inset:0;background:rgba(15,32,39,.45);"
      + "display:flex;align-items:center;justify-content:center;z-index:1000";
    const control = (f) => {
      const val = f.value != null ? String(f.value) : "";
      if (f.type === "select") {
        return `<select name="${esc(f.name)}">${(f.options || []).map((o) =>
          `<option value="${esc(o.value)}"${String(o.value) === val ? " selected" : ""}>${esc(o.label)}</option>`
        ).join("")}</select>`;
      }
      if (f.type === "textarea") {
        return `<textarea name="${esc(f.name)}" rows="3" style="width:100%"
          placeholder="${esc(f.placeholder || "")}">${esc(val)}</textarea>`;
      }
      return `<input name="${esc(f.name)}" type="${f.type === "number" ? "number" : "text"}"
        value="${esc(val)}" placeholder="${esc(f.placeholder || "")}"${f.required ? " required" : ""}>`;
    };
    overlay.innerHTML = `<form class="panel" style="min-width:320px;max-width:440px;margin:0">
      <h3>${esc(title)}</h3>
      ${fields.map((f) => `<label style="display:block;margin:8px 0;font-size:13px">
        ${esc(f.label)}<br>${control(f)}</label>`).join("")}
      <div style="margin-top:12px;text-align:right">
        <button type="button" class="btn secondary" data-cancel>取消</button>
        <button type="submit" class="btn">确定</button></div></form>`;
    const onKey = (e) => { if (e.key === "Escape") done(null); };
    const done = (value) => {
      overlay.remove(); document.removeEventListener("keydown", onKey); resolve(value);
    };
    document.addEventListener("keydown", onKey);
    overlay.addEventListener("mousedown", (e) => { if (e.target === overlay) done(null); });
    overlay.querySelector("[data-cancel]").onclick = () => done(null);
    overlay.querySelector("form").onsubmit = (e) => {
      e.preventDefault();
      const out = {};
      fields.forEach((f) => {
        const raw = (e.target[f.name].value || "").trim();
        out[f.name] = f.type === "number" ? Number(raw || 0) : raw;
      });
      done(out);
    };
    document.body.appendChild(overlay);
    const first = overlay.querySelector("input,select,textarea");
    if (first) first.focus();
  });
}

/* 规则编辑器：字段与比较符来自 GET /api/spd/meta——前端不自维护字段表，
 * 后端扩了采集项这里自动能选，不会出现"前端能选、后端不认"。
 * 用在四处：病种纳入/排除规则、转诊触发规则、患者分组 auto_rule、问卷异常规则。 */
let SPD_META = null;
async function spdMeta() {
  if (!SPD_META) SPD_META = await api("/api/spd/meta");
  return SPD_META;
}

function spdRuleEditor(el, meta, initial) {
  const rowHtml = (r = {}) => `<div class="spd-rule-row" style="display:flex;gap:6px;margin:4px 0;flex-wrap:wrap">
    <select class="rule-field">${meta.fields.map((f) =>
      `<option value="${esc(f.key)}"${f.key === r.field ? " selected" : ""}>${esc(f.name)}</option>`).join("")}</select>
    <select class="rule-op">${meta.operators.map((o) =>
      `<option value="${esc(o.key)}"${o.key === r.op ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
    <input class="rule-value" style="width:170px" placeholder="值（介于/属于用逗号分隔）"
      value="${esc(Array.isArray(r.value) ? r.value.join(",") : (r.value ?? ""))}">
    <button type="button" class="btn secondary rule-del">删</button></div>`;
  el.innerHTML = `<div class="spd-rule-rows">${(initial || []).map((r) => rowHtml(r)).join("")}</div>
    <button type="button" class="btn secondary rule-add">+ 添加条件</button>`;
  el.addEventListener("click", (e) => {
    if (e.target.classList.contains("rule-add"))
      el.querySelector(".spd-rule-rows").insertAdjacentHTML("beforeend", rowHtml());
    if (e.target.classList.contains("rule-del")) e.target.closest(".spd-rule-row").remove();
  });
  return {
    value: () => [...el.querySelectorAll(".spd-rule-row")].map((row) => {
      const field = row.querySelector(".rule-field").value;
      const op = row.querySelector(".rule-op").value;
      const raw = row.querySelector(".rule-value").value.trim();
      let value;
      if (op === "between") value = raw.split(/[,，]/).map(Number);
      else if (op === "in" || op === "not_in")
        value = raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
      else if (op === "exists") value = raw !== "false" && raw !== "否";
      else value = raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
      return { field, op, value };
    }),
  };
}

/* ============================================================
 * 1. 平台管理端（运行中枢）
 * ==========================================================*/

async function renderSpdAdmin() {
  $("#page-desc").textContent =
    "运行中枢：超期任务提醒、慢病与专病并行运行状态、配置完备度、数据源接入监控";
  const [wb, catalog, sources] = await Promise.all([
    api("/api/spd/workbench/admin"),
    spdCatalog(true),
    api("/api/spd/data-sources"),
  ]);
  const a = wb.alerts, cfg = wb.config_health, ds = wb.data_sources;
  $("#page-body").innerHTML = `
    ${(a.overdue_tasks || a.overdue_followups || a.pending_review_screenings)
      ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 待处理提醒</h3>
         <p style="font-size:13.5px">
           <span class="tag red" style="margin-right:8px">超期任务 ${a.overdue_tasks}</span>
           <span class="tag red" style="margin-right:8px">超期随访 ${a.overdue_followups}</span>
           <span class="tag orange" style="margin-right:8px">待复核筛查 ${a.pending_review_screenings}</span>
           <span class="tag orange" style="margin-right:8px">待受理申请 ${a.pending_applies}</span>
           <span class="tag orange">待确认迁出 ${a.pending_migrations}</span></p>
         <p class="desc" style="font-size:12.5px">本次刷新已扫描并置超期 ${a.swept.overdue} 条、升级 ${a.swept.escalated} 条</p></div>`
      : ""}
    ${spdCards([
      ["在管患者", wb.enrollment.enrolled],
      ["高危患者", wb.enrollment.high_risk, wb.enrollment.high_risk > 0],
      ["本月新增", wb.enrollment.new_this_month],
      ["待办任务", wb.tasks.open],
      ["超期任务", wb.tasks.overdue, wb.tasks.overdue > 0],
      ["慢病病种", wb.parallel_tracks.chronic.programs],
      ["专病病种", wb.parallel_tracks.specialty.programs],
      ["服务团队", cfg.teams],
      ["村医账号", cfg.village_doctors],
    ])}
    <div class="panel"><h3>慢病 / 专病并行运行（共用底座，分别统计）</h3>
      ${table(["业务线", "病种数", "在管人数"], [
        ["慢病管理", wb.parallel_tracks.chronic.programs, wb.parallel_tracks.chronic.enrolled],
        ["专病管理", wb.parallel_tracks.specialty.programs, wb.parallel_tracks.specialty.enrolled],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`)}</div>
    <div class="panel"><h3>配置完备度</h3>
      ${table(["配置项", "数量", "说明"], [
        ["专病档案（启用/全部）", `${cfg.active_programs}/${cfg.programs}`, "病种定义与纳入排除规则"],
        ["已发布路径", cfg.published_paths, `草稿 ${cfg.draft_paths} 个`],
        ["已发布量表", cfg.published_scales, "风险/阶段/康复/筛查"],
        ["未配纳入规则的病种", (cfg.programs_without_rules || []).join("、") || "无",
          "缺规则则无法自动识别患者"],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc(r[2])}</td></tr>`)}</div>
    <div class="panel"><h3>专病档案配置</h3>
      <form class="inline" id="spd-program-form">
        <input name="code" placeholder="病种编码" required>
        <input name="name" placeholder="病种名称" required>
        <select name="category"><option value="chronic">慢病</option><option value="specialty">专病</option></select>
        <input name="lead_dept" placeholder="牵头科室">
        <button>新建病种</button>
      </form>
      <div style="display:flex;gap:24px;flex-wrap:wrap;margin-top:8px">
        <div style="flex:1;min-width:280px"><p class="desc">纳入规则（全部满足才入目标池）</p>
          <div id="spd-include-rules"></div></div>
        <div style="flex:1;min-width:280px"><p class="desc">排除规则（任一满足即排除，优先于纳入）</p>
          <div id="spd-exclude-rules"></div></div>
      </div>
      <p class="msg" id="spd-program-msg"></p>
      ${table(["编码", "名称", "口径", "版本", "阶段数", "纳入规则", "状态", "操作"],
        catalog.programs, (p) =>
        `<tr><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${p.category === "chronic" ? "慢病" : "专病"}</td>
         <td>${esc(p.version || "")}</td><td>${(p.stages || []).length}</td>
         <td>${(cfg.programs_without_rules || []).includes(p.code)
            ? '<span class="tag red">未配置</span>' : '<span class="tag green">已配置</span>'}</td>
         <td>${p.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-pg-view="${p.id}">明细</button>
             <button class="btn secondary" data-pg-edit="${p.id}">改档</button>
             <button class="btn secondary" data-pg-target="${p.id}">控制目标</button>
             <button class="btn secondary" data-pg-ver="${p.id}">版本</button></td></tr>`)}
      <p class="msg" id="spd-pg-msg"></p>
      <div id="spd-pg-detail"></div></div>
    <div class="panel"><h3>数据源接入与运行监控</h3>
      <p class="desc">成功率按最近 100 次同步计算；超过 24 小时未同步计入陈旧</p>
      ${spdCards([["数据源", ds.total], ["异常", ds.failed, ds.failed > 0],
                  ["延迟", ds.delayed, ds.delayed > 0], ["24h未同步", ds.stale_over_24h, ds.stale_over_24h > 0],
                  ["平均成功率", ds.avg_success_rate + "%"]])}
      ${table(["编码", "名称", "类型", "频率(分)", "最近同步", "行数", "延迟(ms)", "成功率", "状态", "操作"],
        sources, (s) =>
        `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td><td>${esc(s.source_type)}</td>
         <td>${s.freq_minutes}</td><td>${esc(s.last_sync_at ? s.last_sync_at.replace("T", " ").slice(0, 16) : "—")}</td>
         <td>${s.last_rows}</td><td>${s.last_latency_ms}</td><td>${s.success_rate}%</td>
         <td>${s.status === "running" ? '<span class="tag green">正常</span>'
            : s.status === "delayed" ? '<span class="tag orange">延迟</span>'
            : '<span class="tag red">异常</span>'}</td>
         <td><button class="btn secondary" data-ds-edit="${s.id}">改配置</button>
             <button class="btn secondary" data-ds-logs="${s.id}">同步日志</button>
             <button class="btn secondary" data-ds-log-add="${s.id}">补记一次</button></td></tr>`)}
      <button class="btn secondary" id="spd-ds-monitor">刷新接入监控</button>
      <p class="msg" id="spd-ds-msg"></p>
      <div id="spd-ds-detail"></div></div>
    <div class="panel"><h3>监测设备</h3>
      <p class="desc">设备按 SN 建档后绑给患者，测量数据才知道该记到谁头上；
        <b>一台设备同时只能绑一个患者</b>——共用设备要先解绑再绑下一个。</p>
      <form class="inline" id="spd-dev-form">
        <input name="sn" placeholder="设备SN" required>
        <select name="device_type">${Object.entries(SPD_DEVICE_TYPES).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <input name="model" placeholder="型号">
        <input name="org_id" type="number" placeholder="归属机构ID（留空=本机构）">
        <button>建档</button>
      </form><p class="msg" id="spd-dev-msg"></p>
      <div id="spd-dev-list"></div></div>`;
  const meta = await spdMeta();
  const includeEditor = spdRuleEditor($("#spd-include-rules"), meta, []);
  const excludeEditor = spdRuleEditor($("#spd-exclude-rules"), meta, []);
  $("#spd-program-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/programs", {
      ...formJson(e.target),
      include_rules: includeEditor.value(),
      exclude_rules: excludeEditor.value(),
    }, "#spd-program-msg");
  };

  const drawDevices = async () => {
    const rows = await api("/api/spd/devices?limit=50");
    $("#spd-dev-list").innerHTML = table(
      ["ID", "SN", "类型", "型号", "归属机构", "绑定患者", "操作"], rows, (d) =>
      `<tr><td>${d.id}</td><td>${esc(d.sn)}</td>
       <td>${esc(SPD_DEVICE_TYPES[d.device_type] || d.device_type)}</td>
       <td>${esc(d.model || "—")}</td><td>${d.org_id ?? "—"}</td>
       <td>${d.patient_id ?? '<span class="tag orange">未绑定</span>'}</td>
       <td><button class="btn secondary" data-dev-bind="${d.id}"
            data-bound="${d.patient_id ? 1 : 0}">${d.patient_id ? "换绑/解绑" : "绑定患者"}</button></td></tr>`);
  };
  await drawDevices();
  $("#spd-dev-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["org_id"]);
    if (!body.org_id) delete body.org_id;
    return postAction("/api/spd/devices", body, "#spd-dev-msg");
  };
  $("#spd-ds-monitor").onclick = async () => {
    try {
      const d = await api("/api/spd/data-sources-monitor");
      const rows = d.items || d.sources || [];
      $("#spd-ds-detail").innerHTML =
        `<p class="desc">接入监控：共 ${rows.length} 个数据源</p>`
        + table(["编码", "状态", "最近同步", "近 24h 成功/总次", "平均延迟(ms)"], rows, (x) =>
          `<tr><td>${esc(x.code || x.source_code || "")}</td><td>${esc(x.status || "—")}</td>
           <td>${esc(String(x.last_sync_at || "").replace("T", " ").slice(0, 16) || "—")}</td>
           <td>${esc(x.success_24h ?? "—")}/${esc(x.total_24h ?? "—")}</td>
           <td>${esc(x.avg_latency_ms ?? "—")}</td></tr>`);
      setMsg("#spd-ds-msg", "", true);
    } catch (err) { setMsg("#spd-ds-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const el5 = (k) => e.target.closest(`[${k}]`);
    const dsEdit = el5("data-ds-edit"), dsLogs = el5("data-ds-logs");
    const dsAdd = el5("data-ds-log-add"), devBind = el5("data-dev-bind");
    const pgView = el5("data-pg-view"), pgEdit = el5("data-pg-edit");
    const pgTarget = el5("data-pg-target"), pgVer = el5("data-pg-ver");
    try {
      if (pgView) {
        const d = await api(`/api/spd/programs/${pgView.dataset.pgView}`);
        $("#spd-pg-detail").innerHTML =
          `<p class="desc">病种 ${esc(d.code)}｜${esc(d.name)} 明细</p>`
          + table(["项", "值"], [
            ["口径", d.category === "chronic" ? "慢病" : "专病"],
            ["版本", d.version || "—"], ["牵头机构", d.lead_org_id ?? "—"],
            ["阶段", (d.stages || []).map((x) => x.name || x).join(" → ") || "—"],
            ["纳入规则", (d.include_rules || []).length + " 条"],
            ["排除规则", (d.exclude_rules || []).length + " 条"],
            ["状态", d.active ? "启用" : "停用"],
          ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`);
        return;
      }
      if (pgEdit) {
        const form = await spdModal("改病种档案", [
          { name: "name", label: "名称" },
          { name: "lead_org_id", label: "牵头机构ID", type: "number" },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }], value: "1" },
        ]);
        if (!form) return;
        const body = { active: form.active === "1" };
        if (form.name) body.name = form.name;
        if (form.lead_org_id) body.lead_org_id = Number(form.lead_org_id);
        return postAction(`/api/spd/programs/${pgEdit.dataset.pgEdit}`,
          body, "#spd-pg-msg", "PATCH");
      }
      if (pgTarget) {
        const pid = pgTarget.dataset.pgTarget;
        const rows = await api(`/api/spd/programs/${pid}/targets`);
        $("#spd-pg-detail").innerHTML =
          `<p class="desc">病种 #${esc(pid)} 的控制目标（分期设定，达标判定按这里的区间）</p>`
          + table(["分期", "指标", "类型", "下限", "上限", "单位", "操作"], rows, (t) =>
            `<tr><td>${esc(t.stage || "通用")}</td>
             <td>${esc(t.metric_name || t.metric)}</td>
             <td>${t.kind === "qualitative" ? "定性" : "定量"}</td>
             <td>${t.target_low ?? "—"}</td><td>${t.target_high ?? "—"}</td>
             <td>${esc(t.unit || "—")}</td>
             <td><button class="btn secondary" data-tg-edit="${t.id}">改</button></td></tr>`)
          + `<form class="inline" id="spd-tg-form" data-pid="${esc(pid)}">
              <input name="stage" placeholder="分期（留空=通用）">
              <input name="metric" placeholder="指标编码" required>
              <input name="metric_name" placeholder="指标名称">
              <select name="kind"><option value="quantitative">定量</option>
                <option value="qualitative">定性</option></select>
              <input name="target_low" type="number" step="any" placeholder="下限">
              <input name="target_high" type="number" step="any" placeholder="上限">
              <input name="unit" placeholder="单位">
              <button>新增目标</button></form>`;
        $("#spd-tg-form").onsubmit = (ev) => {
          ev.preventDefault();
          const body = formJson(ev.target, ["target_low", "target_high"]);
          // 定量目标至少要给一侧区间，否则"达标"没有判据
          if (body.kind === "quantitative"
              && body.target_low == null && body.target_high == null) {
            return setMsg("#spd-pg-msg", "定量目标至少要填上限或下限——否则达标判不出来", false);
          }
          return postAction(`/api/spd/programs/${ev.target.dataset.pid}/targets`,
            body, "#spd-pg-msg");
        };
        return;
      }
      if (pgVer) {
        const rows = await api(`/api/spd/programs/${pgVer.dataset.pgVer}/versions`);
        // 版本是给"这条规则当时长什么样"留证的：改了纳入规则，历史入组依据不该跟着变
        $("#spd-pg-detail").innerHTML =
          `<p class="desc">病种 #${esc(pgVer.dataset.pgVer)} 的版本历史
            —— 改纳入/排除规则会留一版，历史入组的判定依据按当时那一版看</p>`
          + table(["版本", "变更时间", "变更人", "说明"], rows, (v) =>
            `<tr><td>${esc(v.version || "")}</td>
             <td>${esc(String(v.created_at || "").replace("T", " ").slice(0, 16))}</td>
             <td>${esc(v.created_by || "—")}</td><td>${esc(v.note || "—")}</td></tr>`);
        return;
      }
      const tgEdit = el5("data-tg-edit");
      if (tgEdit) {
        const form = await spdModal("改控制目标", [
          { name: "target_low", label: "下限", type: "number" },
          { name: "target_high", label: "上限", type: "number" },
          { name: "unit", label: "单位" },
        ]);
        if (!form) return;
        const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ""));
        ["target_low", "target_high"].forEach((k) => {
          if (body[k] !== undefined) body[k] = Number(body[k]);
        });
        if (!Object.keys(body).length) return setMsg("#spd-pg-msg", "没有要修改的字段", false);
        return postAction(`/api/spd/targets/${tgEdit.dataset.tgEdit}`,
          body, "#spd-pg-msg", "PATCH");
      }
      if (dsEdit) {
        const form = await spdModal("改数据源配置", [
          { name: "name", label: "名称" },
          { name: "endpoint", label: "接入地址" },
          { name: "freq_minutes", label: "同步频率（分钟，1–1440）", type: "number" },
          { name: "scope", label: "同步范围说明" },
        ]);
        if (!form) return;
        // 空串不提交：这些字段都是可选，空串会被当成"要改成空"
        const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ""));
        if (body.freq_minutes) body.freq_minutes = Number(body.freq_minutes);
        if (!Object.keys(body).length) return setMsg("#spd-ds-msg", "没有要修改的字段", false);
        return postAction(`/api/spd/data-sources/${dsEdit.dataset.dsEdit}`,
          body, "#spd-ds-msg", "PATCH");
      }
      if (dsLogs) {
        const rows = await api(`/api/spd/data-sources/${dsLogs.dataset.dsLogs}/sync-logs?limit=30`);
        $("#spd-ds-detail").innerHTML =
          `<p class="desc">数据源 #${esc(dsLogs.dataset.dsLogs)} 的同步日志（近 30 次）</p>`
          + table(["时间", "结果", "行数", "延迟(ms)", "说明"], rows, (l) =>
            `<tr><td>${esc(String(l.created_at || "").replace("T", " ").slice(0, 19))}</td>
             <td>${l.success
                ? '<span class="tag green">成功</span>' : '<span class="tag red">失败</span>'}</td>
             <td>${esc(l.rows ?? 0)}</td><td>${esc(l.latency_ms ?? 0)}</td>
             <td>${esc(l.message || "—")}</td></tr>`);
        return;
      }
      if (dsAdd) {
        // 人工补记：对接方跑完批量同步后回填一次结果，用于把成功率算对
        const form = await spdModal("补记一次同步结果", [
          { name: "success", label: "结果", type: "select",
            options: [{ value: "1", label: "成功" }, { value: "0", label: "失败" }], value: "1" },
          { name: "rows", label: "同步行数", type: "number", value: 0 },
          { name: "latency_ms", label: "耗时（毫秒）", type: "number", value: 0 },
          { name: "message", label: "说明（失败务必写清）" },
        ]);
        if (!form) return;
        if (form.success === "0" && !(form.message || "").trim()) {
          return setMsg("#spd-ds-msg", "补记失败必须写明原因——否则成功率掉了查不出为什么", false);
        }
        return postAction(`/api/spd/data-sources/${dsAdd.dataset.dsLogAdd}/sync-logs`, {
          success: form.success === "1", rows: Number(form.rows || 0),
          latency_ms: Number(form.latency_ms || 0), message: form.message || "",
        }, "#spd-ds-msg");
      }
      if (devBind) {
        const bound = devBind.dataset.bound === "1";
        const pid = prompt(bound
          ? "换绑到哪个患者ID？（留空=解绑）"
          : "绑定到哪个患者ID？", "");
        if (pid === null) return;
        // 一台设备同时只绑一个患者：留空即解绑，后端按 patient_id=null 处理
        const body = pid ? { patient_id: Number(pid) } : { patient_id: null };
        return postAction(`/api/spd/devices/${devBind.dataset.devBind}/bind`,
          body, "#spd-dev-msg");
      }
    } catch (err) { setMsg("#spd-ds-msg", err.message, false); }
  };
}

/* ============================================================
 * 2. 卫健管理端（决策监管中枢）
 * ==========================================================*/

async function renderSpdHealthCommission() {
  $("#page-desc").textContent =
    "决策监管中枢：区域核心指标、患者结构、分级诊疗、路径执行与考核结果";
  const [wb, region] = await Promise.all([
    api("/api/spd/workbench/health-commission"),
    api("/api/spd/stats/region"),
  ]);
  const c = wb.core, names = Object.fromEntries(
    (await spdCatalog()).programs.map((p) => [p.code, p.name]));
  $("#page-body").innerHTML = `
    ${spdCards([
      ["建档居民", c.registered_patients], ["累计筛查", c.screened],
      ["疑似人群", c.suspect], ["目标人群", c.candidates],
      ["纳管在管", c.enrolled], ["自我管理", c.self_managed],
      ["服务人数", c.service_persons], ["服务人次", c.service_times],
      ["筛查转化率", c.screening_conversion_rate + "%"],
      ["转诊闭环率", wb.referrals.closure_rate + "%"],
      ["随访完成率", wb.followups.completion_rate + "%"],
      ["路径完成率", wb.paths.completion_rate + "%"],
    ])}
    <p class="desc">数据更新时间：${esc((c.updated_at || "").replace("T", " ").slice(0, 19))}</p>
    <div class="panel"><h3>县乡村三级服务能力</h3>
      ${table(["层级", "机构数", "在管患者", "服务团队"],
        Object.entries(wb.by_level), ([level, v]) =>
        `<tr><td>${esc(level)}</td><td>${v.orgs}</td><td>${v.enrolled}</td><td>${v.teams}</td></tr>`)}</div>
    <div class="panel"><h3>病种分布（在管）</h3>
      ${barChart(spdPairs(region.by_program, names), { color: "#0b6e6e", unit: " 人" })}</div>
    <div class="panel"><h3>风险分层</h3>
      ${barChart(spdPairs(region.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#b26a00", unit: " 人" })}</div>
    <div class="panel"><h3>年龄结构</h3>
      ${barChart(Object.entries(region.age_distribution), { color: "#0a4d78", unit: " 人" })}</div>
    <div class="panel"><h3>分级诊疗与转诊</h3>
      ${table(["指标", "数值"], [
        ["转诊总量", wb.referrals.total], ["在途", wb.referrals.open],
        ["已闭环", wb.referrals.closed], ["闭环率", wb.referrals.closure_rate + "%"],
        ["有效上转就诊", wb.referrals.effective_visits],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`)}</div>
    <div class="panel"><h3>重点慢专病中心运行</h3>
      ${table(["编码", "名称", "病种", "适用机构", "团队", "状态"], wb.centers, (x) =>
        `<tr><td>${esc(x.code)}</td><td>${esc(x.name)}</td>
         <td>${esc(names[x.program_code] || x.program_code)}</td>
         <td>${x.orgs}</td><td>${x.teams}</td>
         <td>${x.status === "running" ? '<span class="tag green">运行中</span>'
            : '<span class="tag orange">' + esc(x.status) + "</span>"}</td></tr>`)}</div>
    <div class="panel"><h3>考核结果排名</h3>
      ${table(["排名", "考核对象", "周期", "综合得分"], wb.scores, (s) =>
        `<tr><td>${s.rank}</td><td>${esc(s.object_name)}</td><td>${esc(s.period)}</td>
         <td>${s.total_score}</td></tr>`)}</div>`;
}

/* ============================================================
 * 3. 专病专家端（临床指导中枢）
 * ==========================================================*/

async function renderSpdExpert() {
  $("#page-desc").textContent =
    "临床指导中枢：病种标准配置完备度、路径与量表覆盖、分中心运行、区域执行大屏";
  const wb = await api("/api/spd/workbench/expert");
  $("#page-body").innerHTML = `
    ${spdCards([
      ["管理病种", wb.programs.length], ["在管患者", wb.enrollment.enrolled],
      ["覆盖机构", wb.org_coverage], ["路径实例", wb.paths.total],
      ["路径完成率", wb.paths.completion_rate + "%"],
      ["评估人次", wb.assessments.total],
      ["转诊闭环率", wb.referrals.closure_rate + "%"],
    ])}
    <div class="panel"><h3>病种标准落地情况</h3>
      <p class="desc">纳入规则、管理阶段、路径模板、量表——任一缺失都会让基层"没有可执行的规则"</p>
      ${table(["病种", "口径", "版本", "纳入规则", "阶段", "路径模板", "已发布", "量表", "在管"],
        wb.programs, (p) =>
        `<tr><td>${esc(p.program_name)}</td>
         <td>${p.category === "chronic" ? "慢病" : "专病"}</td><td>${esc(p.version)}</td>
         <td>${p.has_include_rules ? '<span class="tag green">已配</span>' : '<span class="tag red">缺</span>'}</td>
         <td>${p.stages}</td><td>${p.path_templates}</td>
         <td>${p.published_paths ? `<span class="tag green">${p.published_paths}</span>`
            : '<span class="tag red">0</span>'}</td>
         <td>${p.scales}</td><td>${p.enrolled}</td></tr>`)}</div>
    <div class="panel"><h3>重点慢专病中心</h3>
      ${table(["名称", "病种", "牵头科室", "版本", "状态"], wb.centers, (c) =>
        `<tr><td>${esc(c.name)}</td><td>${esc(c.program_code)}</td><td>${esc(c.lead_dept)}</td>
         <td>${esc(c.version)}</td><td>${esc(c.status)}</td></tr>`)}
      <form class="inline" id="spd-center-form" style="margin-top:10px">
        <input name="code" placeholder="中心编码" required>
        <input name="name" placeholder="中心名称" required>
        <input name="program_code" placeholder="病种编码" required>
        <input name="lead_dept" placeholder="牵头科室">
        <button>新建分中心</button>
      </form><p class="msg" id="spd-center-msg"></p></div>
    <div class="panel"><h3>风险评估结果分布</h3>
      ${barChart(spdPairs(wb.assessments.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#8d4bab", unit: " 人次" })}</div>`;
  $("#spd-center-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/centers", formJson(e.target), "#spd-center-msg");
  };
}

/* ============================================================
 * 4. 全程管理中心端（统筹调度中枢）
 * ==========================================================*/

async function renderSpdCenter() {
  $("#page-desc").textContent =
    "统筹调度中枢：统一待办、目标池分发与认领、在途转诊、生命周期确认、上报任务配置";
  const [wb, candidates, catalog, reportTasks] = await Promise.all([
    api("/api/spd/workbench/center"),
    api("/api/spd/candidates?status=target&limit=50"),
    spdCatalog(),
    api("/api/spd/case-report-tasks"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["我的待办", wb.todo.mine.open], ["全部待办", wb.todo.all.open],
      ["无人认领", wb.todo.unassigned, wb.todo.unassigned > 0],
      ["超期", wb.todo.all.overdue, wb.todo.all.overdue > 0],
      ["已升级", wb.todo.all.escalated, wb.todo.all.escalated > 0],
      ["疑似人群", wb.pool.suspect], ["目标人群", wb.pool.target],
      ["待分发", wb.pool.unassigned, wb.pool.unassigned > 0],
      ["待复核筛查", wb.pool.pending_review, wb.pool.pending_review > 0],
      ["在管患者", wb.enrollment.enrolled], ["本月新增", wb.monthly.new_enrollments],
      ["在途转诊", wb.referrals.open],
    ])}
    <div class="panel"><h3>待办按类型</h3>
      ${barChart(spdPairs(wb.todo.all.by_type, SPD_TASK_TYPES), { unit: " 条" })}</div>
    <div class="panel"><h3>目标池分发</h3>
      <p class="desc">按辖区、病种、风险把目标人群分给服务团队；已被认领的患者不会被覆盖</p>
      <form class="inline" id="spd-dist-form">
        <input name="candidate_ids" placeholder="目标池ID，逗号分隔" required style="min-width:220px">
        <select name="team_id"><option value="">选择团队</option>
          ${catalog.teams.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <input name="assigned_user_id" type="number" placeholder="责任人用户ID">
        <button>分发</button>
      </form><p class="msg" id="spd-dist-msg"></p>
      ${table(["ID", "患者", "病种", "风险", "机构", "团队", "责任人", "纳入依据"],
        candidates, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name || c.patient_id)}</td>
         <td>${esc(c.program_code)}</td><td>${spdTag(SPD_RISK, c.risk_level)}</td>
         <td>${c.org_id ?? "—"}</td><td>${c.team_id ?? "—"}</td>
         <td>${c.assigned_user_id ?? "—"}</td><td>${esc(c.reason || "—")}</td></tr>`)}</div>
    <div class="panel"><h3>生命周期</h3>
      ${table(["状态", "人数"], [
        ["已排除", wb.lifecycle.excluded], ["已迁出", wb.lifecycle.migrated],
        ["已死亡", wb.lifecycle.dead], ["召回中", wb.lifecycle.recalling],
        ["待确认迁入", wb.lifecycle.pending_migrations],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${r[1]}</td></tr>`)}</div>
    <div class="panel"><h3>转诊在途</h3>
      ${barChart(spdPairs(wb.referrals.by_status,
        Object.fromEntries(Object.entries(SPD_REF_STATUS).map(([k, v]) => [k, v[0]]))),
        { color: "#0a4d78", unit: " 单" })}</div>
    <div class="panel"><h3>上报任务配置（中心端 #11）</h3>
      <p class="desc">配置病种、管理科室与负责人；停用的任务不再出现在成员端上报下拉里</p>
      <form class="inline" id="spd-crt-form">
        <input name="code" placeholder="任务编码" required>
        <input name="name" placeholder="任务名称" required>
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <input name="dept" placeholder="管理科室" style="width:120px">
        <input name="manager_user_id" type="number" placeholder="负责人用户ID" style="width:130px">
        <button>新建任务</button>
      </form><p class="msg" id="spd-crt-msg"></p>
      ${table(["ID", "编码", "名称", "病种", "科室", "负责人", "状态", "操作"], reportTasks, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.code)}</td><td>${esc(t.name)}</td>
         <td>${esc(t.program_code || "—")}</td><td>${esc(t.dept || "—")}</td>
         <td>${t.manager_user_id ?? "—"}</td>
         <td>${t.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-crt="${t.id}" data-active="${t.active ? 0 : 1}">
           ${t.active ? "停用" : "启用"}</button></td></tr>`)}</div>`;
  $("#spd-dist-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["team_id", "assigned_user_id"]);
    body.candidate_ids = String(body.candidate_ids || "").split(/[，,\s]+/)
      .filter(Boolean).map(Number);
    return postAction("/api/spd/candidates/distribute", body, "#spd-dist-msg");
  };
  $("#spd-crt-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/case-report-tasks",
      formJson(e.target, ["manager_user_id"]), "#spd-crt-msg");
  };
  $("#page-body").addEventListener("click", (e) => {
    const toggle = e.target.closest("[data-crt]");
    if (toggle) {
      return postAction(`/api/spd/case-report-tasks/${toggle.dataset.crt}`,
        { active: toggle.dataset.active === "1" }, "#spd-crt-msg", "PATCH");
    }
  });
}

/* ============================================================
 * 5. 服务团队端（专家 / 成员 / 个案管理师）
 * ==========================================================*/

async function renderSpdTeam() {
  $("#page-desc").textContent =
    "基层执行：团队专家、团队成员、个案管理师三个视角共用同一批数据，切换角色查看";
  const role = localStorage.getItem("spd_team_role") || "member";
  const wb = await api(`/api/spd/workbench/team?role=${role}`);
  const roleNames = { expert: "团队专家端", member: "团队成员端", case_manager: "个案管理师端" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>视角切换</h3>
      <p>${Object.entries(roleNames).map(([k, v]) =>
        `<button class="btn ${k === role ? "" : "secondary"}" data-role="${k}" style="margin-right:8px">${esc(v)}</button>`).join("")}</p>
      <p class="desc">当前：${esc(roleNames[role])}。三个端的数字同源，只是聚合口径不同。</p></div>
    ${spdCards([
      ["在管患者", wb.patients.managed], ["本月新增", wb.patients.new_this_month],
      ["高危患者", wb.patients.high_risk, wb.patients.high_risk > 0],
      ["我的待办", wb.tasks.open], ["今日到期", wb.tasks.due_today],
      ["超期", wb.tasks.overdue, wb.tasks.overdue > 0],
      ["待评估", wb.plans.pending_assess], ["待定目标", wb.plans.pending_target],
      ["待建路径", wb.plans.pending_path], ["到期随访", wb.plans.due_followups],
      ["到期复诊", wb.plans.due_revisits],
    ])}
    ${(wb.alerts.abnormal_measure || wb.alerts.referrals || wb.alerts.recall || wb.alerts.dead)
      ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 预警</h3>
         <p style="font-size:13.5px">
           <span class="tag red" style="margin-right:8px">指标异常 ${wb.alerts.abnormal_measure}</span>
           <span class="tag orange" style="margin-right:8px">在途转诊 ${wb.alerts.referrals}</span>
           <span class="tag orange" style="margin-right:8px">召回中 ${wb.alerts.recall}</span>
           <span class="tag">死亡登记 ${wb.alerts.dead}</span></p></div>`
      : ""}
    <div class="panel"><h3>所属团队</h3>
      ${table(["ID", "团队", "层级", "机构", "服务病种"], wb.teams, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(t.level)}</td>
         <td>${t.org_id}</td><td>${esc((t.program_codes || []).join("、") || "—")}</td></tr>`)}</div>
    <div class="panel"><h3>患者风险分层</h3>
      ${barChart(spdPairs(wb.patients.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#b26a00", unit: " 人" })}</div>
    ${wb.packages ? `<div class="panel"><h3>服务包执行</h3>
      ${table(["已绑服务包", "项目总次数", "已消耗", "消费率"], [wb.packages], (p) =>
        `<tr><td>${p.bound}</td><td>${p.total_items}</td><td>${p.used_items}</td>
         <td>${p.usage_rate}%</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>待办按类型</h3>
      ${barChart(spdPairs(wb.tasks.by_type, SPD_TASK_TYPES), { unit: " 条" })}</div>`;
  $("#page-body").onclick = (e) => {
    const btn = e.target.closest("[data-role]");
    if (!btn) return;
    localStorage.setItem("spd_team_role", btn.dataset.role);
    route();
  };
}

/* ============================================================
 * 5B. 服务团队与村医档案配置（配置域 `spd/config/teams.py` 的 12 个端点）
 *
 * 补的是"后端交付了、界面没人建"的缺口：在这一页之前，服务团队与村医档案
 * 只在运行中枢上露两个计数（`cfg.teams` / `cfg.village_doctors`），建团队、
 * 配成员权限、开通村医、打印绑定二维码一律无门可入——而团队是基层执行链的
 * 起点，团队建不出来，任务分派、随访、转诊在真环境里一条都跑不起来。
 *
 * 权限口径照后端：写接口收在 CONFIG_ROLES（director/doctor，admin 自动通过），
 * 读接口任何登录用户都能看，所以这一页**不设 roles**——非授权角色点"新建"
 * 收到的是后端的 403 文案，而不是"这个功能不存在"。
 * ==========================================================*/

const SPD_TEAM_LEVELS = { county: "县级", township: "乡级", village: "村级", center: "中心" };
const SPD_MEMBER_ROLES = {
  doctor: "医生", nurse: "护士", rehab: "康复师", case_manager: "个案管理师",
  village_doctor: "村医", expert: "专病专家",
};
const SPD_PATIENT_SCOPES = { self: "仅本人", team: "本团队", org: "本机构", region: "全域" };
const SPD_DATA_SCOPES = { org: "本机构", group: "医共体分组", region: "全域" };

/* 逗号分隔的病种编码 → 数组。中英文逗号都收：配置是人手敲的，
 * 输入法没切回来不该变成一条查不出原因的 422。 */
function spdCodeList(raw) {
  return String(raw || "").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
}

function spdOptions(map) {
  // 两种词表都吃：扁平的 {code: "文案"}，以及带配色的 {code: ["文案", "tag类名"]}
  // （后者供 spdTag 用）。不分辨的话，下拉里会出现「疑似,orange」这种东西。
  return Object.entries(map).map(([value, label]) => ({
    value, label: Array.isArray(label) ? label[0] : label,
  }));
}

/* ============================================================
 * 慢专病配置域：量表 / 宣教材料 / 服务包 / 患者标签（P1-40）
 *
 * 这四类都是**运营配置**：后端十五个端点齐全，界面此前只接通了四个——
 * 量表建不出来、发布不了、二维码取不到，宣教材料改不了，服务包与标签
 * 整块没有入口。与 2026-08-27 的 spd/care、本轮的服务团队配置同一形状：
 * 后端交付了、界面缺失，而需求对照表把它们算作已实现。
 * ==========================================================*/

const SPD_DEVICE_TYPES = {
  bp: "血压计", glucose: "血糖仪", band: "手环", scale: "体重秤",
  poct: "POCT", ecg: "心电",
};

const SPD_QC_RESULTS = {
  pass: ["通过", "green"], warn: ["存疑", "orange"], fail: ["不通过", "red"],
};
const SPD_CALL_RESULTS = {
  connected: "已接通", failed: "未接通", cancelled: "已取消",
};

const SPD_POINT_EVENTS = {
  sign: "签约", referral_up: "上转", referral_down: "下转",
  followup: "随访", abnormal_report: "异常上报", signin: "每日签到",
};

const SPD_CAND_STATUS = {
  suspect: ["疑似", "orange"], target: ["目标", "green"], excluded: ["已排除", ""],
};
const SPD_RECALL_STATUS = {
  pending: ["待联系", "orange"], contacted: ["已联系", ""],
  returned: ["已回归", "green"], failed: ["召回失败", "red"],
};
const SPD_APPLY_STATUS = {
  pending: ["待受理", "orange"], accepted: ["已受理", "green"], rejected: ["已拒绝", "red"],
};

const SPD_SCALE_CATEGORIES = { risk: "风险评估", stage: "分期评定", rehab: "康复评定", screen: "筛查" };
const SPD_SCALE_STATUS = { draft: ["草稿", "orange"], published: ["已发布", "green"], disabled: ["已停用", "red"] };
const SPD_MEDIA_TYPES = { text: "图文", audio: "音频", video: "视频" };

async function renderSpdContentConfig() {
  $("#page-desc").textContent =
    "慢专病配置：评估量表（建→发布→二维码）、宣教材料、服务包、患者标签";
  const [catalog, scales, edus, packages, tags] = await Promise.all([
    spdCatalog(),
    api("/api/spd/scales?limit=100"),
    api("/api/spd/edu-materials?limit=100"),
    api("/api/spd/service-packages?limit=100"),
    api("/api/spd/tags"),
  ]);
  const programNames = Object.fromEntries(catalog.programs.map((p) => [p.code, p.name]));
  const progText = (code) => (code ? (programNames[code] || code) : "通用");

  $("#page-body").innerHTML =
    panel("评估量表", `
      <p class="desc">量表要<b>先发布</b>才能被评估引用，发布时生成二维码令牌；
        没有题目的量表发布会被挡回（422）。停用不删行——历史评估记录仍要查得到用的是哪一版。</p>
      <form class="inline" id="spd-scale-form">
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="量表名称" required>
        <select name="category">${Object.entries(SPD_SCALE_CATEGORIES).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <input name="version" placeholder="版本（默认 v1）">
        <button>新建量表</button>
      </form>
      <p class="desc">题目与计分区间在建好后用「改题目」维护（JSON：items / scoring）。</p>
      <p class="msg" id="spd-scale-msg"></p>
      <form class="inline" id="spd-scale-filter">
        <select name="category"><option value="">全部分类</option>${Object.entries(SPD_SCALE_CATEGORIES).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <select name="status"><option value="">全部状态</option>${Object.entries(SPD_SCALE_STATUS).map(([v, t]) =>
          `<option value="${v}">${esc(t[0])}</option>`).join("")}</select>
        <button class="btn secondary">筛选</button>
      </form>
      ${table(["ID", "编码", "名称", "分类", "病种", "版本", "题数", "状态", "操作"], scales, (x) => {
        const st = SPD_SCALE_STATUS[x.status] || [x.status, ""];
        return `<tr><td>${x.id}</td><td>${esc(x.code)}</td><td>${esc(x.name)}</td>
         <td>${esc(SPD_SCALE_CATEGORIES[x.category] || x.category)}</td>
         <td>${esc(progText(x.program_code))}</td><td>${esc(x.version)}</td>
         <td>${(x.items || []).length}</td>
         <td><span class="tag ${st[1]}">${esc(st[0])}</span></td>
         <td><button class="btn secondary" data-scale-edit="${x.id}">改题目</button>
         ${x.status === "published"
            ? `<button class="btn secondary" data-scale-qr="${x.id}">二维码</button>
               <button class="btn danger" data-scale-off="${x.id}">停用</button>`
            : `<button class="btn" data-scale-pub="${x.id}">发布</button>`}</td></tr>`;
      })}
      <div id="spd-scale-qr"></div>`)
    + panel("宣教材料", `
      <form class="inline" id="spd-edu-form">
        <input name="code" placeholder="编码" required>
        <input name="title" placeholder="标题" required>
        <select name="media_type">${Object.entries(SPD_MEDIA_TYPES).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <input name="dept" placeholder="归口科室">
        <input name="media_url" placeholder="音视频地址（图文留空）">
        <button>新建材料</button>
      </form>
      <p class="msg" id="spd-edu-msg"></p>
      ${table(["ID", "编码", "标题", "形式", "病种", "科室", "操作"], edus, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.code)}</td><td>${esc(x.title)}</td>
         <td>${esc(SPD_MEDIA_TYPES[x.media_type] || x.media_type)}</td>
         <td>${esc(progText(x.program_code))}</td><td>${esc(x.dept) || "—"}</td>
         <td><button class="btn secondary" data-edu-edit="${x.id}">改内容</button></td></tr>`)}`)
    + panel("服务包", `
      <p class="desc">服务包是签约与计费的载体：`+"`items`"+`列出包内项目与次数（JSON），
        价格用两位小数，周期按天。</p>
      <form class="inline" id="spd-pkg-form">
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="包名" required>
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <input name="price" type="number" step="0.01" min="0" placeholder="价格(元)">
        <input name="period_days" type="number" min="1" max="3650" placeholder="周期(天，默认365)">
        <button>新建服务包</button>
      </form>
      <p class="msg" id="spd-pkg-msg"></p>
      ${table(["ID", "编码", "包名", "病种", "价格", "周期(天)", "项目数", "操作"], packages, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.code)}</td><td>${esc(x.name)}</td>
         <td>${esc(progText(x.program_code))}</td><td>${esc(x.price)}</td>
         <td>${esc(x.period_days)}</td><td>${(x.items || []).length}</td>
         <td><button class="btn secondary" data-pkg-edit="${x.id}">改配置</button></td></tr>`)}`)
    + panel("患者标签", `
      <form class="inline" id="spd-tag-form">
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="标签名" required>
        <input name="category" placeholder="分类（默认 patient）">
        <input name="color" placeholder="颜色（如 #f00）">
        <button>新建标签</button>
      </form>
      <p class="msg" id="spd-tag-msg"></p>
      ${table(["ID", "编码", "名称", "分类", "颜色"], tags, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.code)}</td><td>${esc(x.name)}</td>
         <td>${esc(x.category)}</td><td>${esc(x.color) || "—"}</td></tr>`)}`);

  $("#spd-scale-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target);
    if (!body.version) delete body.version;
    return postAction("/api/spd/scales", body, "#spd-scale-msg");
  };
  $("#spd-scale-filter").onsubmit = async (e) => {
    e.preventDefault();
    const q = Object.entries(formJson(e.target)).filter(([, v]) => v)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    const rows = await api(`/api/spd/scales?limit=100${q ? "&" + q : ""}`);
    setMsg("#spd-scale-msg", `筛出 ${rows.length} 份量表（刷新页面回到全部）`, true);
  };
  $("#spd-edu-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/edu-materials", formJson(e.target), "#spd-edu-msg");
  };
  $("#spd-pkg-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["price", "period_days"]);
    if (!body.period_days) delete body.period_days;
    if (!body.price) delete body.price;
    return postAction("/api/spd/service-packages", body, "#spd-pkg-msg");
  };
  $("#spd-tag-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target);
    if (!body.category) delete body.category;
    return postAction("/api/spd/tags", body, "#spd-tag-msg");
  };

  $("#page-body").onclick = async (e) => {
    const el = (k) => e.target.closest(`[${k}]`);
    const pub = el("data-scale-pub"), off = el("data-scale-off"), qr = el("data-scale-qr");
    const sEdit = el("data-scale-edit"), eEdit = el("data-edu-edit"), pEdit = el("data-pkg-edit");
    try {
      if (pub) {
        return postAction(`/api/spd/scales/${pub.dataset.scalePub}/publish`, null, "#spd-scale-msg");
      }
      if (off) {
        if (!confirm("停用后该量表不再可被新评估引用（历史记录不受影响），确定停用？")) return;
        return postAction(`/api/spd/scales/${off.dataset.scaleOff}/disable`, null, "#spd-scale-msg");
      }
      if (qr) {
        // 照抄村医绑定码的既有做法：qr.svg 直接挂 <img src>，不另造取文本的助手。
        // 未发布的量表取码是 409，所以这个按钮只在已发布行上出现。
        const id = qr.dataset.scaleQr;
        $("#spd-scale-qr").innerHTML =
          `<p class="desc">量表 #${esc(id)} 的评估二维码：扫码直达居民端自查页并预选该量表。</p>
           <img src="/api/spd/scales/${esc(id)}/qr.svg" alt="量表评估二维码" width="200" height="200">`;
        return;
      }
      if (sEdit) {
        const cur = scales.find((x) => String(x.id) === sEdit.dataset.scaleEdit) || {};
        const form = await spdModal("改量表题目与计分", [
          { name: "items", label: "题目 items（JSON 数组）", type: "textarea",
            value: JSON.stringify(cur.items || [], null, 0) },
          { name: "scoring", label: "计分 scoring（JSON 对象）", type: "textarea",
            value: JSON.stringify(cur.scoring || {}, null, 0) },
        ]);
        if (!form) return;
        let body;
        try {
          body = { items: JSON.parse(form.items || "[]"), scoring: JSON.parse(form.scoring || "{}") };
        } catch (err) { return setMsg("#spd-scale-msg", `JSON 格式有误：${err.message}`, false); }
        return postAction(`/api/spd/scales/${sEdit.dataset.scaleEdit}`, body, "#spd-scale-msg", "PATCH");
      }
      if (eEdit) {
        const cur = edus.find((x) => String(x.id) === eEdit.dataset.eduEdit) || {};
        const form = await spdModal("改宣教材料", [
          { name: "title", label: "标题", value: cur.title || "" },
          { name: "content", label: "图文内容", type: "textarea", value: cur.content || "" },
          { name: "media_url", label: "音视频地址", value: cur.media_url || "" },
          { name: "dept", label: "归口科室", value: cur.dept || "" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/edu-materials/${eEdit.dataset.eduEdit}`, form, "#spd-edu-msg", "PATCH");
      }
      if (pEdit) {
        const cur = packages.find((x) => String(x.id) === pEdit.dataset.pkgEdit) || {};
        const form = await spdModal("改服务包", [
          { name: "name", label: "包名", value: cur.name || "" },
          { name: "price", label: "价格(元)", type: "number", value: cur.price ?? 0 },
          { name: "period_days", label: "周期(天)", type: "number", value: cur.period_days ?? 365 },
          { name: "items", label: "包内项目 items（JSON 数组）", type: "textarea",
            value: JSON.stringify(cur.items || [], null, 0) },
        ]);
        if (!form) return;
        let items;
        try { items = JSON.parse(form.items || "[]"); }
        catch (err) { return setMsg("#spd-pkg-msg", `JSON 格式有误：${err.message}`, false); }
        return postAction(`/api/spd/service-packages/${pEdit.dataset.pkgEdit}`,
          { name: form.name, price: Number(form.price), period_days: Number(form.period_days), items },
          "#spd-pkg-msg", "PATCH");
      }
    } catch (err) { setMsg("#spd-scale-msg", err.message, false); }
  };
}

async function renderSpdTeamConfig() {
  $("#page-desc").textContent =
    "服务团队与村医档案：建团队、配成员与权限范围、开通村医账号并出绑定二维码";
  const [catalog, orgs, teams] = await Promise.all([
    spdCatalog(),
    api("/api/organizations"),
    // include_inactive：停用的团队也要看得见，否则点一次「停用」它就从界面上消失，
    // 再没有任何入口能把它启用回来（P1-41）。
    api("/api/spd/teams?limit=100&include_inactive=true"),
  ]);
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const orgOptions = orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("");
  const programNames = Object.fromEntries(catalog.programs.map((p) => [p.code, p.name]));
  const programText = (codes) =>
    (codes || []).map((c) => programNames[c] || c).join("、") || "—";
  let members = [];
  let vds = [];

  // ADR-0009 第二步：面板外壳直接用 `panel()` 组件（定义见 core.js）——新页不再手写。
  $("#page-body").innerHTML = spdCards([
    ["服务团队", teams.length],
    ["团队成员合计", teams.reduce((n, t) => n + (t.member_count || 0), 0)],
    ["未配病种的团队", teams.filter((t) => !(t.program_codes || []).length).length,
      teams.some((t) => !(t.program_codes || []).length)],
  ])
    + panel("服务团队", `
      <p class="desc">团队是任务分派与数据可见范围的载体：成员的"患者范围"按团队算，
        没有团队就没有分派对象。列表含停用项，停用的可以再启用。</p>
      <form class="inline" id="spd-team-form">
        <input name="name" placeholder="团队名称" required>
        <select name="org_id">${orgOptions}</select>
        <select name="level">${Object.entries(SPD_TEAM_LEVELS).map(([v, t]) =>
          `<option value="${v}"${v === "township" ? " selected" : ""}>${esc(t)}</option>`).join("")}</select>
        <select name="data_scope">${Object.entries(SPD_DATA_SCOPES).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <input name="dept" placeholder="牵头科室">
        <input name="service_area" placeholder="服务区域（如：城关镇 3 个村）">
        <select name="program_codes" multiple size="3">${spdProgramOptions(catalog, false)}</select>
        <button>新建团队</button>
      </form>
      <p class="desc">按住 Ctrl/⌘ 可多选服务病种；不选表示暂不限定病种</p>
      <p class="msg" id="spd-team-msg"></p>
      ${table(["ID", "团队", "层级", "机构", "服务病种", "组长", "成员", "数据范围", "状态", "操作"],
        teams, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.name)}</td>
         <td>${esc(SPD_TEAM_LEVELS[t.level] || t.level)}</td>
         <td>${esc(orgNames[t.org_id] || t.org_id)}</td>
         <td>${esc(programText(t.program_codes))}</td>
         <td>${t.leader_user_id ?? "—"}</td><td>${t.member_count ?? 0}</td>
         <td>${esc(SPD_DATA_SCOPES[t.data_scope] || t.data_scope)}</td>
         <td>${t.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-team-view="${t.id}">成员</button>
             <button class="btn secondary" data-team-add="${t.id}">加成员</button>
             <button class="btn secondary" data-team-edit="${t.id}">改配置</button>
             ${t.active
               ? `<button class="btn secondary" data-team-off="${t.id}">停用</button>`
               : `<button class="btn secondary" data-team-on="${t.id}">启用</button>`}</td></tr>`)}`)
    + panel("团队成员与权限", '<div id="spd-team-members"></div>')
    + panel("村医档案", `
      <p class="desc">村医先有平台账号（用户管理里开通），这里补建村医档案：
        绑定令牌随档案生成，扫"绑定码"进医生移动端即完成绑定；停用即收回入口，码也不再出。</p>
      <form class="inline" id="spd-vd-form">
        <input name="user_id" type="number" placeholder="用户ID" required>
        <select name="org_id">${orgOptions}</select>
        <input name="township" placeholder="乡镇">
        <input name="village" placeholder="村">
        <input name="license_no" placeholder="执业证号">
        <input name="license_valid_to" placeholder="证照有效期 YYYY-MM-DD">
        <input name="phone" placeholder="联系电话">
        <button>建村医档案</button>
      </form>
      <form class="inline" id="spd-vd-filter">
        <select name="org_id"><option value="">全部机构</option>${orgOptions}</select>
        <input name="township" placeholder="按乡镇筛选">
        <input name="village" placeholder="按村筛选">
        <button class="secondary">查询</button>
      </form>
      <p class="msg" id="spd-vd-msg"></p>
      <div id="spd-vd-list"></div>
      <div id="spd-vd-qr"></div>`)
    + panel("批量开通村医", `
      <p class="desc">每行一个：用户ID,乡镇,村（乡镇与村可省略）。重复的逐行跳过并回报原因，
        不会因为一行重复整批回滚。</p>
      <form id="spd-vd-batch-form">
        <select name="org_id">${orgOptions}</select>
        <textarea name="items" rows="4" style="width:100%;margin-top:8px"
          placeholder="1001,城关镇,东村&#10;1002,城关镇,西村"></textarea>
        <button class="btn" style="margin-top:8px">批量开通</button>
      </form>`);

  const drawMembers = async (teamId) => {
    const detail = await api(`/api/spd/teams/${teamId}`);
    members = detail.members || [];
    $("#spd-team-members").innerHTML = `
      <p class="desc">团队 ${esc(detail.name)}（#${detail.id}）共 ${detail.member_count ?? 0} 人。
        权限决定成员在服务端能做什么，患者范围决定他能看见谁。</p>
      ${table(["ID", "成员", "角色", "负责阶段", "患者范围", "随访", "转诊", "审核", "评估", "状态", "操作"],
        members, (m) =>
        `<tr><td>${m.id}</td><td>${esc(m.user_name || m.user_id)}</td>
         <td>${esc(SPD_MEMBER_ROLES[m.member_role] || m.member_role)}</td>
         <td>${esc(m.stage_scope || "—")}</td>
         <td>${esc(SPD_PATIENT_SCOPES[m.patient_scope] || m.patient_scope)}</td>
         <td>${m.can_followup ? "✓" : "—"}</td><td>${m.can_referral ? "✓" : "—"}</td>
         <td>${m.can_audit ? "✓" : "—"}</td><td>${m.can_assess ? "✓" : "—"}</td>
         <td>${m.active ? '<span class="tag green">在岗</span>' : '<span class="tag">已停</span>'}</td>
         <td><button class="btn secondary" data-mem-edit="${m.id}">改权限</button>
             <button class="btn secondary" data-mem-del="${m.id}">移除</button></td></tr>`)}`;
  };
  const drawVds = async (query) => {
    const qs = new URLSearchParams({ limit: "100", ...(query || {}) }).toString();
    vds = await api(`/api/spd/village-doctors?${qs}`);
    $("#spd-vd-list").innerHTML = table(
      ["ID", "村医", "机构", "乡镇", "村", "执业证号", "证照有效期", "电话", "状态", "操作"],
      vds, (v) =>
      `<tr><td>${v.id}</td><td>${esc(v.user_name || v.user_id)}</td>
       <td>${esc(orgNames[v.org_id] || v.org_id)}</td>
       <td>${esc(v.township || "—")}</td><td>${esc(v.village || "—")}</td>
       <td>${esc(v.license_no || "—")}</td><td>${esc(v.license_valid_to || "—")}</td>
       <td>${esc(v.phone || "—")}</td>
       <td>${v.active ? '<span class="tag green">在用</span>' : '<span class="tag">停用</span>'}</td>
       <td><button class="btn secondary" data-vd-edit="${v.id}">改档</button>
           <button class="btn secondary" data-vd-off="${v.id}" data-vd-active="${v.active ? 1 : 0}">
             ${v.active ? "停用" : "启用"}</button>
           <button class="btn secondary" data-vd-qr="${v.id}">绑定码</button></td></tr>`);
  };
  await Promise.all([teams.length ? drawMembers(teams[0].id) : null, drawVds()]);

  $("#spd-team-form").onsubmit = (e) => {
    e.preventDefault();
    const picked = [...(e.target.program_codes.selectedOptions || [])].map((o) => o.value);
    return postAction("/api/spd/teams", {
      ...formJson(e.target, ["org_id"]), program_codes: picked,
    }, "#spd-team-msg");
  };
  $("#spd-vd-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/village-doctors",
      formJson(e.target, ["user_id", "org_id"]), "#spd-vd-msg");
  };
  $("#spd-vd-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawVds(formJson(e.target)); }
    catch (err) { setMsg("#spd-vd-msg", err.message, false); }
  };
  $("#spd-vd-batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const orgId = Number(e.target.org_id.value);
    const items = String(e.target.items.value || "").split("\n")
      .map((line) => line.split(/[,，\t]/).map((cell) => cell.trim()))
      .filter((cells) => cells[0])
      .map((cells) => ({
        user_id: Number(cells[0]), org_id: orgId,
        township: cells[1] || "", village: cells[2] || "",
      }));
    if (!items.length) return setMsg("#spd-vd-msg", "请至少填一行用户ID", false);
    try {
      // 这一条不走 postAction：逐行结果（谁没建成、为什么）是这个接口的全部价值，
      // route() 重画会把它冲掉，导入方又只剩"失败了"三个字。
      const out = await api("/api/spd/village-doctors/batch", {
        method: "POST", body: JSON.stringify({ items }),
      });
      const detail = out.skipped.map((s) => `${s.user_id}（${s.reason}）`).join("、");
      setMsg("#spd-vd-msg",
        `已开通 ${out.created} 个，跳过 ${out.skipped.length} 个${detail ? "：" + detail : ""}`, true);
      await drawVds();
    } catch (err) { setMsg("#spd-vd-msg", err.message, false); }
  };

  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const view = el("data-team-view"), add = el("data-team-add");
    const edit = el("data-team-edit"), off = el("data-team-off"), on = el("data-team-on");
    const memEdit = el("data-mem-edit"), memDel = el("data-mem-del");
    const vdEdit = el("data-vd-edit"), vdOff = el("data-vd-off"), vdQr = el("data-vd-qr");
    if (view) {
      try { await drawMembers(view.dataset.teamView); }
      catch (err) { setMsg("#spd-team-msg", err.message, false); }
      return;
    }
    if (add) {
      const form = await spdModal("添加团队成员", [
        { name: "user_id", label: "用户ID", type: "number", required: true },
        { name: "member_role", label: "成员角色", type: "select",
          options: spdOptions(SPD_MEMBER_ROLES), value: "doctor" },
        { name: "patient_scope", label: "可见患者范围", type: "select",
          options: spdOptions(SPD_PATIENT_SCOPES), value: "team" },
        { name: "stage_scope", label: "负责阶段（可留空）" },
        { name: "program_codes", label: "服务病种编码（逗号分隔，可留空）" },
      ]);
      if (!form || !form.user_id) return;
      return postAction(`/api/spd/teams/${add.dataset.teamAdd}/members`, {
        user_id: form.user_id, member_role: form.member_role,
        patient_scope: form.patient_scope, stage_scope: form.stage_scope,
        program_codes: spdCodeList(form.program_codes),
      }, "#spd-team-msg");
    }
    if (edit) {
      const team = teams.find((t) => String(t.id) === edit.dataset.teamEdit) || {};
      const form = await spdModal("修改团队配置", [
        { name: "name", label: "团队名称", value: team.name, required: true },
        { name: "level", label: "层级", type: "select", options: spdOptions(SPD_TEAM_LEVELS),
          value: team.level },
        { name: "data_scope", label: "数据范围", type: "select",
          options: spdOptions(SPD_DATA_SCOPES), value: team.data_scope },
        { name: "dept", label: "牵头科室", value: team.dept },
        { name: "service_area", label: "服务区域", value: team.service_area },
        { name: "leader_user_id", label: "组长用户ID（0 = 不指定）", type: "number",
          value: team.leader_user_id || 0 },
        { name: "program_codes", label: "服务病种编码（逗号分隔）",
          value: (team.program_codes || []).join(",") },
      ]);
      if (!form) return;
      return postAction(`/api/spd/teams/${edit.dataset.teamEdit}`, {
        name: form.name, level: form.level, data_scope: form.data_scope,
        dept: form.dept, service_area: form.service_area,
        leader_user_id: form.leader_user_id || null,
        program_codes: spdCodeList(form.program_codes),
      }, "#spd-team-msg", "PATCH");
    }
    if (off) {
      // 停用只改状态、不删行；列表带 include_inactive，停用后仍看得见、可再启用。
      if (!confirm("停用后该团队不再接收分派（列表里仍可见，可随时启用），确定停用？")) return;
      return postAction(`/api/spd/teams/${off.dataset.teamOff}`,
        { active: false }, "#spd-team-msg", "PATCH");
    }
    if (on) {
      return postAction(`/api/spd/teams/${on.dataset.teamOn}`,
        { active: true }, "#spd-team-msg", "PATCH");
    }
    if (memEdit) {
      const member = members.find((m) => String(m.id) === memEdit.dataset.memEdit) || {};
      const yesNo = [{ value: "1", label: "允许" }, { value: "0", label: "禁止" }];
      const flag = (v) => (v ? "1" : "0");
      const form = await spdModal("修改成员权限", [
        { name: "member_role", label: "成员角色", type: "select",
          options: spdOptions(SPD_MEMBER_ROLES), value: member.member_role },
        { name: "patient_scope", label: "可见患者范围", type: "select",
          options: spdOptions(SPD_PATIENT_SCOPES), value: member.patient_scope },
        { name: "stage_scope", label: "负责阶段", value: member.stage_scope },
        { name: "can_followup", label: "随访", type: "select", options: yesNo,
          value: flag(member.can_followup) },
        { name: "can_referral", label: "转诊", type: "select", options: yesNo,
          value: flag(member.can_referral) },
        { name: "can_audit", label: "审核", type: "select", options: yesNo,
          value: flag(member.can_audit) },
        { name: "can_assess", label: "评估", type: "select", options: yesNo,
          value: flag(member.can_assess) },
        { name: "active", label: "在岗", type: "select",
          options: [{ value: "1", label: "在岗" }, { value: "0", label: "停岗" }],
          value: flag(member.active) },
      ]);
      if (!form) return;
      return postAction(`/api/spd/team-members/${memEdit.dataset.memEdit}`, {
        member_role: form.member_role, patient_scope: form.patient_scope,
        stage_scope: form.stage_scope, can_followup: form.can_followup === "1",
        can_referral: form.can_referral === "1", can_audit: form.can_audit === "1",
        can_assess: form.can_assess === "1", active: form.active === "1",
      }, "#spd-team-msg", "PATCH");
    }
    if (memDel) {
      if (!confirm("确定把该成员移出团队？")) return;
      try {
        await api(`/api/spd/team-members/${memDel.dataset.memDel}`, { method: "DELETE" });
        setMsg("#spd-team-msg", "已移出团队", true);
        route();
      } catch (err) { setMsg("#spd-team-msg", err.message, false); }
      return;
    }
    if (vdEdit) {
      const vd = vds.find((v) => String(v.id) === vdEdit.dataset.vdEdit) || {};
      const form = await spdModal("修改村医档案", [
        { name: "township", label: "乡镇", value: vd.township },
        { name: "village", label: "村", value: vd.village },
        { name: "license_no", label: "执业证号", value: vd.license_no },
        { name: "license_valid_to", label: "证照有效期（YYYY-MM-DD）", value: vd.license_valid_to },
        { name: "phone", label: "联系电话", value: vd.phone },
      ]);
      if (!form) return;
      return postAction(`/api/spd/village-doctors/${vdEdit.dataset.vdEdit}`,
        form, "#spd-vd-msg", "PATCH");
    }
    if (vdOff) {
      return postAction(`/api/spd/village-doctors/${vdOff.dataset.vdOff}`,
        { active: vdOff.dataset.vdActive !== "1" }, "#spd-vd-msg", "PATCH");
    }
    if (vdQr) {
      // 码指向 /m/doctor#bind=<token>（后端拼的），打印给村医扫。
      $("#spd-vd-qr").innerHTML = `<p class="desc">村医 #${esc(vdQr.dataset.vdQr)} 的绑定二维码，
        扫码进入医生移动端完成绑定；停用的村医不出码。</p>
        <img src="/api/spd/village-doctors/${esc(vdQr.dataset.vdQr)}/qr.svg"
             alt="村医绑定二维码" width="200" height="200">`;
    }
  };
}

/* ============================================================
 * 6. 患者与档案（筛查 → 目标池 → 纳管 → 生命周期）
 * ==========================================================*/

async function renderSpdPatients() {
  $("#page-desc").textContent =
    "筛查建档纳管：机会性/主动筛查、高危复核、签约建档、排除迁出死亡召回";
  const catalog = await spdCatalog();
  $("#page-body").innerHTML = `
    <div class="panel"><h3>筛查登记</h3>
      <p class="desc">量表评分与病种规则双通道判定，任一命中即入目标池；排除规则优先于纳入</p>
      <form class="inline" id="spd-screen-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${spdProgramOptions(catalog)}</select>
        <select name="source">
          <option value="opportunistic">机会性筛查</option>
          <option value="active">主动筛查</option>
          <option value="import">数据比对</option>
        </select>
        <select name="scale_code"><option value="">不使用量表</option>
          ${catalog.scales.filter((s) => s.category === "screen")
            .map((s) => `<option value="${esc(s.code)}">${esc(s.name)}</option>`).join("")}</select>
        <button>登记筛查</button>
      </form>
      <form class="inline" id="spd-autoscreen-form" style="margin-top:10px">
        <select name="program_code">${spdProgramOptions(catalog)}</select>
        <input name="org_id" type="number" placeholder="机构ID(留空取本机构)">
        <button class="secondary">按规则自动识别</button>
      </form><p class="msg" id="spd-screen-msg"></p>
      <div id="spd-screen-list"></div></div>
    <div class="panel"><h3>签约建档纳管</h3>
      <form class="inline" id="spd-enroll-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${spdProgramOptions(catalog)}</select>
        <input name="org_id" type="number" placeholder="纳管机构ID">
        <select name="team_id"><option value="">服务团队</option>
          ${catalog.teams.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <input name="doctor_user_id" type="number" placeholder="主管医生ID">
        <input name="manager_user_id" type="number" placeholder="个案管理师ID">
        <select name="risk_level">
          <option value="low">低危</option><option value="mid">中危</option>
          <option value="high">高危</option><option value="very_high">极高危</option>
        </select>
        <input name="sign_date" placeholder="签约日 YYYY-MM-DD">
        <button>签约纳管</button>
      </form><p class="msg" id="spd-enroll-msg"></p>
      <form class="inline" id="spd-enroll-filter">
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <select name="risk_level"><option value="">全部风险</option>
          <option value="low">低危</option><option value="mid">中危</option>
          <option value="high">高危</option><option value="very_high">极高危</option></select>
        <input name="keyword" placeholder="姓名/证件号">
        <button class="secondary">查询</button>
      </form>
      <div id="spd-enroll-list"></div></div>
    <div class="panel"><h3>生命周期处置</h3>
      <p class="desc">排除 / 迁出 / 死亡 / 召回会同步终止后续任务、路径、干预与复诊；跨机构迁出需目标机构确认</p>
      <form class="inline" id="spd-life-form">
        <input name="enrollment_id" type="number" placeholder="纳管档案ID" required>
        <select name="event">
          <option value="exclude">移除病种标签</option>
          <option value="migrate">迁出</option>
          <option value="death">死亡登记</option>
          <option value="recall">发起召回</option>
          <option value="resume">恢复管理</option>
        </select>
        <input name="target_org_id" type="number" placeholder="迁入机构ID(跨机构迁出)">
        <input name="reason" placeholder="原因">
        <button>提交</button>
      </form><p class="msg" id="spd-life-msg"></p>
      <div id="spd-life-list"></div></div>
    <div class="panel"><h3>患者分组</h3>
      <p class="desc">自动规则命中的在管患者会被吸入分组；手工加入的成员不受规则影响</p>
      <form class="inline" id="spd-group-form">
        <input name="name" placeholder="分组名称" required>
        <select name="scope">
          <option value="personal">个人</option><option value="dept">科室</option>
          <option value="team">团队</option>
        </select>
        <input name="dept" placeholder="科室（scope=dept 时填）">
        <button>新建分组</button>
      </form>
      <p class="desc">自动纳入规则（全部满足才吸入）</p>
      <div id="spd-group-rules"></div>
      <p class="msg" id="spd-group-msg"></p>
      <div id="spd-group-list"></div>
      <div id="spd-group-members"></div></div>
    <div class="panel"><h3>目标池（候选）</h3>
      <p class="desc">筛查命中的疑似患者落在这里等认领：认领是"我来跟"，
        改状态是"这个人到底算不算目标"。<b>已被他人认领的再认领返回 409</b>——
        不静默改人，两个团队同时跟一个患者比谁都不跟更糟。</p>
      <form class="inline" id="spd-cand-filter">
        <select name="program_code">${spdProgramOptions(catalog, true)}</select>
        <select name="status"><option value="">全部状态</option>
          ${Object.entries(SPD_CAND_STATUS).map(([v, t]) =>
            `<option value="${v}">${esc(t[0])}</option>`).join("")}</select>
        <button class="btn secondary">查询</button>
      </form>
      <p class="msg" id="spd-cand-msg"></p>
      <div id="spd-cand-list"></div></div>
    <div class="panel"><h3>召回台账</h3>
      <p class="desc">失访患者的召回过程要留痕：联系一次记一次，
        <b>召回成功（returned）会自动把档案恢复为在管</b>，不需要再手工恢复。</p>
      <p class="msg" id="spd-recall-msg"></p>
      <div id="spd-recall-list"></div></div>
    <div class="panel"><h3>居民服务申请</h3>
      <p class="desc">居民端发起的专病服务申请：<b>受理即把居民放进目标池</b>，
        等待签约建档；拒绝要写明理由，居民端看得到。</p>
      <form class="inline" id="spd-apply-filter">
        <select name="status">
          <option value="pending">待受理</option>
          <option value="accepted">已受理</option>
          <option value="rejected">已拒绝</option>
          <option value="">全部</option></select>
        <button class="btn secondary">查询</button>
      </form>
      <p class="msg" id="spd-apply-msg"></p>
      <div id="spd-apply-list"></div></div>`;

  const drawScreenings = async () => {
    const rows = await api("/api/spd/screenings?limit=30");
    $("#spd-screen-list").innerHTML = table(
      ["ID", "患者", "病种", "来源", "得分", "风险", "结论", "复核", "操作"], rows, (s) =>
      `<tr><td>${s.id}</td><td>${esc(s.patient_name || s.patient_id)}</td>
       <td>${esc(s.program_code)}</td><td>${esc(s.source)}</td><td>${s.score}</td>
       <td>${spdTag(SPD_RISK, s.risk_level)}</td>
       <td>${s.result === "suspect" ? '<span class="tag orange">疑似</span>'
          : s.result === "excluded" ? '<span class="tag">排除</span>'
          : '<span class="tag green">未见异常</span>'}</td>
       <td>${s.reviewed ? esc(s.review_result) : '<span class="tag orange">待复核</span>'}</td>
       <td>${s.reviewed ? "—" :
          `<button class="btn secondary" data-review="${s.id}" data-r="confirmed">确认</button>
           <button class="btn secondary" data-review="${s.id}" data-r="excluded">排除</button>`}</td></tr>`);
  };
  const drawEnrollments = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/enrollments?${qs}`);
    $("#spd-enroll-list").innerHTML = table(
      ["ID", "患者", "病种", "阶段", "风险", "机构", "团队", "建档", "下次随访", "状态", "操作"],
      rows, (e) =>
      `<tr><td>${e.id}</td><td>${esc(e.patient_name || e.patient_id)}</td>
       <td>${esc(e.program_code)}</td><td>${esc(e.stage || "—")}</td>
       <td>${spdTag(SPD_RISK, e.risk_level)}</td><td>${e.org_id}</td>
       <td>${e.team_id ?? "—"}</td>
       <td>${e.archived ? '<span class="tag green">已建档</span>' : '<span class="tag orange">待完善</span>'}</td>
       <td>${esc(e.next_followup_at || "—")}</td>
       <td>${e.status === "active" ? '<span class="tag green">在管</span>'
          : '<span class="tag">' + esc(e.status) + "</span>"}</td>
       <td><button class="btn secondary" data-enr-view="${e.id}">明细</button>
           <button class="btn secondary" data-enr-edit="${e.id}">改档</button></td></tr>`);
  };
  const drawLifecycle = async () => {
    const rows = await api("/api/spd/lifecycle-events?limit=20");
    $("#spd-life-list").innerHTML = table(
      ["ID", "档案", "患者", "事件", "原因", "发生日期", "确认", "操作"], rows, (v) =>
      `<tr><td>${v.id}</td><td>${v.enrollment_id}</td><td>${esc(v.patient_name || "")}</td>
       <td>${esc({ exclude: "排除", migrate: "迁出", death: "死亡", recall: "召回", resume: "恢复" }[v.event] || v.event)}</td>
       <td>${esc(v.reason || "—")}</td><td>${esc(v.occurred_at || "—")}</td>
       <td>${v.confirmed ? '<span class="tag green">已确认</span>' : '<span class="tag orange">待确认</span>'}</td>
       <td>${v.confirmed ? "—" : `<button class="btn secondary" data-confirm="${v.id}">确认迁入</button>`}</td></tr>`);
  };
  await Promise.all([drawScreenings(), drawEnrollments(), drawLifecycle()]);

  $("#spd-screen-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/screenings", formJson(e.target, ["patient_id"]), "#spd-screen-msg");
  };
  $("#spd-autoscreen-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/spd/screenings/auto-run", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["org_id"])) });
      alert(`扫描 ${r.scanned} 人：疑似 ${r.suspect}、排除 ${r.excluded}、未见异常 ${r.normal}\n规则版本 ${r.rule_version}`);
      route();
    } catch (err) { setMsg("#spd-screen-msg", err.message, false); }
  };
  $("#spd-enroll-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/enrollments",
      formJson(e.target, ["patient_id", "org_id", "team_id", "doctor_user_id", "manager_user_id"]),
      "#spd-enroll-msg");
  };
  $("#spd-enroll-filter").onsubmit = async (e) => {
    e.preventDefault();
    await drawEnrollments(formJson(e.target));
  };
  $("#spd-life-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction(
      `/api/spd/enrollments/${e.target.enrollment_id.value}/lifecycle`,
      formJson(e.target, ["target_org_id"]), "#spd-life-msg");
  };
  $("#page-body").onclick = async (e) => {
    const review = e.target.closest("[data-review]");
    const confirm = e.target.closest("[data-confirm]");
    if (review) {
      return postAction(`/api/spd/screenings/${review.dataset.review}/review`,
        { review_result: review.dataset.r }, "#spd-screen-msg");
    }
    if (confirm) {
      return postAction(`/api/spd/lifecycle-events/${confirm.dataset.confirm}/confirm`,
        null, "#spd-life-msg");
    }
    const el = (k) => e.target.closest(`[${k}]`);
    const claim = el("data-cand-claim"), cstat = el("data-cand-status");
    const enrView = el("data-enr-view"), enrEdit = el("data-enr-edit");
    const pkgBind = el("data-pkg-bind"), pkgUnbind = el("data-pkg-unbind");
    const pkgUse = el("data-pkg-use"), pkgUsages = el("data-pkg-usages");
    const recall = el("data-recall"), aOk = el("data-apply-ok"), aNo = el("data-apply-no");
    const gView = el("data-group-view"), gAdd = el("data-group-add"), gDel = el("data-gm-del");
    try {
      if (claim) {
        // 已被他人认领后端回 409，照实把那句话显示出来，不吞掉
        return postAction(`/api/spd/candidates/${claim.dataset.candClaim}/claim`,
          null, "#spd-cand-msg");
      }
      if (cstat) {
        const form = await spdModal("改目标池状态", [
          { name: "status", label: "状态", type: "select",
            options: spdOptions(SPD_CAND_STATUS), value: "target" },
          { name: "reason", label: "理由", placeholder: "排除时务必写明" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/candidates/${cstat.dataset.candStatus}/status`,
          form, "#spd-cand-msg");
      }
      if (enrView) {
        const id = enrView.dataset.enrView;
        const d = await api(`/api/spd/enrollments/${id}`);
        const prof = d.patient_id
          ? await api(`/api/spd/patients/${d.patient_id}/profile`).catch(() => null)
          : null;
        $("#spd-enroll-list").insertAdjacentHTML("afterend",
          `<div id="spd-enr-detail"><p class="desc">档案 #${esc(id)} 明细</p>`
          + table(["项", "值"], [
            ["患者", d.patient_name || d.patient_id], ["病种", d.program_code],
            ["阶段", d.stage || "—"], ["风险", (SPD_RISK[d.risk_level] || [])[0] || d.risk_level],
            ["纳管机构", d.org_id], ["团队", d.team_id ?? "—"],
            ["主管医生", d.doctor_user_id ?? "—"], ["个案管理师", d.manager_user_id ?? "—"],
            ["知情同意", d.consent_signed ? `已签 ${d.consent_no || ""}` : "未签"],
            ["服务起始", d.service_start || "—"], ["状态", d.status],
            ["档案在管病种数", prof ? (prof.enrollments || []).length : "—"],
          ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`)
          + `<h4 style="margin-top:10px">服务包（${(d.packages || []).length} 个）</h4>`
          + table(["ID", "服务包", "项目明细", "剩余", "消费率", "状态", "操作"], (d.packages || []), (b) =>
            `<tr><td>${b.id}</td><td>${esc(b.package_name)}</td>
             <td>${(b.items || []).map((i) => `${esc(i.name || i.code)} ${i.used || 0}/${i.total || 0}`).join("；") || "—"}</td>
             <td>${b.remaining}</td><td>${b.usage_rate}%</td>
             <td><span class="tag ${b.status === "bound" ? "green" : ""}">${b.status === "bound" ? "在用" : "已解绑"}</span></td>
             <td>${b.status === "bound"
               ? `<button class="btn secondary" data-pkg-use="${b.id}" data-enr="${id}">扣减登记</button>
                  <button class="btn secondary" data-pkg-usages="${b.id}">扣减流水</button>
                  <button class="btn danger" data-pkg-unbind="${b.id}" data-enr="${id}">解绑</button>`
               : `<button class="btn secondary" data-pkg-usages="${b.id}">扣减流水</button>`}</td></tr>`)
          + `<p style="margin-top:6px"><button class="btn secondary" data-pkg-bind="${esc(id)}">绑定服务包</button></p>
             <div id="spd-pkg-usages"></div>`
          + "</div>");
        const dup = document.querySelectorAll("#spd-enr-detail");
        if (dup.length > 1) dup[0].remove();   // 只留最新一份，不越点越长
        return;
      }
      if (pkgBind) {
        const enrId = pkgBind.dataset.pkgBind;
        const list = await api("/api/spd/service-packages?limit=100");
        const form = await spdModal("绑定服务包", [
          { name: "package_id", label: "服务包", type: "select",
            // spdModal 的 select 吃的是 {value,label} 对象，不是 [v,l] 数组
            options: list.map((x) => ({ value: String(x.id), label: `${x.name}（${x.code}）` })) },
        ]);
        if (!form || !form.package_id) return;
        await api(`/api/spd/enrollments/${enrId}/packages`, { method: "POST",
          body: JSON.stringify({ package_id: Number(form.package_id) }) });
        setMsg("#spd-enroll-msg", "服务包已绑定", true);
        return;
      }
      if (pkgUnbind) {
        // 解绑只改状态、不删流水：已经发生的服务不能因为解绑就从账上消失。
        if (!confirm(`解绑服务包绑定 ${pkgUnbind.dataset.pkgUnbind}？解绑后不能再扣减，已发生的扣减流水保留。`)) return;
        await api(`/api/spd/package-bindings/${pkgUnbind.dataset.pkgUnbind}/unbind`, { method: "POST" });
        setMsg("#spd-enroll-msg", "已解绑", true);
        return;
      }
      if (pkgUse) {
        const form = await spdModal("服务项目扣减登记", [
          { name: "item_code", label: "项目编码（服务包里的 code）", required: true },
          { name: "qty", label: "次数", type: "number", value: 1 },
          { name: "note", label: "说明" },
        ]);
        if (!form || !form.item_code) return;
        // 剩余次数不足后端拒绝（不扣成负数）；错误照原样显示，别吞。
        await api(`/api/spd/package-bindings/${pkgUse.dataset.pkgUse}/usages`, { method: "POST",
          body: JSON.stringify({ item_code: form.item_code, qty: Number(form.qty) || 1, note: form.note || "" }) });
        setMsg("#spd-enroll-msg", "已登记扣减", true);
        return;
      }
      if (pkgUsages) {
        const rows = await api(`/api/spd/package-bindings/${pkgUsages.dataset.pkgUsages}/usages`);
        $("#spd-pkg-usages").innerHTML = `<h4>绑定 ${esc(pkgUsages.dataset.pkgUsages)} 的扣减流水（${rows.length} 条）</h4>` +
          table(["时间", "项目", "次数", "单价", "说明"], rows, (r) =>
            `<tr><td>${esc(r.used_at.replace("T", " ").slice(0, 19))}</td>
             <td>${esc(r.item_name || r.item_code)}</td><td>${r.qty}</td><td>${r.price}</td>
             <td>${esc(r.note) || "—"}</td></tr>`);
        return;
      }
      if (enrEdit) {
        const id = enrEdit.dataset.enrEdit;
        const cur = await api(`/api/spd/enrollments/${id}`);
        const form = await spdModal("改纳管档案", [
          { name: "team_id", label: "服务团队ID", type: "number", value: cur.team_id ?? "" },
          { name: "doctor_user_id", label: "主管医生ID", type: "number", value: cur.doctor_user_id ?? "" },
          { name: "manager_user_id", label: "个案管理师ID", type: "number", value: cur.manager_user_id ?? "" },
          { name: "risk_level", label: "风险等级", type: "select",
            options: spdOptions(SPD_RISK), value: cur.risk_level || "low" },
          { name: "stage", label: "分期", value: cur.stage || "" },
          { name: "consent_no", label: "知情同意书号", value: cur.consent_no || "" },
          { name: "service_start", label: "服务起始 YYYY-MM-DD", value: cur.service_start || "" },
        ]);
        if (!form) return;
        // 空串不提交：EnrollUpdate 的字段都是可选，传空串会把它当成"要改成空"
        const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ""));
        ["team_id", "doctor_user_id", "manager_user_id"].forEach((k) => {
          if (body[k] !== undefined) body[k] = Number(body[k]);
        });
        return postAction(`/api/spd/enrollments/${id}`, body, "#spd-enroll-msg", "PATCH");
      }
      if (recall) {
        const form = await spdModal("记录召回进展", [
          { name: "status", label: "进展", type: "select",
            options: spdOptions(SPD_RECALL_STATUS), value: "contacted" },
          { name: "contact_note", label: "联系情况" },
          { name: "result", label: "结果说明" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/recalls/${recall.dataset.recall}/progress`,
          form, "#spd-recall-msg");
      }
      if (aOk || aNo) {
        const id = (aOk || aNo).dataset[aOk ? "applyOk" : "applyNo"];
        const note = prompt(aOk ? "受理意见（可留空）" : "拒绝理由（居民端看得到）", "");
        if (note === null) return;
        if (!aOk && !note.trim()) return setMsg("#spd-apply-msg", "拒绝必须写明理由", false);
        return postAction(`/api/spd/service-applies/${id}/handle`,
          { status: aOk ? "accepted" : "rejected", handle_note: note }, "#spd-apply-msg");
      }
      if (gView) {
        try { await drawGroupMembers(gView.dataset.groupView); }
        catch (err) { setMsg("#spd-group-msg", err.message, false); }
        return;
      }
      if (gAdd) {
        const form = await spdModal("加入分组成员", [
          { name: "patient_ids", label: "患者ID（逗号分隔）", placeholder: "101,102" },
          { name: "use_auto_rule", label: "按分组自动规则筛", type: "select",
            options: [{ value: "0", label: "否" }, { value: "1", label: "是" }], value: "0" },
          { name: "program_code", label: "限定病种（自动规则时可填）" },
        ]);
        if (!form) return;
        const ids = (form.patient_ids || "").split(",").map((x) => Number(x.trim()))
          .filter((x) => x > 0);
        return postAction(`/api/spd/groups/${gAdd.dataset.groupAdd}/members`, {
          patient_ids: ids, use_auto_rule: form.use_auto_rule === "1",
          program_code: form.program_code || "",
        }, "#spd-group-msg");
      }
      if (gDel) {
        const [gid, pid] = gDel.dataset.gmDel.split(":");
        if (!confirm("把该患者移出分组？手工加入的成员移出后不会被规则再吸回来。")) return;
        await api(`/api/spd/groups/${gid}/members/${pid}`, { method: "DELETE" });
        return drawGroupMembers(gid);
      }
    } catch (err) { setMsg("#spd-cand-msg", err.message, false); }
  };
  const drawGroups = async () => {
    const groups = await api("/api/spd/groups");
    $("#spd-group-list").innerHTML = table(
      ["ID", "名称", "范围", "科室", "自动规则", "成员数", "操作"], groups, (g) =>
      `<tr><td>${g.id}</td><td>${esc(g.name)}</td><td>${esc(g.scope)}</td>
       <td>${esc(g.dept || "—")}</td><td>${(g.auto_rule || []).length} 条</td>
       <td>${g.member_count ?? "—"}</td>
       <td><button class="btn secondary" data-group-view="${g.id}">成员</button>
           <button class="btn secondary" data-group-add="${g.id}">加成员</button></td></tr>`);
  };
  const drawCandidates = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/candidates?${qs}`);
    $("#spd-cand-list").innerHTML = table(
      ["ID", "患者", "病种", "风险", "状态", "认领团队", "纳入依据", "操作"], rows, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.patient_name || c.patient_id)}</td>
       <td>${esc(c.program_code)}</td><td>${spdTag(SPD_RISK, c.risk_level)}</td>
       <td>${spdTag(SPD_CAND_STATUS, c.status)}</td>
       <td>${c.claimed_team_id ?? "—"}</td><td>${esc(c.source || "—")}</td>
       <td>${c.claimed_team_id ? "" : `<button class="btn" data-cand-claim="${c.id}">认领</button>`}
           <button class="btn secondary" data-cand-status="${c.id}">改状态</button></td></tr>`);
  };
  const drawRecalls = async () => {
    const rows = await api("/api/spd/recalls?limit=30");
    $("#spd-recall-list").innerHTML = table(
      ["ID", "档案", "原因", "状态", "联系次数", "结果", "发起时间", "操作"], rows, (r) =>
      `<tr><td>${r.id}</td><td>${r.enrollment_id}</td><td>${esc(r.reason || "—")}</td>
       <td>${spdTag(SPD_RECALL_STATUS, r.status)}</td>
       <td>${(r.contacts || []).length}</td><td>${esc(r.result || "—")}</td>
       <td>${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
       <td><button class="btn secondary" data-recall="${r.id}">记录进展</button></td></tr>`);
  };
  const drawApplies = async (query) => {
    const qs = new URLSearchParams(query && query.status !== undefined
      ? { limit: "30", status: query.status } : { limit: "30", status: "pending" }).toString();
    const rows = await api(`/api/spd/service-applies?${qs}`);
    $("#spd-apply-list").innerHTML = table(
      ["ID", "患者", "病种", "申请说明", "状态", "处理意见", "申请时间", "操作"], rows, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.program_code)}</td>
       <td>${esc(a.note || "—")}</td><td>${spdTag(SPD_APPLY_STATUS, a.status)}</td>
       <td>${esc(a.handle_note || "—")}</td>
       <td>${esc((a.created_at || "").replace("T", " ").slice(0, 16))}</td>
       <td>${a.status === "pending"
          ? `<button class="btn" data-apply-ok="${a.id}">受理</button>
             <button class="btn danger" data-apply-no="${a.id}">拒绝</button>`
          : "—"}</td></tr>`);
  };
  const drawGroupMembers = async (groupId) => {
    const rows = await api(`/api/spd/groups/${groupId}/members`);
    $("#spd-group-members").innerHTML =
      `<p class="desc">分组 #${esc(groupId)} 的成员（${rows.length} 人）</p>`
      + table(["患者", "病种", "风险", "操作"], rows, (m) =>
        `<tr><td>${esc(m.patient_name || m.patient_id)}</td>
         <td>${esc(m.program_code || "—")}</td><td>${spdTag(SPD_RISK, m.risk_level)}</td>
         <td><button class="btn danger" data-gm-del="${groupId}:${m.patient_id}">移出</button></td></tr>`);
  };

  await Promise.all([drawGroups(), drawCandidates(), drawRecalls(), drawApplies()]);
  $("#spd-cand-filter").onsubmit = async (e) => {
    e.preventDefault();
    const q = Object.fromEntries(Object.entries(formJson(e.target)).filter(([, v]) => v));
    try { await drawCandidates(q); } catch (err) { setMsg("#spd-cand-msg", err.message, false); }
  };
  $("#spd-apply-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawApplies(formJson(e.target)); }
    catch (err) { setMsg("#spd-apply-msg", err.message, false); }
  };

  const meta = await spdMeta();
  const groupEditor = spdRuleEditor($("#spd-group-rules"), meta, []);
  $("#spd-group-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/groups", {
      ...formJson(e.target), auto_rule: groupEditor.value(),
    }, "#spd-group-msg");
  };
}

/* ============================================================
 * 7. 路径与任务中心
 * ==========================================================*/

async function renderSpdPath() {
  $("#page-desc").textContent =
    "标准路径与统一任务：模板发布、患者路径实例、节点流转、任务接收分派催办升级";
  const [catalog, templates, summary] = await Promise.all([
    spdCatalog(), api("/api/spd/path-templates?limit=30"), api("/api/spd/tasks/summary"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["待办任务", summary.open_total], ["超期", summary.overdue, summary.overdue > 0],
      ["今日到期", summary.due_today], ["已升级", summary.escalated, summary.escalated > 0],
    ])}
    <div class="panel"><h3>路径模板</h3>
      <p class="desc">已发布的模板不能直接改节点——要改就复制新版本，避免在跑的患者任务突然变形</p>
      <form class="inline" id="spd-tpl-form">
        <select name="program_id">
          ${catalog.programs.map((p, i) => `<option value="${i + 1}">${esc(p.name)}</option>`).join("")}
        </select>
        <input name="code" placeholder="路径编码" required>
        <input name="name" placeholder="路径名称" required>
        <select name="scene">
          <option value="outpatient">门诊路径</option><option value="inpatient">住院路径</option>
          <option value="home">居家管理</option><option value="followup">随访路径</option>
        </select>
        <button>新建路径</button>
      </form><p class="msg" id="spd-tpl-msg"></p>
      ${table(["ID", "编码", "名称", "场景", "版本", "节点数", "状态", "操作"], templates, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.code)}</td><td>${esc(t.name)}</td>
         <td>${esc(t.scene)}</td><td>${esc(t.version)}</td><td>${t.node_count ?? 0}</td>
         <td>${t.status === "published" ? '<span class="tag green">已发布</span>'
            : t.status === "draft" ? '<span class="tag orange">草稿</span>'
            : '<span class="tag">已停用</span>'}</td>
         <td><button class="btn secondary" data-tpl-view="${t.id}">看节点</button>
             <button class="btn secondary" data-tpl-node="${t.id}">加节点</button>
             <button class="btn secondary" data-tpl-pub="${t.id}">发布</button>
             <button class="btn secondary" data-tpl-copy="${t.id}">复制</button>
             <button class="btn danger" data-tpl-del="${t.id}">删除</button></td></tr>`)}</div>
      <div id="spd-tpl-nodes"></div>
    <div class="panel"><h3>启动患者路径</h3>
      <form class="inline" id="spd-inst-form">
        <input name="enrollment_id" type="number" placeholder="纳管档案ID" required>
        <select name="template_id">
          ${catalog.path_templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
        </select>
        <button>启动路径</button>
      </form><p class="msg" id="spd-inst-msg"></p>
      <div id="spd-inst-list"></div>
      <div id="spd-inst-detail"></div></div>
    <div class="panel"><h3>任务中心</h3>
      <form class="inline" id="spd-task-filter">
        <select name="task_type"><option value="">全部类型</option>
          ${Object.entries(SPD_TASK_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <select name="status"><option value="">全部状态</option>
          ${Object.entries(SPD_TASK_STATUS).map(([k, v]) => `<option value="${k}">${esc(v[0])}</option>`).join("")}</select>
        <label style="font-size:13px"><input type="checkbox" name="mine" value="true"> 只看我的</label>
        <button class="secondary">查询</button>
      </form>
      <form class="inline" id="spd-task-batch">
        <select name="action">
          <option value="claim">批量接收</option>
          <option value="urge">批量催办</option>
          <option value="escalate">批量升级</option>
          <option value="assign">批量分派</option>
          <option value="cancel">批量取消</option>
        </select>
        <input name="assignee_id" type="number" placeholder="受派人用户ID（分派时填）">
        <input name="note" placeholder="备注">
        <button class="btn secondary">对勾选的任务执行</button>
        <button type="button" class="btn secondary" id="spd-task-export">导出CSV</button>
      </form>
      <p class="msg" id="spd-task-msg"></p>
      <div id="spd-task-list"></div>
      <div id="spd-task-detail"></div></div>`;

  const drawInstances = async () => {
    const rows = await api("/api/spd/path-instances?limit=20");
    $("#spd-inst-list").innerHTML = table(
      ["ID", "患者", "路径", "当前节点", "阶段", "进度", "状态", "操作"], rows, (i) =>
      `<tr><td>${i.id}</td><td>${esc(i.patient_name || i.patient_id || "")}</td>
       <td>${esc(i.template_name)}</td><td>${esc(i.current_node_key || "—")}</td>
       <td>${esc(i.current_stage || "—")}</td><td>${i.progress}%</td>
       <td>${i.status === "running" ? '<span class="tag orange">执行中</span>'
          : i.status === "completed" ? '<span class="tag green">已完成</span>'
          : '<span class="tag">' + esc(i.status) + "</span>"}</td>
       <td><button class="btn secondary" data-inst-view="${i.id}">明细</button>
       ${i.status === "running"
          ? `<button class="btn secondary" data-adv="${i.id}">推进节点</button>
             <button class="btn secondary" data-inst-adj="${i.id}">调整</button>` : ""}</td></tr>`);
  };
  const drawTasks = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/tasks?${qs}`);
    $("#spd-task-list").innerHTML = table(
      ["ID", "患者", "任务", "类型", "状态", "优先级", "截止", "催办", "操作"], rows, (t) =>
      `<tr><td>${t.id}</td><td>${esc(t.patient_name || t.patient_id)}</td>
       <td>${esc(t.title)}</td><td>${esc(SPD_TASK_TYPES[t.task_type] || t.task_type)}</td>
       <td>${spdTag(SPD_TASK_STATUS, t.status)}${t.escalated ? ' <span class="tag red">升级</span>' : ""}</td>
       <td>${t.priority === 3 ? "特急" : t.priority === 2 ? "紧急" : "普通"}</td>
       <td>${esc(t.due_date || "—")}</td><td>${t.urged_count}</td>
       <td><input type="checkbox" class="task-pick" data-pick="${t.id}">
           <button class="btn secondary" data-task-view="${t.id}">明细</button>
           <button class="btn secondary" data-task-claim="${t.id}">接收</button>
           <button class="btn secondary" data-task-assign="${t.id}">分派</button>
           <button class="btn secondary" data-task-urge="${t.id}">催办</button>
           <button class="btn secondary" data-task-submit="${t.id}">提交</button>
           ${t.status === "submitted"
             ? `<button class="btn" data-task-review="${t.id}">审核</button>` : ""}
           ${t.escalated ? "" : `<button class="btn danger" data-task-esc="${t.id}">升级</button>`}
           <button class="btn secondary" data-task-done="${t.id}">办结</button></td></tr>`);
  };
  await Promise.all([drawInstances(), drawTasks()]);

  $("#spd-tpl-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/path-templates", formJson(e.target, ["program_id"]), "#spd-tpl-msg");
  };
  $("#spd-inst-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/path-instances",
      formJson(e.target, ["enrollment_id", "template_id"]), "#spd-inst-msg");
  };
  $("#spd-task-filter").onsubmit = async (e) => {
    e.preventDefault();
    await drawTasks(formJson(e.target));
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const node = el("data-tpl-node"), pub = el("data-tpl-pub"), copy = el("data-tpl-copy");
    const tplView = el("data-tpl-view"), tplDel = el("data-tpl-del");
    const nodeEdit = el("data-node-edit"), nodeDel = el("data-node-del");
    // 节点明细里的「改/删」要能在操作后自己刷新，所以把绘制提出来复用
    const drawTplNodes = async (templateId) => {
      const d = await api(`/api/spd/path-templates/${templateId}`);
      const editable = d.status !== "published";
      $("#spd-tpl-nodes").innerHTML = `
        <h4>路径 ${esc(d.code)} ${esc(d.name)} v${esc(d.version)} 的节点（${(d.nodes || []).length} 个）</h4>
        ${editable ? "" : '<p class="msg">已发布的路径不可直接改节点——在跑的实例会突然多出一个没人知道的任务。要改请先「复制」出新版本。</p>'}
        ${table(["序", "key", "名称", "阶段", "科室", "执行角色", "时限(天)", "下一节点", "操作"],
          (d.nodes || []), (n) =>
          `<tr><td>${n.seq}</td><td><span class="tag">${esc(n.key)}</span></td><td>${esc(n.name)}</td>
           <td>${esc(n.stage) || "—"}</td><td>${esc(n.dept) || "—"}</td><td>${esc(n.exec_role) || "—"}</td>
           <td>${n.due_days}</td><td>${esc(n.next_key) || "—"}</td>
           <td>${editable
             ? `<button class="btn secondary" data-node-edit="${n.id}" data-tpl="${d.id}">改</button>
                <button class="btn danger" data-node-del="${n.id}" data-tpl="${d.id}">删</button>`
             : "—"}</td></tr>`)}`;
    };
    if (tplView) return drawTplNodes(tplView.dataset.tplView);
    if (tplDel) {
      // 删除按钮对所有模板都给出：能不能删的真正判据是"有没有患者实例引用"，
      // 列表里没有这个字段，后端会以 409 作答。按 status 猜着禁用，会把
      // "已发布但没人用过"这种可以删的挡在外面。
      if (!confirm(`删除路径模板 ${tplDel.dataset.tplDel}？节点一并删除，不可恢复。\n已被患者实例引用的路径删不掉，只能停用。`)) return;
      await api(`/api/spd/path-templates/${tplDel.dataset.tplDel}`, { method: "DELETE" });
      setMsg("#spd-tpl-msg", "路径模板已删除", true);
      return route();
    }
    if (nodeEdit) {
      const form = await spdModal("修改路径节点", [
        { name: "name", label: "节点名称" },
        { name: "stage", label: "所属阶段" },
        { name: "seq", label: "排序号", type: "number" },
        { name: "dept", label: "科室" },
        { name: "exec_role", label: "执行角色" },
        { name: "due_days", label: "时限（天）", type: "number" },
        { name: "next_key", label: "下一节点 key" },
      ]);
      if (!form) return;
      // 只把填了的字段发上去：PATCH 收的是裸 dict，把空串一并发过去会把
      // 原来有值的字段清空——改一个时限顺手抹掉科室，事后没人查得出。
      const body = {};
      for (const [k, v] of Object.entries(form)) if (v !== "" && v !== undefined) body[k] = v;
      for (const k of ["seq", "due_days"]) if (k in body) body[k] = Number(body[k]);
      if (!Object.keys(body).length) return;
      await api(`/api/spd/path-nodes/${nodeEdit.dataset.nodeEdit}`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg("#spd-tpl-msg", "节点已修改", true);
      return drawTplNodes(nodeEdit.dataset.tpl);
    }
    if (nodeDel) {
      if (!confirm(`删除节点 ${nodeDel.dataset.nodeDel}？在跑的实例走到这里会少一环。`)) return;
      await api(`/api/spd/path-nodes/${nodeDel.dataset.nodeDel}`, { method: "DELETE" });
      setMsg("#spd-tpl-msg", "节点已删除", true);
      return drawTplNodes(nodeDel.dataset.tpl);
    }
    const adv = el("data-adv");
    const claim = el("data-task-claim"), urge = el("data-task-urge"), done = el("data-task-done");
    if (node) {
      const form = await spdModal("添加路径节点", [
        { name: "key", label: "节点 key（英文，如 assess）", required: true },
        { name: "name", label: "节点名称" },
        { name: "stage", label: "所属阶段（可留空）" },
        { name: "due_days", label: "时限（天）", type: "number", value: 7 },
      ]);
      if (!form || !form.key) return;
      return postAction(`/api/spd/path-templates/${node.dataset.tplNode}/nodes`, {
        key: form.key, name: form.name || form.key,
        stage: form.stage, due_days: form.due_days || 7,
      }, "#spd-tpl-msg");
    }
    if (pub) {
      return postAction(`/api/spd/path-templates/${pub.dataset.tplPub}/status`,
        { status: "published" }, "#spd-tpl-msg");
    }
    if (copy) {
      return postAction(`/api/spd/path-templates/${copy.dataset.tplCopy}/copy`, {}, "#spd-tpl-msg");
    }
    if (adv) return postAction(`/api/spd/path-instances/${adv.dataset.adv}/advance`, null, "#spd-inst-msg");
    if (claim) return postAction(`/api/spd/tasks/${claim.dataset.taskClaim}/claim`, null, "#spd-task-msg");
    if (urge) return postAction(`/api/spd/tasks/${urge.dataset.taskUrge}/urge`, null, "#spd-task-msg");
    if (done) {
      const form = await spdModal("办结任务", [
        { name: "note", label: "办理结果", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/tasks/${done.dataset.taskDone}/complete`,
        { result: { note: form.note } }, "#spd-task-msg");
    }
    const el2 = (k) => e.target.closest(`[${k}]`);
    const tView = el2("data-task-view"), tAssign = el2("data-task-assign");
    const tSubmit = el2("data-task-submit"), tReview = el2("data-task-review");
    const tEsc = el2("data-task-esc");
    const iView = el2("data-inst-view"), iAdj = el2("data-inst-adj");
    try {
      if (tView) {
        const d = await api(`/api/spd/tasks/${tView.dataset.taskView}`);
        $("#spd-task-detail").innerHTML =
          `<p class="desc">任务 #${esc(d.id)} 明细</p>`
          + table(["项", "值"], [
            ["标题", d.title], ["类型", SPD_TASK_TYPES[d.task_type] || d.task_type],
            ["患者", d.patient_name || d.patient_id],
            ["状态", (SPD_TASK_STATUS[d.status] || [])[0] || d.status],
            ["责任人", d.assignee_id ?? "—"],
            ["转派自", d.transferred_from ?? "—"],
            ["截止", d.due_date || "—"], ["催办次数", d.urged_count ?? 0],
            ["已升级", d.escalated ? "是" : "否"],
            ["办理结果", JSON.stringify(d.result || {})],
          ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`);
        return;
      }
      if (tAssign) {
        const form = await spdModal("分派/转派任务", [
          { name: "assignee_id", label: "受派人用户ID", type: "number", required: true },
          { name: "note", label: "说明" },
        ]);
        if (!form || !form.assignee_id) return;
        // 转派会记 transferred_from，事后查得出"这活是从谁那儿转来的"
        return postAction(`/api/spd/tasks/${tAssign.dataset.taskAssign}/assign`,
          { assignee_id: Number(form.assignee_id), note: form.note || "" }, "#spd-task-msg");
      }
      if (tSubmit) {
        const form = await spdModal("提交任务", [
          { name: "note", label: "办理说明", type: "textarea" },
          { name: "evidence", label: "附件ID（逗号分隔，按需留痕）" },
          { name: "draft", label: "只存草稿不提交审核", type: "select",
            options: [{ value: "0", label: "否，提交审核" }, { value: "1", label: "是，存草稿" }],
            value: "0" },
        ]);
        if (!form) return;
        // evidence 后端收的是附件 id 列表——曾是自由字符串，那能让 require_evidence
        // 被一串乱码糊弄过去，所以这里只递数字
        const ev = (form.evidence || "").split(",").map((x) => Number(x.trim()))
          .filter((x) => x > 0);
        return postAction(`/api/spd/tasks/${tSubmit.dataset.taskSubmit}/submit`, {
          result: { note: form.note || "" }, evidence: ev,
          draft: form.draft === "1", note: form.note || "",
        }, "#spd-task-msg");
      }
      if (tReview) {
        const form = await spdModal("审核任务", [
          { name: "approved", label: "结论", type: "select",
            options: [{ value: "1", label: "通过（完成并推进路径）" },
                      { value: "0", label: "退回（回到办理中）" }], value: "1" },
          { name: "note", label: "审核意见" },
        ]);
        if (!form) return;
        if (form.approved === "0" && !(form.note || "").trim()) {
          return setMsg("#spd-task-msg", "退回必须写明意见——办理人要知道退回的理由", false);
        }
        return postAction(`/api/spd/tasks/${tReview.dataset.taskReview}/review`,
          { approved: form.approved === "1", note: form.note || "" }, "#spd-task-msg");
      }
      if (tEsc) {
        if (!confirm("升级会置为紧急并交由上级机构督办，确定？")) return;
        return postAction(`/api/spd/tasks/${tEsc.dataset.taskEsc}/escalate`,
          null, "#spd-task-msg");
      }
      if (iView) {
        const d = await api(`/api/spd/path-instances/${iView.dataset.instView}`);
        const nodes = d.nodes || [];
        $("#spd-inst-detail").innerHTML =
          `<p class="desc">路径实例 #${esc(d.id)}：${esc(d.template_name || "")}
            （进度 ${esc(d.progress ?? 0)}%）</p>`
          + table(["节点", "名称", "阶段", "任务状态", "责任人", "准入"], nodes, (n) =>
            `<tr><td>${esc(n.key)}</td><td>${esc(n.name || "")}</td>
             <td>${esc(n.stage || "—")}</td>
             <td>${esc((SPD_TASK_STATUS[n.task_status] || [])[0] || n.task_status || "—")}</td>
             <td>${n.assignee_id ?? "—"}</td>
             <td><button class="btn secondary" data-node-check="${n.id}"
                  data-inst="${d.id}">查准入</button></td></tr>`);
        return;
      }
      if (iAdj) {
        const form = await spdModal("调整路径实例", [
          { name: "status", label: "状态", type: "select",
            options: [{ value: "running", label: "执行中" }, { value: "paused", label: "暂停" },
                      { value: "cancelled", label: "取消" }], value: "running" },
          { name: "owner_user_id", label: "负责人用户ID", type: "number" },
        ]);
        if (!form) return;
        // 改的是**实例**不是模板：个性化调整不该让同模板的其他患者跟着变
        const body = { status: form.status };
        if (form.owner_user_id) body.owner_user_id = Number(form.owner_user_id);
        return postAction(`/api/spd/path-instances/${iAdj.dataset.instAdj}`,
          body, "#spd-inst-msg", "PATCH");
      }
      const nCheck = el2("data-node-check");
      if (nCheck) {
        // 路径**不折行拼接**：折成两段字符串，孤儿端点棘轮就看不见这个调用点了
        // （它按路径字面量扫源码）。折行是排版偏好，让闸门失明不是。
        const inst = nCheck.dataset.inst;
        const r = await api(
          `/api/spd/path-nodes/${nCheck.dataset.nodeCheck}/enter-check?instance_id=${inst}`);
        setMsg("#spd-inst-msg",
          r.allowed ? "准入条件已满足，可以推进" : `不可推进：${r.reason || "准入条件未满足"}`,
          !!r.allowed);
        return;
      }
    } catch (err) { setMsg("#spd-task-msg", err.message, false); }
  };

  $("#spd-task-batch").onsubmit = async (e) => {
    e.preventDefault();
    const picked = [...document.querySelectorAll(".task-pick:checked")]
      .map((c) => Number(c.dataset.pick));
    if (!picked.length) return setMsg("#spd-task-msg", "先勾选要处理的任务", false);
    const f = formJson(e.target);
    if (f.action === "assign" && !f.assignee_id) {
      return setMsg("#spd-task-msg", "批量分派必须填受派人用户ID", false);
    }
    const body = { task_ids: picked, action: f.action, note: f.note || "" };
    if (f.assignee_id) body.assignee_id = Number(f.assignee_id);
    return postAction("/api/spd/tasks/batch", body, "#spd-task-msg");
  };
  $("#spd-task-export").onclick = async () => {
    try {
      // 后端回「行数据 + 表头」，CSV 由前端拼——导出口径与列表筛选保持一致
      const q = new URLSearchParams(formJson($("#spd-task-filter"))).toString();
      const d = await api(`/api/spd/tasks-export?${q}`);
      const esc2 = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
      const csv = [d.headers.map(esc2).join(","),
        ...d.rows.map((r) => r.map(esc2).join(","))].join("\n");
      const url = URL.createObjectURL(new Blob(["\ufeff" + csv], { type: "text/csv" }));
      const a = document.createElement("a");
      a.href = url; a.download = "spd-tasks.csv"; a.click();
      URL.revokeObjectURL(url);
      setMsg("#spd-task-msg", `已导出 ${d.rows.length} 行`, true);
    } catch (err) { setMsg("#spd-task-msg", err.message, false); }
  };
}

/* ============================================================
 * 8. 逐级转诊
 * ==========================================================*/

async function renderSpdReferral() {
  $("#page-desc").textContent =
    "村医 → 乡镇卫生院 → 区市县医院三级转诊：分级审核、到院有效判定、下转随访接收闭环";
  const [closure, cases, alerts, rules] = await Promise.all([
    api("/api/spd/referrals-stats/closure"),
    api("/api/spd/referrals?open_only=false&limit=30"),
    api("/api/spd/referrals-alerts?hours=48"),
    api("/api/spd/referral-rules"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["转诊总量", closure.total], ["计入闭环分母", closure.denominator],
      ["已闭环", closure.closed], ["闭环率", closure.closure_rate + "%"],
      ["有效上转就诊", closure.effective_visits], ["有效率", closure.effective_rate + "%"],
      ["超时未推进", alerts.count, alerts.count > 0],
    ])}
    <p class="desc">闭环率分母不含撤回与退回单——退回是"该拦的拦住了"，计进去会逼着基层不敢退回</p>
    <div class="panel"><h3>在途单据按层级</h3>
      ${barChart(spdPairs(closure.pending_by_level,
        { village: "村医", station: "服务站", township: "卫生院", county: "县级医院" }),
        { color: "#0a4d78", unit: " 单" })}</div>
    <div class="panel"><h3>发起转诊</h3>
      <form class="inline" id="spd-ref-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="program_code" placeholder="病种编码">
        <select name="direction"><option value="up">上转</option><option value="down">下转</option></select>
        <input name="target_org_id" type="number" placeholder="目标机构ID">
        <input name="reason" placeholder="转诊理由" style="min-width:200px">
        <button>发起</button>
      </form><p class="msg" id="spd-ref-msg"></p>
      ${table(["ID", "患者", "病种", "方向", "当前层级", "状态", "有效就诊", "操作"], cases, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${esc(c.program_code || "—")}</td>
         <td>${c.direction === "up" ? "上转" : "下转"}</td>
         <td>${esc({ village: "村医", station: "服务站", township: "卫生院", county: "县级" }[c.current_level] || c.current_level)}</td>
         <td>${spdTag(SPD_REF_STATUS, c.status)}</td>
         <td>${c.effective_visit ? '<span class="tag green">是</span>' : "—"}</td>
         <td><button class="btn secondary" data-ref-detail="${c.id}">详情轨迹</button>
             <button class="btn secondary" data-ref-pass="${c.id}">通过</button>
             <button class="btn secondary" data-ref-reject="${c.id}">退回</button>
             <button class="btn secondary" data-ref-arrive="${c.id}">到院</button>
             <button class="btn secondary" data-ref-down="${c.id}">下转</button>
             <button class="btn secondary" data-ref-recv="${c.id}">随访接收</button>
             ${["submitted", "station_reviewed"].includes(c.status)
               ? `<button class="btn danger" data-ref-withdraw="${c.id}">撤回</button>` : ""}</td></tr>`)}</div>
      <div id="spd-ref-detail"></div>
    <div class="panel"><h3>转诊触发规则</h3>
      <p class="desc">命中规则默认只提示不自动开单——批量随访录入时自动开单会瞬间产生几十张单子</p>
      <form class="inline" id="spd-refcheck-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="program_code" placeholder="病种编码（可空）">
        <label style="font-size:13px"><input type="checkbox" name="auto_create"> 命中就直接开单</label>
        <button class="secondary">按规则试算</button>
      </form>
      <div id="spd-refcheck-result"></div>
      <form class="inline" id="spd-refrule-form">
        <input name="code" placeholder="规则编码" required>
        <input name="name" placeholder="规则名称" required>
        <input name="program_code" placeholder="病种编码（留空=全部）">
        <select name="handle_level">
          <option value="township">卫生院处置</option><option value="village">村医处置</option>
          <option value="station">服务站处置</option><option value="county">县级处置</option>
        </select>
        <button>新建触发规则</button>
      </form>
      <p class="desc">触发条件（任一满足即触发）</p>
      <div id="spd-refrule-rules"></div>
      <p class="msg" id="spd-refrule-msg"></p>
      ${table(["编码", "名称", "病种", "处理层级", "条件数", "自动建任务", "状态", "操作"], rules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.program_code || "全部")}</td>
         <td>${esc(r.handle_level)}</td><td>${(r.conditions || []).length}</td>
         <td>${r.auto_task ? "是" : "否"}</td>
         <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-refrule-edit="${r.id}">改</button>
             <button class="btn ${r.active ? "danger" : "secondary"}" data-refrule-toggle="${r.id}"
                     data-to="${r.active ? "false" : "true"}">${r.active ? "停用" : "启用"}</button></td></tr>`)}</div>
    ${alerts.count ? `<div class="panel" style="border-left:4px solid #c62828">
      <h3>⚠ 超过 ${alerts.threshold_hours} 小时未推进（${alerts.count}）</h3>
      ${table(["ID", "患者", "状态", "发起时间"], alerts.items, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${spdTag(SPD_REF_STATUS, c.status)}</td>
         <td>${esc(c.created_at.replace("T", " ").slice(0, 16))}</td></tr>`)}</div>` : ""}`;
  $("#spd-ref-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/referrals",
      formJson(e.target, ["patient_id", "target_org_id"]), "#spd-ref-msg");
  };
  $("#spd-refcheck-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const autoCreate = f.get("auto_create") === "on";
    // auto_create 默认关，勾上等于"命中即开单"：一次批量随访录入能开出几十张。
    // 所以勾了要再问一次，不给它做成顺手一点就过去的开关。
    if (autoCreate && !confirm("勾了「命中就直接开单」：规则一命中就生成上转单，不再经医生确认。继续？")) return;
    try {
      const r = await api("/api/spd/referral-rules/check", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")),
        program_code: f.get("program_code") || "", auto_create: autoCreate }) });
      $("#spd-refcheck-result").innerHTML = `
        <p>${r.triggered
          ? `<span class="tag red">命中 ${r.hits.length} 条规则</span>`
          : '<span class="tag green">未命中任何规则</span>'}
          ${r.case ? ` —— 已生成转诊单 #${r.case.id}` : (r.triggered ? "（未开单，按规则只提示）" : "")}</p>
        ${r.hits.length ? table(["规则", "名称", "处理层级", "命中的条件"], r.hits, (h) =>
          `<tr><td><span class="tag">${esc(h.rule.code)}</span></td><td>${esc(h.rule.name)}</td>
           <td>${esc(h.rule.handle_level)}</td>
           <td>${(h.matched || []).map((m) => esc(JSON.stringify(m))).join("；") || "—"}</td></tr>`) : ""}`;
      if (autoCreate) route();
    } catch (err) { setMsg("#spd-refrule-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const pass = e.target.closest("[data-ref-pass]"), reject = e.target.closest("[data-ref-reject]");
    const arrive = e.target.closest("[data-ref-arrive]"), down = e.target.closest("[data-ref-down]");
    const recv = e.target.closest("[data-ref-recv]");
    const detail = e.target.closest("[data-ref-detail]"), withdraw = e.target.closest("[data-ref-withdraw]");
    const ruleEdit = e.target.closest("[data-refrule-edit]"), ruleToggle = e.target.closest("[data-refrule-toggle]");
    if (detail) {
      const c = await api(`/api/spd/referrals/${detail.dataset.refDetail}`);
      const LV = { village: "村医", station: "服务站", township: "卫生院", county: "县级" };
      $("#spd-ref-detail").innerHTML = `
        <h4>转诊单 #${c.id}｜${esc(c.patient_name)}｜${c.direction === "up" ? "上转" : "下转"}</h4>
        <p>理由：${esc(c.reason) || "—"}；触发规则：${esc(c.trigger_rule_code) || "（人工发起）"}；
          当前层级 ${esc(LV[c.current_level] || c.current_level)}；
          有效就诊 ${c.effective_visit ? "是" : "否"}</p>
        <p>触发依据：${esc(JSON.stringify(c.trigger_evidence)) || "—"}</p>
        <p>提交资料：${(c.materials || []).map((m) => esc(typeof m === "string" ? m : JSON.stringify(m))).join("；") || "—"}</p>
        ${table(["环节", "动作", "经办人", "机构", "处理意见", "时间"], (c.steps || []), (st) =>
          `<tr><td>${esc(st.step)}</td><td>${esc(st.action)}</td><td>${st.actor_id}</td><td>${st.org_id ?? "—"}</td>
           <td>${esc(st.opinion) || "—"}</td><td>${esc(st.created_at.replace("T", " ").slice(0, 19))}</td></tr>`)}`;
      return;
    }
    if (withdraw) {
      // 只有发起人本人、且尚未被上级接收时可撤（后端否则 403/409）
      if (!confirm(`撤回转诊单 ${withdraw.dataset.refWithdraw}？只有发起人本人、且还没被上级接收时撤得掉。`)) return;
      try {
        await api(`/api/spd/referrals/${withdraw.dataset.refWithdraw}/withdraw`, { method: "POST" });
        setMsg("#spd-ref-msg", "已撤回", true);
        return route();
      } catch (err) { return setMsg("#spd-ref-msg", err.message, false); }
    }
    if (ruleToggle) {
      await api(`/api/spd/referral-rules/${ruleToggle.dataset.refruleToggle}`, { method: "PATCH",
        body: JSON.stringify({ active: ruleToggle.dataset.to === "true" }) });
      setMsg("#spd-refrule-msg", "已保存", true);
      return route();
    }
    if (ruleEdit) {
      const cur = rules.find((r) => String(r.id) === ruleEdit.dataset.refruleEdit) || {};
      const form = await spdModal("改转诊触发规则", [
        { name: "name", label: "规则名称", value: cur.name || "" },
        { name: "handle_level", label: "处理层级", type: "select",
          options: spdOptions({ village: "村医处置", station: "服务站处置", township: "卫生院处置", county: "县级处置" }),
          value: cur.handle_level || "township" },
        { name: "notify_role", label: "通知角色", value: cur.notify_role || "" },
        { name: "target_org_id", label: "目标机构ID（可空）", type: "number", value: cur.target_org_id ?? "" },
      ]);
      if (!form) return;
      // PATCH 收裸 dict 且只认出现过的键：空串一并发上去会把原值抹成空
      const body = {};
      for (const [k, v] of Object.entries(form)) if (v !== "" && v !== undefined) body[k] = v;
      if ("target_org_id" in body) body.target_org_id = Number(body.target_org_id);
      if (!Object.keys(body).length) return;
      await api(`/api/spd/referral-rules/${ruleEdit.dataset.refruleEdit}`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg("#spd-refrule-msg", "规则已修改", true);
      return route();
    }
    if (pass) {
      const form = await spdModal("审核通过", [{ name: "opinion", label: "审核意见", type: "textarea" }]);
      if (!form) return;
      return postAction(`/api/spd/referrals/${pass.dataset.refPass}/review`,
        { action: "pass", opinion: form.opinion }, "#spd-ref-msg");
    }
    if (reject) {
      const form = await spdModal("退回转诊", [{ name: "opinion", label: "退回理由", type: "textarea" }]);
      if (!form) return;
      return postAction(`/api/spd/referrals/${reject.dataset.refReject}/review`,
        { action: "reject", opinion: form.opinion }, "#spd-ref-msg");
    }
    if (arrive) return postAction(`/api/spd/referrals/${arrive.dataset.refArrive}/arrive`,
      { effective_visit: true }, "#spd-ref-msg");
    if (down) {
      const form = await spdModal("下转", [
        { name: "target_org_id", label: "下转目标机构ID", type: "number", required: true },
      ]);
      if (!form || !form.target_org_id) return;
      return postAction(`/api/spd/referrals/${down.dataset.refDown}/down`,
        { target_org_id: form.target_org_id, stable: true }, "#spd-ref-msg");
    }
    if (recv) {
      const form = await spdModal("下转随访接收", [{ name: "opinion", label: "接收意见", type: "textarea" }]);
      if (!form) return;
      return postAction(`/api/spd/referrals/${recv.dataset.refRecv}/receive-followup`,
        { opinion: form.opinion }, "#spd-ref-msg");
    }
  };
  const meta = await spdMeta();
  const condEditor = spdRuleEditor($("#spd-refrule-rules"), meta, []);
  $("#spd-refrule-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/referral-rules", {
      ...formJson(e.target), conditions: condEditor.value(),
    }, "#spd-refrule-msg");
  };
}

/* ============================================================
 * 9. 考核与积分
 * ==========================================================*/

async function renderSpdAssess() {
  $("#page-desc").textContent =
    "指标库 → 分级考核方案 → 自动取数计分 → 扣分下钻；村医积分与商品兑换核销";
  const [indicators, plans, scores, goods, accounts, pointRules, redeems] =
    await Promise.all([
    api("/api/spd/indicators?limit=50"), api("/api/spd/assess-plans"),
    api("/api/spd/scores?limit=30"), api("/api/spd/goods"),
    api("/api/spd/point-accounts?limit=20"),
    api("/api/spd/point-rules"), api("/api/spd/redeems?limit=30"),
  ]);
  const objectNames = { org: "机构", doctor: "医生", village_doctor: "村医", team: "团队" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>考核指标库</h3>
      <p class="desc">取数口径 + 公式（AST 白名单求值）+ 评分规则三段式，各县只需调权重与目标值</p>
      ${table(["编码", "名称", "对象", "取数口径", "公式", "权重", "目标值", "版本", "状态", "操作"],
        indicators, (i) =>
        `<tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td>
         <td>${esc(objectNames[i.object_type] || i.object_type)}</td>
         <td>${esc(i.data_source)}</td><td><code>${esc(i.formula || "—")}</code></td>
         <td>${i.weight}</td><td>${i.target_value ?? "—"}</td><td>${esc(i.version)}</td>
         <td>${i.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-ind-edit="${i.id}">改档</button>
             <button class="btn secondary" data-ind-use="${i.id}">看引用</button></td></tr>`)}
      <div id="spd-ind-usage"></div></div>
    <div class="panel"><h3>分级考核方案</h3>
      <form class="inline" id="spd-plan-form">
        <input name="code" placeholder="方案编码" required>
        <input name="name" placeholder="方案名称" required>
        <select name="level">
          <option value="hospital">县级医院</option><option value="township">卫生院</option>
          <option value="station">服务站</option><option value="village">村医</option>
          <option value="team">团队</option>
        </select>
        <select name="object_type">
          <option value="org">机构</option><option value="village_doctor">村医</option>
          <option value="doctor">医生</option><option value="team">团队</option>
        </select>
        <input name="items" placeholder="指标:权重，如 followup_rate:40,path_rate:30" style="min-width:260px">
        <button>新建方案</button>
      </form><p class="msg" id="spd-plan-msg"></p>
      ${table(["ID", "编码", "名称", "层级", "对象", "周期", "指标数", "操作"], plans, (p) =>
        `<tr><td>${p.id}</td><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${esc(p.level)}</td><td>${esc(objectNames[p.object_type] || p.object_type)}</td>
         <td>${esc(p.period_type)}</td><td>${(p.items || []).length}</td>
         <td><button class="btn secondary" data-run="${p.id}">跑分</button>
             <button class="btn secondary" data-plan-edit="${p.id}">改档</button></td></tr>`)}</div>
    <div class="panel"><h3>考核结果</h3>
      ${table(["排名", "对象", "周期", "综合得分", "操作"], scores, (s) =>
        `<tr><td>${s.rank}</td><td>${esc(s.object_name)}</td><td>${esc(s.period)}</td>
         <td>${s.total_score}</td>
         <td><button class="btn secondary" data-score="${s.id}">下钻明细</button></td></tr>`)}
      <div id="spd-score-detail"></div></div>
    <div class="panel"><h3>村医积分账户</h3>
      ${table(["用户ID", "姓名", "余额", "累计获得", "累计兑换"], accounts, (a) =>
        `<tr><td>${a.user_id}</td><td>${esc(a.user_name)}</td><td>${a.balance}</td>
         <td>${a.earned}</td><td>${a.used}</td></tr>`)}</div>
    <div class="panel"><h3>积分商品与核销</h3>
      <form class="inline" id="spd-goods-form">
        <input name="code" placeholder="商品编码" required>
        <input name="name" placeholder="商品名称" required>
        <input name="points" type="number" placeholder="所需积分" required>
        <input name="stock" type="number" placeholder="库存" required>
        <button>上架</button>
      </form>
      <form class="inline" id="spd-verify-form" style="margin-top:10px">
        <input name="verify_code" placeholder="核销码" required>
        <button class="secondary">核销</button>
      </form><p class="msg" id="spd-goods-msg"></p>
      ${table(["编码", "名称", "所需积分", "库存", "操作"], goods, (g) =>
        `<tr><td>${esc(g.code)}</td><td>${esc(g.name)}</td><td>${g.points}</td>
         <td>${g.stock}</td>
         <td><button class="btn secondary" data-goods-edit="${g.id}">改档</button>
             ${g.stock > 0
               ? `<button class="btn" data-goods-redeem="${g.id}">兑换</button>`
               : '<span class="tag">已无库存</span>'}</td></tr>`)}
      <p class="desc">兑换记录（核销码给村医，凭码到指定点领取）</p>
      ${table(["ID", "商品", "积分", "核销码", "状态", "时间"], redeems, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.goods_name || r.goods_id)}</td>
         <td>${esc(r.points ?? "—")}</td><td><code>${esc(r.verify_code || "—")}</code></td>
         <td>${r.verified
            ? '<span class="tag green">已核销</span>'
            : '<span class="tag orange">待核销</span>'}</td>
         <td>${esc(String(r.created_at || "").replace("T", " ").slice(0, 16))}</td></tr>`)}</div>
    <div class="panel"><h3>积分规则</h3>
      <p class="desc">按事件给分；<b>日上限 0 表示不限</b>。规则改了只影响之后发生的事件，
        不会回算历史积分——积分是发出去的，回算等于事后改账。</p>
      <form class="inline" id="spd-prule-form">
        <input name="code" placeholder="规则编码" required>
        <input name="name" placeholder="规则名称" required>
        <select name="event">${Object.entries(SPD_POINT_EVENTS).map(([v, t]) =>
          `<option value="${v}">${esc(t)}</option>`).join("")}</select>
        <input name="points" type="number" min="0" max="1000" placeholder="分值" required>
        <input name="daily_limit" type="number" min="0" placeholder="日上限(0=不限)">
        <button>新建规则</button>
      </form><p class="msg" id="spd-prule-msg"></p>
      ${table(["编码", "名称", "事件", "分值", "日上限", "状态", "操作"], pointRules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td>
         <td>${esc(SPD_POINT_EVENTS[r.event] || r.event)}</td>
         <td>${r.points}</td><td>${r.daily_limit || "不限"}</td>
         <td>${r.active === false ? '<span class="tag">停用</span>' : '<span class="tag green">启用</span>'}</td>
         <td><button class="btn secondary" data-prule-edit="${r.id}">改档</button></td></tr>`)}
      <form class="inline" id="spd-signin-form" style="margin-top:10px">
        <button class="btn secondary">我今天签到</button>
      </form></div>
    <div class="panel"><h3>得分分析与工作量</h3>
      <form class="inline" id="spd-ana-form">
        <input name="plan_id" type="number" placeholder="方案ID（分析用）">
        <input name="period" placeholder="周期 2026-08 / 2026-Q3 / 2026"
               value="${esc(new Date().toISOString().slice(0, 7))}" required>
        <select name="object_type"><option value="doctor">医生</option>
          <option value="org">机构</option><option value="village_doctor">村医</option>
          <option value="team">团队</option></select>
        <button class="btn secondary">查得分分析</button>
        <button type="button" class="btn secondary" id="spd-workload-btn">查工作量</button>
      </form><p class="msg" id="spd-ana-msg"></p>
      <div id="spd-ana-result"></div></div>`;
  $("#spd-plan-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target);
    body.items = String(body.items || "").split(/[，,]/).filter(Boolean).map((pair) => {
      const [code, weight] = pair.split(":").map((s) => (s || "").trim());
      return { indicator_code: code, weight: Number(weight || 0) };
    });
    return postAction("/api/spd/assess-plans", body, "#spd-plan-msg");
  };
  $("#spd-goods-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/goods", formJson(e.target, ["points", "stock"]), "#spd-goods-msg");
  };
  $("#spd-verify-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/redeems/verify", formJson(e.target), "#spd-goods-msg");
  };
  $("#spd-prule-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/point-rules",
      formJson(e.target, ["points", "daily_limit"]), "#spd-prule-msg");
  };
  $("#spd-signin-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/point-accounts/signin", null, "#spd-prule-msg");
  };
  $("#spd-ana-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = formJson(e.target);
    if (!f.plan_id) return setMsg("#spd-ana-msg", "得分分析要指定方案ID", false);
    try {
      const q = new URLSearchParams({ plan_id: f.plan_id, period: f.period }).toString();
      const d = await api(`/api/spd/scores-analysis?${q}`);
      const rows = d.items || d.rows || [];
      $("#spd-ana-result").innerHTML =
        `<p class="desc">${esc(f.period)} 得分分析（${rows.length} 条）</p>`
        + table(["对象", "得分", "排名", "薄弱指标"], rows, (x) =>
          `<tr><td>${esc(x.object_name || x.object_id)}</td>
           <td>${esc(x.total_score ?? "—")}</td><td>${esc(x.rank ?? "—")}</td>
           <td>${esc((x.weak_indicators || []).join("、") || "—")}</td></tr>`);
      setMsg("#spd-ana-msg", "", true);
    } catch (err) { setMsg("#spd-ana-msg", err.message, false); }
  };
  $("#spd-workload-btn").onclick = async () => {
    const f = formJson($("#spd-ana-form"));
    try {
      const q = new URLSearchParams({
        period: f.period, object_type: f.object_type }).toString();
      const d = await api(`/api/spd/workload?${q}`);
      const rows = d.items || d.rows || [];
      $("#spd-ana-result").innerHTML =
        `<p class="desc">${esc(f.period)} 工作量（${rows.length} 条）</p>`
        + table(["对象", "随访", "任务", "转诊", "评估", "合计"], rows, (x) =>
          `<tr><td>${esc(x.object_name || x.object_id)}</td>
           <td>${esc(x.followups ?? "—")}</td><td>${esc(x.tasks ?? "—")}</td>
           <td>${esc(x.referrals ?? "—")}</td><td>${esc(x.assessments ?? "—")}</td>
           <td>${esc(x.total ?? "—")}</td></tr>`);
      setMsg("#spd-ana-msg", "", true);
    } catch (err) { setMsg("#spd-ana-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const run = e.target.closest("[data-run]"), score = e.target.closest("[data-score]");
    const el3 = (k) => e.target.closest(`[${k}]`);
    const indEdit = el3("data-ind-edit"), indUse = el3("data-ind-use");
    const planEdit = el3("data-plan-edit"), gEdit = el3("data-goods-edit");
    const gRedeem = el3("data-goods-redeem"), pEdit = el3("data-prule-edit");
    try {
      if (indEdit) {
        const cur = indicators.find((x) => String(x.id) === indEdit.dataset.indEdit) || {};
        const form = await spdModal("改考核指标", [
          { name: "name", label: "名称", value: cur.name || "" },
          { name: "weight", label: "权重", type: "number", value: cur.weight ?? 0 },
          { name: "target_value", label: "目标值", type: "number", value: cur.target_value ?? "" },
          { name: "formula", label: "公式（AST 白名单求值）", value: cur.formula || "" },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }],
            value: cur.active === false ? "0" : "1" },
        ]);
        if (!form) return;
        const body = { name: form.name, formula: form.formula,
          weight: Number(form.weight), active: form.active === "1" };
        if (form.target_value !== "") body.target_value = Number(form.target_value);
        return postAction(`/api/spd/indicators/${indEdit.dataset.indEdit}`,
          body, "#spd-plan-msg", "PATCH");
      }
      if (indUse) {
        const d = await api(`/api/spd/indicators/${indUse.dataset.indUse}/usage`);
        const rows = d.plans || d.items || [];
        // 改指标前先看谁在引它：权重一改，所有引用它的方案得分跟着变
        $("#spd-ind-usage").innerHTML =
          `<p class="desc">指标 #${esc(indUse.dataset.indUse)} 被 ${rows.length} 个方案引用
            —— 改它的权重/公式，这些方案的历史与后续得分口径都会变</p>`
          + table(["方案", "层级", "权重"], rows, (x) =>
            `<tr><td>${esc(x.plan_name || x.name || x.plan_code)}</td>
             <td>${esc(x.level || "—")}</td><td>${esc(x.weight ?? "—")}</td></tr>`);
        return;
      }
      if (planEdit) {
        const cur = plans.find((x) => String(x.id) === planEdit.dataset.planEdit) || {};
        const form = await spdModal("改考核方案", [
          { name: "name", label: "名称", value: cur.name || "" },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }],
            value: cur.active === false ? "0" : "1" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/assess-plans/${planEdit.dataset.planEdit}`,
          { name: form.name, active: form.active === "1" }, "#spd-plan-msg", "PATCH");
      }
      if (gEdit) {
        const cur = goods.find((x) => String(x.id) === gEdit.dataset.goodsEdit) || {};
        const form = await spdModal("改商品", [
          { name: "name", label: "名称", value: cur.name || "" },
          { name: "points", label: "所需积分", type: "number", value: cur.points ?? 0 },
          { name: "stock", label: "库存", type: "number", value: cur.stock ?? 0 },
        ]);
        if (!form) return;
        return postAction(`/api/spd/goods/${gEdit.dataset.goodsEdit}`, {
          name: form.name, points: Number(form.points), stock: Number(form.stock),
        }, "#spd-goods-msg", "PATCH");
      }
      if (gRedeem) {
        if (!confirm("用当前登录账号的积分兑换这件商品？兑换后会生成核销码。")) return;
        return postAction("/api/spd/redeems",
          { goods_id: Number(gRedeem.dataset.goodsRedeem) }, "#spd-goods-msg");
      }
      if (pEdit) {
        const cur = pointRules.find((x) => String(x.id) === pEdit.dataset.pruleEdit) || {};
        const form = await spdModal("改积分规则", [
          { name: "name", label: "名称", value: cur.name || "" },
          { name: "points", label: "分值", type: "number", value: cur.points ?? 1 },
          { name: "daily_limit", label: "日上限(0=不限)", type: "number",
            value: cur.daily_limit ?? 0 },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }],
            value: cur.active === false ? "0" : "1" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/point-rules/${pEdit.dataset.pruleEdit}`, {
          name: form.name, points: Number(form.points),
          daily_limit: Number(form.daily_limit), active: form.active === "1",
        }, "#spd-prule-msg", "PATCH");
      }
    } catch (err) { setMsg("#spd-plan-msg", err.message, false); }
    if (run) {
      const form = await spdModal("跑一次考核计分", [
        { name: "period", label: "考核周期（如 2026-08 / 2026-Q3 / 2026）",
          value: new Date().toISOString().slice(0, 7), required: true },
      ]);
      if (!form || !form.period) return;
      return postAction("/api/spd/scores/run",
        { plan_id: Number(run.dataset.run), period: form.period }, "#spd-plan-msg");
    }
    if (score) {
      const d = await api(`/api/spd/scores/${score.dataset.score}`);
      $("#spd-score-detail").innerHTML = `<div class="panel" style="border-left:4px solid #0b6e6e">
        <h3>${esc(d.object_name)} · ${esc(d.period)} 得分明细（${d.total_score} 分）</h3>
        ${table(["指标", "原始数据", "指标值", "权重", "得分", "扣分", "扣分依据"],
          d.detail, (x) =>
          `<tr><td>${esc(x.indicator_name || x.indicator_code)}</td>
           <td><code>${esc(JSON.stringify(x.metrics || {}))}</code></td>
           <td>${x.value ?? "—"}</td><td>${x.weight ?? "—"}</td><td>${x.score ?? "—"}</td>
           <td>${x.deduction ?? "—"}</td><td>${esc(x.reason || x.error || "—")}</td></tr>`)}</div>`;
    }
  };
}

/* ============================================================
 * 10. 智能随访服务端
 * ==========================================================*/

async function renderSpdFollowup() {
  $("#page-desc").textContent =
    "通用随访能力：方案规则与问卷、多时间点任务生成、多渠道执行、呼叫录音、抽查质控";
  const [rules, questionnaires, stats, calls] = await Promise.all([
    api("/api/spd/followup-rules"), api("/api/spd/questionnaires"),
    api("/api/spd/followup-stats"), api("/api/spd/call-tasks?limit=20"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["随访任务", stats.total], ["已完成", stats.done],
      ["完成率", stats.completion_rate + "%"],
      ["超期", stats.overdue, stats.overdue > 0],
      ["异常随访", stats.abnormal, stats.abnormal > 0],
    ])}
    <div class="panel"><h3>随访方案（诊断/手术/医嘱关键词命中）</h3>
      <p class="desc">没配关键词的方案不匹配任何人——否则一个空方案会给全院出院患者都排上随访</p>
      ${table(["编码", "名称", "场景", "科室", "时间点(天)", "问卷", "执行角色", "预置", "操作"],
        rules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.scene)}</td>
         <td>${esc(r.dept || "—")}</td><td>${(r.points || []).join("、")}</td>
         <td>${esc(r.questionnaire_code || "—")}</td><td>${esc(r.executor_role)}</td>
         <td>${r.preset ? "是" : "否"}</td>
         <td><button class="btn secondary" data-rule-edit="${r.id}">改档</button></td></tr>`)}
      <form class="inline" id="spd-fuplan-form" style="margin-top:10px">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="rule_id">${rules.map((r) => `<option value="${r.id}">${esc(r.name)}</option>`).join("")}</select>
        <input name="base_date" placeholder="基准日 YYYY-MM-DD（出院/手术日）">
        <button>生成随访计划</button>
      </form>
      <form class="inline" id="spd-fumatch-form" style="margin-top:8px">
        <select name="scene">
          <option value="inpatient">出院随访</option><option value="outpatient">门诊随访</option>
          <option value="surgery">术后随访</option><option value="checkup">体检随访</option>
        </select>
        <input name="days" type="number" value="7" placeholder="回溯天数">
        <button class="secondary">按患者特征自动匹配</button>
      </form><p class="msg" id="spd-fu-msg"></p></div>
    <div class="panel"><h3>随访问卷与异常分级</h3>
      ${table(["编码", "名称", "场景", "题目数", "异常规则", "跟踪科室", "处置角色", "操作"],
        questionnaires, (q) =>
        `<tr><td>${esc(q.code)}</td><td>${esc(q.name)}</td><td>${esc(q.scene)}</td>
         <td>${(q.items || []).length}</td><td>${(q.abnormal_rules || []).length}</td>
         <td>${esc(q.track_dept || "—")}</td><td>${esc(q.handle_role)}</td>
         <td><button class="btn secondary" data-quest-edit="${q.id}">改档</button></td></tr>`)}
      <form class="inline" id="spd-quest-form" style="margin-top:10px">
        <input name="code" placeholder="问卷编码" required>
        <input name="name" placeholder="问卷名称" required>
        <select name="scene">
          <option value="inpatient">出院</option><option value="outpatient">门诊</option>
          <option value="surgery">术后</option><option value="checkup">体检</option>
        </select>
        <input name="track_dept" placeholder="跟踪科室">
        <button>新建问卷</button>
      </form>
      <p class="desc">异常判定规则（答案命中任一条即标记异常并派处置任务）</p>
      <div id="spd-quest-rules"></div>
      <p class="msg" id="spd-quest-msg"></p></div>
    <div class="panel"><h3>随访看板</h3>
      <form class="inline" id="spd-fu-filter">
        <select name="status"><option value="">全部状态</option>
          <option value="planned">待随访</option><option value="done">已完成</option>
          <option value="unreachable">失访</option></select>
        <select name="scene"><option value="">全部场景</option>
          <option value="inpatient">出院</option><option value="outpatient">门诊</option>
          <option value="surgery">术后</option><option value="checkup">体检</option></select>
        <label style="font-size:13px"><input type="checkbox" name="overdue" value="true"> 只看超期</label>
        <button class="secondary">查询</button>
      </form>
      <div id="spd-fu-list"></div></div>
    <div class="panel"><h3>随访质量</h3>
      ${barChart(spdPairs(stats.by_abnormal,
        { none: "无异常", low: "轻度", mid: "中度", high: "重度" }),
        { color: "#b26a00", unit: " 例" })}
      ${(stats.by_executor || []).length
        ? barChart(stats.by_executor.map((x) => [x.executor_name || `#${x.executor_id}`, x.done]),
          { color: "#0b6e6e", unit: " 例" })
        : ""}
      <form class="inline" id="spd-qc-form" style="margin-top:10px">
        <input name="dept" placeholder="科室（可留空）">
        <input name="ratio" type="number" step="0.05" value="0.1" placeholder="抽查比例">
        <button class="secondary">生成抽查计划</button>
      </form><p class="msg" id="spd-qc-msg"></p></div>
    <div class="panel"><h3>呼叫任务与录音</h3>
      ${table(["ID", "患者", "号码", "来源", "状态", "时长(秒)", "录音", "创建时间"], calls, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${esc(c.phone || "—")}</td>
         <td>${esc(c.ref_type)}</td>
         <td>${c.status === "connected" ? '<span class="tag green">已接通</span>'
            : c.status === "failed" ? '<span class="tag red">未接通</span>'
            : '<span class="tag orange">待呼叫</span>'}</td>
         <td>${c.duration_s}</td><td>${c.record_url ? "有" : "—"}</td>
         <td>${esc(c.created_at.replace("T", " ").slice(0, 16))}</td>
         <td>${c.status === "pending" || c.status === "dispatched"
            ? `<button class="btn secondary" data-call-res="${c.id}">记结果</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>抽查样本与复核结论</h3>
      <p class="desc">上面「生成抽查计划」只负责抽样，样本抽出来之后要有人复核——
        此前只写不读，抽完就没有下文了。<b>复核结论只有通过/存疑/不通过三档</b>，
        自由文本填在备注里：按结论分桶统计才有管理价值。</p>
      <form class="inline" id="spd-qc-filter">
        <input name="batch" placeholder="批次号">
        <select name="result"><option value="">全部结论</option>
          ${Object.entries(SPD_QC_RESULTS).map(([v, t]) =>
            `<option value="${v}">${esc(t[0])}</option>`).join("")}</select>
        <button class="btn secondary">查询</button>
      </form><p class="msg" id="spd-qc2-msg"></p>
      <div id="spd-qc-list"></div></div>
    <div class="panel"><h3>随访前置资料 / 健康日历</h3>
      <div id="spd-fu-ctx"></div></div>`;

  const drawRecords = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/followup-records?${qs}`);
    $("#spd-fu-list").innerHTML = table(
      ["ID", "患者", "场景", "计划日期", "执行日期", "渠道", "异常", "状态", "操作"],
      rows, (r) =>
      `<tr><td>${r.id}</td><td>${esc(r.patient_name)}</td><td>${esc(r.scene)}</td>
       <td>${esc(r.planned_at)}</td><td>${esc(r.executed_at || "—")}</td>
       <td>${esc(r.channel)}</td>
       <td>${r.abnormal_level && r.abnormal_level !== "none"
          ? `<span class="tag ${r.abnormal_level === "high" ? "red" : "orange"}">${esc(r.abnormal_level)}</span>`
          : "—"}</td>
       <td>${r.status === "done" ? '<span class="tag green">已完成</span>'
          : r.status === "planned" ? '<span class="tag orange">待随访</span>'
          : '<span class="tag">' + esc(r.status) + "</span>"}</td>
       <td><button class="btn secondary" data-fu-ctx="${r.id}">前置资料</button>
           <button class="btn secondary" data-fu-edit="${r.id}">改档</button>
           <button class="btn secondary" data-fu-cal="${r.patient_id}">日历</button>
       ${r.status === "planned"
          ? `<button class="btn secondary" data-fu-exec="${r.id}">执行</button>
             <button class="btn secondary" data-fu-call="${r.id}" data-pid="${r.patient_id}">转呼叫</button>`
          : ""}</td></tr>`);
  };
  await drawRecords();

  $("#spd-fuplan-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/followup-plans",
      formJson(e.target, ["patient_id", "rule_id"]), "#spd-fu-msg");
  };
  $("#spd-fumatch-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/spd/followup-plans/auto-match", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["days"])) });
      alert(`扫描 ${r.scanned} 人，匹配 ${r.matched} 人，生成随访任务 ${r.created} 条`);
      route();
    } catch (err) { setMsg("#spd-fu-msg", err.message, false); }
  };
  $("#spd-fu-filter").onsubmit = async (e) => {
    e.preventDefault();
    await drawRecords(formJson(e.target));
  };
  $("#spd-qc-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/qc-samples/plan", formJson(e.target, ["ratio"]), "#spd-qc-msg");
  };
  $("#page-body").onclick = async (e) => {
    const exec = e.target.closest("[data-fu-exec]"), call = e.target.closest("[data-fu-call]");
    if (exec) {
      const form = await spdModal("执行随访", [
        { name: "channel", label: "随访渠道", type: "select", value: "phone",
          options: [{ value: "phone", label: "电话" }, { value: "wechat", label: "微信" },
                    { value: "sms", label: "短信" }, { value: "visit", label: "面访" }] },
        { name: "result", label: "随访结果", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/followup-records/${exec.dataset.fuExec}/execute`, {
        channel: form.channel || "phone", result: form.result, answers: {},
      }, "#spd-fu-msg");
    }
    if (call) {
      return postAction("/api/spd/call-tasks", {
        patient_id: Number(call.dataset.pid), ref_type: "followup",
        ref_id: Number(call.dataset.fuCall),
      }, "#spd-fu-msg");
    }
    const el4 = (k) => e.target.closest(`[${k}]`);
    const ctx = el4("data-fu-ctx"), fuEdit = el4("data-fu-edit"), cal = el4("data-fu-cal");
    const callRes = el4("data-call-res"), qcRes = el4("data-qc-res");
    const rEdit = el4("data-rule-edit"), qEdit = el4("data-quest-edit");
    try {
      if (ctx) {
        const d = await api(`/api/spd/followup-records/${ctx.dataset.fuCtx}/context`);
        const pt = d.patient || {};
        // 随访前置资料：打电话之前把该知道的一屏看全，省得边问边翻
        $("#spd-fu-ctx").innerHTML =
          `<p class="desc">随访 #${esc(ctx.dataset.fuCtx)} 的前置资料</p>`
          + table(["项", "值"], [
            ["患者", pt.name || ""], ["年龄", pt.age ?? "—"], ["性别", pt.gender || "—"],
            ["近期就诊", (d.encounters || []).length + " 次"],
            ["住院", (d.admissions || []).length + " 次"],
            ["历史随访", (d.history || []).length + " 次"],
          ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`)
          + ((d.history || []).length
            ? table(["日期", "场景", "结果"], d.history, (h) =>
              `<tr><td>${esc(h.executed_at || h.planned_at || "")}</td>
               <td>${esc(h.scene || "")}</td><td>${esc(h.result || "—")}</td></tr>`)
            : "");
        return;
      }
      if (cal) {
        const day = prompt("查看哪一天的健康日历（YYYY-MM-DD，留空=今天）", "");
        if (day === null) return;
        const q = new URLSearchParams({ patient_id: cal.dataset.fuCal });
        if (day) q.set("day", day);
        const d = await api(`/api/spd/health-calendar?${q.toString()}`);
        const items = d.items || [];
        $("#spd-fu-ctx").innerHTML =
          `<p class="desc">患者 #${esc(cal.dataset.fuCal)} ${esc(d.day || day || "今天")} 的安排</p>`
          + (items.length
            ? table(["类型", "内容", "时间"], items, (x) =>
              `<tr><td>${esc(x.kind || "")}</td><td>${esc(x.title || "")}</td>
               <td>${esc(x.at || "—")}</td></tr>`)
            : '<p class="desc">这一天没有安排</p>');
        return;
      }
      if (fuEdit) {
        const form = await spdModal("改随访记录", [
          { name: "planned_at", label: "计划日期 YYYY-MM-DD" },
          { name: "channel", label: "渠道", type: "select", value: "phone",
            options: [{ value: "phone", label: "电话" }, { value: "wechat", label: "微信" },
                      { value: "sms", label: "短信" }, { value: "visit", label: "面访" }] },
          { name: "result", label: "随访结果", type: "textarea" },
        ]);
        if (!form) return;
        // 空串不提交：这些字段都是可选，空串会被当成"要改成空"
        const body = Object.fromEntries(Object.entries(form).filter(([, v]) => v !== ""));
        return postAction(`/api/spd/followup-records/${fuEdit.dataset.fuEdit}`,
          body, "#spd-fu-msg", "PATCH");
      }
      if (callRes) {
        const form = await spdModal("记录呼叫结果", [
          { name: "status", label: "结果", type: "select",
            options: spdOptions(SPD_CALL_RESULTS), value: "connected" },
          { name: "duration_s", label: "通话时长（秒）", type: "number", value: 0 },
          { name: "record_url", label: "录音地址" },
          { name: "result", label: "通话要点", type: "textarea" },
        ]);
        if (!form) return;
        return postAction(`/api/spd/call-tasks/${callRes.dataset.callRes}/result`, {
          status: form.status, duration_s: Number(form.duration_s || 0),
          record_url: form.record_url || "", result: form.result || "",
        }, "#spd-fu-msg");
      }
      if (qcRes) {
        const form = await spdModal("复核结论", [
          { name: "result", label: "结论", type: "select",
            options: spdOptions(SPD_QC_RESULTS), value: "pass" },
          { name: "method", label: "复核方式", type: "select", value: "record",
            options: [{ value: "record", label: "查记录" }, { value: "phone", label: "电话回访" },
                      { value: "wechat", label: "微信核实" }] },
          { name: "note", label: "备注（存疑/不通过务必写清）" },
        ]);
        if (!form) return;
        if (form.result !== "pass" && !(form.note || "").trim()) {
          return setMsg("#spd-qc2-msg", "存疑或不通过必须写明理由——否则这条结论没法复查", false);
        }
        return postAction(`/api/spd/qc-samples/${qcRes.dataset.qcRes}/result`, {
          result: form.result, method: form.method, note: form.note || "",
        }, "#spd-qc2-msg");
      }
      if (rEdit) {
        const form = await spdModal("改随访方案", [
          { name: "name", label: "名称" },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }], value: "1" },
        ]);
        if (!form) return;
        const body = { active: form.active === "1" };
        if (form.name) body.name = form.name;
        return postAction(`/api/spd/followup-rules/${rEdit.dataset.ruleEdit}`,
          body, "#spd-fu-msg", "PATCH");
      }
      if (qEdit) {
        const form = await spdModal("改问卷", [
          { name: "name", label: "名称" },
          { name: "active", label: "状态", type: "select",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }], value: "1" },
        ]);
        if (!form) return;
        const body = { active: form.active === "1" };
        if (form.name) body.name = form.name;
        return postAction(`/api/spd/questionnaires/${qEdit.dataset.questEdit}`,
          body, "#spd-fu-msg", "PATCH");
      }
    } catch (err) { setMsg("#spd-fu-msg", err.message, false); }
  };

  const drawQcSamples = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/qc-samples?${qs}`);
    $("#spd-qc-list").innerHTML = table(
      ["ID", "批次", "随访", "患者", "结论", "方式", "备注", "操作"], rows, (q) =>
      `<tr><td>${q.id}</td><td>${esc(q.batch || "—")}</td>
       <td>${q.record_id ?? "—"}</td><td>${esc(q.patient_name || q.patient_id || "—")}</td>
       <td>${q.result ? spdTag(SPD_QC_RESULTS, q.result) : '<span class="tag orange">待复核</span>'}</td>
       <td>${esc(q.method || "—")}</td><td>${esc(q.note || "—")}</td>
       <td>${q.result ? "—" : `<button class="btn" data-qc-res="${q.id}">记结论</button>`}</td></tr>`);
  };
  await drawQcSamples();
  $("#spd-qc-filter").onsubmit = async (e) => {
    e.preventDefault();
    const q = Object.fromEntries(Object.entries(formJson(e.target)).filter(([, v]) => v));
    try { await drawQcSamples(q); } catch (err) { setMsg("#spd-qc2-msg", err.message, false); }
  };
  const meta = await spdMeta();
  const abnormalEditor = spdRuleEditor($("#spd-quest-rules"), meta, []);
  $("#spd-quest-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/questionnaires", {
      ...formJson(e.target), abnormal_rules: abnormalEditor.value(),
    }, "#spd-quest-msg");
  };
}

/* ============================================================
 * 11. 智能辅助应用端（报告推送）
 * ==========================================================*/

async function renderSpdReport() {
  $("#page-desc").textContent =
    "通用AI辅助：分层报告模板、推送任务调度、多源聚合编排为文本/表格/图表";
  const [templates, tasks, instances] = await Promise.all([
    api("/api/spd/report-templates"), api("/api/spd/report-tasks"),
    api("/api/spd/report-instances?limit=20"),
  ]);
  const scopeNames = { center: "专病中心", dept: "科室团队", grassroots: "基层机构", personal: "个人" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>报告模板</h3>
      <p class="desc">段落取数与工作台同源——报告是同一批数字的另一种排版，不是另存一份统计</p>
      ${table(["编码", "名称", "周期", "层级", "段落", "状态", "操作"], templates, (t) =>
        `<tr><td>${esc(t.code)}</td><td>${esc(t.name)}</td>
         <td>${esc({ daily: "日报", weekly: "周报", monthly: "月报", custom: "自定义" }[t.period] || t.period)}</td>
         <td>${esc(scopeNames[t.scope_level] || t.scope_level)}</td>
         <td>${(t.sections || []).map((s) => esc(s.title)).join("、")}</td>
         <td>${t.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-tpl-edit="${t.id}">改档</button></td></tr>`)}
      <form class="inline" id="spd-rpt-gen" style="margin-top:10px">
        <select name="template_code">${templates.map((t) => `<option value="${esc(t.code)}">${esc(t.name)}</option>`).join("")}</select>
        <input name="org_id" type="number" placeholder="机构ID（留空取本机构）">
        <button>立即生成</button>
      </form><p class="msg" id="spd-rpt-msg"></p></div>
    <div class="panel"><h3>推送任务</h3>
      <form class="inline" id="spd-rpttask-form">
        <select name="template_id">${templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <input name="name" placeholder="任务名称" required>
        <select name="frequency">
          <option value="daily">每日</option><option value="weekly">每周</option>
          <option value="monthly">每月</option><option value="custom">自定义</option>
        </select>
        <input name="push_time" placeholder="推送时间 08:00" value="08:00">
        <input name="priority" type="number" value="1" placeholder="优先级">
        <button>新建推送任务</button>
      </form><p class="msg" id="spd-rpttask-msg"></p>
      ${table(["ID", "任务", "频率", "推送时间", "订阅人数", "优先级", "状态", "最近运行", "操作"],
        tasks, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(t.frequency)}</td>
         <td>${esc(t.push_time)}</td><td>${(t.subscriber_ids || []).length}</td>
         <td>${t.priority}</td>
         <td>${t.status === "active" ? '<span class="tag green">启用</span>' : '<span class="tag orange">暂停</span>'}</td>
         <td>${esc(t.last_run_at ? t.last_run_at.replace("T", " ").slice(0, 16) : "—")}</td>
         <td><button class="btn secondary" data-rpt-run="${t.id}">立即执行</button>
             <button class="btn secondary" data-rpt-toggle="${t.id}" data-s="${t.status === "active" ? "paused" : "active"}">
               ${t.status === "active" ? "暂停" : "启用"}</button></td></tr>`)}</div>
    <div class="panel"><h3>已生成报告</h3>
      ${table(["ID", "标题", "周期", "层级", "生成时间", "操作"], instances, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.title)}</td><td>${esc(r.period_label)}</td>
         <td>${esc(scopeNames[r.scope_level] || r.scope_level)}</td>
         <td>${esc(r.created_at.replace("T", " ").slice(0, 16))}</td>
         <td><button class="btn secondary" data-rpt-view="${r.id}">查看</button></td></tr>`)}
      <div id="spd-rpt-view"></div></div>`;
  $("#spd-rpt-gen").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/report-instances", formJson(e.target, ["org_id"]), "#spd-rpt-msg");
  };
  $("#spd-rpttask-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/report-tasks",
      formJson(e.target, ["template_id", "priority"]), "#spd-rpttask-msg");
  };
  $("#page-body").onclick = async (e) => {
    const run = e.target.closest("[data-rpt-run]");
    const toggle = e.target.closest("[data-rpt-toggle]");
    const view = e.target.closest("[data-rpt-view]");
    const tplEdit = e.target.closest("[data-tpl-edit]");
    if (tplEdit) {
      const form = await spdModal("改报表模板", [
        { name: "name", label: "名称" },
        { name: "active", label: "状态", type: "select",
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }], value: "1" },
      ]);
      if (!form) return;
      // 空串不提交：name 留空表示"不改名字"，不是"改成空名字"
      const body = { active: form.active === "1" };
      if (form.name) body.name = form.name;
      return postAction(`/api/spd/report-templates/${tplEdit.dataset.tplEdit}`,
        body, "#spd-rpt-msg", "PATCH");
    }
    if (run) return postAction("/api/spd/report-instances",
      { task_id: Number(run.dataset.rptRun) }, "#spd-rpttask-msg");
    if (toggle) return postAction(`/api/spd/report-tasks/${toggle.dataset.rptToggle}`,
      { status: toggle.dataset.s }, "#spd-rpttask-msg", "PATCH");
    if (view) {
      const d = await api(`/api/spd/report-instances/${view.dataset.rptView}`);
      const sections = (d.content.sections || []).map((s) => {
        if (s.type === "table") {
          return `<h4>${esc(s.title)}</h4>${table(s.columns || [], s.rows || [],
            (row) => `<tr>${row.map((v) => `<td>${esc(v)}</td>`).join("")}</tr>`)}`;
        }
        if (s.type === "chart") {
          return `<h4>${esc(s.title)}</h4>${barChart(
            (s.series || []).map((x) => [x.label, x.rate]), { unit: "%" })}`;
        }
        return `<h4>${esc(s.title)}</h4><p>${esc(s.text || s.note || "")}</p>`;
      }).join("");
      $("#spd-rpt-view").innerHTML =
        `<div class="panel" style="border-left:4px solid #0b6e6e"><h3>${esc(d.title)}</h3>${sections}</div>`;
    }
  };
}

/* ============================================================
 * 12. 服务团队成员端（基层服务执行端）
 *
 * 对应招标需求「六、服务团队成员端」：#12 监测录入与趋势、#14 量表评估、
 * #8 评估统计、#9/#15 干预模板与批量干预、#7/#16 宣教推送与成效、
 * #11 异常上报与处置。后端（app/spd/routers/care.py）先于本页交付，
 * 本页只消费既有端点，不新增任何接口。
 * ==========================================================*/

const SPD_MEAS_LEVEL = { normal: ["正常", "green"], high: ["偏高", "red"], low: ["偏低", "orange"] };
const SPD_INTV_STATUS = {
  planned: ["计划中", "orange"], doing: ["执行中", ""],
  done: ["已完成", "green"], removed: ["已移除", ""],
};
const SPD_PUSH_STATUS = {
  pending: ["待发送", "orange"], sent: ["已发送", "green"],
  read: ["已读", "green"], failed: ["失败", "red"],
};
const SPD_REPORT_STATUS = {
  pending: ["待处置", "orange"], handling: ["处置中", ""],
  done: ["已处置", "green"], closed: ["已关闭", ""],
};
const SPD_INTV_CATEGORY = { diet: "饮食", exercise: "运动", drug: "用药", psych: "心理", other: "其他" };
const SPD_EDU_CHANNEL = { sms: "短信", wechat: "公众号", app: "居民端" };
const SPD_REPORT_TYPE = { review: "复核", referral: "转诊", followup: "随访", dispose: "处置" };
/* 常用监测指标的输入提示。**不是白名单**——后端收任意 metric 字符串并按
 * spd_targets 判级，这里只做 datalist 提示，别把它升级成 select 限死。 */
const SPD_METRIC_HINTS = ["bp_sys", "bp_dia", "glucose_fasting", "glucose_post",
  "hba1c", "bmi", "spo2", "heart_rate", "weight", "uric_acid"];

async function renderSpdMember() {
  $("#page-desc").textContent =
    "基层服务执行：监测录入与趋势、量表评估与统计、干预模板与批量干预、宣教推送、异常上报";
  const [catalog, templates, materials, interventions, assessStats, eduStats, pushes,
         reportTasks, reports] = await Promise.all([
    spdCatalog(),
    api("/api/spd/intervention-templates"),
    api("/api/spd/edu-materials?limit=100"),
    api("/api/spd/interventions?limit=30"),
    api("/api/spd/assessments/stats"),
    api("/api/spd/edu-pushes/stats"),
    api("/api/spd/edu-pushes?limit=20"),
    api("/api/spd/case-report-tasks?active=true"),
    api("/api/spd/case-reports?limit=30"),
  ]);
  const programOptions = spdProgramOptions(catalog, true);
  $("#page-body").innerHTML = `
    ${panel("监测数据录入（成员端 #12）", `
      <p class="desc">按管理目标即时判级，偏高/偏低自动生成处置任务（3 日内办结）</p>
      <form class="inline" id="spd-meas-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="metric" list="spd-metric-hints" placeholder="指标（如 bp_sys）" required>
        <datalist id="spd-metric-hints">${SPD_METRIC_HINTS.map((m) =>
          `<option value="${m}"></option>`).join("")}</datalist>
        <input name="value" type="number" step="any" placeholder="数值" required>
        <input name="unit" placeholder="单位" style="width:80px">
        <select name="program_code">${programOptions}</select>
        <input name="note" placeholder="备注">
        <button>录入</button>
      </form><p class="msg" id="spd-meas-msg"></p>
      <form class="inline" id="spd-meas-query">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="metric" list="spd-metric-hints" placeholder="指标（趋势必填）">
        <button class="secondary">查记录</button>
        <button class="secondary" type="button" id="spd-meas-trend-btn">看趋势</button>
      </form>
      <div id="spd-meas-result"></div>`)}
    ${panel("量表评估（成员端 #14 / 统计 #8）", `
      <p class="desc">选择患者与已发布量表逐题作答，自动评分、给出风险等级并回写纳管档案</p>
      <form class="inline" id="spd-assess-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="scale_id">${catalog.scales.map((s) =>
          `<option value="${s.id}">${esc(s.name)}（${esc(s.code)}）</option>`).join("")}</select>
        <button>开展评估</button>
      </form><p class="msg" id="spd-assess-msg"></p>
      ${spdCards([["评估人数", assessStats.persons], ["评估人次", assessStats.times]])}
      ${barChart(spdPairs(assessStats.by_risk,
        Object.fromEntries(Object.entries(SPD_RISK).map(([k, v]) => [k, v[0]]))),
        { unit: " 人次" })}
      <div id="spd-assess-list"></div>`)}
    ${panel("干预模板与批量干预（成员端 #15 / 专家端 #9）", `
      <form class="inline" id="spd-intvtpl-form">
        <input name="code" placeholder="模板编码" required>
        <input name="name" placeholder="模板名称" required>
        <select name="program_code">${programOptions}</select>
        <select name="category">${Object.entries(SPD_INTV_CATEGORY).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="content" placeholder="干预内容" required style="min-width:200px">
        <input name="frequency" placeholder="频次（如 每周1次）" style="width:130px">
        <input name="cycle_days" type="number" placeholder="周期天数" style="width:100px">
        <select name="auto_risk_level"><option value="">不自动触发</option>
          ${Object.entries(SPD_RISK).map(([k, v]) =>
            `<option value="${k}">${esc(v[0])}自动触发</option>`).join("")}</select>
        <button>建模板</button>
      </form>
      <form class="inline" id="spd-intv-form" style="margin-top:8px">
        <input name="patient_ids" placeholder="患者ID，逗号分隔批量" required style="min-width:180px">
        <select name="program_code">${programOptions}</select>
        <select name="template_id"><option value="">不引用模板</option>
          ${templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <input name="goal" placeholder="干预目标">
        <input name="next_at" type="date" title="下次干预时间">
        <label style="font-size:13px"><input type="checkbox" name="create_task" checked> 生成任务</label>
        <button>批量下发</button>
      </form><p class="msg" id="spd-intv-msg"></p>
      ${table(["ID", "患者", "目标", "内容", "频次", "下次时间", "状态", "反馈", "操作"],
        interventions, (i) =>
        `<tr><td>${i.id}</td><td>${esc(i.patient_name || i.patient_id)}</td>
         <td>${esc(i.goal || "—")}</td><td>${esc((i.content || "").slice(0, 40))}</td>
         <td>${esc(i.frequency || "—")}</td><td>${esc(i.next_at || "—")}</td>
         <td>${spdTag(SPD_INTV_STATUS, i.status)}</td><td>${esc(i.feedback || "—")}</td>
         <td>${i.status === "removed"
           ? `<button class="btn secondary" data-intv="${i.id}" data-s="planned">恢复</button>`
           : `<button class="btn secondary" data-intv="${i.id}" data-s="done">办结</button>
              <button class="btn secondary" data-intv="${i.id}" data-s="removed">移除</button>`}
         </td></tr>`)}`)}
    ${panel("宣教推送与成效（成员端 #7 / #16）", `
      <p class="desc">立即推送当场走通道（短信/居民端/公众号），定时推送到点派发；失败记原因不假成功</p>
      <form class="inline" id="spd-edu-form">
        <select name="material_id">${materials.filter((m) => m.active).map((m) =>
          `<option value="${m.id}">${esc(m.title)}</option>`).join("")}</select>
        <input name="patient_ids" placeholder="患者ID，逗号分隔" required style="min-width:180px">
        <select name="channel">${Object.entries(SPD_EDU_CHANNEL).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="send_at" placeholder="定时（YYYY-MM-DD HH:MM:SS，留空立即）" style="min-width:230px">
        <button>推送</button>
      </form><p class="msg" id="spd-edu-msg"></p>
      ${spdCards([["覆盖人数", eduStats.covered_patients], ["推送人次", eduStats.push_times],
        ["实际送达", eduStats.sent], ["已读率", `${eduStats.read_rate}%`]])}
      ${table(["素材", "患者", "渠道", "发送时间", "状态", "失败原因"], pushes, (p) =>
        `<tr><td>${esc(p.title)}</td><td>${p.patient_id}</td>
         <td>${esc(SPD_EDU_CHANNEL[p.channel] || p.channel)}</td><td>${esc(p.send_at)}</td>
         <td>${spdTag(SPD_PUSH_STATUS, p.status)}</td><td>${esc(p.fail_reason || "—")}</td></tr>`)}`)}
    ${panel("异常上报与处置（成员端 #11）", `
      <p class="desc">上报同时生成统一任务——"上报了"和"有人在办"是同一件事的两面</p>
      <form class="inline" id="spd-report-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="task_id"><option value="">无对应上报任务</option>
          ${reportTasks.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <select name="report_type">${Object.entries(SPD_REPORT_TYPE).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="content" placeholder="异常情况说明" required style="min-width:220px">
        <button>上报</button>
      </form><p class="msg" id="spd-report-msg"></p>
      ${table(["ID", "患者", "类型", "内容", "触发规则", "状态", "处置意见", "操作"],
        reports, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.patient_name || r.patient_id)}</td>
         <td>${esc(SPD_REPORT_TYPE[r.report_type] || r.report_type)}</td>
         <td>${esc((r.content || "").slice(0, 40))}</td><td>${esc(r.trigger_rule || "—")}</td>
         <td>${spdTag(SPD_REPORT_STATUS, r.status)}</td><td>${esc(r.handle_note || "—")}</td>
         <td>${["pending", "handling"].includes(r.status)
           ? `<button class="btn secondary" data-crpt="${r.id}">处置</button>` : "—"}</td></tr>`)}`)}`;

  $("#spd-meas-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/measurements",
      formJson(e.target, ["patient_id", "value"]), "#spd-meas-msg");
  };
  const measQuery = async () => {
    const body = formJson($("#spd-meas-query"), ["patient_id"]);
    if (!body.patient_id) return;
    const params = new URLSearchParams({ patient_id: body.patient_id, limit: 30 });
    if (body.metric) params.set("metric", body.metric);
    const rows = await api(`/api/spd/measurements?${params}`);
    $("#spd-meas-result").innerHTML = table(
      ["时间", "指标", "数值", "等级", "来源", "备注"], rows, (m) =>
      `<tr><td>${esc(m.measured_at.slice(0, 16))}</td><td>${esc(m.metric)}</td>
       <td>${m.value}${esc(m.unit)}</td><td>${spdTag(SPD_MEAS_LEVEL, m.level)}</td>
       <td>${esc(m.source)}</td><td>${esc(m.note || "—")}</td></tr>`);
  };
  $("#spd-meas-query").onsubmit = (e) => { e.preventDefault(); return measQuery(); };
  $("#spd-meas-trend-btn").onclick = async () => {
    const body = formJson($("#spd-meas-query"), ["patient_id"]);
    if (!body.patient_id || !body.metric) {
      return setMsg("#spd-meas-msg", "看趋势需要同时填患者ID与指标", false);
    }
    const t = await api(`/api/spd/measurements/trend?patient_id=${body.patient_id}`
      + `&metric=${encodeURIComponent(body.metric)}`);
    $("#spd-meas-result").innerHTML = `
      ${barChart(t.points.map((p) => [p.label, p.avg]), { unit: t.latest?.unit || "" })}
      ${table(["时段", "均值", "最低", "最高", "次数"], t.points, (p) =>
        `<tr><td>${esc(p.label)}</td><td>${p.avg}</td><td>${p.min}</td>
         <td>${p.max}</td><td>${p.count}</td></tr>`)}`;
  };
  $("#spd-assess-form").onsubmit = async (e) => {
    e.preventDefault();
    const picked = formJson(e.target, ["patient_id", "scale_id"]);
    const scale = await api(`/api/spd/scales/${picked.scale_id}`);
    /* 逐题构造模态字段：single→下拉；multi→逗号分隔文本；number→数字。
     * 选项 value 用 label 本身——score_scale 就是按 label 查分值表的。 */
    const fields = (scale.items || []).map((item) => {
      if (item.type === "number") {
        return { name: item.key, label: item.title, type: "number" };
      }
      if (item.type === "multi") {
        return { name: item.key, label: `${item.title}（多选，逗号分隔）`,
                 placeholder: (item.options || []).map((o) => o.label).join("/") };
      }
      return { name: item.key, label: item.title, type: "select",
               options: (item.options || []).map((o) => ({ value: o.label, label: o.label })) };
    });
    if (!fields.length) return setMsg("#spd-assess-msg", "该量表没有题目，先去量表配置补齐", false);
    const answersRaw = await spdModal(`${scale.name} · 逐题作答`, fields);
    if (!answersRaw) return;
    const answers = {};
    (scale.items || []).forEach((item) => {
      const v = answersRaw[item.key];
      answers[item.key] = item.type === "multi"
        ? String(v || "").split(/[，,]/).map((s) => s.trim()).filter(Boolean)
        : v;
    });
    try {
      const r = await api("/api/spd/assessments", { method: "POST", body: JSON.stringify({
        patient_id: picked.patient_id, scale_code: scale.code, answers }) });
      /* 不调 route() 刷新整页——那会把这条结果消息一并刷掉。
       * 统计卡片下次进入页面自然更新，当下要紧的是让操作者看到评估结论。 */
      setMsg("#spd-assess-msg",
        `评估完成：${r.score} 分，风险等级 ${SPD_RISK[r.risk_level]?.[0] || r.risk_level || "未分级"}。${r.advice || ""}`);
    } catch (err) { setMsg("#spd-assess-msg", err.message, false); }
  };
  $("#spd-intvtpl-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/intervention-templates",
      formJson(e.target, ["cycle_days"]), "#spd-intv-msg");
  };
  $("#spd-intv-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["template_id"]);
    body.patient_ids = String(body.patient_ids || "").split(/[，,\s]+/)
      .filter(Boolean).map(Number);
    body.create_task = e.target.create_task.checked;
    return postAction("/api/spd/interventions", body, "#spd-intv-msg");
  };
  $("#spd-edu-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["material_id"]);
    body.patient_ids = String(body.patient_ids || "").split(/[，,\s]+/)
      .filter(Boolean).map(Number);
    return postAction("/api/spd/edu-pushes", body, "#spd-edu-msg");
  };
  $("#spd-report-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/case-reports",
      formJson(e.target, ["patient_id", "task_id"]), "#spd-report-msg");
  };
  $("#page-body").addEventListener("click", async (e) => {
    const intv = e.target.closest("[data-intv]");
    const crpt = e.target.closest("[data-crpt]");
    if (intv) {
      const status = intv.dataset.s;
      const body = { status };
      if (status === "done") {
        const form = await spdModal("办结干预", [
          { name: "feedback", label: "患者反馈（可空）", type: "textarea" }]);
        if (!form) return;
        if (form.feedback) body.feedback = form.feedback;
      }
      return postAction(`/api/spd/interventions/${intv.dataset.intv}`, body,
        "#spd-intv-msg", "PATCH");
    }
    if (crpt) {
      const form = await spdModal("处置异常上报", [
        { name: "status", label: "处置结果", type: "select", options: [
          { value: "handling", label: "开始处置" }, { value: "done", label: "处置完成" },
          { value: "closed", label: "关闭（无需处理）" }] },
        { name: "handle_note", label: "处置意见", type: "textarea" }]);
      if (!form) return;
      return postAction(`/api/spd/case-reports/${crpt.dataset.crpt}/handle`, form,
        "#spd-report-msg");
    }
  });
}

/* ============================================================
 * 13. 个案管理师端（专属服务衔接端）
 *
 * 对应招标需求「七、个案管理师端」：#6 在线咨询（会话/回复/病例调阅/转随访）、
 * #9 复诊计划（看板/新增/移除恢复/邀约留痕）、#14 健康处方。
 * 居民发起的咨询在这里被应答——这个页面存在之前，医生工作台的
 * "待回复咨询"计数是一扇没有门的窗。
 * ==========================================================*/

const SPD_CONSULT_STATUS = { open: ["进行中", "orange"], closed: ["已结束", ""] };
const SPD_REVISIT_STATUS = {
  planned: ["已排期", "orange"], done: ["已复诊", "green"],
  overdue: ["已逾期", "red"], removed: ["已移除", ""],
};
const SPD_REMIND_STATUS = { none: ["未提醒", ""], sent: ["已提醒", "orange"], contacted: ["已联系", "green"] };
const SPD_REVISIT_SOURCE = { path: "路径生成", discharge: "出院计划", high_risk: "高危触发", manual: "手工" };

async function renderSpdManager() {
  $("#page-desc").textContent =
    "专属服务衔接：应答居民在线咨询并可转随访、复诊计划看板与邀约留痕、健康处方";
  const [catalog, consults, revisits] = await Promise.all([
    spdCatalog(),
    api("/api/spd/consults?limit=50"),
    api("/api/spd/revisits?limit=50"),
  ]);
  const programOptions = spdProgramOptions(catalog, true);
  $("#page-body").innerHTML = `
    ${panel("在线咨询（个案管理师端 #6）", `
      <p class="desc">居民端发起的专病咨询在此应答；可依据咨询记录一键转随访任务</p>
      ${table(["ID", "患者", "病种", "消息数", "发起时间", "状态", "操作"], consults, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name || c.patient_id)}</td>
         <td>${esc(c.program_code || "—")}</td><td>${c.messages}</td>
         <td>${esc(c.created_at.slice(0, 16))}</td>
         <td>${spdTag(SPD_CONSULT_STATUS, c.status)}</td>
         <td><button class="btn secondary" data-consult="${c.id}">打开会话</button>
          ${c.status === "open"
            ? `<button class="btn secondary" data-consult-close="${c.id}">结束</button>` : ""}
          <button class="btn secondary" data-consult-fu="${c.id}">转随访</button></td></tr>`)}
      <p class="msg" id="spd-consult-msg"></p>
      <div id="spd-consult-thread"></div>`)}
    ${panel("复诊计划看板（个案管理师端 #9 / 智能随访端 #7）", `
      <form class="inline" id="spd-revisit-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${programOptions}</select>
        <input name="plan_date" type="date" required>
        <input name="dept" placeholder="科室" style="width:110px">
        <input name="items" placeholder="复查项目">
        <button>新增计划</button>
      </form>
      <form class="inline" id="spd-revisit-filter" style="margin-top:8px">
        <select name="status"><option value="">全部状态</option>
          ${Object.entries(SPD_REVISIT_STATUS).map(([k, v]) =>
            `<option value="${k}">${esc(v[0])}</option>`).join("")}</select>
        <label style="font-size:13px"><input type="checkbox" name="overdue"> 只看逾期</label>
        <button class="secondary">筛选</button>
      </form><p class="msg" id="spd-revisit-msg"></p>
      <div id="spd-revisit-list">${spdRevisitTable(revisits)}</div>`)}
    ${panel("健康处方（个案管理师端 #14）", `
      <p class="desc">用药 / 康复 / 生活三段至少填一段；居民端「干预」页可见</p>
      <form class="inline" id="spd-rx-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${programOptions}</select>
        <input name="drug_advice" placeholder="用药指导" style="min-width:170px">
        <input name="rehab_advice" placeholder="康复训练" style="min-width:170px">
        <input name="life_advice" placeholder="生活方式建议" style="min-width:170px">
        <button>开具</button>
      </form>
      <form class="inline" id="spd-rx-query" style="margin-top:8px">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <button class="secondary">查该患者的处方</button>
      </form><p class="msg" id="spd-rx-msg"></p>
      <div id="spd-rx-list"></div>`)}`;

  $("#spd-revisit-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/revisits", formJson(e.target, ["patient_id"]), "#spd-revisit-msg");
  };
  $("#spd-revisit-filter").onsubmit = async (e) => {
    e.preventDefault();
    const params = new URLSearchParams({ limit: 50 });
    const status = e.target.status.value;
    if (status) params.set("status", status);
    if (e.target.overdue.checked) params.set("overdue", "true");
    $("#spd-revisit-list").innerHTML = spdRevisitTable(
      await api(`/api/spd/revisits?${params}`));
  };
  $("#spd-rx-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/health-prescriptions",
      formJson(e.target, ["patient_id"]), "#spd-rx-msg");
  };
  $("#spd-rx-query").onsubmit = async (e) => {
    e.preventDefault();
    const pid = Number(e.target.patient_id.value);
    if (!pid) return;
    const rows = await api(`/api/spd/health-prescriptions?patient_id=${pid}`);
    $("#spd-rx-list").innerHTML = table(
      ["时间", "病种", "用药", "康复", "生活", "目标说明"], rows, (r) =>
      `<tr><td>${esc(r.created_at.slice(0, 10))}</td><td>${esc(r.program_code || "—")}</td>
       <td>${esc(r.drug_advice || "—")}</td><td>${esc(r.rehab_advice || "—")}</td>
       <td>${esc(r.life_advice || "—")}</td><td>${esc(r.target_note || "—")}</td></tr>`);
  };
  $("#page-body").addEventListener("click", async (e) => {
    const open = e.target.closest("[data-consult]");
    const close = e.target.closest("[data-consult-close]");
    const followup = e.target.closest("[data-consult-fu]");
    const revisit = e.target.closest("[data-revisit]");
    if (open) return spdShowConsultThread(Number(open.dataset.consult));
    if (close) return postAction(`/api/spd/consults/${close.dataset.consultClose}/close`,
      null, "#spd-consult-msg");
    if (followup) {
      const form = await spdModal("依据咨询发起随访", [
        { name: "title", label: "随访标题", value: "咨询转随访", required: true },
        { name: "due_days", label: "几天内完成", type: "number", value: 7 }]);
      if (!form) return;
      return postAction(`/api/spd/consults/${followup.dataset.consultFu}/to-followup`,
        form, "#spd-consult-msg");
    }
    if (revisit) {
      const action = revisit.dataset.s;
      const body = { status: action };
      if (action === "done") body.actual_date = new Date().toISOString().slice(0, 10);
      if (action === "__remind") {
        delete body.status;
        body.remind_status = "contacted";
        body.note = "电话/微信邀约已联系";
      }
      return postAction(`/api/spd/revisits/${revisit.dataset.revisit}`, body,
        "#spd-revisit-msg", "PATCH");
    }
  });
}

function spdRevisitTable(rows) {
  return table(["ID", "患者", "病种", "计划日期", "科室", "项目", "来源", "状态", "提醒", "操作"],
    rows, (r) =>
    `<tr><td>${r.id}</td><td>${esc(r.patient_name || r.patient_id)}</td>
     <td>${esc(r.program_code || "—")}</td><td>${esc(r.plan_date)}</td>
     <td>${esc(r.dept || "—")}</td><td>${esc(r.items || "—")}</td>
     <td>${esc(SPD_REVISIT_SOURCE[r.source] || r.source)}</td>
     <td>${spdTag(SPD_REVISIT_STATUS, r.status)}</td>
     <td>${spdTag(SPD_REMIND_STATUS, r.remind_status)}</td>
     <td>${r.status === "removed"
       ? `<button class="btn secondary" data-revisit="${r.id}" data-s="planned">恢复</button>`
       : `<button class="btn secondary" data-revisit="${r.id}" data-s="done">已复诊</button>
          <button class="btn secondary" data-revisit="${r.id}" data-s="__remind">已联系</button>
          <button class="btn secondary" data-revisit="${r.id}" data-s="removed">移除</button>`}
     </td></tr>`);
}

async function spdShowConsultThread(consultId) {
  const messages = await api(`/api/spd/consults/${consultId}/messages`);
  $("#spd-consult-thread").innerHTML = `
    <div class="panel" style="border-left:4px solid #0b6e6e">
      <h3>会话 #${consultId}</h3>
      ${messages.map((m) => `<p style="margin:6px 0">
        <span class="tag ${m.sender === "patient" ? "orange" : "green"}">${
          m.sender === "patient" ? "居民" : "医护"}</span>
        ${esc(m.content)}
        <small style="color:#8a939e">${esc(m.created_at.slice(0, 16))}</small></p>`).join("")
        || '<p class="desc">暂无消息</p>'}
      <form class="inline" id="spd-consult-reply">
        <input name="content" placeholder="回复内容" required style="min-width:300px">
        <button>回复</button>
      </form></div>`;
  $("#spd-consult-reply").onsubmit = async (e) => {
    e.preventDefault();
    const content = e.target.content.value.trim();
    if (!content) return;
    try {
      await api(`/api/spd/consults/${consultId}/reply`,
        { method: "POST", body: JSON.stringify({ content }) });
      await spdShowConsultThread(consultId);
    } catch (err) { setMsg("#spd-consult-msg", err.message, false); }
  };
}
