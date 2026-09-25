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

// activeOnly：筛查 / 自动识别 / 建档这类「开新业务」的表单只列启用的病种——停用的病种
// 后端一概拒（P1-89）；筛选栏仍列全部，看历史要用
function spdProgramOptions(catalog, blank, activeOnly) {
  return (blank ? '<option value="">全部病种</option>' : "")
    + catalog.programs.filter((p) => !activeOnly || p.active)
      .map((p) => `<option value="${esc(p.code)}">${esc(p.name)}</option>`).join("");
}

/* ============================================================
 * 共用交互组件（P2-2）：模态表单 + 规则编辑器。
 * build-free 约束不变——纯 DOM，无任何组件库。
 * ==========================================================*/

/* 系统输入框（window.prompt）的替代：Promise 化的浮层表单，一次拿齐多个字段。
 * fields: [{name, label, type: text|number|textarea|select, options, value, placeholder, required}]
 * 确定 resolve(值对象)；取消 / Esc / 点遮罩 resolve(null)——调用方判 null 直接返回，
 * 与 prompt 返回 null 的习惯一致，改造调用点时不用改控制流。
 * opts.intro：表单上方的只读说明（多行纯文本，转义后原样换行）——填之前要先看的参考信息
 * （如处方点评要点）放这里，不必先弹一个 alert 再开表单。 */
