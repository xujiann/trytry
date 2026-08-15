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
  submitted: ["待服务站复核", "orange"], station_reviewed: ["待卫生院审核", "orange"],
  township_reviewed: ["待县级接收", "orange"], accepted: ["已接收", ""],
  arrived: ["已到院", ""], down_referred: ["已下转", ""],
  closed: ["已闭环", "green"], rejected: ["已退回", "red"], withdrawn: ["已撤回", ""],
};

function spdTag(map, key) {
  const [text, cls] = map[key] || [key || "—", ""];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
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
      </form><p class="msg" id="spd-program-msg"></p>
      ${table(["编码", "名称", "口径", "版本", "阶段数", "纳入规则", "状态"],
        catalog.programs, (p) =>
        `<tr><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${p.category === "chronic" ? "慢病" : "专病"}</td>
         <td>${esc(p.version || "")}</td><td>${(p.stages || []).length}</td>
         <td>${(cfg.programs_without_rules || []).includes(p.code)
            ? '<span class="tag red">未配置</span>' : '<span class="tag green">已配置</span>'}</td>
         <td>${p.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td></tr>`)}</div>
    <div class="panel"><h3>数据源接入与运行监控</h3>
      <p class="desc">成功率按最近 100 次同步计算；超过 24 小时未同步计入陈旧</p>
      ${spdCards([["数据源", ds.total], ["异常", ds.failed, ds.failed > 0],
                  ["延迟", ds.delayed, ds.delayed > 0], ["24h未同步", ds.stale_over_24h, ds.stale_over_24h > 0],
                  ["平均成功率", ds.avg_success_rate + "%"]])}
      ${table(["编码", "名称", "类型", "频率(分)", "最近同步", "行数", "延迟(ms)", "成功率", "状态"],
        sources, (s) =>
        `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td><td>${esc(s.source_type)}</td>
         <td>${s.freq_minutes}</td><td>${esc(s.last_sync_at ? s.last_sync_at.replace("T", " ").slice(0, 16) : "—")}</td>
         <td>${s.last_rows}</td><td>${s.last_latency_ms}</td><td>${s.success_rate}%</td>
         <td>${s.status === "running" ? '<span class="tag green">正常</span>'
            : s.status === "delayed" ? '<span class="tag orange">延迟</span>'
            : '<span class="tag red">异常</span>'}</td></tr>`)}</div>`;
  $("#spd-program-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/spd/programs", formJson(e.target), "#spd-program-msg");
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
    "统筹调度中枢：统一待办、目标池分发与认领、在途转诊、生命周期确认";
  const [wb, candidates, catalog] = await Promise.all([
    api("/api/spd/workbench/center"),
    api("/api/spd/candidates?status=target&limit=50"),
    spdCatalog(),
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
        { color: "#0a4d78", unit: " 单" })}</div>`;
  $("#spd-dist-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["team_id", "assigned_user_id"]);
    body.candidate_ids = String(body.candidate_ids || "").split(/[，,\s]+/)
      .filter(Boolean).map(Number);
    return postAction("/api/spd/candidates/distribute", body, "#spd-dist-msg");
  };
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
      <div id="spd-life-list"></div></div>`;

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
      ["ID", "患者", "病种", "阶段", "风险", "机构", "团队", "建档", "下次随访", "状态"],
      rows, (e) =>
      `<tr><td>${e.id}</td><td>${esc(e.patient_name || e.patient_id)}</td>
       <td>${esc(e.program_code)}</td><td>${esc(e.stage || "—")}</td>
       <td>${spdTag(SPD_RISK, e.risk_level)}</td><td>${e.org_id}</td>
       <td>${e.team_id ?? "—"}</td>
       <td>${e.archived ? '<span class="tag green">已建档</span>' : '<span class="tag orange">待完善</span>'}</td>
       <td>${esc(e.next_followup_at || "—")}</td>
       <td>${e.status === "active" ? '<span class="tag green">在管</span>'
          : '<span class="tag">' + esc(e.status) + "</span>"}</td></tr>`);
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
         <td><button class="btn secondary" data-tpl-node="${t.id}">加节点</button>
             <button class="btn secondary" data-tpl-pub="${t.id}">发布</button>
             <button class="btn secondary" data-tpl-copy="${t.id}">复制</button></td></tr>`)}</div>
    <div class="panel"><h3>启动患者路径</h3>
      <form class="inline" id="spd-inst-form">
        <input name="enrollment_id" type="number" placeholder="纳管档案ID" required>
        <select name="template_id">
          ${catalog.path_templates.map((t) => `<option value="${t.id}">${esc(t.name)}</option>`).join("")}
        </select>
        <button>启动路径</button>
      </form><p class="msg" id="spd-inst-msg"></p>
      <div id="spd-inst-list"></div></div>
    <div class="panel"><h3>任务中心</h3>
      <form class="inline" id="spd-task-filter">
        <select name="task_type"><option value="">全部类型</option>
          ${Object.entries(SPD_TASK_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <select name="status"><option value="">全部状态</option>
          ${Object.entries(SPD_TASK_STATUS).map(([k, v]) => `<option value="${k}">${esc(v[0])}</option>`).join("")}</select>
        <label style="font-size:13px"><input type="checkbox" name="mine" value="true"> 只看我的</label>
        <button class="secondary">查询</button>
      </form><p class="msg" id="spd-task-msg"></p>
      <div id="spd-task-list"></div></div>`;

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
       <td>${i.status === "running"
          ? `<button class="btn secondary" data-adv="${i.id}">推进节点</button>` : "—"}</td></tr>`);
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
       <td><button class="btn secondary" data-task-claim="${t.id}">接收</button>
           <button class="btn secondary" data-task-urge="${t.id}">催办</button>
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
    const adv = el("data-adv");
    const claim = el("data-task-claim"), urge = el("data-task-urge"), done = el("data-task-done");
    if (node) {
      const key = prompt("节点 key（英文，如 assess）");
      if (!key) return;
      return postAction(`/api/spd/path-templates/${node.dataset.tplNode}/nodes`, {
        key, name: prompt("节点名称") || key,
        stage: prompt("所属阶段（可留空）") || "",
        due_days: Number(prompt("时限（天）", "7") || 7),
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
      return postAction(`/api/spd/tasks/${done.dataset.taskDone}/complete`,
        { result: { note: prompt("办理结果") || "" } }, "#spd-task-msg");
    }
  };
}

/* ============================================================
 * 8. 逐级转诊
 * ==========================================================*/

async function renderSpdReferral() {
  $("#page-desc").textContent =
    "村医 → 服务站 → 卫生院 → 县级医院逐级转诊：分级审核、到院有效判定、下转随访接收闭环";
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
         <td><button class="btn secondary" data-ref-pass="${c.id}">通过</button>
             <button class="btn secondary" data-ref-reject="${c.id}">退回</button>
             <button class="btn secondary" data-ref-arrive="${c.id}">到院</button>
             <button class="btn secondary" data-ref-down="${c.id}">下转</button>
             <button class="btn secondary" data-ref-recv="${c.id}">随访接收</button></td></tr>`)}</div>
    <div class="panel"><h3>转诊触发规则</h3>
      <p class="desc">命中规则默认只提示不自动开单——批量随访录入时自动开单会瞬间产生几十张单子</p>
      ${table(["编码", "名称", "病种", "处理层级", "条件数", "自动建任务", "状态"], rules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.program_code || "全部")}</td>
         <td>${esc(r.handle_level)}</td><td>${(r.conditions || []).length}</td>
         <td>${r.auto_task ? "是" : "否"}</td>
         <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td></tr>`)}</div>
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
  $("#page-body").onclick = async (e) => {
    const pass = e.target.closest("[data-ref-pass]"), reject = e.target.closest("[data-ref-reject]");
    const arrive = e.target.closest("[data-ref-arrive]"), down = e.target.closest("[data-ref-down]");
    const recv = e.target.closest("[data-ref-recv]");
    if (pass) return postAction(`/api/spd/referrals/${pass.dataset.refPass}/review`,
      { action: "pass", opinion: prompt("审核意见") || "" }, "#spd-ref-msg");
    if (reject) return postAction(`/api/spd/referrals/${reject.dataset.refReject}/review`,
      { action: "reject", opinion: prompt("退回理由") || "" }, "#spd-ref-msg");
    if (arrive) return postAction(`/api/spd/referrals/${arrive.dataset.refArrive}/arrive`,
      { effective_visit: true }, "#spd-ref-msg");
    if (down) {
      const org = Number(prompt("下转目标机构ID") || 0);
      if (!org) return;
      return postAction(`/api/spd/referrals/${down.dataset.refDown}/down`,
        { target_org_id: org, stable: true }, "#spd-ref-msg");
    }
    if (recv) return postAction(`/api/spd/referrals/${recv.dataset.refRecv}/receive-followup`,
      { opinion: prompt("接收意见") || "" }, "#spd-ref-msg");
  };
}