function spdModal(title, fields, opts = {}) {
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
      // 数字框必须带 step="any"：不带时浏览器按默认步长 1 校验，1.25、12.80、6.1 一律提交不了，
      // 只弹一句"两个最接近的有效值分别为 1 和 2"——用户只能取整了再填（P1-67）。
      // 整数字段填了小数由后端 422 报人话，校验只有后端一份；移动端的数字框一直是这么写的。
      return `<input name="${esc(f.name)}" type="${f.type === "number" ? "number" : f.type === "password" ? "password" : "text"}"
        value="${esc(val)}" placeholder="${esc(f.placeholder || "")}"${f.type === "number" ? ' step="any"' : ""}${f.required ? " required" : ""}>`;
    };
    // 字段多的表单（如术中记录）会比视口高：遮罩是 fixed 的，页面滚不动，超出的部分连同
    // "确定"按钮就够不着了——表单自己限高并可滚动
    overlay.innerHTML = `<form class="panel" style="min-width:320px;max-width:440px;margin:0;max-height:90vh;overflow-y:auto">
      <h3>${esc(title)}</h3>
      ${opts.intro ? `<div class="desc" style="white-space:pre-wrap;font-size:12px">${esc(opts.intro)}</div>` : ""}
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
 * 用在三处：病种纳入/排除规则、转诊触发规则、患者分组 auto_rule。问卷异常规则不用它——
 * 那里的字段是问卷自己的题目，见下面的 spdAbnormalRuleEditor（P1-122）。 */
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
      return { field, op, value: spdRuleValue(op, row.querySelector(".rule-value").value.trim()) };
    }),
  };
}

// 规则值框里的文字按比较符换成后端要的类型：介于是 [下限, 上限]、属于 / 不属于是列表、存在是布尔，其余能读成数就是数
function spdRuleValue(op, raw) {
  if (op === "between") return raw.split(/[,，]/).map(Number);
  if (op === "in" || op === "not_in") return raw.split(/[,，]/).map((s) => s.trim()).filter(Boolean);
  if (op === "exists") return raw !== "false" && raw !== "否";
  return raw !== "" && !Number.isNaN(Number(raw)) ? Number(raw) : raw;
}

/* 问卷异常判定规则编辑器（P1-122）：字段取自本问卷自己的题目，不是 /api/spd/meta 的事实字段——执行随访时
 * 按题目 key 把作答交给规则求值，引用别的字段永远命中不了。产出后端要的形状 `{when: {field, op, value},
 * level, action}`（原先交的是平铺的条件，后端读不到 when，一条规则都存不进去）。
 * 题目改了调 setFields：只换各行字段下拉的选项、不重画整块——题目框失焦触发 change 的同一刻用户多半正点着
 * 「添加」，整块重画会把这次点击吞掉。某行引用的题目删了，保留原值并标「题目已不在」，交给后端报错，不静默丢规则。 */
function spdAbnormalRuleEditor(el, operators) {
  const LEVELS = [["low", "轻度"], ["mid", "中度"], ["high", "重度"]];
  let fields = [];
  const fieldOptions = (selected) => fields.map((f) =>
    `<option value="${esc(f.key)}"${f.key === selected ? " selected" : ""}>${esc(f.name)}</option>`).join("")
    + (selected && !fields.some((f) => f.key === selected)
      ? `<option value="${esc(selected)}" selected>${esc(selected)}（题目已不在）</option>` : "");
  const rowHtml = () => `<div class="spd-abn-row" style="display:flex;gap:6px;margin:4px 0;flex-wrap:wrap">
    <select class="abn-field">${fieldOptions("")}</select>
    <select class="abn-op">${operators.map((o) => `<option value="${esc(o.key)}">${esc(o.name)}</option>`).join("")}</select>
    <input class="abn-value" style="width:120px" placeholder="值（介于/属于用逗号分隔）">
    <select class="abn-level">${LEVELS.map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
    <input class="abn-action" style="width:200px" placeholder="处置措施（派出任务的标题）">
    <button type="button" class="btn secondary abn-del">删</button></div>`;
  el.innerHTML = `<p class="desc abn-empty">先在上面填题目，再加异常判定规则</p>
    <div class="spd-abn-rows"></div>
    <button type="button" class="btn secondary abn-add">+ 添加异常规则</button>`;
  el.addEventListener("click", (e) => {
    if (e.target.classList.contains("abn-add") && fields.length)
      el.querySelector(".spd-abn-rows").insertAdjacentHTML("beforeend", rowHtml());
    if (e.target.classList.contains("abn-del")) e.target.closest(".spd-abn-row").remove();
  });
  const setFields = (next) => {
    fields = next;
    el.querySelector(".abn-empty").style.display = fields.length ? "none" : "";
    el.querySelector(".abn-add").style.display = fields.length ? "" : "none";
    el.querySelectorAll(".abn-field").forEach((sel) => { sel.innerHTML = fieldOptions(sel.value); });
  };
  setFields([]);
  return {
    setFields,
    value: () => [...el.querySelectorAll(".spd-abn-row")].map((row) => {
      const op = row.querySelector(".abn-op").value;
      return {
        when: { field: row.querySelector(".abn-field").value, op,
                value: spdRuleValue(op, row.querySelector(".abn-value").value.trim()) },
        level: row.querySelector(".abn-level").value,
        action: row.querySelector(".abn-action").value.trim(),
      };
    }),
  };
}

/* 问卷题目的简写（P1-122）：`key:题目:类型:选项1/选项2`，多题用分号隔开；类型 single（默认）/ multi / number，
 * number 不带选项。产出与种子问卷同一形状 `{key, title, type, options: [{label}]}`；key 缺了、重了由后端报。 */
function spdParseQuestionItems(text) {
  return String(text || "").split(/[;；]/).map((s) => s.trim()).filter(Boolean).map((part) => {
    const [key = "", title = "", type = "", opts = ""] = part.split(/[:：]/).map((s) => s.trim());
    const item = { key, title: title || key, type: type || "single" };
    if (item.type !== "number")
      item.options = opts.split(/[/／]/).map((s) => s.trim()).filter(Boolean).map((label) => ({ label }));
    return item;
  });
}

/* 执行随访时逐题作答（P1-122）：按问卷题目生成 spdModal 的字段，name 带前缀，免得与渠道、结果撞名。
 * 数值题用文本框——spdModal 的数字框把空值读成 0，「没答」会被当成答了 0 分。 */
function spdQuestionFields(items, prefix) {
  return (items || []).map((it) => {
    const name = prefix + it.key, label = it.title || it.label || it.key;
    const options = (it.options || []).map((o) => (typeof o === "string" ? o : o.label)).filter(Boolean);
    if (it.type === "number") return { name, label, placeholder: "填数值，不答留空" };
    if (it.type === "multi") return { name, label: `${label}（多选，逗号分隔）` };
    if (options.length) return { name, label, type: "select", value: "",
      options: [{ value: "", label: "（未答）" }, ...options.map((o) => ({ value: o, label: o }))] };
    return { name, label };
  });
}

// 与 spdQuestionFields 配对：没答的题不进 answers（规则按「没有这个字段」处理，不当成答了空串或 0）；
// 多选拆成列表，数值题能读成数就交数
function spdCollectAnswers(items, form, prefix) {
  const answers = {};
  for (const it of items || []) {
    const raw = String(form[prefix + it.key] ?? "").trim();
    if (!raw) continue;
    if (it.type === "multi") answers[it.key] = raw.split(/[,，、]/).map((s) => s.trim()).filter(Boolean);
    else if (it.type === "number" && !Number.isNaN(Number(raw))) answers[it.key] = Number(raw);
    else answers[it.key] = raw;
  }
  return answers;
}

/* ============================================================
 * 1. 平台管理端（运行中枢）
 * ==========================================================*/

async function renderSpdAdmin() {
  $("#page-desc").textContent =
    "运行中枢：超期任务提醒、慢病与专病并行运行状态、配置完备度；病种/量表/服务包/宣教素材/标签/设备/数据源维护与机构树";
  const [wb, catalog, sources, programs, dsm, devices, tags, orgTree, scales, packages, materials] = await Promise.all([
    api("/api/spd/workbench/admin"),
    spdCatalog(true),
    api("/api/spd/data-sources"),
    api("/api/spd/programs?limit=100"),
    api("/api/spd/data-sources-monitor"),
    api("/api/spd/devices?limit=100"),
    api("/api/spd/tags"),
    api("/api/spd/org-tree"),
    api("/api/spd/scales?limit=100"),
    api("/api/spd/service-packages?limit=100"),
    api("/api/spd/edu-materials?limit=100"),
  ]);
  const a = wb.alerts, cfg = wb.config_health, ds = wb.data_sources;
  const orgTreeHtml = (nodes, depth) => (nodes || []).map((n) =>
    `<div style="padding-left:${depth * 18}px;font-size:13.5px">${esc(n.name)}
       <span class="desc">${esc(n.level_name || ORG_TYPES[n.org_type] || n.org_type || "")} · 团队 ${n.team_count ?? 0} · 在管 ${n.enrolled ?? 0}</span></div>`
    + orgTreeHtml(n.children, depth + 1)).join("");
  // ADR-0009 第六批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 提醒面板的红色左边框走 `accent`，"有待处理才渲染"的条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${(a.overdue_tasks || a.overdue_followups || a.pending_review_screenings)
      ? panel("⚠ 待处理提醒", `
         <p style="font-size:13.5px">
           <span class="tag red" style="margin-right:8px">超期任务 ${a.overdue_tasks}</span>
           <span class="tag red" style="margin-right:8px">超期随访 ${a.overdue_followups}</span>
           <span class="tag orange" style="margin-right:8px">待复核筛查 ${a.pending_review_screenings}</span>
           <span class="tag orange" style="margin-right:8px">待受理申请 ${a.pending_applies}</span>
           <span class="tag orange">待确认迁出 ${a.pending_migrations}</span></p>
         <p class="desc" style="font-size:12.5px">本次刷新已扫描并置超期 ${a.swept.overdue} 条、升级 ${a.swept.escalated} 条</p>`,
        { accent: "#c62828" })
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
    ${panel("慢病 / 专病并行运行（共用底座，分别统计）",
      table(["业务线", "病种数", "在管人数"], [
        ["慢病管理", wb.parallel_tracks.chronic.programs, wb.parallel_tracks.chronic.enrolled],
        ["专病管理", wb.parallel_tracks.specialty.programs, wb.parallel_tracks.specialty.enrolled],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${r[1]}</td><td>${r[2]}</td></tr>`))}
    ${panel("配置完备度",
      table(["配置项", "数量", "说明"], [
        ["专病档案（启用/全部）", `${cfg.active_programs}/${cfg.programs}`, "病种定义与纳入排除规则"],
        ["已发布路径", cfg.published_paths, `草稿 ${cfg.draft_paths} 个`],
        ["已发布量表", cfg.published_scales, "风险/阶段/康复/筛查"],
        ["未配纳入规则的病种", (cfg.programs_without_rules || []).join("、") || "无",
          "缺规则则无法自动识别患者"],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td><td>${esc(r[2])}</td></tr>`))}
    ${panel("专病档案配置", `
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
      ${table(["ID", "编码", "名称", "口径", "版本", "阶段数", "纳入规则", "状态", "操作"],
        programs, (p) =>
        `<tr><td>${p.id}</td><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${p.category === "chronic" ? "慢病" : "专病"}</td>
         <td>${esc(p.version || "")}</td><td>${(p.stages || []).length}</td>
         <td>${(cfg.programs_without_rules || []).includes(p.code)
            ? '<span class="tag red">未配置</span>' : '<span class="tag green">已配置</span>'}</td>
         <td>${p.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-prog-edit="${p.id}" data-name="${esc(p.name)}" data-dept="${esc(p.lead_dept || "")}">编辑</button>
             <button class="btn secondary" data-prog-versions="${p.id}">版本</button>
             <button class="btn secondary" data-prog-targets="${p.id}">管理目标</button></td></tr>`)}
      <div id="spd-cfg-detail"></div>`)}
    ${panel("评估量表", `
      <p class="desc">发布后生成扫码自评令牌并进入评估下拉；停用即从下拉里消失，历史评估照常可查</p>
      ${table(["ID", "编码", "名称", "类别", "病种", "版本", "状态", "操作"], scales, (sc) =>
        `<tr><td>${sc.id}</td><td>${esc(sc.code)}</td><td>${esc(sc.name)}</td>
         <td>${esc(SPD_SCALE_CATEGORY[sc.category] || sc.category)}</td><td>${esc(sc.program_code || "—")}</td>
         <td>${esc(sc.version)}</td><td>${spdTag(SPD_SCALE_STATUS, sc.status)}</td>
         <td>${sc.status === "published"
           ? `<button class="btn secondary" data-scale-qr="${sc.id}">二维码</button>
              <button class="btn secondary" data-scale-off="${sc.id}">停用</button>`
           : `<button class="btn secondary" data-scale-pub="${sc.id}">发布</button>`}</td></tr>`)}
      <p class="msg" id="spd-scale-msg"></p>`)}
    ${panel("服务包", `
      <form class="inline" id="spd-package-form">
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="program_code">${spdProgramOptions(catalog)}</select>
        <input name="price" type="number" step="any" placeholder="价格(元)" style="width:100px">
        <input name="period_days" type="number" placeholder="有效期(天)" style="width:110px">
        <input name="items" placeholder="项目：编码:名称:次数，分号分隔" style="min-width:240px">
        <button>新建服务包</button>
      </form><p class="msg" id="spd-package-msg"></p>
      ${table(["ID", "编码", "名称", "病种", "价格", "有效期(天)", "项目数", "状态", "操作"], packages, (k) =>
        `<tr><td>${k.id}</td><td>${esc(k.code)}</td><td>${esc(k.name)}</td><td>${esc(k.program_code || "—")}</td>
         <td>${k.price}</td><td>${k.period_days}</td><td>${(k.items || []).length}</td>
         <td>${k.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-pkg-edit="${k.id}" data-name="${esc(k.name)}"
              data-price="${k.price}" data-days="${k.period_days}" data-active="${k.active ? 1 : 0}">编辑</button></td></tr>`)}`)}
    ${panel("宣教素材", `
      ${table(["ID", "编码", "标题", "病种", "形式", "科室", "状态", "操作"], materials, (m) =>
        `<tr><td>${m.id}</td><td>${esc(m.code)}</td><td>${esc(m.title)}</td><td>${esc(m.program_code || "—")}</td>
         <td>${esc(m.media_type_name)}</td><td>${esc(m.dept || "—")}</td>
         <td>${m.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-edu-edit="${m.id}" data-title="${esc(m.title)}" data-dept="${esc(m.dept || "")}"
              data-url="${esc(m.media_url || "")}" data-active="${m.active ? 1 : 0}">编辑</button></td></tr>`)}
      <p class="msg" id="spd-edu-msg"></p>`)}
    ${panel("标签字典", `
      <form class="inline" id="spd-tag-form">
        <input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="category"><option value="patient">患者标签</option><option value="risk">风险标签</option>
          <option value="service">服务标签</option></select>
        <input name="color" placeholder="颜色（如 red）" style="width:120px">
        <button>新建标签</button>
      </form><p class="msg" id="spd-tag-msg"></p>
      ${table(["ID", "编码", "名称", "类别", "颜色"], tags, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.code)}</td><td>${esc(t.name)}</td><td>${esc(t.category_name)}</td>
         <td>${esc(t.color || "—")}</td></tr>`)}`)}
    ${panel("设备台账", `
      <form class="inline" id="spd-device-form">
        <input name="sn" placeholder="设备序列号" required>
        <select name="device_type">${Object.entries(SPD_DEVICE_TYPES).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="model" placeholder="型号">
        <input name="org_id" type="number" placeholder="归属机构ID">
        <button>登记设备</button>
      </form><p class="msg" id="spd-device-msg"></p>
      ${table(["ID", "序列号", "类型", "型号", "机构", "绑定患者", "状态", "最近同步", "操作"], devices, (d) =>
        `<tr><td>${d.id}</td><td>${esc(d.sn)}</td><td>${esc(SPD_DEVICE_TYPES[d.device_type] || d.device_type)}</td>
         <td>${esc(d.model || "—")}</td><td>${d.org_id ?? "—"}</td><td>${d.bound_patient_id ?? "—"}</td>
         <td>${d.status === "bound" ? '<span class="tag green">已绑定</span>' : '<span class="tag">空闲</span>'}</td>
         <td>${esc(d.last_sync_at ? d.last_sync_at.replace("T", " ").slice(0, 16) : "—")}</td>
         <td><button class="btn secondary" data-dev-bind="${d.id}">${d.bound_patient_id ? "换绑 / 解绑" : "绑定患者"}</button></td></tr>`)}`)}
    ${panel("机构树（县—乡—村）", `
      <p class="desc">患者归属、任务派发、逐级转诊、团队授权与考核共用这一棵树；数字是各机构的活跃团队数与在管人数</p>
      ${orgTreeHtml(orgTree, 0) || '<p class="desc">暂无机构</p>'}`)}
    ${panel("数据源接入与运行监控", `
      <p class="desc">成功率按最近 100 次同步计算；超过 24 小时未同步计入陈旧</p>
      ${spdCards([["数据源", ds.total], ["异常", ds.failed, ds.failed > 0],
                  ["延迟", ds.delayed, ds.delayed > 0], ["24h未同步", ds.stale_over_24h, ds.stale_over_24h > 0],
                  ["平均成功率", ds.avg_success_rate + "%"]])}
      <p class="desc">监控接口：${Object.entries(dsm.by_status || {}).map(([k, v]) => `${esc(k)} ${v}`).join("，") || "无状态记录"}；
        陈旧 ${(dsm.stale_over_24h || []).length} 个，平均成功率 ${dsm.avg_success_rate}%</p>
      ${table(["ID", "编码", "名称", "类型", "频率(分)", "最近同步", "行数", "延迟(ms)", "成功率", "状态", "操作"],
        sources, (src) =>
        `<tr><td>${src.id}</td><td>${esc(src.code)}</td><td>${esc(src.name)}</td><td>${esc(src.source_type)}</td>
         <td>${src.freq_minutes}</td><td>${esc(src.last_sync_at ? src.last_sync_at.replace("T", " ").slice(0, 16) : "—")}</td>
         <td>${src.last_rows}</td><td>${src.last_latency_ms}</td><td>${src.success_rate}%</td>
         <td>${src.status === "running" ? '<span class="tag green">正常</span>'
            : src.status === "delayed" ? '<span class="tag orange">延迟</span>'
            : '<span class="tag red">异常</span>'}</td>
         <td><button class="btn secondary" data-ds-edit="${src.id}" data-name="${esc(src.name)}"
              data-freq="${src.freq_minutes}" data-active="${src.active === false ? 0 : 1}">编辑</button>
             <button class="btn secondary" data-ds-logs="${src.id}">同步日志</button>
             <button class="btn secondary" data-ds-sync="${src.id}">记一次同步</button></td></tr>`)}
      <p class="msg" id="spd-ds-msg"></p>
      <div id="spd-ds-detail"></div>`)}`;
  const showTargets = async (programId) => {
    const box = $("#spd-cfg-detail");
    try {
      const rows = await api(`/api/spd/programs/${programId}/targets`);
      box.innerHTML = panel(`管理目标 · 病种 #${programId}`, `
        <form class="inline" id="spd-target-form">
          <input name="stage" placeholder="阶段（可留空）" style="width:120px">
          <input name="metric" placeholder="指标编码（如 bp_sys）" required>
          <input name="metric_name" placeholder="指标名称">
          <select name="kind"><option value="quantitative">定量</option><option value="qualitative">定性</option></select>
          <input name="target_low" type="number" step="any" placeholder="下限" style="width:90px">
          <input name="target_high" type="number" step="any" placeholder="上限" style="width:90px">
          <input name="unit" placeholder="单位" style="width:80px">
          <input name="qualitative" placeholder="定性目标描述">
          <button>新增目标</button>
        </form>
        ${table(["ID", "阶段", "指标", "名称", "类型", "下限", "上限", "单位", "定性", "操作"], rows, (t) =>
          `<tr><td>${t.id}</td><td>${esc(t.stage || "—")}</td><td>${esc(t.metric)}</td><td>${esc(t.metric_name || "—")}</td>
           <td>${t.kind === "qualitative" ? "定性" : "定量"}</td><td>${t.target_low ?? "—"}</td><td>${t.target_high ?? "—"}</td>
           <td>${esc(t.unit || "—")}</td><td>${esc(t.qualitative || "—")}</td>
           <td><button class="btn secondary" data-target-edit="${t.id}" data-prog="${programId}"
                data-name="${esc(t.metric_name || "")}" data-unit="${esc(t.unit || "")}">编辑</button></td></tr>`)}`);
      $("#spd-target-form").onsubmit = async (e) => {
        e.preventDefault();
        const body = formJson(e.target, ["target_low", "target_high"]);
        try {
          await api(`/api/spd/programs/${programId}/targets`, { method: "POST", body: JSON.stringify(body) });
          await showTargets(programId);
          setMsg("#spd-program-msg", "管理目标已新增");
        } catch (err) { setMsg("#spd-program-msg", err.message, false); }
      };
    } catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  // 监听与首屏 innerHTML 同一个同步块（P2-31）；病种表单例外见下方注释
  $("#spd-package-form").onsubmit = (e) => {
    e.preventDefault();
    const f = formJson(e.target, ["price", "period_days"]);
    // "编码:名称:次数;…" → items；后端 _bind_package 读的是 code/name/times
    f.items = String(f.items || "").split(/[;；]/).map((x) => x.trim()).filter(Boolean).map((x) => {
      const [code, name, times] = x.split(/[:：]/);
      return { code: (code || "").trim(), name: (name || code || "").trim(), times: Number(times) || 1 };
    });
    return postAction("/api/spd/service-packages", f, "#spd-package-msg");
  };
  $("#spd-tag-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/tags", formJson(e.target), "#spd-tag-msg");
  };
  $("#spd-device-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/devices", formJson(e.target, ["org_id"]), "#spd-device-msg");
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const progEdit = el("data-prog-edit"), progVersions = el("data-prog-versions"), progTargets = el("data-prog-targets");
    const targetEdit = el("data-target-edit");
    const scalePub = el("data-scale-pub"), scaleOff = el("data-scale-off"), scaleQr = el("data-scale-qr");
    const pkgEdit = el("data-pkg-edit"), eduEdit = el("data-edu-edit"), devBind = el("data-dev-bind");
    const dsEdit = el("data-ds-edit"), dsLogs = el("data-ds-logs"), dsSync = el("data-ds-sync");
    if (progEdit) {
      const form = await spdModal("编辑病种（规则请用下方编辑器新建版本）", [
        { name: "name", label: "名称", value: progEdit.dataset.name, required: true },
        { name: "lead_dept", label: "牵头科室", value: progEdit.dataset.dept },
        { name: "description", label: "说明", type: "textarea" },
      ]);
      if (!form) return;
      const body = {};
      if (form.name) body.name = form.name;
      body.lead_dept = form.lead_dept || "";
      if (form.description) body.description = form.description;
      return postAction(`/api/spd/programs/${progEdit.dataset.progEdit}`, body, "#spd-program-msg", "PATCH");
    }
    if (progVersions) {
      try {
        const rows = await api(`/api/spd/programs/${progVersions.dataset.progVersions}/versions`);
        $("#spd-cfg-detail").innerHTML = panel(`版本历史 · 病种 #${progVersions.dataset.progVersions}`,
          table(["版本", "修改人", "说明", "时间"], rows, (v) =>
            `<tr><td>${esc(v.version)}</td><td>${esc(v.changed_by || "—")}</td><td>${esc(v.note || "—")}</td>
             <td>${esc((v.created_at || "").replace("T", " ").slice(0, 16))}</td></tr>`));
      } catch (err) { setMsg("#spd-program-msg", err.message, false); }
      return;
    }
    if (progTargets) return showTargets(progTargets.dataset.progTargets);
    if (targetEdit) {
      const form = await spdModal("编辑管理目标（留空的项不改）", [
        { name: "metric_name", label: "指标名称", value: targetEdit.dataset.name },
        { name: "target_low", label: "下限", type: "number" },
        { name: "target_high", label: "上限", type: "number" },
        { name: "unit", label: "单位", value: targetEdit.dataset.unit },
        { name: "qualitative", label: "定性目标描述" },
      ]);
      if (!form) return;
      const body = {};
      if (form.metric_name) body.metric_name = form.metric_name;
      if (form.target_low) body.target_low = form.target_low;
      if (form.target_high) body.target_high = form.target_high;
      if (form.unit) body.unit = form.unit;
      if (form.qualitative) body.qualitative = form.qualitative;
      try {
        await api(`/api/spd/targets/${targetEdit.dataset.targetEdit}`, { method: "PATCH", body: JSON.stringify(body) });
        await showTargets(targetEdit.dataset.prog);
        setMsg("#spd-program-msg", "管理目标已更新");
      } catch (err) { setMsg("#spd-program-msg", err.message, false); }
      return;
    }
    if (scalePub) return postAction(`/api/spd/scales/${scalePub.dataset.scalePub}/publish`, null, "#spd-scale-msg");
    if (scaleOff) return postAction(`/api/spd/scales/${scaleOff.dataset.scaleOff}/disable`, null, "#spd-scale-msg");
    if (scaleQr) {
      try { await spdOpenSvg(`/api/spd/scales/${scaleQr.dataset.scaleQr}/qr.svg`); }
      catch (err) { setMsg("#spd-scale-msg", err.message, false); }
      return;
    }
    if (pkgEdit) {
      const form = await spdModal("编辑服务包", [
        { name: "name", label: "名称", value: pkgEdit.dataset.name, required: true },
        { name: "price", label: "价格（元）", type: "number", value: pkgEdit.dataset.price },
        { name: "period_days", label: "有效期（天）", type: "number", value: pkgEdit.dataset.days },
        { name: "active", label: "状态", type: "select", value: pkgEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/service-packages/${pkgEdit.dataset.pkgEdit}`, {
        name: form.name, price: form.price, period_days: form.period_days || 365, active: form.active === "1",
      }, "#spd-package-msg", "PATCH");
    }
    if (eduEdit) {
      const form = await spdModal("编辑宣教素材", [
        { name: "title", label: "标题", value: eduEdit.dataset.title, required: true },
        { name: "dept", label: "科室", value: eduEdit.dataset.dept },
        { name: "media_url", label: "资料链接", value: eduEdit.dataset.url },
        { name: "active", label: "状态", type: "select", value: eduEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/edu-materials/${eduEdit.dataset.eduEdit}`, {
        title: form.title, dept: form.dept || "", media_url: form.media_url || "", active: form.active === "1",
      }, "#spd-edu-msg", "PATCH");
    }
    if (devBind) {
      const form = await spdModal("绑定 / 解绑设备", [
        { name: "patient_id", label: "患者ID（填 0 或留空 = 解绑）", type: "number" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/devices/${devBind.dataset.devBind}/bind`,
        { patient_id: form.patient_id || null }, "#spd-device-msg");
    }
    if (dsEdit) {
      const form = await spdModal("编辑数据源（管理员）", [
        { name: "name", label: "名称", value: dsEdit.dataset.name, required: true },
        { name: "endpoint", label: "接入地址（留空不改）" },
        { name: "freq_minutes", label: "同步频率（分钟）", type: "number", value: dsEdit.dataset.freq },
        { name: "scope", label: "数据范围说明（留空不改）" },
        { name: "active", label: "状态", type: "select", value: dsEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      const body = { name: form.name, active: form.active === "1" };
      if (form.freq_minutes) body.freq_minutes = form.freq_minutes;
      if (form.endpoint) body.endpoint = form.endpoint;
      if (form.scope) body.scope = form.scope;
      return postAction(`/api/spd/data-sources/${dsEdit.dataset.dsEdit}`, body, "#spd-ds-msg", "PATCH");
    }
    if (dsLogs) {
      try {
        const rows = await api(`/api/spd/data-sources/${dsLogs.dataset.dsLogs}/sync-logs?limit=50`);
        $("#spd-ds-detail").innerHTML = panel(`同步日志 · 数据源 #${dsLogs.dataset.dsLogs}`,
          table(["开始时间", "行数", "延迟(ms)", "结果", "说明"], rows, (l) =>
            `<tr><td>${esc((l.started_at || "").replace("T", " ").slice(0, 19))}</td><td>${l.rows}</td><td>${l.latency_ms}</td>
             <td>${l.success ? '<span class="tag green">成功</span>' : '<span class="tag red">失败</span>'}</td>
             <td>${esc(l.message || "—")}</td></tr>`));
      } catch (err) { setMsg("#spd-ds-msg", err.message, false); }
      return;
    }
    if (dsSync) {
      const form = await spdModal("登记一次同步结果（接口方回报 / 手工补录）", [
        { name: "rows", label: "同步行数", type: "number", value: 0 },
        { name: "latency_ms", label: "耗时（毫秒）", type: "number", value: 0 },
        { name: "success", label: "结果", type: "select", value: "1",
          options: [{ value: "1", label: "成功" }, { value: "0", label: "失败" }] },
        { name: "message", label: "说明（失败原因等）" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/data-sources/${dsSync.dataset.dsSync}/sync-logs`, {
        rows: form.rows || 0, latency_ms: form.latency_ms || 0, success: form.success === "1", message: form.message || "",
      }, "#spd-ds-msg");
    }
  };
  // P2-31 例外：下面的 onsubmit 闭包依赖 meta 构建的两个规则编辑器，提前挂会把窗口期提交从
  // 「兜底无效」变成「TypeError」，非零行为差；窗口期由 shared.js 的 document 层兜底护住。
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
    ${panel("县乡村三级服务能力",
      table(["层级", "机构数", "在管患者", "服务团队"],
        Object.entries(wb.by_level), ([level, v]) =>
        `<tr><td>${esc(level)}</td><td>${v.orgs}</td><td>${v.enrolled}</td><td>${v.teams}</td></tr>`))}
    ${panel("病种分布（在管）",
      barChart(spdPairs(region.by_program, names), { color: "#0b6e6e", unit: " 人" }))}
    ${panel("风险分层",
      barChart(spdPairs(region.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#b26a00", unit: " 人" }))}
    ${panel("年龄结构",
      barChart(Object.entries(region.age_distribution), { color: "#0a4d78", unit: " 人" }))}
    ${panel("分级诊疗与转诊",
      table(["指标", "数值"], [
        ["转诊总量", wb.referrals.total], ["在途", wb.referrals.open],
        ["已闭环", wb.referrals.closed], ["闭环率", wb.referrals.closure_rate + "%"],
        ["有效上转就诊", wb.referrals.effective_visits],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${esc(r[1])}</td></tr>`))}
    ${panel("重点慢专病中心运行",
      table(["编码", "名称", "病种", "适用机构", "团队", "状态"], wb.centers, (x) =>
        `<tr><td>${esc(x.code)}</td><td>${esc(x.name)}</td>
         <td>${esc(names[x.program_code] || x.program_code)}</td>
         <td>${x.orgs}</td><td>${x.teams}</td>
         <td><span class="tag ${x.status === "running" ? "green" : "orange"}">${esc(x.status_name)}</span></td></tr>`))}
    ${panel("考核结果排名",
      table(["排名", "考核对象", "周期", "综合得分"], wb.scores, (s) =>
        `<tr><td>${s.rank}</td><td>${esc(s.object_name)}</td><td>${esc(s.period)}</td>
         <td>${s.total_score}</td></tr>`))}`;
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
    ${panel("病种标准落地情况", `
      <p class="desc">纳入规则、管理阶段、路径模板、量表——任一缺失都会让基层"没有可执行的规则"</p>
      ${table(["病种", "口径", "版本", "纳入规则", "阶段", "路径模板", "已发布", "量表", "在管"],
        wb.programs, (p) =>
        `<tr><td>${esc(p.program_name)}</td>
         <td>${p.category === "chronic" ? "慢病" : "专病"}</td><td>${esc(p.version)}</td>
         <td>${p.has_include_rules ? '<span class="tag green">已配</span>' : '<span class="tag red">缺</span>'}</td>
         <td>${p.stages}</td><td>${p.path_templates}</td>
         <td>${p.published_paths ? `<span class="tag green">${p.published_paths}</span>`
            : '<span class="tag red">0</span>'}</td>
         <td>${p.scales}</td><td>${p.enrolled}</td></tr>`)}`)}
    ${panel("重点慢专病中心", `
      ${table(["ID", "名称", "病种", "牵头科室", "版本", "状态", "操作"], wb.centers, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.program_code)}</td><td>${esc(c.lead_dept)}</td>
         <td>${esc(c.version)}</td><td>${esc(c.status_name)}</td>
         <td><button class="btn secondary" data-center-edit="${c.id}" data-name="${esc(c.name)}"
              data-dept="${esc(c.lead_dept || "")}" data-version="${esc(c.version || "")}" data-status="${esc(c.status || "")}">编辑</button></td></tr>`)}
      <form class="inline" id="spd-center-form" style="margin-top:10px">
        <input name="code" placeholder="中心编码" required>
        <input name="name" placeholder="中心名称" required>
        <input name="program_code" placeholder="病种编码" required>
        <input name="lead_dept" placeholder="牵头科室">
        <button>新建分中心</button>
      </form><p class="msg" id="spd-center-msg"></p>`)}
    ${panel("风险评估结果分布", `
      ${barChart(spdPairs(wb.assessments.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#8d4bab", unit: " 人次" })}`)}`;
  $("#spd-center-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/centers", formJson(e.target), "#spd-center-msg");
  };
  $("#page-body").onclick = async (e) => {
    const btn = e.target.closest("[data-center-edit]");
    if (!btn) return;
    const form = await spdModal("编辑专病中心", [
      { name: "name", label: "名称", value: btn.dataset.name, required: true },
      { name: "lead_dept", label: "牵头科室", value: btn.dataset.dept },
      { name: "version", label: "版本", value: btn.dataset.version },
      { name: "status", label: "状态", type: "select", value: btn.dataset.status || "running",
        options: [{ value: "draft", label: "筹建" }, { value: "running", label: "运行中" }, { value: "paused", label: "暂停" }] },
      { name: "leader_user_id", label: "负责人用户ID（留空不改）", type: "number" },
    ]);
    if (!form) return;
    const body = { name: form.name, lead_dept: form.lead_dept || "", version: form.version || "", status: form.status };
    if (form.leader_user_id) body.leader_user_id = form.leader_user_id;
    return postAction(`/api/spd/centers/${btn.dataset.centerEdit}`, body, "#spd-center-msg", "PATCH");
  };
}

const SPD_ENROLL_STATUS = {
  active: "在管", excluded: "已排除", migrated: "已迁出", dead: "已死亡", recalled: "召回中",
};
const SPD_CAND_STATUS = { suspect: "疑似", target: "目标", excluded: "已排除", enrolled: "已纳管" };
const SPD_RECALL_STATUS = {
  pending: ["待联系", "orange"], contacted: ["已联系", ""], returned: ["已召回", "green"], failed: ["召回失败", "red"],
};
const SPD_APPLY_STATUS = { pending: ["待受理", "orange"], accepted: ["已受理", "green"], rejected: ["已拒绝", ""] };

/** 专病 360 档案（GET /api/spd/patients/{id}/profile）：纳管、路径、服务包、任务、监测、评估、
    转诊一屏聚合。中心端与纳管页共用（个案管理师端 #7 / 中心端 #8）。 */
function spdProfileHtml(p) {
  const pt = p.patient || {};
  const programs = (p.programs || []).map((g) => {
    const e = g.enrollment || {};
    return `<div style="margin:8px 0;padding:8px;border:1px solid #e5e7eb;border-radius:6px">
      <b>${esc(g.program_name || e.program_code || "")}</b>
      ${spdTag(SPD_RISK, e.risk_level)} <span class="tag">${esc(SPD_ENROLL_STATUS[e.status] || e.status || "")}</span>
      阶段 ${esc(e.stage || "—")} · 待办 ${g.open_tasks ?? 0} · 下次随访 ${esc(e.next_followup_at || "—")}
      <div class="desc">路径：${(g.paths || []).map((i) =>
        `#${i.id} ${esc(i.template_code)} ${esc(i.current_node_key || "—")} ${i.progress}%`).join("；") || "—"}</div>
      <div class="desc">服务包：${(g.packages || []).map((b) =>
        `${esc(b.package_name)} 余 ${b.remaining}（已用 ${b.usage_rate}%）`).join("；") || "—"}</div>
      <div class="desc">近期任务：${(g.recent_tasks || []).map((t) =>
        `#${t.id} ${esc(t.title)} ${spdTag(SPD_TASK_STATUS, t.status)}`).join("；") || "—"}</div>
    </div>`;
  }).join("") || '<p class="desc">该患者没有签约专病</p>';
  const measurements = table(["指标", "值", "单位", "分级", "来源", "时间"], (p.measurements || []).slice(0, 10), (m) =>
    `<tr><td>${esc(m.metric)}</td><td>${esc(String(m.value ?? ""))}</td><td>${esc(m.unit || "")}</td>
     <td>${spdTag(SPD_MEAS_LEVEL, m.level)}</td><td>${esc(m.source_name || "—")}</td><td>${esc(m.measured_at || "")}</td></tr>`);
  const assessments = table(["ID", "量表", "得分", "风险", "时间"], p.assessments || [], (a) =>
    `<tr><td>${a.id}</td><td>${esc(a.scale_code)}</td><td>${esc(String(a.score ?? ""))}</td>
     <td>${spdTag(SPD_RISK, a.risk_level)}</td><td>${esc(a.created_at || "")}</td></tr>`);
  const referrals = table(["ID", "方向", "状态", "时间"], p.referrals || [], (r) =>
    `<tr><td>${r.id}</td><td>${r.direction === "up" ? "上转" : "下转"}</td>
     <td>${spdTag(SPD_REF_STATUS, r.status)}</td><td>${esc(r.created_at || "")}</td></tr>`);
  return panel(`专病 360 档案 · ${pt.name || ""}`, `
    <p class="desc">#${pt.id ?? ""} · ${esc(pt.gender || "—")} · ${esc(pt.birth_date || "—")}
      · 健康卡 ${esc(pt.ehc_no || "—")} · ${esc(pt.phone || "—")}</p>
    ${programs}
    <h4>近期监测（最近 10 条）</h4>${measurements}
    <h4>评估</h4>${assessments}
    <h4>转诊</h4>${referrals}`);
}

async function spdShowProfile(sel, patientId) {
  const box = $(sel);
  box.innerHTML = '<p class="desc">加载中…</p>';
  try { box.innerHTML = spdProfileHtml(await api(`/api/spd/patients/${patientId}/profile`)); }
  catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
}

/** 纳管档案明细（GET /api/spd/enrollments/{id}）：服务包绑定（记用量 / 用量明细 / 解绑）与路径实例。
    「记用量」按钮把项目清单放在 data-items 里（esc 过的 JSON，读回 dataset 时浏览器已还原）。 */
function spdEnrollmentDetailHtml(e) {
  const packages = table(["ID", "服务包", "价格", "项目 已用/总", "余量", "使用率", "状态", "到期", "操作"], e.packages || [], (b) =>
    `<tr><td>${b.id}</td><td>${esc(b.package_name)}</td><td>${b.price}</td>
     <td>${(b.items || []).map((i) => `${esc(i.name || i.code || "")} ${i.used}/${i.total}`).join("；") || "—"}</td>
     <td>${b.remaining}</td><td>${b.usage_rate}%</td>
     <td>${b.status === "bound" ? '<span class="tag green">绑定中</span>' : '<span class="tag">已解绑</span>'}</td>
     <td>${esc(b.period_end || "—")}</td>
     <td><button class="btn secondary" data-bind-usages="${b.id}">用量明细</button>
       ${b.status === "bound"
         ? `<button class="btn secondary" data-bind-use="${b.id}" data-enr="${e.id}"
              data-items="${esc(JSON.stringify((b.items || []).map((i) => ({ code: i.code, name: i.name }))))}">记用量</button>
            <button class="btn secondary" data-bind-unbind="${b.id}" data-enr="${e.id}">解绑</button>`
         : ""}</td></tr>`);
  const paths = table(["ID", "路径", "状态", "当前节点", "阶段", "进度"], e.paths || [], (i) =>
    `<tr><td>${i.id}</td><td>${esc(i.template_code)}</td><td>${esc(SPD_INST_STATUS[i.status] || i.status)}</td>
     <td>${esc(i.current_node_key || "—")}</td><td>${esc(i.current_stage || "—")}</td><td>${i.progress}%</td></tr>`);
  return panel(`纳管档案 #${e.id}`, `
    <p class="desc">${esc(e.patient_name || String(e.patient_id))} · ${esc(e.program_code)}
      · ${esc(SPD_ENROLL_STATUS[e.status] || e.status)} · ${spdTag(SPD_RISK, e.risk_level)} · 阶段 ${esc(e.stage || "—")}
      · 签约 ${esc(e.sign_date || "—")} · 知情同意 ${e.consent_signed ? "已签" : "未签"}${e.consent_no ? " " + esc(e.consent_no) : ""}
      · 服务期 ${esc(e.service_start || "—")} ~ ${esc(e.service_end || "—")}</p>
    <p class="desc">危险因素：${esc((e.risk_factors || []).join("、") || "—")}；并发症：${esc((e.complications || []).join("、") || "—")}；
      标签：${esc((e.tags || []).join("、") || "—")}</p>
    <h4>服务包 <button class="btn secondary" data-enr-bind="${e.id}">绑定服务包</button></h4>${packages}
    <div id="spd-usage-list"></div>
    <h4>路径实例</h4>${paths}`);
}

function spdGroupMembersHtml(groupId, rows) {
  return panel(`分组成员 · 分组 #${groupId}`, `
    <form class="inline" id="spd-grp-add" data-group="${groupId}">
      <input name="patient_ids" placeholder="患者ID，逗号分隔" style="min-width:200px">
      <label style="font-size:13px"><input type="checkbox" name="use_auto_rule" value="true"> 按分组自动规则吸入在管患者</label>
      <input name="program_code" placeholder="限定病种编码（可留空）" style="width:160px">
      <button class="secondary">加入</button>
    </form><p class="msg" id="spd-grp-msg"></p>
    ${table(["患者ID", "姓名", "性别", "出生", "健康卡", "加入时间", "操作"], rows, (m) =>
      `<tr><td>${m.patient_id}</td><td>${esc(m.name || "—")}</td><td>${esc(m.gender || "—")}</td>
       <td>${esc(m.birth_date || "—")}</td><td>${esc(m.ehc_no || "—")}</td><td>${esc(m.added_at || "")}</td>
       <td><button class="btn secondary" data-grp-remove="${m.patient_id}" data-group="${groupId}">移出</button></td></tr>`)}`);
}

/* ============================================================
 * 4. 全程管理中心端（统筹调度中枢）
 * ==========================================================*/

async function renderSpdCenter() {
  $("#page-desc").textContent =
    "统筹调度中枢：统一待办、目标池分发与认领、在途转诊、生命周期确认、上报任务配置";
  const [wb, candidates, catalog, reportTasks, applies, recalls] = await Promise.all([
    api("/api/spd/workbench/center"),
    api("/api/spd/candidates?status=target&limit=50"),
    spdCatalog(),
    api("/api/spd/case-report-tasks"),
    api("/api/spd/service-applies?status=pending&limit=30"),
    api("/api/spd/recalls?limit=30"),
  ]);
  // ADR-0009 第三批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 顶部的 spdCards 卡片区不是面板，原样保留。
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
    ${panel("待办按类型", barChart(spdPairs(wb.todo.all.by_type, SPD_TASK_TYPES), { unit: " 条" }))}
    ${panel("目标池分发", `
      <p class="desc">按辖区、病种、风险把目标人群分给服务团队；已被认领的患者不会被覆盖</p>
      <form class="inline" id="spd-dist-form">
        <input name="candidate_ids" placeholder="目标池ID，逗号分隔" required style="min-width:220px">
        <select name="team_id"><option value="">选择团队</option>
          ${catalog.teams.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
        <input name="assigned_user_id" type="number" placeholder="责任人用户ID">
        <button>分发</button>
      </form><p class="msg" id="spd-dist-msg"></p>
      ${table(["ID", "患者", "病种", "风险", "状态", "机构", "团队", "责任人", "纳入依据", "操作"],
        candidates, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name || c.patient_id)}</td>
         <td>${esc(c.program_code)}</td><td>${spdTag(SPD_RISK, c.risk_level)}</td>
         <td>${esc(SPD_CAND_STATUS[c.status] || c.status || "—")}</td>
         <td>${c.org_id ?? "—"}</td><td>${c.team_id ?? "—"}</td>
         <td>${c.assigned_user_id ?? "—"}</td><td>${esc(c.reason || "—")}</td>
         <td>${c.status === "enrolled" ? "" :
           `<button class="btn secondary" data-cand-claim="${c.id}">认领</button>
            <button class="btn secondary" data-cand-status="${c.id}">改状态</button> `}
           <button class="btn secondary" data-profile="${c.patient_id}">画像</button></td></tr>`)}
      <div id="spd-center-profile"></div>`)}
    ${panel("居民服务申请（待受理）", `
      <p class="desc">居民端提交的专病服务申请；受理即进目标池等待签约建档，拒绝须写明原因</p>
      ${table(["ID", "居民", "病种", "申请说明", "状态", "时间", "操作"], applies, (a) =>
        `<tr><td>${a.id}</td><td>${esc(a.name || String(a.patient_id))}</td><td>${esc(a.program_code)}</td>
         <td>${esc(a.note || "—")}</td><td>${spdTag(SPD_APPLY_STATUS, a.status)}</td>
         <td>${esc(a.created_at || "")}</td>
         <td>${a.status === "pending"
           ? `<button class="btn secondary" data-apply="${a.id}" data-decision="accepted">受理</button>
              <button class="btn secondary" data-apply="${a.id}" data-decision="rejected">拒绝</button>`
           : esc(a.handle_note || "—")}</td></tr>`)}
      <p class="msg" id="spd-apply-msg"></p>`)}
    ${panel("召回跟进", `
      <p class="desc">失访/脱管患者的召回过程逐次留痕；登记为「已召回」时档案自动恢复在管</p>
      ${table(["ID", "档案", "原因", "状态", "联系记录", "结果", "发起", "操作"], recalls, (r) =>
        `<tr><td>${r.id}</td><td>${r.enrollment_id}</td><td>${esc(r.reason || "—")}</td>
         <td>${spdTag(SPD_RECALL_STATUS, r.status)}</td>
         <td>${(r.contacts || []).length} 次${(r.contacts || []).length
           ? `，最近 ${esc((r.contacts[r.contacts.length - 1] || {}).at || "")} ${esc((r.contacts[r.contacts.length - 1] || {}).note || "")}` : ""}</td>
         <td>${esc(r.result || "—")}</td><td>${esc(r.created_at || "")}</td>
         <td>${r.status === "returned" || r.status === "failed" ? "—"
           : `<button class="btn secondary" data-recall="${r.id}">登记进度</button>`}</td></tr>`)}
      <p class="msg" id="spd-recall-msg"></p>`)}
    ${panel("生命周期", table(["状态", "人数"], [
        ["已排除", wb.lifecycle.excluded], ["已迁出", wb.lifecycle.migrated],
        ["已死亡", wb.lifecycle.dead], ["召回中", wb.lifecycle.recalling],
        ["待确认迁入", wb.lifecycle.pending_migrations],
      ], (r) => `<tr><td>${esc(r[0])}</td><td>${r[1]}</td></tr>`))}
    ${panel("转诊在途", barChart(spdPairs(wb.referrals.by_status,
        Object.fromEntries(Object.entries(SPD_REF_STATUS).map(([k, v]) => [k, v[0]]))),
        { color: "#0a4d78", unit: " 单" }))}
    ${panel("上报任务配置（中心端 #11）", `
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
           ${t.active ? "停用" : "启用"}</button></td></tr>`)}`)}`;
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
  $("#page-body").addEventListener("click", async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const toggle = el("data-crt"), claim = el("data-cand-claim"), status = el("data-cand-status");
    const profile = el("data-profile"), apply = el("data-apply"), recall = el("data-recall");
    if (toggle) {
      return postAction(`/api/spd/case-report-tasks/${toggle.dataset.crt}`,
        { active: toggle.dataset.active === "1" }, "#spd-crt-msg", "PATCH");
    }
    if (claim) return postAction(`/api/spd/candidates/${claim.dataset.candClaim}/claim`, null, "#spd-dist-msg");
    if (status) {
      const form = await spdModal("调整目标池状态", [
        { name: "status", label: "状态", type: "select", value: "target", options: [
          { value: "suspect", label: "疑似" }, { value: "target", label: "目标" }, { value: "excluded", label: "排除" }] },
        { name: "reason", label: "依据 / 原因", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/candidates/${status.dataset.candStatus}/status`,
        { status: form.status, reason: form.reason || "" }, "#spd-dist-msg");
    }
    if (profile) return spdShowProfile("#spd-center-profile", profile.dataset.profile);
    if (apply) {
      const accepted = apply.dataset.decision === "accepted";
      const form = await spdModal(accepted ? "受理服务申请" : "拒绝服务申请", [
        { name: "handle_note", label: accepted ? "受理说明（可留空）" : "拒绝原因", type: "textarea" },
      ]);
      if (!form) return;
      if (!accepted && !form.handle_note) { setMsg("#spd-apply-msg", "拒绝须写明原因", false); return; }
      return postAction(`/api/spd/service-applies/${apply.dataset.apply}/handle`,
        { status: apply.dataset.decision, handle_note: form.handle_note || "" }, "#spd-apply-msg");
    }
    if (recall) {
      const form = await spdModal("登记召回进度", [
        { name: "status", label: "状态", type: "select", value: "contacted", options: [
          { value: "pending", label: "待联系" }, { value: "contacted", label: "已联系" },
          { value: "returned", label: "已召回（档案恢复在管）" }, { value: "failed", label: "召回失败" }] },
        { name: "contact_note", label: "本次联系情况", type: "textarea" },
        { name: "result", label: "结果（可留空）" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/recalls/${recall.dataset.recall}/progress`,
        { status: form.status, contact_note: form.contact_note || "", result: form.result || "" }, "#spd-recall-msg");
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
  const [wb, teams, villageDoctors] = await Promise.all([
    api(`/api/spd/workbench/team?role=${role}`),
    api("/api/spd/teams?limit=100"),
    api("/api/spd/village-doctors?limit=100"),
  ]);
  const roleNames = { expert: "团队专家端", member: "团队成员端", case_manager: "个案管理师端" };
  // ADR-0009 第四批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 预警面板的红色左边框走 `accent`；两处"有数据才渲染"的条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${panel("视角切换", `
      <p>${Object.entries(roleNames).map(([k, v]) =>
        `<button class="btn ${k === role ? "" : "secondary"}" data-role="${k}" style="margin-right:8px">${esc(v)}</button>`).join("")}</p>
      <p class="desc">当前：${esc(roleNames[role])}。三个端的数字同源，只是聚合口径不同。</p>`)}
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
      ? panel("⚠ 预警", `
         <p style="font-size:13.5px">
           <span class="tag red" style="margin-right:8px">指标异常 ${wb.alerts.abnormal_measure}</span>
           <span class="tag orange" style="margin-right:8px">在途转诊 ${wb.alerts.referrals}</span>
           <span class="tag orange" style="margin-right:8px">召回中 ${wb.alerts.recall}</span>
           <span class="tag">死亡登记 ${wb.alerts.dead}</span></p>`, { accent: "#c62828" })
      : ""}
    ${panel("所属团队",
      table(["ID", "团队", "层级", "机构", "服务病种"], wb.teams, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(t.level_name)}</td>
         <td>${t.org_id}</td><td>${esc((t.program_codes || []).join("、") || "—")}</td></tr>`))}
    ${panel("患者风险分层",
      barChart(spdPairs(wb.patients.by_risk,
        { low: "低危", mid: "中危", high: "高危", very_high: "极高危" }),
        { color: "#b26a00", unit: " 人" }))}
    ${wb.packages ? panel("服务包执行",
      table(["已绑服务包", "项目总次数", "已消耗", "消费率"], [wb.packages], (p) =>
        `<tr><td>${p.bound}</td><td>${p.total_items}</td><td>${p.used_items}</td>
         <td>${p.usage_rate}%</td></tr>`)) : ""}
    ${panel("待办按类型",
      barChart(spdPairs(wb.tasks.by_type, SPD_TASK_TYPES), { unit: " 条" }))}
    ${panel("团队维护", `
      <form class="inline" id="spd-team-form">
        <input name="name" placeholder="团队名称" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="level">${Object.entries(SPD_TEAM_LEVELS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="program_codes" placeholder="服务病种编码，逗号分隔">
        <input name="leader_user_id" type="number" placeholder="组长用户ID">
        <button>新建团队</button>
      </form><p class="msg" id="spd-team-msg"></p>
      ${table(["ID", "团队", "层级", "机构", "服务病种", "组长", "成员数", "操作"], teams, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.name)}</td><td>${esc(SPD_TEAM_LEVELS[t.level] || t.level)}</td>
         <td>${t.org_id}</td><td>${esc((t.program_codes || []).join("、") || "—")}</td>
         <td>${t.leader_user_id ?? "—"}</td><td>${t.member_count ?? "—"}</td>
         <td><button class="btn secondary" data-team-members="${t.id}">成员</button>
             <button class="btn secondary" data-team-edit="${t.id}" data-name="${esc(t.name)}" data-level="${esc(t.level)}">编辑</button></td></tr>`)}
      <div id="spd-team-detail"></div>`)}
    ${panel("村医档案", `
      <form class="inline" id="spd-vd-form">
        <input name="user_id" type="number" placeholder="用户ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="township" placeholder="乡镇"><input name="village" placeholder="村">
        <input name="license_no" placeholder="执业证号"><input name="license_valid_to" placeholder="有效期至 YYYY-MM-DD">
        <input name="phone" placeholder="电话">
        <button>建档</button>
      </form>
      <details class="fold"><summary>批量导入（每行：用户ID,机构ID,乡镇,村,执业证号,有效期至,电话）</summary>
        <form id="spd-vd-batch-form">
          <textarea name="lines" rows="4" style="width:100%" placeholder="12,3,东乡镇,河西村,110xxxx,2027-12-31,138..."></textarea>
          <button class="secondary">批量导入</button>
        </form></details>
      <p class="msg" id="spd-vd-msg"></p>
      ${table(["ID", "村医", "机构", "乡镇", "村", "执业证", "有效期至", "电话", "状态", "操作"], villageDoctors, (v) =>
        `<tr><td>${v.id}</td><td>${esc(v.user_name || String(v.user_id))}</td><td>${v.org_id}</td>
         <td>${esc(v.township || "—")}</td><td>${esc(v.village || "—")}</td><td>${esc(v.license_no || "—")}</td>
         <td>${esc(v.license_valid_to || "—")}</td><td>${esc(v.phone || "—")}</td>
         <td>${v.active ? '<span class="tag green">在岗</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-vd-edit="${v.id}" data-township="${esc(v.township || "")}"
              data-village="${esc(v.village || "")}" data-license="${esc(v.license_no || "")}"
              data-valid="${esc(v.license_valid_to || "")}" data-phone="${esc(v.phone || "")}" data-active="${v.active ? 1 : 0}">编辑</button>
             <button class="btn secondary" data-vd-qr="${v.id}">绑定二维码</button></td></tr>`)}`)}`;
  const showTeam = async (teamId) => {
    const box = $("#spd-team-detail");
    try {
      const t = await api(`/api/spd/teams/${teamId}`);
      box.innerHTML = panel(`团队成员 · ${t.name}`, `
        <form class="inline" id="spd-tm-form">
          <input name="user_id" type="number" placeholder="用户ID" required>
          <select name="member_role">${Object.entries(SPD_MEMBER_ROLES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
          <input name="program_codes" placeholder="负责病种编码，逗号分隔">
          <button>加入团队</button>
        </form>
        ${table(["ID", "成员", "角色", "负责病种", "患者范围", "随访", "转诊", "审核", "评估", "状态", "操作"], t.members || [], (m) =>
          `<tr><td>${m.id}</td><td>${esc(m.user_name || String(m.user_id))}</td><td>${esc(SPD_MEMBER_ROLES[m.member_role] || m.member_role)}</td>
           <td>${esc((m.program_codes || []).join("、") || "—")}</td><td>${esc(m.patient_scope || "—")}</td>
           <td>${m.can_followup ? "✓" : "—"}</td><td>${m.can_referral ? "✓" : "—"}</td><td>${m.can_audit ? "✓" : "—"}</td><td>${m.can_assess ? "✓" : "—"}</td>
           <td>${m.active === false ? '<span class="tag">停用</span>' : '<span class="tag green">在岗</span>'}</td>
           <td><button class="btn secondary" data-tm-edit="${m.id}" data-team="${teamId}" data-role="${esc(m.member_role)}">改角色</button>
               <button class="btn danger" data-tm-del="${m.id}" data-team="${teamId}">移出</button></td></tr>`)}`);
      $("#spd-tm-form").onsubmit = async (e) => {
        e.preventDefault();
        const f = formJson(e.target, ["user_id"]);
        f.program_codes = String(f.program_codes || "").split(/[，,\s]+/).filter(Boolean);
        try {
          await api(`/api/spd/teams/${teamId}/members`, { method: "POST", body: JSON.stringify(f) });
          await showTeam(teamId);
          setMsg("#spd-team-msg", "已加入团队");
        } catch (err) { setMsg("#spd-team-msg", err.message, false); }
      };
    } catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#spd-team-form").onsubmit = (e) => {
    e.preventDefault();
    const f = formJson(e.target, ["org_id", "leader_user_id"]);
    f.program_codes = String(f.program_codes || "").split(/[，,\s]+/).filter(Boolean);
    return postAction("/api/spd/teams", f, "#spd-team-msg");
  };
  $("#spd-vd-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/village-doctors", formJson(e.target, ["user_id", "org_id"]), "#spd-vd-msg");
  };
  $("#spd-vd-batch-form").onsubmit = async (e) => {
    e.preventDefault();
    const items = String(new FormData(e.target).get("lines") || "").split(/\n/).map((l) => l.trim()).filter(Boolean).map((l) => {
      const [user_id, org_id, township, village, license_no, license_valid_to, phone] = l.split(/[，,]/).map((x) => (x || "").trim());
      return { user_id: Number(user_id), org_id: Number(org_id), township: township || "", village: village || "",
        license_no: license_no || "", license_valid_to: license_valid_to || "", phone: phone || "" };
    });
    if (!items.length) { setMsg("#spd-vd-msg", "没有可导入的行", false); return; }
    try {
      const r = await api("/api/spd/village-doctors/batch", { method: "POST", body: JSON.stringify({ items }) });
      // 后端按 user_id 报跳过原因（用户不存在 / 已建档 / 并发冲突），不是按行号
      const skipped = (r.skipped || []).map((k) => `用户 ${k.user_id ?? "?"}：${k.reason || ""}`).join("；");
      setMsg("#spd-vd-msg", `导入 ${r.created} 条${skipped ? `，跳过 ${r.skipped.length} 条：${skipped}` : ""}`);
      route();
    } catch (err) { setMsg("#spd-vd-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const roleBtn = el("data-role"), teamMembers = el("data-team-members"), teamEdit = el("data-team-edit");
    const tmEdit = el("data-tm-edit"), tmDel = el("data-tm-del"), vdEdit = el("data-vd-edit"), vdQr = el("data-vd-qr");
    if (roleBtn) {
      localStorage.setItem("spd_team_role", roleBtn.dataset.role);
      route();
      return;
    }
    if (teamMembers) return showTeam(teamMembers.dataset.teamMembers);
    if (teamEdit) {
      const form = await spdModal("编辑团队", [
        { name: "name", label: "团队名称", value: teamEdit.dataset.name, required: true },
        { name: "level", label: "层级", type: "select", value: teamEdit.dataset.level,
          options: Object.entries(SPD_TEAM_LEVELS).map(([k, v]) => ({ value: k, label: v })) },
        { name: "leader_user_id", label: "组长用户ID（留空不改）", type: "number" },
        { name: "active", label: "状态", type: "select", value: "1",
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      const body = { name: form.name, level: form.level, active: form.active === "1" };
      if (form.leader_user_id) body.leader_user_id = form.leader_user_id;
      return postAction(`/api/spd/teams/${teamEdit.dataset.teamEdit}`, body, "#spd-team-msg", "PATCH");
    }
    if (tmEdit) {
      const form = await spdModal("调整成员角色与权限", [
        { name: "member_role", label: "角色", type: "select", value: tmEdit.dataset.role,
          options: Object.entries(SPD_MEMBER_ROLES).map(([k, v]) => ({ value: k, label: v })) },
        { name: "patient_scope", label: "患者范围", type: "select", value: "team",
          options: [{ value: "self", label: "本人" }, { value: "team", label: "本团队" }, { value: "org", label: "本机构" }, { value: "region", label: "全域" }] },
        { name: "can_referral", label: "可发起转诊", type: "select", value: "0", options: [{ value: "1", label: "是" }, { value: "0", label: "否" }] },
        { name: "can_audit", label: "可审核", type: "select", value: "0", options: [{ value: "1", label: "是" }, { value: "0", label: "否" }] },
        { name: "can_assess", label: "可评估", type: "select", value: "0", options: [{ value: "1", label: "是" }, { value: "0", label: "否" }] },
        { name: "active", label: "状态", type: "select", value: "1", options: [{ value: "1", label: "在岗" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      try {
        await api(`/api/spd/team-members/${tmEdit.dataset.tmEdit}`, { method: "PATCH", body: JSON.stringify({
          member_role: form.member_role, patient_scope: form.patient_scope,
          can_referral: form.can_referral === "1", can_audit: form.can_audit === "1", can_assess: form.can_assess === "1",
          active: form.active === "1",
        }) });
        await showTeam(tmEdit.dataset.team);
        setMsg("#spd-team-msg", "成员已更新");
      } catch (err) { setMsg("#spd-team-msg", err.message, false); }
      return;
    }
    if (tmDel) {
      try {
        await api(`/api/spd/team-members/${tmDel.dataset.tmDel}`, { method: "DELETE" });
        await showTeam(tmDel.dataset.team);
        setMsg("#spd-team-msg", "已移出团队");
      } catch (err) { setMsg("#spd-team-msg", err.message, false); }
      return;
    }
    if (vdEdit) {
      const form = await spdModal("编辑村医档案", [
        { name: "township", label: "乡镇", value: vdEdit.dataset.township },
        { name: "village", label: "村", value: vdEdit.dataset.village },
        { name: "license_no", label: "执业证号", value: vdEdit.dataset.license },
        { name: "license_valid_to", label: "有效期至 YYYY-MM-DD", value: vdEdit.dataset.valid },
        { name: "phone", label: "电话", value: vdEdit.dataset.phone },
        { name: "active", label: "状态", type: "select", value: vdEdit.dataset.active,
          options: [{ value: "1", label: "在岗" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/village-doctors/${vdEdit.dataset.vdEdit}`, {
        township: form.township || "", village: form.village || "", license_no: form.license_no || "",
        license_valid_to: form.license_valid_to || "", phone: form.phone || "", active: form.active === "1",
      }, "#spd-vd-msg", "PATCH");
    }
    if (vdQr) {
      try { await spdOpenSvg(`/api/spd/village-doctors/${vdQr.dataset.vdQr}/qr.svg`); }
      catch (err) { setMsg("#spd-vd-msg", err.message, false); }
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
    ${panel("筛查登记", `
      <p class="desc">量表评分与病种规则双通道判定，任一命中即入目标池；排除规则优先于纳入</p>
      <form class="inline" id="spd-screen-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${spdProgramOptions(catalog, false, true)}</select>
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
        <select name="program_code">${spdProgramOptions(catalog, false, true)}</select>
        <input name="org_id" type="number" placeholder="机构ID(留空取本机构)">
        <button class="secondary">按规则自动识别</button>
      </form><p class="msg" id="spd-screen-msg"></p>
      <div id="spd-screen-list"></div>`)}
    ${panel("签约建档纳管", `
      <form class="inline" id="spd-enroll-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="program_code">${spdProgramOptions(catalog, false, true)}</select>
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
      <div id="spd-enroll-list"></div>
      <div id="spd-enroll-detail"></div>
      <div id="spd-profile"></div>`)}
    ${panel("生命周期处置", `
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
      <div id="spd-life-list"></div>`)}
    ${panel("患者分组", `
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
      <div id="spd-group-detail"></div>`)}`;

  const drawScreenings = async () => {
    const rows = await api("/api/spd/screenings?limit=30");
    $("#spd-screen-list").innerHTML = table(
      ["ID", "患者", "病种", "来源", "得分", "风险", "结论", "复核", "操作"], rows, (s) =>
      `<tr><td>${s.id}</td><td>${esc(s.patient_name || s.patient_id)}</td>
       <td>${esc(s.program_code)}</td><td>${esc(s.source_name)}</td><td>${s.score}</td>
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
          : '<span class="tag">' + esc(SPD_ENROLL_STATUS[e.status] || e.status) + "</span>"}</td>
       <td><button class="btn secondary" data-enr-detail="${e.id}">明细</button>
         ${e.status === "active" ? `<button class="btn secondary" data-enr-edit="${e.id}"
           data-stage="${esc(e.stage || "")}" data-risk="${esc(e.risk_level || "low")}">调整</button>` : ""}
         <button class="btn secondary" data-profile="${e.patient_id}">画像</button></td></tr>`);
  };
  const showEnrollment = async (id) => {
    const box = $("#spd-enroll-detail");
    box.innerHTML = '<p class="desc">加载中…</p>';
    try { box.innerHTML = spdEnrollmentDetailHtml(await api(`/api/spd/enrollments/${id}`)); }
    catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  const showGroup = async (groupId) => {
    const box = $("#spd-group-detail");
    box.innerHTML = '<p class="desc">加载中…</p>';
    try {
      box.innerHTML = spdGroupMembersHtml(groupId, await api(`/api/spd/groups/${groupId}/members?limit=100`));
    } catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; return; }
    // 这张表单是点开明细后才画出来的，监听紧随 innerHTML 同步挂上
    $("#spd-grp-add").onsubmit = async (e) => {
      e.preventDefault();
      const f = formJson(e.target);
      const body = {
        patient_ids: String(f.patient_ids || "").split(/[，,\s]+/).filter(Boolean).map(Number),
        use_auto_rule: f.use_auto_rule === "true", program_code: f.program_code || "",
      };
      if (!body.patient_ids.length && !body.use_auto_rule) { setMsg("#spd-grp-msg", "填患者ID或勾选按规则吸入", false); return; }
      try {
        const r = await api(`/api/spd/groups/${groupId}/members`, { method: "POST", body: JSON.stringify(body) });
        await showGroup(groupId);
        setMsg("#spd-grp-msg", `新加入 ${r.added} 人，分组现有 ${r.total} 人`);
      } catch (err) { setMsg("#spd-grp-msg", err.message, false); }
    };
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
    const el = (attr) => e.target.closest(`[${attr}]`);
    const review = el("data-review"), confirm = el("data-confirm");
    const enrDetail = el("data-enr-detail"), enrEdit = el("data-enr-edit"), enrBind = el("data-enr-bind");
    const bindUsages = el("data-bind-usages"), bindUse = el("data-bind-use"), bindUnbind = el("data-bind-unbind");
    const profile = el("data-profile"), grpMembers = el("data-grp-members"), grpRemove = el("data-grp-remove");
    if (review) {
      return postAction(`/api/spd/screenings/${review.dataset.review}/review`,
        { review_result: review.dataset.r }, "#spd-screen-msg");
    }
    if (confirm) {
      return postAction(`/api/spd/lifecycle-events/${confirm.dataset.confirm}/confirm`,
        null, "#spd-life-msg");
    }
    if (enrDetail) return showEnrollment(enrDetail.dataset.enrDetail);
    if (profile) return spdShowProfile("#spd-profile", profile.dataset.profile);
    if (enrEdit) {
      const form = await spdModal("调整纳管档案（留空的项不改）", [
        { name: "stage", label: "管理阶段", value: enrEdit.dataset.stage || "" },
        { name: "risk_level", label: "风险分层", type: "select", value: enrEdit.dataset.risk || "low", options: [
          { value: "low", label: "低危" }, { value: "mid", label: "中危" },
          { value: "high", label: "高危" }, { value: "very_high", label: "极高危" }] },
        { name: "team_id", label: "服务团队ID", type: "number" },
        { name: "doctor_user_id", label: "主管医生用户ID", type: "number" },
        { name: "manager_user_id", label: "个案管理师用户ID", type: "number" },
        { name: "next_followup_at", label: "下次随访日期 YYYY-MM-DD" },
      ]);
      if (!form) return;
      const body = { risk_level: form.risk_level };
      if (form.stage) body.stage = form.stage;
      for (const k of ["team_id", "doctor_user_id", "manager_user_id"]) if (form[k]) body[k] = form[k];
      if (form.next_followup_at) body.next_followup_at = form.next_followup_at;
      return postAction(`/api/spd/enrollments/${enrEdit.dataset.enrEdit}`, body, "#spd-enroll-msg", "PATCH");
    }
    if (enrBind) {
      const enrollmentId = enrBind.dataset.enrBind;
      let packages = [];
      try { packages = await api("/api/spd/service-packages?limit=100"); }
      catch (err) { setMsg("#spd-enroll-msg", err.message, false); return; }
      const active = packages.filter((k) => k.active !== false);
      if (!active.length) { setMsg("#spd-enroll-msg", "没有可绑定的服务包，先在配置里建", false); return; }
      const form = await spdModal("绑定服务包", [
        { name: "package_id", label: "服务包", type: "select", value: String(active[0].id),
          options: active.map((k) => ({ value: String(k.id), label: `${k.name}（${k.price} 元 / ${k.period_days} 天）` })) },
      ]);
      if (!form) return;
      try {
        await api(`/api/spd/enrollments/${enrollmentId}/packages`, { method: "POST",
          body: JSON.stringify({ package_id: Number(form.package_id) }) });
        await showEnrollment(enrollmentId);
        setMsg("#spd-enroll-msg", "服务包已绑定");
      } catch (err) { setMsg("#spd-enroll-msg", err.message, false); }
      return;
    }
    if (bindUsages) {
      try {
        const rows = await api(`/api/spd/package-bindings/${bindUsages.dataset.bindUsages}/usages?limit=100`);
        $("#spd-usage-list").innerHTML = panel(`用量明细 · 绑定 #${bindUsages.dataset.bindUsages}`,
          table(["ID", "项目", "次数", "单价", "备注", "时间"], rows, (u) =>
            `<tr><td>${u.id}</td><td>${esc(u.item_name || u.item_code)}</td><td>${u.qty}</td>
             <td>${u.price}</td><td>${esc(u.note || "—")}</td><td>${esc(u.used_at || "")}</td></tr>`));
      } catch (err) { setMsg("#spd-enroll-msg", err.message, false); }
      return;
    }
    if (bindUse) {
      let items = [];
      try { items = JSON.parse(bindUse.dataset.items || "[]"); } catch (err) { items = []; }
      if (!items.length) { setMsg("#spd-enroll-msg", "该服务包没有可扣减的项目", false); return; }
      const form = await spdModal("服务项目扣减登记", [
        { name: "item_code", label: "项目", type: "select", value: items[0].code,
          options: items.map((i) => ({ value: i.code, label: i.name || i.code })) },
        { name: "qty", label: "次数", type: "number", value: 1 },
        { name: "note", label: "备注（可留空）", type: "textarea" },
      ]);
      if (!form) return;
      try {
        await api(`/api/spd/package-bindings/${bindUse.dataset.bindUse}/usages`, { method: "POST",
          body: JSON.stringify({ item_code: form.item_code, qty: form.qty || 1, note: form.note || "" }) });
        await showEnrollment(bindUse.dataset.enr);
        setMsg("#spd-enroll-msg", "扣减已登记");
      } catch (err) { setMsg("#spd-enroll-msg", err.message, false); }
      return;
    }
    if (bindUnbind) {
      const form = await spdModal("解绑服务包", [
        { name: "ack", label: "解绑后不能再扣减，已用次数保留", type: "select", value: "yes",
          options: [{ value: "yes", label: "确定解绑" }] },
      ]);
      if (!form) return;
      try {
        await api(`/api/spd/package-bindings/${bindUnbind.dataset.bindUnbind}/unbind`, { method: "POST" });
        await showEnrollment(bindUnbind.dataset.enr);
        setMsg("#spd-enroll-msg", "服务包已解绑");
      } catch (err) { setMsg("#spd-enroll-msg", err.message, false); }
      return;
    }
    if (grpMembers) return showGroup(grpMembers.dataset.grpMembers);
    if (grpRemove) {
      try {
        await api(`/api/spd/groups/${grpRemove.dataset.group}/members/${grpRemove.dataset.grpRemove}`, { method: "DELETE" });
        await showGroup(grpRemove.dataset.group);
        setMsg("#spd-grp-msg", "已移出");
      } catch (err) { setMsg("#spd-grp-msg", err.message, false); }
      return;
    }
  };
  // 取数放最后：以上监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 renderSpdPath）
  await Promise.all([drawScreenings(), drawEnrollments(), drawLifecycle()]);
  const drawGroups = async () => {
    const groups = await api("/api/spd/groups");
    $("#spd-group-list").innerHTML = table(
      ["ID", "名称", "范围", "科室", "自动规则", "成员数", "操作"], groups, (g) =>
      `<tr><td>${g.id}</td><td>${esc(g.name)}</td><td>${esc(g.scope_name)}</td>
       <td>${esc(g.dept || "—")}</td><td>${(g.auto_rule || []).length} 条</td>
       <td>${g.member_count ?? "—"}</td>
       <td><button class="btn secondary" data-grp-members="${g.id}">成员</button></td></tr>`);
  };
  await drawGroups();
  // P2-31 例外：下面的 onsubmit 闭包依赖 meta 构建的 groupEditor，提前挂会把窗口期提交从
  // 「兜底无效」变成「TypeError」，非零行为差；窗口期由 shared.js 的 document 层兜底护住。
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

const SPD_SCALE_CATEGORY = { risk: "风险", stage: "分期", rehab: "康复", screen: "筛查" };
const SPD_SCALE_STATUS = { draft: ["草稿", "orange"], published: ["已发布", "green"], disabled: ["已停用", ""] };
const SPD_DEVICE_TYPES = { bp: "血压计", glucose: "血糖仪", band: "手环", scale: "体脂秤", poct: "POCT", ecg: "心电" };
const SPD_TEAM_LEVELS = { county: "县级", township: "乡镇", village: "村级", center: "中心" };
const SPD_MEMBER_ROLES = {
  doctor: "医生", nurse: "护士", rehab: "康复", case_manager: "个案管理师", village_doctor: "村医", expert: "专家",
};

/** 量表 / 村医的二维码是鉴权 SVG：Bearer 模式下直接 window.open 没有令牌，得先 fetch 再写 blob。
 *  开窗必须在点击手势里同步做（await 之后再开会被弹窗拦截），与 openPrintPage 同一口径。 */
async function spdOpenSvg(path) {
  const win = window.open("", "_blank");
  if (!win) throw new Error("浏览器拦截了新窗口，请允许弹出后重试");
  try {
    const resp = await fetch(path, {
      credentials: "same-origin",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!resp.ok) throw new Error(`获取二维码失败(${resp.status})`);
    win.location = URL.createObjectURL(await resp.blob());
  } catch (err) { win.close(); throw err; }
}

const SPD_INST_STATUS = {
  running: "执行中", paused: "已暂停", completed: "已完成", cancelled: "已取消",
};

/** 任务行的操作按钮按状态给：待接收的能接收/分派，办理中的能提交/转派/上传佐证，
    待审核的只能审核，已结束的只剩详情。九个流转端点后端都还按角色（SERVICE_ROLES）
    与机构归属再判一次——这里只是不把注定 409 的按钮摆出来。 */
function spdTaskActions(t) {
  const b = (attr, label) => `<button class="btn secondary" ${attr}="${t.id}">${label}</button>`;
  const parts = [b("data-task-detail", "详情")];
  if (t.status === "done" || t.status === "cancelled") return parts.join(" ");
  if (t.status === "submitted") {
    parts.push(b("data-task-review", "审核"));
    return parts.join(" ");
  }
  if (t.status === "pending") parts.push(b("data-task-claim", "接收"));
  parts.push(b("data-task-assign", t.assignee_id ? "转派" : "分派"));
  parts.push(b("data-task-submit", "提交"));
  if (t.require_evidence) parts.push(b("data-task-evidence", "上传佐证"));
  parts.push(b("data-task-urge", "催办"));
  if (!t.escalated) parts.push(b("data-task-escalate", "升级"));
  parts.push(b("data-task-done", "办结"));
  return parts.join(" ");
}

function spdPriorityLabel(p) {
  return p === 3 ? "特急" : p === 2 ? "紧急" : "普通";
}

/** 任务详情（GET /api/spd/tasks/{id}）：列表行放不下的字段——表单、办理结果、佐证、
    审核意见、来源。佐证走鉴权下载（`downloadAttachment`），不能用裸 <a href>：
    Bearer 模式下浏览器直开链接没有令牌。 */
function spdTaskDetailHtml(t) {
  const kv = (k, v) => `<div><b>${k}</b>：${v}</div>`;
  const json = (o) => `<pre style="white-space:pre-wrap;margin:4px 0">${esc(JSON.stringify(o || {}, null, 1))}</pre>`;
  const evidence = (t.evidence_urls || []).map((e) =>
    `<button class="btn secondary" data-attdl="${e.attachment_id}" data-fn="task-${t.id}-evidence-${e.attachment_id}">佐证 #${e.attachment_id}</button>`
  ).join(" ") || "—";
  return panel(`任务详情 #${t.id}`, `
    ${kv("标题", esc(t.title))}
    ${kv("患者", `${esc(t.patient_name || "")}（#${t.patient_id}）${t.phone ? " " + esc(t.phone) : ""}`)}
    ${kv("病种 / 类型", `${esc(t.program_code || "—")} / ${esc(SPD_TASK_TYPES[t.task_type] || t.task_type)}`)}
    ${kv("状态", spdTag(SPD_TASK_STATUS, t.status) + (t.escalated ? ' <span class="tag red">已升级</span>' : ""))}
    ${kv("优先级 / 截止", `${spdPriorityLabel(t.priority)} / ${esc(t.due_date || "—")}`)}
    ${kv("责任人ID / 团队ID / 机构ID", `${t.assignee_id ?? "—"} / ${t.team_id ?? "—"} / ${t.org_id ?? "—"}`)}
    ${kv("执行角色 / 来源 / 表单编码", `${esc(t.exec_role || "—")} / ${esc(t.source || "—")} / ${esc(t.form_code || "—")}`)}
    ${kv("催办次数", t.urged_count ?? 0)}
    ${kv("审核意见", esc(t.review_note || "—"))}
    ${kv("创建 / 完成", `${esc(t.created_at || "—")} / ${esc(t.finished_at || "—")}`)}
    ${kv("佐证材料", evidence)}
    ${kv("表单", json(t.form))}
    ${kv("办理结果", json(t.result))}`);
}

/** 路径执行明细（GET /api/spd/path-instances/{id}）：节点清单 + 每节点任务状态。
    `nodeIds` 由模板详情（GET /api/spd/path-templates/{id}）按 key 对出来——实例明细
    里的节点没有 id，而进入条件校验要 node_id；模板看不到时明细照常显示，只少这个按钮。 */
function spdInstanceDetailHtml(inst, nodeIds) {
  const rows = inst.nodes || [];
  return panel(`路径明细 #${inst.id}`, `
    <p class="desc">${esc(inst.patient_name || String(inst.patient_id || ""))} · ${esc(inst.template_name)} ·
      ${esc(SPD_INST_STATUS[inst.status] || inst.status)} · 进度 ${inst.progress}%
      · 当前节点 ${esc(inst.current_node_key || "—")}</p>
    ${table(["序", "节点", "阶段", "执行角色", "时限(天)", "本节点任务", "操作"], rows, (n) =>
      `<tr><td>${n.seq}</td>
       <td>${esc(n.name || n.key)}${n.is_current ? ' <span class="tag orange">当前</span>' : ""}</td>
       <td>${esc(n.stage || "—")}</td><td>${esc(n.exec_role || "—")}</td><td>${n.due_days}</td>
       <td>${(n.tasks || []).map((t) => `#${t.id} ${spdTag(SPD_TASK_STATUS, t.status)}`).join(" ") || "—"}</td>
       <td>${nodeIds[n.key]
          ? `<button class="btn secondary" data-node-check="${nodeIds[n.key]}" data-inst="${inst.id}">进入条件</button>`
          : "—"}</td></tr>`)}`);
}

/** 导出端点（GET /api/spd/tasks-export）只回 columns+rows，CSV 在前端拼（后端 docstring 的
    约定：平台的导出都走这个形状，不多养一份编码/换行/BOM 处理）。单元格含逗号/引号/换行
    时加引号并把引号翻倍；开头放 BOM 让 Excel 认出 UTF-8；以 = + - @ 开头的非数字文本前置
    单引号，免得被表格软件当公式执行。 */
function spdDownloadCsv(filename, columns, rows) {
  const cell = (v) => {
    let s = v == null ? "" : String(v);
    if (/^[=+\-@]/.test(s) && Number.isNaN(Number(s))) s = "'" + s;
    return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const text = [columns, ...rows].map((r) => r.map(cell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob(["﻿" + text], { type: "text/csv;charset=utf-8" }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

async function renderSpdPath() {
  $("#page-desc").textContent =
    "标准路径与统一任务：模板发布、患者路径实例与进入条件、任务接收分派提交审核催办升级、批量处理与导出";
  const [catalog, templates, summary] = await Promise.all([
    spdCatalog(), api("/api/spd/path-templates?limit=30"), api("/api/spd/tasks/summary"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["待办任务", summary.open_total], ["超期", summary.overdue, summary.overdue > 0],
      ["今日到期", summary.due_today], ["已升级", summary.escalated, summary.escalated > 0],
    ])}
    ${panel("路径模板", `
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
         <td>${esc(t.scene_name)}</td><td>${esc(t.version)}</td><td>${t.node_count ?? 0}</td>
         <td>${t.status === "published" ? '<span class="tag green">已发布</span>'
            : t.status === "draft" ? '<span class="tag orange">草稿</span>'
            : '<span class="tag">已停用</span>'}</td>
         <td><button class="btn secondary" data-tpl-nodes="${t.id}" data-status="${esc(t.status)}">节点</button>
             <button class="btn secondary" data-tpl-node="${t.id}">加节点</button>
             <button class="btn secondary" data-tpl-pub="${t.id}">发布</button>
             <button class="btn secondary" data-tpl-copy="${t.id}">复制</button></td></tr>`)}
      <div id="spd-tpl-detail"></div>`)}
    ${panel("启动患者路径", `
      <form class="inline" id="spd-inst-form">
        <input name="enrollment_id" type="number" placeholder="纳管档案ID" required>
        <select name="template_id">
          ${catalog.path_templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
        </select>
        <button>启动路径</button>
      </form><p class="msg" id="spd-inst-msg"></p>
      <div id="spd-inst-list"></div>
      <div id="spd-inst-detail"></div>`)}
    ${panel("任务中心", `
      <form class="inline" id="spd-task-filter">
        <select name="task_type"><option value="">全部类型</option>
          ${Object.entries(SPD_TASK_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <select name="status"><option value="">全部状态</option>
          ${Object.entries(SPD_TASK_STATUS).map(([k, v]) => `<option value="${k}">${esc(v[0])}</option>`).join("")}</select>
        <label style="font-size:13px"><input type="checkbox" name="mine" value="true"> 只看我的</label>
        <button class="secondary">查询</button>
        <button type="button" class="btn secondary" data-task-export>导出 CSV</button>
      </form><p class="msg" id="spd-task-msg"></p>
      <form class="inline" id="spd-task-batch">
        <span style="font-size:13px">勾选后批量：</span>
        <select name="action">
          <option value="claim">接收</option><option value="urge">催办</option>
          <option value="escalate">升级</option><option value="assign">分配</option>
          <option value="cancel">取消</option>
        </select>
        <input name="assignee_id" type="number" placeholder="责任人用户ID（分配时必填）">
        <input name="note" placeholder="备注（取消时作为原因）">
        <button class="secondary">对勾选任务执行</button>
      </form>
      <div id="spd-task-list"></div>
      <div id="spd-task-detail"></div>`)}`;

  let lastTaskQuery = {};
  const drawInstances = async () => {
    const rows = await api("/api/spd/path-instances?limit=20");
    $("#spd-inst-list").innerHTML = table(
      ["ID", "患者", "路径", "当前节点", "阶段", "进度", "状态", "操作"], rows, (i) =>
      `<tr><td>${i.id}</td><td>${esc(i.patient_name || i.patient_id || "")}</td>
       <td>${esc(i.template_name)}</td><td>${esc(i.current_node_key || "—")}</td>
       <td>${esc(i.current_stage || "—")}</td><td>${i.progress}%</td>
       <td>${i.status === "running" ? '<span class="tag orange">执行中</span>'
          : i.status === "completed" ? '<span class="tag green">已完成</span>'
          : '<span class="tag">' + esc(SPD_INST_STATUS[i.status] || i.status) + "</span>"}</td>
       <td>${i.status === "running" ? `<button class="btn secondary" data-adv="${i.id}">推进节点</button> ` : ""}
           <button class="btn secondary" data-inst-detail="${i.id}">明细</button>
           ${i.status === "completed" || i.status === "cancelled" ? ""
             : `<button class="btn secondary" data-inst-adjust="${i.id}">调整</button>`}</td></tr>`);
  };
  const drawTasks = async (query) => {
    lastTaskQuery = query || {};
    const qs = new URLSearchParams({ limit: "30", ...lastTaskQuery }).toString();
    const rows = await api(`/api/spd/tasks?${qs}`);
    $("#spd-task-list").innerHTML = table(
      ["选", "ID", "患者", "任务", "类型", "状态", "优先级", "截止", "催办", "操作"], rows, (t) =>
      `<tr><td><input type="checkbox" data-task-pick="${t.id}"></td><td>${t.id}</td>
       <td>${esc(t.patient_name || t.patient_id)}</td>
       <td>${esc(t.title)}</td><td>${esc(SPD_TASK_TYPES[t.task_type] || t.task_type)}</td>
       <td>${spdTag(SPD_TASK_STATUS, t.status)}${t.escalated ? ' <span class="tag red">升级</span>' : ""}</td>
       <td>${spdPriorityLabel(t.priority)}</td>
       <td>${esc(t.due_date || "—")}</td><td>${t.urged_count}</td>
       <td>${spdTaskActions(t)}</td></tr>`);
  };
  const showNodes = async (templateId) => {
    const box = $("#spd-tpl-detail");
    try {
      const tpl = await api(`/api/spd/path-templates/${templateId}`);
      const editable = tpl.status !== "published";   // 已发布的后端 409：复制新版本再改
      box.innerHTML = panel(`节点 · ${tpl.name}（${tpl.status === "published" ? "已发布，只读；要改请复制新版本" : "可编辑"}）`,
        table(["ID", "序", "key", "名称", "阶段", "执行角色", "服务类型", "时限(天)", "操作"], tpl.nodes || [], (n) =>
          `<tr><td>${n.id}</td><td>${n.seq}</td><td>${esc(n.key)}</td><td>${esc(n.name)}</td><td>${esc(n.stage || "—")}</td>
           <td>${esc(n.exec_role || "—")}</td><td>${esc(n.service_type_name || "—")}</td><td>${n.due_days}</td>
           <td>${editable
             ? `<button class="btn secondary" data-node-edit="${n.id}" data-tpl="${templateId}" data-name="${esc(n.name)}"
                  data-stage="${esc(n.stage || "")}" data-seq="${n.seq}" data-days="${n.due_days}">编辑</button>
                <button class="btn danger" data-node-del="${n.id}" data-tpl="${templateId}">删除</button>`
             : "—"}</td></tr>`));
    } catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  const showTask = async (id) => {
    const box = $("#spd-task-detail");
    box.innerHTML = '<p class="desc">加载中…</p>';
    try { box.innerHTML = spdTaskDetailHtml(await api(`/api/spd/tasks/${id}`)); }
    catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  const showInstance = async (id) => {
    const box = $("#spd-inst-detail");
    box.innerHTML = '<p class="desc">加载中…</p>';
    try {
      const inst = await api(`/api/spd/path-instances/${id}`);
      let nodeIds = {};
      try {
        const tpl = await api(`/api/spd/path-templates/${inst.template_id}`);
        nodeIds = Object.fromEntries((tpl.nodes || []).map((n) => [n.key, n.id]));
      } catch (err) {
        // 模板看不到（403/404）不影响看明细，只是没有"进入条件"按钮；原因给出来而不是吞掉
        setMsg("#spd-inst-msg", `模板详情不可用：${err.message}`, false);
      }
      box.innerHTML = spdInstanceDetailHtml(inst, nodeIds);
    } catch (err) { box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  // 监听器必须在任何 await 之前挂上（CI 实锤的窗口：innerHTML 画出表单后、
  // 两次取数的网络往返里点"启动路径"，submit 没有监听器接管，浏览器走原生
  // GET 提交——表单参数进了 URL（?enrollment_id=1&template_id=1）、整页重载、
  // POST 根本没发出。挂监听与 innerHTML 同一个同步块里完成，窗口才是零。
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
  $("#spd-task-batch").onsubmit = async (e) => {
    e.preventDefault();
    const ids = [...document.querySelectorAll("[data-task-pick]:checked")]
      .map((c) => Number(c.dataset.taskPick));
    if (!ids.length) { setMsg("#spd-task-msg", "先勾选要处理的任务", false); return; }
    try {
      // 不走 postAction：批量端点逐条判定、跳过的要连原因一起给人看，整页重画会把它冲掉
      const r = await api("/api/spd/tasks/batch", { method: "POST",
        body: JSON.stringify({ task_ids: ids, ...formJson(e.target, ["assignee_id"]) }) });
      const skipped = (r.skipped || []).map((s) => `#${s.id} ${s.reason}`).join("，");
      setMsg("#spd-task-msg", `已处理 ${r.processed} 条${skipped ? `，跳过 ${r.skipped.length} 条：${skipped}` : ""}`);
      await drawTasks(lastTaskQuery);
    } catch (err) { setMsg("#spd-task-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const node = el("data-tpl-node"), pub = el("data-tpl-pub"), copy = el("data-tpl-copy");
    const tplNodes = el("data-tpl-nodes"), nodeEdit = el("data-node-edit"), nodeDel = el("data-node-del");
    const adv = el("data-adv"), instDetail = el("data-inst-detail"), instAdjust = el("data-inst-adjust");
    const nodeCheck = el("data-node-check");
    const claim = el("data-task-claim"), urge = el("data-task-urge"), done = el("data-task-done");
    const detail = el("data-task-detail"), assign = el("data-task-assign"), escalate = el("data-task-escalate");
    const submit = el("data-task-submit"), review = el("data-task-review"), evidence = el("data-task-evidence");
    const exportBtn = el("data-task-export"), attdl = el("data-attdl");
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
    if (tplNodes) return showNodes(tplNodes.dataset.tplNodes);
    if (nodeEdit) {
      const form = await spdModal("编辑路径节点（留空的项不改）", [
        { name: "name", label: "节点名称", value: nodeEdit.dataset.name },
        { name: "stage", label: "所属阶段", value: nodeEdit.dataset.stage },
        { name: "seq", label: "顺序", type: "number", value: nodeEdit.dataset.seq },
        { name: "due_days", label: "时限（天）", type: "number", value: nodeEdit.dataset.days },
        { name: "exec_role", label: "执行角色（doctor/nurse/village_doctor…，留空不改）" },
      ]);
      if (!form) return;
      const body = {};
      if (form.name) body.name = form.name;
      body.stage = form.stage || "";
      if (form.seq) body.seq = form.seq;
      if (form.due_days) body.due_days = form.due_days;
      if (form.exec_role) body.exec_role = form.exec_role;
      try {
        await api(`/api/spd/path-nodes/${nodeEdit.dataset.nodeEdit}`, { method: "PATCH", body: JSON.stringify(body) });
        await showNodes(nodeEdit.dataset.tpl);
        setMsg("#spd-tpl-msg", "节点已更新");
      } catch (err) { setMsg("#spd-tpl-msg", err.message, false); }
      return;
    }
    if (nodeDel) {
      // P2-43：原先点一下就删；节点的时限、角色、表单配置一并没了，不能恢复
      if (!await spdModal("删除路径节点", [], { intro: "删除后该节点的时限、角色与表单配置一并删除，不能恢复。" })) return;
      try {
        await api(`/api/spd/path-nodes/${nodeDel.dataset.nodeDel}`, { method: "DELETE" });
        await showNodes(nodeDel.dataset.tpl);
        setMsg("#spd-tpl-msg", "节点已删除");
      } catch (err) { setMsg("#spd-tpl-msg", err.message, false); }
      return;
    }
    if (adv) return postAction(`/api/spd/path-instances/${adv.dataset.adv}/advance`, null, "#spd-inst-msg");
    if (instDetail) return showInstance(instDetail.dataset.instDetail);
    if (instAdjust) {
      const form = await spdModal("调整路径实例（改的是实例不是模板）", [
        { name: "status", label: "状态", type: "select", value: "", options: [
          { value: "", label: "不改" }, { value: "running", label: "执行中（恢复）" },
          { value: "paused", label: "暂停" }, { value: "cancelled", label: "取消（未完成的任务一并取消）" }] },
        { name: "owner_user_id", label: "负责人用户ID（留空不改）", type: "number" },
      ]);
      if (!form) return;
      const body = {};
      if (form.status) body.status = form.status;
      if (form.owner_user_id) body.owner_user_id = form.owner_user_id;
      if (!Object.keys(body).length) return;
      return postAction(`/api/spd/path-instances/${instAdjust.dataset.instAdjust}`, body, "#spd-inst-msg", "PATCH");
    }
    if (nodeCheck) {
      try {
        const r = await api(`/api/spd/path-nodes/${nodeCheck.dataset.nodeCheck}/enter-check?instance_id=${nodeCheck.dataset.inst}`);
        const conds = (r.conditions || []).map((c) => JSON.stringify(c)).join("；") || "该节点没有进入条件";
        const matched = (r.matched || []).map((c) => JSON.stringify(c)).join("；");
        setMsg("#spd-inst-msg",
          `${r.allowed ? "满足进入条件" : "不满足进入条件"}：${conds}${matched ? `｜已满足：${matched}` : ""}`, r.allowed);
      } catch (err) { setMsg("#spd-inst-msg", err.message, false); }
      return;
    }
    if (claim) return postAction(`/api/spd/tasks/${claim.dataset.taskClaim}/claim`, null, "#spd-task-msg");
    if (urge) return postAction(`/api/spd/tasks/${urge.dataset.taskUrge}/urge`, null, "#spd-task-msg");
    if (escalate) return postAction(`/api/spd/tasks/${escalate.dataset.taskEscalate}/escalate`, null, "#spd-task-msg");
    if (detail) return showTask(detail.dataset.taskDetail);
    if (assign) {
      const form = await spdModal("分配 / 转派任务", [
        { name: "assignee_id", label: "责任人用户ID", type: "number", required: true },
        { name: "note", label: "备注（可留空）", type: "textarea" },
      ]);
      if (!form || !form.assignee_id) return;
      return postAction(`/api/spd/tasks/${assign.dataset.taskAssign}/assign`,
        { assignee_id: form.assignee_id, note: form.note || "" }, "#spd-task-msg");
    }
    if (submit) {
      const form = await spdModal("提交任务", [
        { name: "note", label: "办理结果 / 说明", type: "textarea" },
        { name: "mode", label: "提交方式", type: "select", value: "final", options: [
          { value: "final", label: "提交审核" }, { value: "draft", label: "保存草稿（状态转办理中）" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/tasks/${submit.dataset.taskSubmit}/submit`,
        { result: { note: form.note || "" }, draft: form.mode === "draft" }, "#spd-task-msg");
    }
    if (review) {
      const form = await spdModal("审核任务", [
        { name: "approved", label: "结论", type: "select", value: "true", options: [
          { value: "true", label: "通过（完成并推进路径）" }, { value: "false", label: "退回（回到办理中）" }] },
        { name: "note", label: "审核意见", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/tasks/${review.dataset.taskReview}/review`,
        { approved: form.approved !== "false", note: form.note || "" }, "#spd-task-msg");
    }
    if (evidence) {
      // 佐证 = 挂在该任务名下的附件（owner_type=spd_task）。上传后用"保存草稿"把附件 id
      // 写进任务的 evidence 清单（要带上已有的，后端是整体替换），最终提交时后端再核一遍。
      const taskId = evidence.dataset.taskEvidence;
      const input = document.createElement("input");
      input.type = "file";
      input.accept = "image/*,.pdf";
      input.onchange = async () => {
        try {
          const att = await uploadAttachment("spd_task", taskId, input);
          const t = await api(`/api/spd/tasks/${taskId}`);
          const ids = [...(t.evidence || []).map(Number).filter(Boolean), att.id];
          await api(`/api/spd/tasks/${taskId}/submit`, { method: "POST",
            body: JSON.stringify({ result: t.result || {}, evidence: ids, draft: true }) });
          setMsg("#spd-task-msg", `佐证 #${att.id} 已挂到任务 #${taskId}（共 ${ids.length} 份），提交时会一并核验`);
          await drawTasks(lastTaskQuery);
        } catch (err) { setMsg("#spd-task-msg", err.message, false); }
      };
      input.click();
      return;
    }
    if (attdl) {
      try { await downloadAttachment(attdl.dataset.attdl, attdl.dataset.fn); }
      catch (err) { setMsg("#spd-task-msg", err.message, false); }
      return;
    }
    if (exportBtn) {
      const filters = formJson($("#spd-task-filter"));
      delete filters.mine;   // 导出端点没有 mine 参数：它按调用方可见机构导出
      const qs = new URLSearchParams({ limit: "2000", ...filters }).toString();
      try {
        const d = await api(`/api/spd/tasks-export?${qs}`);
        spdDownloadCsv(`spd_tasks_${new Date().toISOString().slice(0, 10)}.csv`, d.columns, d.rows);
        setMsg("#spd-task-msg", `已导出 ${d.total} 条（上限 2000，多于此请按状态/类型分次导）`);
      } catch (err) { setMsg("#spd-task-msg", err.message, false); }
      return;
    }
    if (done) {
      const form = await spdModal("办结任务", [
        { name: "note", label: "办理结果", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/tasks/${done.dataset.taskDone}/complete`,
        { result: { note: form.note } }, "#spd-task-msg");
    }
  };
  // 取数放最后：首屏 innerHTML 与全部监听器已在同一个同步块里就位，
  // 这两趟网络往返期间的任何点击都有人接（见上方"监听器必须在 await 前"注释）
  await Promise.all([drawInstances(), drawTasks()]);
}

/* ============================================================
 * 8. 逐级转诊
 * ==========================================================*/

const SPD_HANDLE_LEVELS = { village: "村医处置", station: "服务站处置", township: "卫生院处置", county: "县级处置" };

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
    ${panel("在途单据按层级",
      barChart(spdPairs(closure.pending_by_level,
        { village: "村医", station: "服务站", township: "卫生院", county: "县级医院" }),
        { color: "#0a4d78", unit: " 单" }))}
    ${panel("发起转诊", `
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
         <td><button class="btn secondary" data-ref-detail="${c.id}">全轨迹</button>
             <button class="btn secondary" data-ref-pass="${c.id}">通过</button>
             <button class="btn secondary" data-ref-reject="${c.id}">退回</button>
             <button class="btn secondary" data-ref-arrive="${c.id}">到院</button>
             <button class="btn secondary" data-ref-down="${c.id}">下转</button>
             <button class="btn secondary" data-ref-recv="${c.id}">随访接收</button>
             ${["submitted", "station_reviewed"].includes(c.status)
               ? `<button class="btn danger" data-ref-withdraw="${c.id}">撤回</button>` : ""}</td></tr>`)}
      <div id="spd-ref-detail"></div>`)}
    ${panel("转诊触发规则", `
      <p class="desc">命中规则默认只提示不自动开单——批量随访录入时自动开单会瞬间产生几十张单子</p>
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
      ${table(["ID", "编码", "名称", "病种", "处理层级", "条件数", "自动建任务", "状态", "操作"], rules, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.program_code || "全部")}</td>
         <td>${esc(SPD_HANDLE_LEVELS[r.handle_level] || r.handle_level)}</td><td>${(r.conditions || []).length}</td>
         <td>${r.auto_task ? "是" : "否"}</td>
         <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-refrule-edit="${r.id}" data-name="${esc(r.name)}"
              data-level="${esc(r.handle_level)}" data-auto="${r.auto_task ? 1 : 0}"
              data-active="${r.active ? 1 : 0}">编辑</button></td></tr>`)}`)}
    ${panel("按规则试算（不自动开单）", `
      <p class="desc">拿某位患者的当前事实（年龄 / 性别 / 诊断 / 各监测值 / 风险分层）过一遍启用中的规则，
        看命中哪几条、是哪个条件命中的。**默认只提示不开单**——命中即自动开单，
        一次批量随访录入能开出几十张单子（后端 docstring 的原话）；要开单勾上下面那个框</p>
      <form class="inline" id="spd-refcheck-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="program_code" placeholder="病种编码（可选）">
        <label style="font-size:13px"><input type="checkbox" name="auto_create" value="true"> 命中即开上转单</label>
        <button class="secondary">试算</button>
      </form><p class="msg" id="spd-refcheck-msg"></p>
      <div id="spd-refcheck-box"></div>`)}
    ${alerts.count ? panel(`⚠ 超过 ${alerts.threshold_hours} 小时未推进（${alerts.count}）`, `
      ${table(["ID", "患者", "状态", "发起时间"], alerts.items, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${spdTag(SPD_REF_STATUS, c.status)}</td>
         <td>${esc(c.created_at.replace("T", " ").slice(0, 16))}</td></tr>`)}`,
      { accent: "#c62828" }) : ""}`;
  $("#spd-ref-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/referrals",
      formJson(e.target, ["patient_id", "target_org_id"]), "#spd-ref-msg");
  };
  $("#spd-refcheck-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/api/spd/referral-rules/check", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), program_code: f.get("program_code") || "",
        auto_create: f.get("auto_create") === "true" }) });
      $("#spd-refcheck-box").innerHTML = `
        <p class="msg ${r.triggered ? "err" : "ok"}">${r.triggered
          ? `命中 ${r.hits.length} 条规则${r.case ? `，已开转诊单 #${r.case.id}` : "（未开单）"}`
          : "未命中任何启用中的规则"}</p>
        ${table(["规则", "处理层级", "命中条件"], r.hits, (h) =>
          `<tr><td>${esc(h.rule.code)} ${esc(h.rule.name)}</td>
           <td>${esc(SPD_HANDLE_LEVELS[h.rule.handle_level] || h.rule.handle_level)}</td>
           <td>${(h.matched || []).map((m) => esc(JSON.stringify(m))).join("；") || "—"}</td></tr>`)}
        <p class="desc">本次参与判定的事实：${Object.entries(r.facts || {})
          .map(([k, v]) => `${esc(k)}=${esc(v === null ? "—" : v)}`).join("，") || "（无）"}</p>`;
      setMsg("#spd-refcheck-msg", "");
      if (r.case) route();
    } catch (err) { $("#spd-refcheck-box").innerHTML = ""; setMsg("#spd-refcheck-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const pass = e.target.closest("[data-ref-pass]"), reject = e.target.closest("[data-ref-reject]");
    const arrive = e.target.closest("[data-ref-arrive]"), down = e.target.closest("[data-ref-down]");
    const recv = e.target.closest("[data-ref-recv]");
    const detail = e.target.closest("[data-ref-detail]"), withdraw = e.target.closest("[data-ref-withdraw]");
    const ruleEdit = e.target.closest("[data-refrule-edit]");
    if (detail) {
      try {
        const c = await api(`/api/spd/referrals/${detail.dataset.refDetail}`);
        $("#spd-ref-detail").innerHTML = panel(`转诊单 #${c.id} 全轨迹 · ${c.patient_name}`,
          table(["环节", "动作", "经办人", "机构", "意见", "时间"], c.steps, (s) =>
            `<tr><td>${esc(s.step)}</td><td>${esc(s.action)}</td><td>${s.actor_id ?? "—"}</td>
             <td>${s.org_id ?? "—"}</td><td>${esc(s.opinion) || "—"}</td>
             <td>${esc((s.created_at || "").replace("T", " ").slice(0, 19))}</td></tr>`));
      } catch (err) { setMsg("#spd-ref-msg", err.message, false); }
      return;
    }
    if (withdraw) {
      // 后端只允许发起人本人、且尚未进上级审核时撤回；按钮也只在这两个状态摆出来
      if (!confirm("撤回后该转诊单即关闭，且不计入闭环率分母。确认撤回？")) return;
      return postAction(`/api/spd/referrals/${withdraw.dataset.refWithdraw}/withdraw`, null, "#spd-ref-msg");
    }
    if (ruleEdit) {
      const form = await spdModal("编辑转诊触发规则（条件请新建规则）", [
        { name: "name", label: "规则名称", value: ruleEdit.dataset.name, required: true },
        { name: "handle_level", label: "处理层级", type: "select", value: ruleEdit.dataset.level,
          options: Object.entries(SPD_HANDLE_LEVELS).map(([k, v]) => ({ value: k, label: v })) },
        { name: "auto_task", label: "命中后自动建任务", type: "select", value: ruleEdit.dataset.auto,
          options: [{ value: "0", label: "否（只提示）" }, { value: "1", label: "是" }] },
        { name: "active", label: "状态", type: "select", value: ruleEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/referral-rules/${ruleEdit.dataset.refruleEdit}`, {
        name: form.name, handle_level: form.handle_level,
        auto_task: form.auto_task === "1", active: form.active === "1",
      }, "#spd-refrule-msg", "PATCH");
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
  // P2-31 例外：下面的 onsubmit 闭包依赖 meta 构建的 condEditor，提前挂会把窗口期提交从
  // 「兜底无效」变成「TypeError」，非零行为差；窗口期由 shared.js 的 document 层兜底护住。
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

const SPD_ASSESS_OBJECTS = { org: "机构", doctor: "医生", village_doctor: "村医", team: "团队" };
const SPD_PLAN_LEVELS = { hospital: "县级医院", township: "卫生院", station: "服务站", village: "村医", team: "团队" };
const SPD_PERIOD_TYPES = { month: "月度", quarter: "季度", year: "年度" };
const SPD_POINT_EVENTS = {
  sign: "签约", referral_up: "上转", referral_down: "下转承接", followup: "随访",
  abnormal_report: "异常上报", signin: "每日签到",
};
const SPD_REDEEM_STATUS = { pending: ["待核销", "orange"], verified: ["已核销", "green"], cancelled: ["已取消", ""] };

/** 考核方案的指标串「指标:权重，指标:权重」→ items；新建与编辑共用同一份解析。 */
function spdParsePlanItems(text) {
  return String(text || "").split(/[，,]/).filter(Boolean).map((pair) => {
    const [code, weight] = pair.split(":").map((s) => (s || "").trim());
    return { indicator_code: code, weight: Number(weight || 0) };
  });
}

/** 按方案周期类型给「得分分析」一个默认周期：month → 2026-09，quarter → 2026-Q3，year → 2026。 */
function spdDefaultPeriod(periodType) {
  const d = new Date(), y = d.getFullYear(), m = d.getMonth() + 1;
  if (periodType === "year") return String(y);
  if (periodType === "quarter") return `${y}-Q${Math.ceil(m / 3)}`;
  return `${y}-${String(m).padStart(2, "0")}`;
}

async function renderSpdAssess() {
  $("#page-desc").textContent =
    "指标库 → 分级考核方案 → 自动取数计分 → 扣分下钻与得分分析；工作量统计；村医积分规则、商品兑换与核销";
  const [indicators, plans, scores, goods, accounts, pointRules, redeems, workload] = await Promise.all([
    api("/api/spd/indicators?limit=50"), api("/api/spd/assess-plans"),
    api("/api/spd/scores?limit=30"), api("/api/spd/goods"),
    api("/api/spd/point-accounts?limit=20"),
    api("/api/spd/point-rules"), api("/api/spd/redeems?limit=50"), api("/api/spd/workload"),
  ]);
  const objectNames = SPD_ASSESS_OBJECTS;
  const onOff = (flag) => (flag ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>');
  const workloadHtml = (w) => `
      <p class="desc">${esc(w.period)} · ${w.object_type === "org" ? "按机构" : "按人员"}；已完成任务按类型拆分，与考核指标共用同一批表，报表与得分对得上</p>
      ${table(["对象", "任务总数", "已完成", "完成率", "已完成按类型"], w.items || [], (x) =>
        `<tr><td>${esc(x.object_name || String(x.object_id))}</td><td>${x.total}</td><td>${x.done}</td>
         <td>${x.completion_rate}%</td>
         <td>${esc(Object.entries(x.by_type || {}).map(([k, v]) => (SPD_TASK_TYPES[k] || k) + " " + v).join("，") || "—")}</td></tr>`)}`;
  $("#page-body").innerHTML = `
    ${panel("考核指标库", `
      <p class="desc">取数口径 + 公式（AST 白名单求值）+ 评分规则三段式，各县只需调权重与目标值</p>
      ${table(["ID", "编码", "名称", "对象", "取数口径", "公式", "权重", "目标值", "版本", "状态", "操作"],
        indicators, (i) =>
        `<tr><td>${i.id}</td><td>${esc(i.code)}</td><td>${esc(i.name)}</td>
         <td>${esc(objectNames[i.object_type] || i.object_type)}</td>
         <td>${esc(i.data_source)}</td><td><code>${esc(i.formula || "—")}</code></td>
         <td>${i.weight}</td><td>${i.target_value ?? "—"}</td><td>${esc(i.version)}</td>
         <td>${onOff(i.active)}</td>
         <td><button class="btn secondary" data-ind-edit="${i.id}" data-name="${esc(i.name)}" data-weight="${i.weight}"
              data-target="${i.target_value ?? ""}" data-formula="${esc(i.formula || "")}" data-active="${i.active ? 1 : 0}">编辑</button>
             <button class="btn secondary" data-ind-usage="${i.id}">使用情况</button></td></tr>`)}
      <p class="msg" id="spd-ind-msg"></p>
      <div id="spd-ind-detail"></div>`)}
    ${panel("分级考核方案", `
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
      ${table(["ID", "编码", "名称", "层级", "对象", "周期", "指标数", "状态", "操作"], plans, (p) =>
        `<tr><td>${p.id}</td><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${esc(p.level_name)}</td><td>${esc(objectNames[p.object_type] || p.object_type)}</td>
         <td>${esc(p.period_type_name)}</td><td>${(p.items || []).length}</td>
         <td>${onOff(p.active !== false)}</td>
         <td><button class="btn secondary" data-run="${p.id}">跑分</button>
             <button class="btn secondary" data-plan-analysis="${p.id}" data-period-type="${esc(p.period_type || "")}">得分分析</button>
             <button class="btn secondary" data-plan-edit="${p.id}" data-name="${esc(p.name)}" data-period-type="${esc(p.period_type || "")}"
              data-items="${esc((p.items || []).map((it) => `${it.indicator_code || it.code || ""}:${it.weight ?? 0}`).join(","))}"
              data-active="${p.active === false ? 0 : 1}">编辑</button></td></tr>`)}`)}
    ${panel("考核结果", `
      ${table(["排名", "对象", "周期", "综合得分", "操作"], scores, (s) =>
        `<tr><td>${s.rank}</td><td>${esc(s.object_name)}</td><td>${esc(s.period)}</td>
         <td>${s.total_score}</td>
         <td><button class="btn secondary" data-score="${s.id}">下钻明细</button></td></tr>`)}
      <div id="spd-score-detail"></div>`)}
    ${panel("工作量统计", `
      <form class="inline" id="spd-workload-form">
        <select name="object_type"><option value="doctor">按人员</option><option value="org">按机构</option></select>
        <input name="period" placeholder="周期：2026-08 / 2026-Q3 / 2026" value="${esc(workload.period || "")}" style="width:190px">
        <input name="org_id" type="number" placeholder="机构ID（可选）" style="width:120px">
        <input name="program_code" placeholder="病种编码（可选）" style="width:130px">
        <button>查询</button>
      </form><p class="msg" id="spd-workload-msg"></p>
      <div id="spd-workload-box">${workloadHtml(workload)}</div>`)}
    ${panel("村医积分账户", `
      ${table(["用户ID", "姓名", "余额", "累计获得", "累计兑换"], accounts, (a) =>
        `<tr><td>${a.user_id}</td><td>${esc(a.user_name)}</td><td>${a.balance}</td>
         <td>${a.earned}</td><td>${a.used}</td></tr>`)}`)}
    ${panel("村医积分规则", `
      <p class="desc">签约 / 上转 / 下转承接 / 随访 / 异常上报按事件自动入账；「每日签到」规则给医生移动端的签到按钮用；每日上限 0 = 不限</p>
      <form class="inline" id="spd-prule-form">
        <input name="code" placeholder="规则编码" required>
        <input name="name" placeholder="规则名称" required>
        <select name="event">${Object.entries(SPD_POINT_EVENTS).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="points" type="number" placeholder="积分" style="width:90px">
        <input name="daily_limit" type="number" placeholder="每日上限" style="width:110px">
        <button>新建规则</button>
      </form><p class="msg" id="spd-prule-msg"></p>
      ${table(["ID", "编码", "名称", "事件", "积分", "每日上限", "状态", "操作"], pointRules, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(SPD_POINT_EVENTS[r.event] || r.event)}</td>
         <td>${r.points}</td><td>${r.daily_limit || "不限"}</td><td>${onOff(r.active)}</td>
         <td><button class="btn secondary" data-prule-edit="${r.id}" data-name="${esc(r.name)}" data-points="${r.points}"
              data-limit="${r.daily_limit}" data-active="${r.active ? 1 : 0}">编辑</button></td></tr>`)}`)}
    ${panel("积分商品与核销", `
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
      ${table(["ID", "编码", "名称", "所需积分", "库存", "操作"], goods, (g) =>
        `<tr><td>${g.id}</td><td>${esc(g.code)}</td><td>${esc(g.name)}</td><td>${g.points}</td>
         <td>${g.stock}</td>
         <td><button class="btn secondary" data-goods-edit="${g.id}" data-name="${esc(g.name)}" data-points="${g.points}"
              data-stock="${g.stock}">编辑</button></td></tr>`)}`)}
    ${panel("兑换记录", `
      <p class="desc">村医在移动端兑换后拿到核销码、到点位出示，经办在上方「核销」栏录码完成发放；这里只看状态，核销码不回显</p>
      ${table(["ID", "商品", "积分", "状态", "兑换时间", "核销时间"], redeems, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.goods_name)}</td><td>${r.points}</td><td>${spdTag(SPD_REDEEM_STATUS, r.status)}</td>
         <td>${esc((r.created_at || "").replace("T", " ").slice(0, 16))}</td>
         <td>${esc((r.verified_at || "").replace("T", " ").slice(0, 16) || "—")}</td></tr>`)}`)}`;
  $("#spd-plan-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target);
    body.items = spdParsePlanItems(body.items);
    return postAction("/api/spd/assess-plans", body, "#spd-plan-msg");
  };
  $("#spd-workload-form").onsubmit = async (e) => {
    e.preventDefault();
    const query = new URLSearchParams(formJson(e.target)).toString();
    try {
      $("#spd-workload-box").innerHTML = workloadHtml(await api(`/api/spd/workload?${query}`));
      setMsg("#spd-workload-msg", "");
    } catch (err) { setMsg("#spd-workload-msg", err.message, false); }
  };
  $("#spd-prule-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/point-rules", formJson(e.target, ["points", "daily_limit"]), "#spd-prule-msg");
  };
  $("#spd-goods-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/goods", formJson(e.target, ["points", "stock"]), "#spd-goods-msg");
  };
  $("#spd-verify-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/redeems/verify", formJson(e.target), "#spd-goods-msg");
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const run = el("data-run"), score = el("data-score");
    const indEdit = el("data-ind-edit"), indUsage = el("data-ind-usage");
    const planEdit = el("data-plan-edit"), planAnalysis = el("data-plan-analysis");
    const pruleEdit = el("data-prule-edit"), goodsEdit = el("data-goods-edit");
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
      return;
    }
    if (indEdit) {
      const form = await spdModal("编辑考核指标（公式留空不改）", [
        { name: "name", label: "名称", value: indEdit.dataset.name, required: true },
        { name: "weight", label: "权重", type: "number", value: indEdit.dataset.weight },
        { name: "target_value", label: "目标值（留空 = 不设目标）", value: indEdit.dataset.target },
        { name: "formula", label: "公式（只能引用该取数口径的变量）", value: indEdit.dataset.formula },
        { name: "active", label: "状态", type: "select", value: indEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      // 目标值 0 是合法目标，所以走文本框再自己转数：留空 = null，不能让 Number("") 把它变成 0
      const target = form.target_value === "" ? null : Number(form.target_value);
      if (target !== null && Number.isNaN(target)) return setMsg("#spd-ind-msg", "目标值须是数字", false);
      const body = { name: form.name, weight: form.weight, target_value: target, active: form.active === "1" };
      if (form.formula) body.formula = form.formula;
      return postAction(`/api/spd/indicators/${indEdit.dataset.indEdit}`, body, "#spd-ind-msg", "PATCH");
    }
    if (indUsage) {
      try {
        const u = await api(`/api/spd/indicators/${indUsage.dataset.indUsage}/usage`);
        $("#spd-ind-detail").innerHTML = panel(`使用情况 · ${u.indicator.name}（被 ${u.used_by} 个方案引用）`,
          table(["方案ID", "编码", "名称", "层级", "权重"], u.plans || [], (p) =>
            `<tr><td>${p.id}</td><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
             <td>${esc(SPD_PLAN_LEVELS[p.level] || p.level)}</td><td>${p.weight ?? "—"}</td></tr>`));
      } catch (err) { setMsg("#spd-ind-msg", err.message, false); }
      return;
    }
    if (planEdit) {
      const form = await spdModal("编辑考核方案（指标串留空不改）", [
        { name: "name", label: "名称", value: planEdit.dataset.name, required: true },
        { name: "period_type", label: "考核周期", type: "select", value: planEdit.dataset.periodType,
          options: Object.entries(SPD_PERIOD_TYPES).map(([k, v]) => ({ value: k, label: v })) },
        { name: "items", label: "指标:权重，逗号分隔", value: planEdit.dataset.items },
        { name: "active", label: "状态", type: "select", value: planEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      const body = { name: form.name, period_type: form.period_type, active: form.active === "1" };
      if (form.items) body.items = spdParsePlanItems(form.items);
      return postAction(`/api/spd/assess-plans/${planEdit.dataset.planEdit}`, body, "#spd-plan-msg", "PATCH");
    }
    if (planAnalysis) {
      const planId = planAnalysis.dataset.planAnalysis;
      const form = await spdModal("得分分布与高频扣分项", [
        { name: "period", label: "考核周期（如 2026-08 / 2026-Q3 / 2026）",
          value: spdDefaultPeriod(planAnalysis.dataset.periodType), required: true },
      ]);
      if (!form || !form.period) return;
      try {
        const a = await api(`/api/spd/scores-analysis?plan_id=${planId}&period=${encodeURIComponent(form.period)}`);
        $("#spd-score-detail").innerHTML = panel(`得分分析 · 方案 #${planId} · ${form.period}`, a.total
          ? `${spdCards([["对象数", a.total], ["平均分", a.average]])}
             ${table(["分数段", "对象数"], Object.entries(a.distribution || {}), ([k, v]) =>
               `<tr><td>${esc(k)}</td><td>${v}</td></tr>`)}
             ${table(["高频扣分项", "扣分次数", "累计扣分"], a.top_deductions || [], (d) =>
               `<tr><td>${esc(d.indicator_name || d.indicator_code)}</td><td>${d.count}</td><td>${d.total_deduction}</td></tr>`)}
             ${table(["排名", "对象", "综合得分"], a.ranking || [], (r) =>
               `<tr><td>${r.rank ?? "—"}</td><td>${esc(r.object_name)}</td><td>${r.total_score}</td></tr>`)}`
          : '<p class="desc">该周期还没有计分结果，先在方案行「跑分」</p>');
      } catch (err) { setMsg("#spd-plan-msg", err.message, false); }
      return;
    }
    if (pruleEdit) {
      const form = await spdModal("编辑积分规则", [
        { name: "name", label: "名称", value: pruleEdit.dataset.name, required: true },
        { name: "points", label: "积分", type: "number", value: pruleEdit.dataset.points },
        { name: "daily_limit", label: "每日上限（0 = 不限）", type: "number", value: pruleEdit.dataset.limit },
        { name: "active", label: "状态", type: "select", value: pruleEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/point-rules/${pruleEdit.dataset.pruleEdit}`, {
        name: form.name, points: form.points, daily_limit: form.daily_limit, active: form.active === "1",
      }, "#spd-prule-msg", "PATCH");
    }
    if (goodsEdit) {
      const form = await spdModal("编辑商品（下架后村医端不再显示）", [
        { name: "name", label: "名称", value: goodsEdit.dataset.name, required: true },
        { name: "points", label: "所需积分", type: "number", value: goodsEdit.dataset.points },
        { name: "stock", label: "库存", type: "number", value: goodsEdit.dataset.stock },
        { name: "active", label: "状态", type: "select", value: "1",
          options: [{ value: "1", label: "上架" }, { value: "0", label: "下架" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/goods/${goodsEdit.dataset.goodsEdit}`, {
        name: form.name, points: form.points, stock: form.stock, active: form.active === "1",
      }, "#spd-goods-msg", "PATCH");
    }
  };
}

/* ============================================================
 * 10. 智能随访服务端
 * ==========================================================*/

const SPD_QC_RESULT = { pending: ["待判定", "orange"], pass: ["合格", "green"], warn: ["提醒", "orange"], fail: ["不合格", "red"] };
const SPD_QC_METHOD = { record: "查记录", phone: "电话回访", wechat: "微信回访" };
const SPD_FU_CHANNELS = { phone: "电话", wechat: "微信", sms: "短信", self: "自填", visit: "面访" };
const SPD_REPORT_PERIODS = { daily: "日报", weekly: "周报", monthly: "月报", custom: "自定义" };
const SPD_REPORT_SCOPES = { center: "专病中心", dept: "科室团队", grassroots: "基层机构", personal: "个人" };

async function renderSpdFollowup() {
  $("#page-desc").textContent =
    "通用随访能力：方案规则与问卷、多时间点任务生成、多渠道执行、呼叫录音、抽查质控";
  const [rules, questionnaires, stats, calls, qcSamples] = await Promise.all([
    api("/api/spd/followup-rules"), api("/api/spd/questionnaires"),
    api("/api/spd/followup-stats"), api("/api/spd/call-tasks?limit=20"),
    api("/api/spd/qc-samples?limit=50"),
  ]);
  $("#page-body").innerHTML = `
    ${spdCards([
      ["随访任务", stats.total], ["已完成", stats.done],
      ["完成率", stats.completion_rate + "%"],
      ["超期", stats.overdue, stats.overdue > 0],
      ["异常随访", stats.abnormal, stats.abnormal > 0],
    ])}
    ${panel("随访方案（诊断/手术/医嘱关键词命中）", `
      <p class="desc">没配关键词的方案不匹配任何人——否则一个空方案会给全院出院患者都排上随访</p>
      ${table(["ID", "编码", "名称", "场景", "科室", "时间点(天)", "问卷", "执行角色", "预置", "状态", "操作"],
        rules, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.scene_name)}</td>
         <td>${esc(r.dept || "—")}</td><td>${(r.points || []).join("、")}</td>
         <td>${esc(r.questionnaire_code || "—")}</td><td>${esc(r.executor_role)}</td>
         <td>${r.preset ? "是" : "否"}</td>
         <td>${r.active === false ? '<span class="tag">停用</span>' : '<span class="tag green">启用</span>'}</td>
         <td><button class="btn secondary" data-rule-edit="${r.id}" data-name="${esc(r.name)}" data-dept="${esc(r.dept || "")}"
              data-points="${(r.points || []).join(",")}" data-quest="${esc(r.questionnaire_code || "")}"
              data-role="${esc(r.executor_role || "")}" data-active="${r.active === false ? 0 : 1}">编辑</button></td></tr>`)}
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
      </form><p class="msg" id="spd-fu-msg"></p>`)}
    ${panel("随访问卷与异常分级", `
      ${table(["ID", "编码", "名称", "场景", "题目数", "异常规则", "跟踪科室", "处置角色", "状态", "操作"],
        questionnaires, (q) =>
        `<tr><td>${q.id}</td><td>${esc(q.code)}</td><td>${esc(q.name)}</td><td>${esc(q.scene_name)}</td>
         <td>${(q.items || []).length}</td><td>${(q.abnormal_rules || []).length}</td>
         <td>${esc(q.track_dept || "—")}</td><td>${esc(q.handle_role)}</td>
         <td>${q.active === false ? '<span class="tag">停用</span>' : '<span class="tag green">启用</span>'}</td>
         <td><button class="btn secondary" data-quest-edit="${q.id}" data-name="${esc(q.name)}" data-dept="${esc(q.track_dept || "")}"
              data-role="${esc(q.handle_role || "")}" data-active="${q.active === false ? 0 : 1}">编辑</button></td></tr>`)}
      <form class="inline" id="spd-quest-form" style="margin-top:10px">
        <input name="code" placeholder="问卷编码" required>
        <input name="name" placeholder="问卷名称" required>
        <select name="scene">
          <option value="inpatient">出院</option><option value="outpatient">门诊</option>
          <option value="surgery">术后</option><option value="checkup">体检</option>
        </select>
        <input name="track_dept" placeholder="跟踪科室">
        <input name="items" placeholder="题目：key:题目:类型:选项1/选项2，分号分隔（类型 single / multi / number）"
          style="min-width:360px">
        <button>新建问卷</button>
      </form>
      <p class="desc">异常判定规则（答案命中任一条即标记异常，中、重度派处置任务）；字段取自上面填的题目</p>
      <div id="spd-quest-rules"></div>
      <p class="msg" id="spd-quest-msg"></p>`)}
    ${panel("随访看板", `
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
      <div id="spd-fu-list"></div>
      <div id="spd-fu-detail"></div>`)}
    ${panel("患者健康日历", `
      <p class="desc">某一天这位患者的随访、复诊与任务安排一屏看——随访前先看当天还有什么，别重复打扰</p>
      <form class="inline" id="spd-cal-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="day" placeholder="日期 YYYY-MM-DD（留空 = 今天）">
        <button class="secondary">查看</button>
      </form><p class="msg" id="spd-cal-msg"></p>
      <div id="spd-cal-box"></div>`)}
    ${panel("随访质量", `
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
      </form><p class="msg" id="spd-qc-msg"></p>
      ${table(["ID", "批次", "科室", "被抽随访", "患者", "计划日期", "结论", "方式", "备注", "操作"], qcSamples, (s) =>
        `<tr><td>${s.id}</td><td>${esc(s.batch)}</td><td>${esc(s.dept || "—")}</td><td>${s.record_id}</td>
         <td>${esc(s.record ? (s.record.patient_name || String(s.record.patient_id)) : "—")}</td>
         <td>${esc(s.record ? s.record.planned_at : "—")}</td>
         <td>${spdTag(SPD_QC_RESULT, s.result || "pending")}</td><td>${esc(SPD_QC_METHOD[s.method] || s.method || "—")}</td>
         <td>${esc(s.note || "—")}</td>
         <td>${s.result ? "—" : `<button class="btn secondary" data-qc-judge="${s.id}">判定</button>`}</td></tr>`)}`)}
    ${panel("呼叫任务与录音", `
      ${table(["ID", "患者", "号码", "来源", "状态", "时长(秒)", "录音", "创建时间", "操作"], calls, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.patient_name)}</td><td>${esc(c.phone || "—")}</td>
         <td>${esc(c.ref_type)}</td>
         <td>${c.status === "connected" ? '<span class="tag green">已接通</span>'
            : c.status === "failed" ? '<span class="tag red">未接通</span>'
            : c.status === "cancelled" ? '<span class="tag">已取消</span>'
            : '<span class="tag orange">待呼叫</span>'}</td>
         <td>${c.duration_s}</td><td>${c.record_url ? "有" : "—"}</td>
         <td>${esc(c.created_at.replace("T", " ").slice(0, 16))}</td>
         <td>${c.status === "pending" ? `<button class="btn secondary" data-call-result="${c.id}">回写结果</button>` : "—"}</td></tr>`)}`)}`;

  const drawRecords = async (query) => {
    const qs = new URLSearchParams({ limit: "30", ...(query || {}) }).toString();
    const rows = await api(`/api/spd/followup-records?${qs}`);
    $("#spd-fu-list").innerHTML = table(
      ["ID", "患者", "场景", "计划日期", "执行日期", "渠道", "异常", "状态", "操作"],
      rows, (r) =>
      `<tr><td>${r.id}</td><td>${esc(r.patient_name)}</td><td>${esc(r.scene_name)}</td>
       <td>${esc(r.planned_at)}</td><td>${esc(r.executed_at || "—")}</td>
       <td>${esc(SPD_FU_CHANNELS[r.channel] || r.channel)}</td>
       <td>${r.abnormal_level && r.abnormal_level !== "none"
          ? `<span class="tag ${r.abnormal_level === "high" ? "red" : "orange"}">${esc(r.abnormal_level_name)}</span>`
          : "—"}</td>
       <td><span class="tag ${r.status === "done" ? "green" : r.status === "planned" ? "orange" : ""}">${esc(r.status_name)}</span></td>
       <td><button class="btn secondary" data-fu-ctx="${r.id}">前置资料</button>
           ${r.status === "planned"
          ? `<button class="btn secondary" data-fu-exec="${r.id}">执行</button>
             <button class="btn secondary" data-fu-call="${r.id}" data-pid="${r.patient_id}">转呼叫</button>`
          : ""}
           ${r.status !== "done" ? `<button class="btn secondary" data-fu-adjust="${r.id}" data-status="${esc(r.status)}"
             data-planned="${esc(r.planned_at || "")}" data-channel="${esc(r.channel || "")}">调整</button>` : ""}</td></tr>`);
  };
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
  $("#spd-cal-form").onsubmit = async (e) => {
    e.preventDefault();
    const q = formJson(e.target, ["patient_id"]);
    try {
      const cal = await api(`/api/spd/health-calendar?patient_id=${q.patient_id}${q.day ? `&day=${encodeURIComponent(q.day)}` : ""}`);
      $("#spd-cal-box").innerHTML = `
        <p class="desc">${esc(cal.day)}：随访 ${(cal.followups || []).length} · 复诊 ${(cal.revisits || []).length} · 任务 ${(cal.tasks || []).length}</p>
        ${table(["随访ID", "场景", "渠道", "计划日期", "状态"], cal.followups || [], (f) =>
          `<tr><td>${f.id}</td><td>${esc(f.scene_name)}</td><td>${esc(SPD_FU_CHANNELS[f.channel] || f.channel)}</td>
           <td>${esc(f.planned_at)}</td><td>${esc(f.status_name)}</td></tr>`)}
        ${table(["复诊ID", "科室", "项目", "状态"], cal.revisits || [], (v) =>
          `<tr><td>${v.id}</td><td>${esc(v.dept || "—")}</td><td>${esc(v.items || "—")}</td><td>${spdTag(SPD_REVISIT_STATUS, v.status)}</td></tr>`)}
        ${table(["任务ID", "标题", "类型", "状态"], cal.tasks || [], (t) =>
          `<tr><td>${t.id}</td><td>${esc(t.title)}</td><td>${esc(SPD_TASK_TYPES[t.task_type] || t.task_type)}</td><td>${spdTag(SPD_TASK_STATUS, t.status)}</td></tr>`)}`;
      setMsg("#spd-cal-msg", "");
    } catch (err) { setMsg("#spd-cal-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const el = (attr) => e.target.closest(`[${attr}]`);
    const exec = el("data-fu-exec"), call = el("data-fu-call"), ctx = el("data-fu-ctx"), adjust = el("data-fu-adjust");
    const ruleEdit = el("data-rule-edit"), questEdit = el("data-quest-edit");
    const qcJudge = el("data-qc-judge"), callResult = el("data-call-result");
    if (ruleEdit) {
      const form = await spdModal("编辑随访方案（时间点留空不改）", [
        { name: "name", label: "名称", value: ruleEdit.dataset.name, required: true },
        { name: "dept", label: "科室", value: ruleEdit.dataset.dept },
        { name: "points", label: "时间点（天，逗号分隔，如 7,30,90）", value: ruleEdit.dataset.points },
        { name: "questionnaire_code", label: "问卷编码", value: ruleEdit.dataset.quest },
        { name: "executor_role", label: "执行角色（nurse / doctor / village_doctor…）", value: ruleEdit.dataset.role },
        { name: "active", label: "状态", type: "select", value: ruleEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      const body = { name: form.name, dept: form.dept || "", questionnaire_code: form.questionnaire_code || "",
        executor_role: form.executor_role || "nurse", active: form.active === "1" };
      if (form.points) {
        body.points = form.points.split(/[，,\s]+/).filter(Boolean).map(Number);
        if (body.points.some((n) => !Number.isInteger(n) || n < 0)) return setMsg("#spd-fu-msg", "时间点须是非负整数", false);
      }
      return postAction(`/api/spd/followup-rules/${ruleEdit.dataset.ruleEdit}`, body, "#spd-fu-msg", "PATCH");
    }
    if (questEdit) {
      const form = await spdModal("编辑随访问卷（题目与异常规则请新建问卷）", [
        { name: "name", label: "名称", value: questEdit.dataset.name, required: true },
        { name: "track_dept", label: "跟踪科室", value: questEdit.dataset.dept },
        { name: "handle_role", label: "处置角色（doctor / nurse…）", value: questEdit.dataset.role },
        { name: "active", label: "状态", type: "select", value: questEdit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/questionnaires/${questEdit.dataset.questEdit}`, {
        name: form.name, track_dept: form.track_dept || "", handle_role: form.handle_role || "doctor", active: form.active === "1",
      }, "#spd-quest-msg", "PATCH");
    }
    if (ctx) {
      try {
        const c = await api(`/api/spd/followup-records/${ctx.dataset.fuCtx}/context`);
        const pt = c.patient;
        $("#spd-fu-detail").innerHTML = panel(`随访前置资料 · ${pt ? pt.name : "患者"} · 记录 #${c.record.id}`, `
          ${pt ? `<p class="desc">${esc(pt.gender || "")} · 出生 ${esc(pt.birth_date || "—")} · 电话 ${esc(pt.phone || "—")}</p>` : ""}
          ${c.questionnaire ? `<p class="desc">本次问卷：${esc(c.questionnaire.name)}（${(c.questionnaire.items || []).length} 题，${(c.questionnaire.abnormal_rules || []).length} 条异常规则）</p>` : ""}
          <h4>近期就诊</h4>
          ${table(["类型", "诊断", "医生", "时间"], c.encounters || [], (x) =>
            `<tr><td>${esc(x.encounter_type_name)}</td><td>${esc(x.diagnosis_name || "—")}</td><td>${esc(x.doctor_name || "—")}</td>
             <td>${esc((x.created_at || "").replace("T", " ").slice(0, 16))}</td></tr>`)}
          <h4>住院</h4>
          ${table(["入院", "出院", "诊断", "医生", "状态"], c.admissions || [], (a) =>
            `<tr><td>${esc((a.admitted_at || "").slice(0, 10))}</td><td>${esc((a.discharged_at || "").slice(0, 10) || "—")}</td>
             <td>${esc(a.diagnosis_name || "—")}</td><td>${esc(a.doctor_name || "—")}</td><td>${esc(a.status_name)}</td></tr>`)}
          <h4>历史随访</h4>
          ${table(["ID", "场景", "执行日期", "渠道", "异常", "结果"], c.history || [], (h) =>
            `<tr><td>${h.id}</td><td>${esc(h.scene_name)}</td><td>${esc(h.executed_at || "—")}</td>
             <td>${esc(SPD_FU_CHANNELS[h.channel] || h.channel)}</td><td>${esc(h.abnormal_level_name || "—")}</td><td>${esc(h.result || "—")}</td></tr>`)}`);
      } catch (err) { setMsg("#spd-fu-msg", err.message, false); }
      return;
    }
    if (adjust) {
      const form = await spdModal("调整随访任务（留空的项不改）", [
        { name: "planned_at", label: "计划日期 YYYY-MM-DD", value: adjust.dataset.planned },
        { name: "executor_id", label: "执行人用户ID", type: "number" },
        { name: "channel", label: "渠道", type: "select", value: "",
          options: [{ value: "", label: "不改" }, ...Object.entries(SPD_FU_CHANNELS).map(([k, v]) => ({ value: k, label: v }))] },
        { name: "status", label: "任务状态", type: "select", value: "",
          options: [{ value: "", label: "不改" }, { value: "removed", label: "移除" }, { value: "planned", label: "恢复为待随访" }] },
      ]);
      if (!form) return;
      const body = {};
      if (form.planned_at && form.planned_at !== adjust.dataset.planned) body.planned_at = form.planned_at;
      if (form.executor_id) body.executor_id = form.executor_id;
      if (form.channel) body.channel = form.channel;
      if (form.status) body.status = form.status;
      if (!Object.keys(body).length) return setMsg("#spd-fu-msg", "没有要改的项", false);
      try {
        await api(`/api/spd/followup-records/${adjust.dataset.fuAdjust}`, { method: "PATCH", body: JSON.stringify(body) });
        await drawRecords();
        setMsg("#spd-fu-msg", "随访任务已调整");
      } catch (err) { setMsg("#spd-fu-msg", err.message, false); }
      return;
    }
    if (qcJudge) {
      const form = await spdModal("记录抽查结论", [
        { name: "result", label: "结论", type: "select", value: "pass",
          options: Object.entries(SPD_QC_RESULT).filter(([k]) => k !== "pending").map(([k, v]) => ({ value: k, label: v[0] })) },
        { name: "method", label: "核查方式", type: "select", value: "record",
          options: Object.entries(SPD_QC_METHOD).map(([k, v]) => ({ value: k, label: v })) },
        { name: "note", label: "说明", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/qc-samples/${qcJudge.dataset.qcJudge}/result`,
        { result: form.result, method: form.method, note: form.note || "" }, "#spd-qc-msg");
    }
    if (callResult) {
      const form = await spdModal("回写通话结果（接通结果会同步写回随访记录）", [
        { name: "status", label: "结果", type: "select", value: "connected",
          options: [{ value: "connected", label: "已接通" }, { value: "failed", label: "未接通" }, { value: "cancelled", label: "已取消" }] },
        { name: "duration_s", label: "通话时长（秒）", type: "number", value: 0 },
        { name: "record_url", label: "录音地址" },
        { name: "result", label: "沟通结果", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/call-tasks/${callResult.dataset.callResult}/result`, {
        status: form.status, duration_s: form.duration_s || 0, record_url: form.record_url || "", result: form.result || "",
      }, "#spd-fu-msg");
    }
    if (exec) {
      // 逐题作答（P1-122）：原先只填渠道与结果、answers 恒为空，问卷的异常分级从界面上永远不触发。
      // 问卷取这条记录的前置资料——按记录上的问卷编码查，停用的问卷也在（问卷目录只列在用的）
      let quest = null;
      try {
        quest = (await api(`/api/spd/followup-records/${exec.dataset.fuExec}/context`)).questionnaire;
      } catch (err) { return setMsg("#spd-fu-msg", err.message, false); }
      const items = quest ? quest.items || [] : [];
      const form = await spdModal(quest ? `执行随访 · ${quest.name}` : "执行随访", [
        { name: "channel", label: "随访渠道", type: "select", value: "phone",
          options: [{ value: "phone", label: "电话" }, { value: "wechat", label: "微信" },
                    { value: "sms", label: "短信" }, { value: "visit", label: "面访" }] },
        ...spdQuestionFields(items, "q_"),
        { name: "result", label: "随访结果", type: "textarea" },
      ]);
      if (!form) return;
      return postAction(`/api/spd/followup-records/${exec.dataset.fuExec}/execute`, {
        channel: form.channel || "phone", result: form.result, answers: spdCollectAnswers(items, form, "q_"),
      }, "#spd-fu-msg");
    }
    if (call) {
      return postAction("/api/spd/call-tasks", {
        patient_id: Number(call.dataset.pid), ref_type: "followup",
        ref_id: Number(call.dataset.fuCall),
      }, "#spd-fu-msg");
    }
  };
  // 取数放最后：以上监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 renderSpdPath）
  await drawRecords();
  // P2-31 例外：下面的 onsubmit 闭包依赖 meta 构建的 abnormalEditor，提前挂会把窗口期提交从
  // 「兜底无效」变成「TypeError」，非零行为差；窗口期由 shared.js 的 document 层兜底护住。
  const meta = await spdMeta();
  const abnormalEditor = spdAbnormalRuleEditor($("#spd-quest-rules"), meta.operators);
  const questItems = $("#spd-quest-form").querySelector('[name="items"]');
  const syncQuestFields = () => abnormalEditor.setFields(spdParseQuestionItems(questItems.value)
    .filter((it) => it.key).map((it) => ({ key: it.key, name: it.title || it.key })));
  questItems.addEventListener("input", syncQuestFields);
  questItems.addEventListener("change", syncQuestFields);
  $("#spd-quest-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/questionnaires", {
      ...formJson(e.target), items: spdParseQuestionItems(questItems.value), abnormal_rules: abnormalEditor.value(),
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
    ${panel("报告模板", `
      <p class="desc">段落取数与工作台同源——报告是同一批数字的另一种排版，不是另存一份统计</p>
      ${table(["ID", "编码", "名称", "周期", "层级", "段落", "状态", "操作"], templates, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.code)}</td><td>${esc(t.name)}</td>
         <td>${esc({ daily: "日报", weekly: "周报", monthly: "月报", custom: "自定义" }[t.period] || t.period)}</td>
         <td>${esc(scopeNames[t.scope_level] || t.scope_level)}</td>
         <td>${(t.sections || []).map((s) => esc(s.title)).join("、")}</td>
         <td>${t.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-rpt-edit="${t.id}" data-name="${esc(t.name)}" data-period="${esc(t.period || "")}"
              data-scope="${esc(t.scope_level || "")}" data-active="${t.active ? 1 : 0}">编辑</button></td></tr>`)}
      <form class="inline" id="spd-rpt-gen" style="margin-top:10px">
        <select name="template_code">${templates.map((t) => `<option value="${esc(t.code)}">${esc(t.name)}</option>`).join("")}</select>
        <input name="org_id" type="number" placeholder="机构ID（留空取本机构）">
        <button>立即生成</button>
      </form><p class="msg" id="spd-rpt-msg"></p>`)}
    ${panel("推送任务", `
      <form class="inline" id="spd-rpttask-form">
        <select name="template_id">${templates.filter((t) => t.active).map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>
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
               ${t.status === "active" ? "暂停" : "启用"}</button></td></tr>`)}`)}
    ${panel("已生成报告", `
      ${table(["ID", "标题", "周期", "层级", "生成时间", "操作"], instances, (r) =>
        `<tr><td>${r.id}</td><td>${esc(r.title)}</td><td>${esc(r.period_label)}</td>
         <td>${esc(scopeNames[r.scope_level] || r.scope_level)}</td>
         <td>${esc(r.created_at.replace("T", " ").slice(0, 16))}</td>
         <td><button class="btn secondary" data-rpt-view="${r.id}">查看</button></td></tr>`)}
      <div id="spd-rpt-view"></div>`)}`;
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
    const edit = e.target.closest("[data-rpt-edit]");
    if (edit) {
      const form = await spdModal("编辑报告模板（段落取数与工作台同源，这里只改名称 / 周期 / 层级 / 启停）", [
        { name: "name", label: "名称", value: edit.dataset.name, required: true },
        { name: "period", label: "周期", type: "select", value: edit.dataset.period,
          options: Object.entries(SPD_REPORT_PERIODS).map(([k, v]) => ({ value: k, label: v })) },
        { name: "scope_level", label: "层级", type: "select", value: edit.dataset.scope,
          options: Object.entries(SPD_REPORT_SCOPES).map(([k, v]) => ({ value: k, label: v })) },
        { name: "active", label: "状态", type: "select", value: edit.dataset.active,
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
      ]);
      if (!form) return;
      return postAction(`/api/spd/report-templates/${edit.dataset.rptEdit}`, {
        name: form.name, period: form.period, scope_level: form.scope_level, active: form.active === "1",
      }, "#spd-rpt-msg", "PATCH");
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
        <label style="font-size:13px">定时推送（留空立即） <input name="send_at" type="datetime-local"></label>
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
       <td>${esc(m.source_name)}</td><td>${esc(m.note || "—")}</td></tr>`);
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
    if (close) {
      // P2-43：原先点一下就结束；结束后医生端不能再回复这次会话
      if (!await spdModal("结束咨询", [], {
        intro: "结束后这次会话关闭、不能再回复；患者端显示「已结束」，患者再发消息会开启新会话。" })) return;
      return postAction(`/api/spd/consults/${close.dataset.consultClose}/close`,
        null, "#spd-consult-msg");
    }
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