/* ============================================================
 * 9. 考核与积分
 * ==========================================================*/

async function renderSpdAssess() {
  $("#page-desc").textContent =
    "指标库 → 分级考核方案 → 自动取数计分 → 扣分下钻；村医积分与商品兑换核销";
  const [indicators, plans, scores, goods, accounts] = await Promise.all([
    api("/api/spd/indicators?limit=50"), api("/api/spd/assess-plans"),
    api("/api/spd/scores?limit=30"), api("/api/spd/goods"),
    api("/api/spd/point-accounts?limit=20"),
  ]);
  const objectNames = { org: "机构", doctor: "医生", village_doctor: "村医", team: "团队" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>考核指标库</h3>
      <p class="desc">取数口径 + 公式（AST 白名单求值）+ 评分规则三段式，各县只需调权重与目标值</p>
      ${table(["编码", "名称", "对象", "取数口径", "公式", "权重", "目标值", "版本", "状态"],
        indicators, (i) =>
        `<tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td>
         <td>${esc(objectNames[i.object_type] || i.object_type)}</td>
         <td>${esc(i.data_source)}</td><td><code>${esc(i.formula || "—")}</code></td>
         <td>${i.weight}</td><td>${i.target_value ?? "—"}</td><td>${esc(i.version)}</td>
         <td>${i.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td></tr>`)}</div>
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
         <td><button class="btn secondary" data-run="${p.id}">跑分</button></td></tr>`)}</div>
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
      ${table(["编码", "名称", "所需积分", "库存"], goods, (g) =>
        `<tr><td>${esc(g.code)}</td><td>${esc(g.name)}</td><td>${g.points}</td>
         <td>${g.stock}</td></tr>`)}</div>`;
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
  $("#page-body").onclick = async (e) => {
    const run = e.target.closest("[data-run]"), score = e.target.closest("[data-score]");
    if (run) {
      const period = prompt("考核周期（如 2026-08 / 2026-Q3 / 2026）",
        new Date().toISOString().slice(0, 7));
      if (!period) return;
      return postAction("/api/spd/scores/run",
        { plan_id: Number(run.dataset.run), period }, "#spd-plan-msg");
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
      ${table(["编码", "名称", "场景", "科室", "时间点(天)", "问卷", "执行角色", "预置"],
        rules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.scene)}</td>
         <td>${esc(r.dept || "—")}</td><td>${(r.points || []).join("、")}</td>
         <td>${esc(r.questionnaire_code || "—")}</td><td>${esc(r.executor_role)}</td>
         <td>${r.preset ? "是" : "否"}</td></tr>`)}
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
      ${table(["编码", "名称", "场景", "题目数", "异常规则", "跟踪科室", "处置角色"],
        questionnaires, (q) =>
        `<tr><td>${esc(q.code)}</td><td>${esc(q.name)}</td><td>${esc(q.scene)}</td>
         <td>${(q.items || []).length}</td><td>${(q.abnormal_rules || []).length}</td>
         <td>${esc(q.track_dept || "—")}</td><td>${esc(q.handle_role)}</td></tr>`)}</div>
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
         <td>${esc(c.created_at.replace("T", " ").slice(0, 16))}</td></tr>`)}</div>`;

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
       <td>${r.status === "planned"
          ? `<button class="btn secondary" data-fu-exec="${r.id}">执行</button>
             <button class="btn secondary" data-fu-call="${r.id}" data-pid="${r.patient_id}">转呼叫</button>`
          : "—"}</td></tr>`);
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
      return postAction(`/api/spd/followup-records/${exec.dataset.fuExec}/execute`, {
        channel: "phone", result: prompt("随访结果") || "", answers: {},
      }, "#spd-fu-msg");
    }
    if (call) {
      return postAction("/api/spd/call-tasks", {
        patient_id: Number(call.dataset.pid), ref_type: "followup",
        ref_id: Number(call.dataset.fuCall),
      }, "#spd-fu-msg");
    }
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
      ${table(["编码", "名称", "周期", "层级", "段落", "状态"], templates, (t) =>
        `<tr><td>${esc(t.code)}</td><td>${esc(t.name)}</td>
         <td>${esc({ daily: "日报", weekly: "周报", monthly: "月报", custom: "自定义" }[t.period] || t.period)}</td>
         <td>${esc(scopeNames[t.scope_level] || t.scope_level)}</td>
         <td>${(t.sections || []).map((s) => esc(s.title)).join("、")}</td>
         <td>${t.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td></tr>`)}
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
