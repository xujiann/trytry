/* 管理端 · 页面（二）：公卫协同、妇幼老年、疫苗与监测、教育培训等。 */

async function renderInfectiousDir() {
  $("#page-desc").textContent = "法定传染病目录（甲类2小时/乙丙类24小时报告时限）与迟报清单";
  const [diseases, late] = await Promise.all([
    api("/api/infectious/diseases"), api("/api/infectious/late-reports")]);
  const CAT = { A: ["甲类", "red"], B: ["乙类", "orange"], C: ["丙类", ""] };
  $("#page-body").innerHTML = `
    ${late.length ? panel(`⚠ 迟报清单（${late.length}）`, `${
      table(["病例ID", "病种", "类别", "发病日期", "报告时间", "迟报"], late, (l) => {
        return `<tr><td>${l.case_id}</td><td>${esc(l.disease_name)}</td><td>${statusTag(CAT, l.category)}</td>
          <td>${esc(l.onset_date)}</td><td>${esc((l.reported_at || "").slice(0, 16).replace("T", " "))}</td>
          <td><span class="tag red">迟报 ${l.days_late} 天</span></td></tr>`;
      })}`, { accent: "#c62828" }) : panel("迟报清单", '<p style="color:#8a939e">无迟报病例</p>')}
    ${panel(`法定传染病目录（${diseases.length}）`, `${
      table(["编码", "名称", "类别", "报告时限"], diseases, (d) => {
        return `<tr><td>${esc(d.code)}</td><td>${esc(d.name)}</td>
          <td>${statusTag(CAT, d.category)}</td><td>${d.report_hours} 小时</td></tr>`;
      })}`)}`;
}

const MILESTONES = { onset: "发病", call: "呼救", depart: "出车", arrive_scene: "到达现场", arrive_hospital: "到达医院", treatment: "开始救治" };
const CHANNELS = { "": "普通", chest_pain: "胸痛", stroke: "卒中", trauma: "创伤" };

async function renderEmTimeline() {
  $("#page-desc").textContent = "急救绿道：通道建单 → 节点录入 → 时间轴时效展示";
  // 三种通道的病例单独取一遍、排在最前（P2-1369，同 P2-408 / P2-456）：清单只回全县最新 200 起、普通呼救占大头，被挤出
  // 窗口的胸痛 / 卒中 / 创伤病例就没有一行给「录节点 / 时间轴」——节点多半是事后补录的（P2-1213）
  const [recent, ...channels] = await Promise.all([api("/api/emergency/cases"),
    ...["chest_pain", "stroke", "trauma"].map((ch) => api(`/api/emergency/cases?channel_type=${ch}`))]);
  const cases = actionableFirst(recent, ...channels);
  $("#page-body").innerHTML = `
    ${panel("绿道建单", `
      <form class="inline" id="gc-form"><input name="location" placeholder="事发地点" required>
        <input name="symptom" placeholder="主诉">
        <select name="channel_type">${Object.entries(CHANNELS).map(([v, t]) => `<option value="${v}">${t}通道</option>`).join("")}</select>
        <input name="dest_org_id" type="number" placeholder="目标医院ID"><button>建单</button></form>
      <p class="msg" id="gc-msg"></p>`)}
    ${panel("急救事件", table(["ID", "地点", "主诉", "通道", "状态", "操作"], cases, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.location)}</td><td>${esc(c.symptom)}</td>
       <td><span class="tag ${c.channel_type ? "red" : ""}">${esc(CHANNELS[c.channel_type] || c.channel_type)}</span></td>
       <td>${esc(c.status_name)}</td>
       <td><button class="btn secondary" data-mile="${c.id}">录节点</button>
           <button class="btn" data-timeline="${c.id}">时间轴</button></td></tr>`))}
    <div class="panel hidden" id="gc-tl-panel"><h3>绿道时间轴</h3><div id="gc-tl"></div></div>`;
  $("#gc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id"]), "#gc-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.mile) {
        // P2-38：原先两连问——节点要按"1=发病，2=呼救…"输序号（输错一位就录成了别的节点），
        // 发生时刻再手打。换成表单：节点下拉、时刻必填；格式不对、时序矛盾、重复录入都由后端报人话。
        // 时刻刻意不预填"现在"：绿道时效（到院-救治等）取的正是它，顺手一点确定就会把补录的节点记成此刻。
        const form = await spdModal("录入绿道节点", [
          { name: "milestone", label: "节点", type: "select",
            options: Object.entries(MILESTONES).map(([value, label]) => ({ value, label })) },
          { name: "occurred_at", label: "发生时刻", required: true, placeholder: "如 2026-08-11 14:30" },
        ]);
        if (!form) return;
        await api(`/api/emergency/cases/${d.mile}/milestones`, { method: "POST",
          body: JSON.stringify({ milestone: form.milestone, occurred_at: form.occurred_at }) });
        route();
      }
      if (d.timeline) {
        const tl = await api(`/api/emergency/cases/${d.timeline}/timeline`);
        $("#gc-tl-panel").classList.remove("hidden");
        $("#gc-tl").innerHTML = `<p style="font-size:13px">通道：<span class="tag red">${CHANNELS[tl.channel_type] || "普通"}</span>
          已记录 ${tl.recorded_count}/6</p>` +
          table(["节点", "时刻", "状态"], tl.timeline, (m) =>
            `<tr><td>${esc(m.name)}</td><td>${esc(m.occurred_at || "—")}</td>
             <td><span class="tag ${m.recorded ? "green" : "orange"}">${m.recorded ? "已记录" : "缺失"}</span></td></tr>`);
      }
    } catch (err) { setMsg("#gc-msg", err.message, false); }
  };
}

async function renderDrgs() {
  $("#page-desc").textContent = "DRGs 分析：62 组目录（多关键词 + 主手术入组，未匹配落 QY）、机构 CMI、MDC 汇总";
  // 机构 CMI 等统计只给管理层（后端 require_roles("director")），原先与目录放在同一个 Promise.all 里：医生 / 经办打开这页，
  // 统计一个 403 整页报错，同页给一线的事中预警、事前提示也跟着够不着（P2-459）。统计取不到就在原位说为什么，其余照常
  const [groups, stats] = await Promise.all([api("/api/drgs/groups"),
    api("/api/drgs/stats").catch((err) => ({ orgs: [], mdcs: [], groups: [], error: err.message }))]);
  const canGroup = currentRole() === "admin";   // 建组、调权、编辑、启停同一权限（后端 require_admin）
  const drawAlerts = async (mult) => {
    try {
      const a = await api(`/api/drgs/in-stay-alerts?los_multiplier=${mult}`);
      $("#drg-alerts").innerHTML = `
        <p class="desc">业务日 ${esc(a.today)}，倍数 ${a.los_multiplier}；
          <b>未入组的在院病例 ${a.ungrouped_in_stay} 例</b>（没有 DRG 就没有同组均值可比，不参与预警）。</p>
        ${table(["住院号", "患者", "机构", "DRG", "已住(天)", "同组均值", "历史例数", "超出倍数"], a.alerts, (r) =>
          `<tr><td>${r.admission_id}</td><td>${r.patient_id}</td><td>${r.org_id}</td>
           <td><span class="tag">${esc(r.drg_code)}</span></td><td><b>${r.stayed_days}</b></td>
           <td>${r.baseline_avg_days}</td><td>${r.baseline_cases}</td>
           <td><span class="tag red">${r.over_ratio}×</span></td></tr>`)}
        <h3 style="margin-top:14px">基线不足，未预警（单列报出，不是"没问题"）</h3>
        ${table(["住院号", "DRG", "历史例数", "已住(天)"], a.insufficient_baseline, (r) =>
          `<tr><td>${r.admission_id}</td><td><span class="tag">${esc(r.drg_code)}</span></td>
           <td><span class="tag orange">${r.history_cases}</span></td><td>${r.stayed_days}</td></tr>`)}
        <p class="desc">${esc(a.caliber)}</p>`;
    } catch (err) { $("#drg-alerts").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  // ADR-0009 第五批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 三个统计面板"有数据才渲染"，条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${stats.error ? panel("机构 CMI 与组均费用", `<p class="msg">${esc(stats.error)}</p>`) : ""}
    ${stats.orgs.length ? panel("机构 CMI 对比（病例组合指数 = Σ权重 / 正式入组例数，QY 兜底组不计入）",
      table(["机构", "出院病例", "正式入组", "入组率", "QY兜底", "兜底率", "CMI", "均次费用"], stats.orgs, (o) =>
        `<tr><td>${esc(o.org_name)}</td><td>${o.cases}</td><td>${o.grouped}</td>
         <td>${o.grouped_pct}%</td><td>${o.fallback}</td>
         <td><span class="tag ${o.fallback_pct > 10 ? "red" : "green"}">${o.fallback_pct}%</span></td>
         <td><b>${o.cmi}</b></td><td>${o.avg_cost} 元</td></tr>`)) : ""}
    ${(stats.mdcs || []).length ? panel("按 MDC（主要诊断大类）汇总",
      table(["MDC", "名称", "分组数", "例数", "CMI", "均次费用"], stats.mdcs, (m) =>
        `<tr><td>${esc(m.mdc)}${m.fallback ? ' <span class="tag red">兜底</span>' : ""}</td><td>${esc(m.mdc_name)}</td>
         <td>${m.groups}</td><td>${m.cases}</td><td>${m.cmi}</td><td>${m.avg_cost} 元</td></tr>`)) : ""}
    ${stats.groups.length ? panel("组均费用",
      barChart(stats.groups.map((g) => [`${g.drg_code} ${g.drg_name}`, g.avg_cost]), { unit: " 元" })) : ""}
    ${panel("分组目录（admin 可增补、调权、编辑、停用 / 启用）", `<p class="msg" id="drg-msg"></p>${canGroup ? `
      <form class="inline" id="drg-group-form" style="margin-bottom:8px">
        <input name="code" placeholder="分组编码" required style="width:110px">
        <input name="name" placeholder="分组名称" required>
        <input name="base_weight" type="number" step="0.0001" min="0.0001" placeholder="基准权重（> 0）" required style="width:130px">
        <input name="mdc" placeholder="MDC" style="width:70px">
        <input name="mdc_name" placeholder="MDC 名称" style="width:130px">
        <input name="keywords" placeholder="主诊断关键词（逗号分隔）" style="min-width:190px">
        <input name="procedure_keywords" placeholder="主手术关键词（逗号分隔）" style="min-width:190px">
        <label style="font-size:13px"><input type="checkbox" name="require_procedure" value="true"> 必须命中主手术</label>
        <button>新增分组</button>
      </form>` : ""}${
      table(["编码", "MDC", "名称", "基准权重", "主诊断关键词", "主手术关键词", "状态", "操作"], groups, (g) =>
        `<tr><td>${esc(g.code)}</td><td>${esc(g.mdc) || "—"}</td><td>${esc(g.name)}</td><td>${g.base_weight}</td>
         <td>${esc(g.keywords) || "—"}</td>
         <td>${esc(g.procedure_keywords) || "—"}${g.require_procedure ? ' <span class="tag orange">必须</span>' : ""}</td>
         <td><span class="tag ${g.active ? "green" : "red"}">${g.active ? "启用" : "停用"}</span></td>
         <td>${canGroup ? `<button class="btn secondary" data-drg-weight="${g.id}">调权</button>${g.is_fallback ? ""
           // 编辑与启停（P2-1535）：兜底组不摆——入组时它按编码取、不看启停，关键词对它也没有意义
           : ` <button class="btn secondary" data-drg-edit="${g.id}">编辑</button>
             <button class="btn secondary" data-drg-toggle="${g.id}" data-active="${g.active ? 1 : 0}">${g.active ? "停用" : "启用"}</button>`}`
           : "—"}</td></tr>`)}`)}
    ${panel("事中预警：在院病例住院日已明显超出同组均值", `
      <form class="inline" id="drg-alert-form">
        <input name="los_multiplier" type="number" step="0.1" min="1" max="5" value="1.5" style="min-width:120px"
          title="后端限 1.0～5.0">
        <button>按倍数重算</button>
      </form>
      <div id="drg-alerts"></div>`)}
    ${panel("事前提示：按拟诊断预判入组（给候选组，不给结论）", `
      <form class="inline" id="drg-pre-form">
        <input name="diagnosis" placeholder="拟诊断" required style="min-width:220px">
        <input name="operation" placeholder="拟手术（可空）" style="min-width:180px">
        <button>预判</button>
      </form>
      <div id="drg-pre"></div>`)}`;
  // 分组目录原先只能调权、不能增补（P2-93 动词级孤儿）：建组的接口一直在，页面上没有入口
  const groupForm = $("#drg-group-form");
  if (groupForm) groupForm.onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["base_weight"]);
    body.require_procedure = e.target.require_procedure.checked;
    return postAction("/api/drgs/groups", body, "#drg-msg");
  };
  $("#drg-alert-form").onsubmit = async (e) => {
    e.preventDefault();
    // 查询失败要说出来（P2-378）：原先 draw 抛错没人接，列表还是上一次的结果
    try { await drawAlerts(Number(new FormData(e.target).get("los_multiplier")) || 1.5); }
    catch (err) { setMsg("#drg-msg", err.message, false); }
  };
  $("#drg-pre-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/api/drgs/pre-check", { method: "POST", body: JSON.stringify({
        diagnosis: f.get("diagnosis"), operation: f.get("operation") || "" }) });
      $("#drg-pre").innerHTML = `
        ${r.matched
          ? `<p class="msg ok">命中 ${r.candidates.length} 个候选组，基准权重区间
             ${r.weight_range.min} ~ ${r.weight_range.max}</p>`
          : `<p class="msg">未匹配到任何分组——<b>事前不落兜底组</b>（兜底组是出院入组时保证
             每个病例都有归属用的，事前拿它当预测结果毫无信息量）。</p>`}
        ${table(["编码", "MDC", "名称", "基准权重", "匹配分", "最长命中词长"], r.candidates, (c) =>
          `<tr><td>${esc(c.code)}</td><td>${esc(c.mdc) || "—"}</td><td>${esc(c.name)}</td>
           <td>${c.base_weight}</td><td>${c.match_score.diagnosis_hits}</td>
           <td>${c.match_score.procedure_hits}</td></tr>`)}
        <p class="desc">匹配分：每命中一个主诊断关键词 10 分、一个主手术关键词 20 分，再加最长命中词的字数；按匹配分排序。</p>
        <p class="desc">${esc(r.caliber)}</p>`;
    } catch (err) { $("#drg-pre").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#page-body").onclick = async (e) => {
    const { drgWeight: id, drgEdit, drgToggle, active } = e.target.dataset;
    // 编辑（P2-1535）：PATCH 收名称、基准权重、关键词、主手术关键词、必须命中主手术、MDC、MDC 名称（动了匹配配置的后端按 P2-1019
    // 判），页面原先只给调权——建错的组（关键词过宽、漏勾必须命中主手术）改不了，种子只增不改，存量库的关键词只能调接口改。
    // 框照 P2-1533「改档」：都是单行字段与下拉，点确定关框、由页面发请求，失败写本页消息行；只送和预填值不同的项（照 P2-969），
    // spdModal 交回的值去了首尾空白，原值也去掉再比。启停用行上的按钮；只改权重的「调权」照旧留着
    if (drgEdit) {
      const g = groups.find((x) => x.id === Number(drgEdit));
      if (!g) return;
      const picked = await spdModal(`编辑分组：${g.code} ${g.name}`, [
        { name: "name", label: "分组名称（必填）", type: "text", value: g.name, required: true },
        { name: "base_weight", label: "基准权重（须 > 0）", type: "number", value: g.base_weight },
        { name: "mdc", label: "MDC", type: "text", value: g.mdc },
        { name: "mdc_name", label: "MDC 名称", type: "text", value: g.mdc_name },
        { name: "keywords", label: "主诊断关键词（逗号分隔）", type: "text", value: g.keywords },
        { name: "procedure_keywords", label: "主手术关键词（逗号分隔）", type: "text", value: g.procedure_keywords },
        { name: "require_procedure", label: "必须命中主手术（外科组：未命中主手术不入该组）", type: "select",
          value: g.require_procedure ? "1" : "0", options: [{ value: "0", label: "否" }, { value: "1", label: "是" }] },
      ], { intro: "只提交改了的项；改动只作用于此后入组的病例，已入组的不重算（权重按入组时的快照计）。启停用行上的「停用 / 启用」。" });
      if (!picked) return;
      const body = {};
      if (picked.name !== g.name.trim()) body.name = picked.name;
      if (picked.base_weight !== g.base_weight) body.base_weight = picked.base_weight;
      for (const f of ["mdc", "mdc_name", "keywords", "procedure_keywords"]) {
        if (picked[f] !== (g[f] || "").trim()) body[f] = picked[f];
      }
      if ((picked.require_procedure === "1") !== g.require_procedure) body.require_procedure = picked.require_procedure === "1";
      if (!Object.keys(body).length) return setMsg("#drg-msg", "没有改动：各项都与原来相同，未提交", false);
      try { await api(`/api/drgs/groups/${drgEdit}`, { method: "PATCH", body: JSON.stringify(body) }); route(); }
      catch (err) { setMsg("#drg-msg", err.message, false); }
      return;
    }
    // 停用 / 启用（P2-1535）：照本文件 ESB 接入方、数据质控规则的切换写法；停用的组预检与出院入组都不再命中
    if (drgToggle) {
      try { await api(`/api/drgs/groups/${drgToggle}`, { method: "PATCH", body: JSON.stringify({ active: active !== "1" }) }); route(); }
      catch (err) { setMsg("#drg-msg", err.message, false); }
      return;
    }
    if (!id) return;
    const picked = await spdModal("调整基准权重", [
      { name: "base_weight", label: "新基准权重（须 > 0）", type: "number" }]);
    if (!picked || !picked.base_weight) return;
    try { await api(`/api/drgs/groups/${id}`, { method: "PATCH", body: JSON.stringify({ base_weight: picked.base_weight }) }); route(); }
    catch (err) { setMsg("#drg-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修）
  await drawAlerts(1.5);
}

/* ---------------- 终审轮新增页面 ---------------- */

const BLOOD_COMPONENTS = { rbc: "红细胞", plasma: "血浆", platelet: "血小板" };
const BLOOD_REQ_STATUS = { pending: ["待审批", "orange"], approved: ["已审批", ""], rejected: ["已驳回", "red"], issued: ["已发血", "green"] };

async function renderBlood() {
  $("#page-desc").textContent = "血库台账（经办登记）→ 用血申请（医师）→ 审批（管理层）→ 发血（经办，库存不足拦截）";
  // 待审批、待发血的单独取一遍、排在最前（P2-408，同审方 P1-148）：申请队列只回最新 200 条，挤出窗口的那张
  // 页面上就再没有「批准 / 驳回」「发血」可点
  const [stocks, recent, pending, approved] = await Promise.all([api("/api/blood/stocks"), api("/api/blood/requests"),
    api("/api/blood/requests?status=pending"), api("/api/blood/requests?status=approved")]);
  const actionableIds = new Set([...pending, ...approved].map((r) => r.id));
  const requests = [...pending, ...approved, ...recent.filter((r) => !actionableIds.has(r.id))];
  const role = currentRole();
  // 血型、成分首项为空、必选（P2-1468，同消毒供应申领机构 P2-1443）：原先两个下拉不给空项，不动它交上去就是「A」「红细胞」，
  // 后端照收——医师忘了改，申请就成了 A 型；血库经办忘了改，B 型血记进 A 型的账。入库、申请两张表单共用这两串选项
  const typeOpts = `<option value="">请选择血型</option>${["A", "B", "AB", "O"].map((t) => `<option>${t}</option>`).join("")}`;
  const compOpts = `<option value="">请选择成分</option>${
    Object.entries(BLOOD_COMPONENTS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}`;
  // ADR-0009 第四批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 两个表单面板按角色条件渲染，条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${["operator", "admin"].includes(role) ? panel("血库入库登记（经办）", `
      <form class="inline" id="bs-form">
        <select name="blood_type" required>${typeOpts}</select>
        <select name="component" required>${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <button>入库</button></form>`) : ""}
    ${["doctor", "admin"].includes(role) ? panel("临床用血申请（医师）", `
      <form class="inline" id="br-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="用血机构ID" required>
        <select name="blood_type" required>${typeOpts}</select>
        <select name="component" required>${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <input name="reason" placeholder="用血原因">
        <button>申请</button></form>`) : ""}
    <p class="msg" id="blood-msg"></p>
    ${panel("血液库存台账", table(["血型", "成分", "库存(ml)"], stocks, (s) =>
      `<tr><td><span class="tag">${esc(s.blood_type)}</span></td><td>${BLOOD_COMPONENTS[s.component] || esc(s.component)}</td><td>${s.quantity_ml}</td></tr>`))}
    ${panel("用血申请队列", table(["ID", "患者", "机构", "血型/成分", "数量", "申请人", "申请时间", "用血原因", "状态", "操作"], requests, (r) => {
      const actions = r.status === "pending" && ["director", "admin"].includes(role)
        ? `<button class="btn secondary" data-brev="${r.id}" data-ok="true">批准</button>
           <button class="btn danger" data-brev="${r.id}" data-ok="false">驳回</button>`
        : r.status === "approved" && ["operator", "admin"].includes(role)
        ? `<button class="btn secondary" data-bissue="${r.id}">发血</button>` : "—";
      // 审批、发血的判断依据摆出来（P2-1469）：原先只印患者号、成分编码查前端表，表单收的用血原因、谁申请的、何时申请的
      // 都看不到。患者印姓名带编号，成分印后端给的文案（component_name），申请人、申请时间、用血原因取清单出参
      return `<tr><td>${r.id}</td><td>${esc(r.patient_name || "—")}（#${r.patient_id}）</td><td>${r.org_id}</td>
        <td>${esc(r.blood_type)} / ${esc(r.component_name)}</td><td>${r.quantity_ml}ml</td>
        <td>${esc(r.requested_by_name) || "—"}</td><td>${esc(r.created_at.slice(0, 16).replace("T", " "))}</td>
        <td>${esc(r.reason) || "—"}</td>
        <td>${statusTag(BLOOD_REQ_STATUS, r.status)}</td><td>${actions}</td></tr>`;
    }))}`;
  const bs = $("#bs-form");
  if (bs) bs.onsubmit = (e) => { e.preventDefault(); postAction("/api/blood/stocks", formJson(e.target, ["quantity_ml"]), "#blood-msg"); };
  const br = $("#br-form");
  if (br) br.onsubmit = (e) => { e.preventDefault(); postAction("/api/blood/requests", formJson(e.target, ["patient_id", "org_id", "quantity_ml"]), "#blood-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.brev) return postAction(`/api/blood/requests/${d.brev}/review?approve=${d.ok}`, null, "#blood-msg");
    if (d.bissue) return postAction(`/api/blood/requests/${d.bissue}/issue`, null, "#blood-msg");
  };
}

const PO_STATUS = { pending: ["待审批", "orange"], approved: ["已审批", ""], rejected: ["已驳回", "red"], received: ["已验收", "green"] };

async function renderProcure() {
  $("#page-desc").textContent = "供应商建档 → 采购申请（经办/药师）→ 审批（管理层）→ 验收入库；存货盘点账实调整";
  const [suppliers, orders, takes] = await Promise.all([
    api("/api/pharmacy/suppliers"), api("/api/pharmacy/purchase-orders"), api("/api/pharmacy/stock-takes")]);
  const role = currentRole();
  const supNames = Object.fromEntries(suppliers.map((s) => [s.id, s.name]));
  $("#page-body").innerHTML = `
    ${panel("供应商建档（管理层/经办）", `
      <form class="inline" id="sup-form">
        <input name="name" placeholder="供应商名称" required>
        <input name="contact" placeholder="联系方式">
        <input name="license_no" placeholder="许可证号">
        <button>建档</button></form>
      ${table(["ID", "名称", "联系方式", "许可证", "状态"], suppliers, (s) =>
        `<tr><td>${s.id}</td><td>${esc(s.name)}</td><td>${esc(s.contact)}</td><td>${esc(s.license_no)}</td>
         <td><span class="tag ${s.active ? "green" : "red"}">${s.active ? "在用" : "停用"}</span></td></tr>`)}`)}
    ${panel("采购申请（经办/药师）", `
      <form class="inline" id="po-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="supplier_id" type="number" placeholder="供应商ID" required>
        <select name="item_type"><option value="drug">药品</option><option value="material">物资耗材</option></select>
        <input name="item_code" placeholder="编码" required>
        <input name="item_name" placeholder="名称" required>
        <input name="quantity" type="number" min="1" placeholder="数量" required>
        <button>提交申请</button></form>
      <p class="msg" id="po-msg"></p>
      ${table(["ID", "机构", "供应商", "类型", "品目", "数量", "状态", "操作"], orders, (o) => {
        const actions = o.status === "pending" && ["director", "admin"].includes(role)
          ? `<button class="btn secondary" data-poap="${o.id}">批准</button>
             <button class="btn danger" data-poap="${o.id}" data-reject="1">驳回</button>`
          : o.status === "approved" && ["operator", "pharmacist", "admin"].includes(role)
          ? `<button class="btn secondary" data-porec="${o.id}" data-qty="${esc(o.quantity)}">验收入库</button>` : "—";
        return `<tr><td>${o.id}</td><td>${o.org_id}</td><td>${esc(supNames[o.supplier_id] || o.supplier_id)}</td>
          <td>${o.item_type === "drug" ? "药品" : "物资"}</td><td>${esc(o.item_name)}（${esc(o.item_code)}）</td>
          <td>${o.quantity}${o.received_quantity != null && o.received_quantity !== o.quantity
            ? `（实收 ${o.received_quantity}）` : ""}</td><td>${statusTag(PO_STATUS, o.status)}</td><td>${actions}</td></tr>`;
      })}`)}
    ${panel("存货盘点（经办/药师，盘后账实相符）", `
      <form class="inline" id="st-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="drug_code" placeholder="药品编码" required>
        <input name="actual_qty" type="number" min="0" placeholder="实盘数量" required>
        <input name="note" placeholder="差异说明">
        <button>盘点</button></form>
      ${table(["ID", "机构", "药品编码", "账面", "实盘", "差异"], takes, (t) =>
        `<tr><td>${t.id}</td><td>${t.org_id}</td><td>${esc(t.drug_code)}</td><td>${t.book_qty}</td><td>${t.actual_qty}</td>
         <td><span class="tag ${t.diff === 0 ? "green" : "red"}">${t.diff > 0 ? "+" : ""}${t.diff}</span></td></tr>`)}`)}`;
  $("#sup-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/suppliers", formJson(e.target), "#po-msg"); };
  $("#po-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/purchase-orders", formJson(e.target, ["org_id", "supplier_id", "quantity"]), "#po-msg"); };
  // 盘点、验收入库改了库存，可能越过 / 回到缺药阈值：管理层铃铛的「缺药预警」办完即刷新（P2-1312，照站内消息页标已读的写法）
  $("#st-form").onsubmit = async (e) => {
    e.preventDefault();
    await postAction("/api/pharmacy/stock-takes", formJson(e.target, ["org_id", "actual_qty"]), "#po-msg");
    pollTodos();
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.poap) return postAction(`/api/pharmacy/purchase-orders/${d.poap}/approve${d.reject ? "?reject=true" : ""}`, null, "#po-msg");
    if (d.porec) {
      // 按实收数验收（P2-852）：原先一律按申请量整单入库，少到的差额成了账上能发、实际不存在的库存
      const form = await spdModal("到货验收", [
        { name: "received_quantity", label: `实收数量（采购 ${d.qty}，留空按采购量）`, type: "text" }]);
      if (!form) return;
      const raw = String(form.received_quantity || "").trim();
      const qty = Number(raw);
      if (raw && !(Number.isInteger(qty) && qty > 0)) return setMsg("#po-msg", "实收数量要填正整数", false);
      await postAction(`/api/pharmacy/purchase-orders/${d.porec}/receive`, raw ? { received_quantity: qty } : null,
        "#po-msg");
      pollTodos();
    }
  };
}

const CERT_TYPES = { birth: "出生医学证明", death: "死亡医学证明", defect: "出生缺陷儿登记" };
// abnormal 是 bool，没有后端文案可取；映成状态码再走 statusTag，与本页其余状态列同写法
const CHK_ITEM = { ok: ["正常", "green"], bad: ["异常", "red"] };
// 清单行的 reviewed 同样是 bool（P2-409），同一个写法
const CHK_REVIEW = { todo: ["待总检", "orange"], done: ["已总检", "green"] };
// 没异常的行看后端给的 has_results（P2-1403）：原先没异常一律画绿色「正常」，什么都没录的体检也是「正常」。同一个写法
const CHK_RESULT = { ok: ["正常", "green"], none: ["未录结果", ""] };
// 未录结果的行也不摆「总检」：后端对它 409「尚无体检结果」（P2-1403），摆出来点了也只是在框里报错

// 体检分项的一行（P2-1404）：登记表单原先没有分项框，接口早就收 `CheckupCreate.items`，页面登记的体检分项永远是 0 条。
// 写法照开方明细（core.js `RX_ITEM_ROW`）：一行一个 div，「添加分项」往后插、「删除本行」删自己那行，提交时逐行取值。分项
// 选填：首屏不摆空行，最后一行也能删。框不带 name——formJson 按 name 收表头那几栏，带了就把最后一行摊成顶层字段；逐行取值
// 走 chkItems。按钮写明 type="button"：缺省是提交按钮，在框里按回车会先「点」到它
const CHK_ITEM_ROW = `<div class="chk-item" style="display:flex;gap:8px;margin:4px 0;flex-wrap:wrap;align-items:center">
  <input data-item="item_code" placeholder="项目编码" required>
  <input data-item="item_name" placeholder="项目名称" required>
  <input data-item="result_value" placeholder="结果" required>
  <input data-item="unit" placeholder="单位" style="min-width:70px">
  <input data-item="ref_range" placeholder="参考范围">
  <label><input data-item="abnormal" type="checkbox"> 异常</label>
  <button type="button" class="btn secondary" data-chkdelrow>删除本行</button></div>`;

/** 体检分项逐行取值（P2-1404）：表单上有几行就送几项；「异常」照报告勾，系统不按参考范围判。 */
function chkItems(form) {
  return [...form.querySelectorAll(".chk-item")].map((row) => {
    const box = (key) => row.querySelector(`[data-item="${key}"]`);
    return { item_code: box("item_code").value, item_name: box("item_name").value,
      result_value: box("result_value").value, unit: box("unit").value, ref_range: box("ref_range").value,
      abnormal: box("abnormal").checked };
  });
}

async function renderCerts() {
  $("#page-desc").textContent = "出生/死亡医学证明签发与出生缺陷登记（限医师/公卫）；成人健康体检记录与异常清单";
  // 总检后端是 require_roles("doctor")（admin 全通）；公卫岗只录入，摆了只会点出 403
  const canReview = ["doctor", "admin"].includes(currentRole());
  // 没总检的单独取一遍、排在最前（P2-409，同审方 P1-148）：清单只回最新 200 条，挤出窗口的那次体检原先就再没有
  // 一行给「总检」。只给能总检的人取——公卫岗只录入，排序照旧
  const [stats, recent, unreviewed, abnormal] = await Promise.all([
    api("/api/certs/stats"), api("/api/checkups"), canReview ? api("/api/checkups?reviewed=false") : [],
    api("/api/checkups/abnormal")]);
  const unreviewedIds = new Set(unreviewed.map((c) => c.id));
  const checkups = [...unreviewed, ...recent.filter((c) => !unreviewedIds.has(c.id))];
  // 死因报告卡与导出后端是 require_roles("director")——法定上报口径，与总检不是同一把钥匙
  const canDeathCard = ["director", "admin"].includes(currentRole());
  const draw = async (certType = "") => {
    const certs = await api(`/api/certs${certType ? `?cert_type=${certType}` : ""}`);
    $("#cert-table").innerHTML = table(["编号", "类型", "姓名", "性别", "日期", "诊断/说明", "机构", "操作"], certs, (c) =>
      `<tr><td><span class="tag">${esc(c.cert_no)}</span></td><td>${CERT_TYPES[c.cert_type] || esc(c.cert_type)}</td>
       <td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.event_date)}</td><td>${esc(c.detail) || "—"}</td><td>${c.org_id}</td>
       <td><button class="btn secondary" data-printcert="${c.id}">打印</button>
           ${canDeathCard && c.cert_type === "death"
             ? `<button class="btn" data-deathcard="${c.id}">死因报告卡</button>` : ""}</td></tr>`);
  };
  // 公卫登记时录的 summary 叫「汇总小结」（P2-1405，登记框占位与清单列名），清单说明里的结论一律写「总检结论」：原先占位写
  // 「体检结论」、列名「结论」显示的却是汇总小结，说明又说「结论全文不在清单里」——同一行读作「结论：各项正常｜已总检」，
  // 医师写的总检结论却是「空腹血糖偏高…」。与打印件（`printing.print_checkup_report`）同一个叫法
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">出生证明</div><div class="value">${stats.birth || 0}</div></div>
      <div class="card"><div class="label">死亡证明</div><div class="value">${stats.death || 0}</div></div>
      <div class="card"><div class="label">缺陷登记</div><div class="value">${stats.defect || 0}</div></div></div>
    ${panel("证明签发（医师/公卫；死亡须关联患者并填死因，缺陷须填诊断）", `
      <form class="inline" id="cert-form">
        <select name="cert_type">${Object.entries(CERT_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="name" placeholder="姓名" required>
        <select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="event_date" placeholder="事件日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="detail" placeholder="死因/缺陷诊断" style="min-width:180px">
        <input name="org_id" type="number" placeholder="签发机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID(死亡必填)">
        <button>签发</button></form>
      <p class="msg" id="cert-msg"></p>
      <form class="inline" id="cert-filter">
        <select name="cert_type"><option value="">全部类型</option>${Object.entries(CERT_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <button>筛选</button></form>
      <div id="cert-table"></div>`)}
    <div class="panel hidden" id="deathcard-panel"><h3>死因报告卡</h3><div id="deathcard-body"></div></div>
    ${canDeathCard ? panel("死因报告卡批量导出（CSV，按死亡日期筛）", `
      <form class="inline" id="death-export">
        <input name="date_from" placeholder="死亡日期起 YYYY-MM-DD" pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="date_to" placeholder="止 YYYY-MM-DD" pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>导出</button>
      </form>
      <p class="desc"><b>平台不直连人口死亡信息登记管理系统</b>：本导出供手工网报或县疾控
        前置机对接使用。身份证号与电话<b>按调用者角色脱敏</b>（非 admin 一律掩码），
        每张卡的患者档案调阅都落 AccessLog。按所选日期区间全量导出，不设条数上限。</p>
      <p class="msg" id="death-msg"></p>`) : ""}
    ${panel("成人健康体检登记（医师/公卫，异常项自动标记并入360档案）", `
      <form class="inline" id="chk-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="体检机构ID" required>
        <input name="package_name" placeholder="套餐（默认常规体检）">
        <input name="exam_date" placeholder="体检日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="summary" placeholder="汇总小结" style="min-width:160px">
        <input name="abnormal_items" placeholder="异常项（有则填）" style="min-width:160px">
        <div id="chk-items" style="flex-basis:100%"></div>
        <button type="button" class="btn secondary" id="chk-add-item">添加分项</button>
        <button>登记</button></form>
      <p class="msg" id="chk-msg"></p>
      <p class="desc">分项选填，一行一项：项目编码、名称、结果必填，单位、参考范围选填；超出参考范围的照报告勾「异常」——
        系统不按参考范围自动判。同一项目只录一行。分项随登记一次交齐，登记后不能补录、更正。</p>`)}
    ${abnormal.length ? panel(`⚠ 体检异常清单（${abnormal.length}，供慢病筛查建档衔接）`,
      table(["体检ID", "患者", "日期", "异常项"], abnormal, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.exam_date)}</td><td><span class="tag red">${esc(a.abnormal_text)}</span></td></tr>`)) : ""}
    ${panel("体检记录", table(["ID", "患者", "套餐", "日期", "汇总小结", "异常", "总检", "操作"], checkups, (c) =>
      `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${esc(c.package_name)}</td><td>${esc(c.exam_date)}</td>
       <td>${esc(c.summary) || "—"}</td><td>${c.has_abnormal ? `<span class="tag red">${esc(c.abnormal_text)}</span>`
         : statusTag(CHK_RESULT, c.has_results ? "ok" : "none")}</td>
       <td data-chkstate="${c.id}">${statusTag(CHK_REVIEW, c.reviewed ? "done" : "todo")}</td>
       <td><button class="btn" data-chkitems="${c.id}">分项结果</button>
           ${canReview && c.has_results ? `<button class="btn secondary" data-chkreview="${c.id}">总检</button>` : ""}
           <button class="btn secondary" data-printchk="${c.id}">打印报告</button></td></tr>`)
      + `<p class="desc">总检限医师（公卫岗只录入），重复总检按覆盖处理（复核改总检结论）；没总检的排在最前。
        「总检」列只标总检了没有，总检结论全文不在清单里：写完在下方回显一次，之后要看总检结论走同一行的<b>「打印报告」</b>
        （打印件里有「总检结论」与总检医师署名）。</p>`)}
    <div class="panel hidden" id="chk-detail"><h3>体检分项结果</h3><div id="chk-detail-body"></div></div>`;
  $("#cert-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/certs", formJson(e.target, ["org_id", "patient_id"]), "#cert-msg"); };
  $("#cert-filter").onsubmit = async (e) => {
    e.preventDefault();
    // 查询失败要说出来（P2-378）：原先 draw() 抛错没人接，列表还是上一次的结果。先清空（P2-1010）：原先只写了原因，
    // 列表照旧是上一次那一类的证书
    $("#cert-table").innerHTML = "";
    try { await draw(new FormData(e.target).get("cert_type")); }
    catch (err) { setMsg("#cert-msg", err.message, false); }
  };
  // 「添加分项」「删除本行」（P2-1404）：分项选填，最后一行也能删（开方明细至少一味药才收起删除，这里不必）
  const chkRows = $("#chk-items");
  $("#chk-add-item").onclick = () => chkRows.insertAdjacentHTML("beforeend", CHK_ITEM_ROW);
  chkRows.onclick = (e) => { if (e.target.dataset.chkdelrow !== undefined) e.target.closest(".chk-item").remove(); };
  // 报错写在体检面板自己的消息行（P2-1404）：原先写到页面最上方证明面板的 #cert-msg，中间隔着最多 200 行的证明表——
  // 分项编码重复这类 422，人在登记表单这儿根本看不见
  $("#chk-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/checkups", { ...formJson(e.target, ["patient_id", "org_id"]), items: chkItems(e.target) }, "#chk-msg");
  };
  const showItems = async (id, review) => {
    const items = await api(`/api/checkups/${id}/items`);
    $("#chk-detail").classList.remove("hidden");
    $("#chk-detail-body").innerHTML = `
      <p class="desc">体检 ${id} 共 ${items.length} 个分项${items.length ? "" : "（该次体检还没有录入分项）"}。</p>
      ${review ? `<p class="msg ok">总检结论已保存：${esc(review.final_conclusion)}（总检医师 ${
        esc(review.final_doctor)}）。刷新后此处不再显示——之后查结论走「打印报告」。</p>` : ""}
      ${table(["项目编码", "项目", "结果", "单位", "参考范围", "判定"], items, (i) =>
        `<tr><td>${esc(i.item_code)}</td><td>${esc(i.item_name)}</td><td>${esc(i.result_value)}</td>
         <td>${esc(i.unit) || "—"}</td><td>${esc(i.ref_range) || "—"}</td>
         <td>${statusTag(CHK_ITEM, i.abnormal ? "bad" : "ok")}</td></tr>`)}`;
  };
  const deathForm = $("#death-export");
  if (deathForm) deathForm.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const qs = [f.get("date_from") ? `date_from=${f.get("date_from")}` : "",
                f.get("date_to") ? `date_to=${f.get("date_to")}` : ""].filter(Boolean).join("&");
    downloadCsv(`/api/certs/death-report-cards/export.csv${qs ? `?${qs}` : ""}`,
      "death_report_cards.csv", "#death-msg");
  };
  $("#page-body").onclick = async (e) => {
    const { printcert, printchk, chkitems, chkreview, deathcard } = e.target.dataset;
    // 体检这几样（分项结果、总检后的回显、打印报告）同样写在体检面板的消息行（P2-1404）；证明的打印与死因报告卡照旧
    const msgSel = chkitems || chkreview || printchk ? "#chk-msg" : "#cert-msg";
    try {
      if (deathcard) {
        const c = await api(`/api/certs/${deathcard}/death-report-card`);
        $("#deathcard-panel").classList.remove("hidden");
        // 身份证号与电话已由后端按角色脱敏，这里只转义、不再叠一层掩码（叠了看的人以为是两段号）
        $("#deathcard-body").innerHTML = `<div class="cards">
          <div class="card"><span class="k">证明编号</span><b>${esc(c.cert_no)}</b></div>
          <div class="card"><span class="k">姓名</span><b>${esc(c.name)}</b></div>
          <div class="card"><span class="k">性别</span><b>${esc(c.gender)}</b></div>
          <div class="card"><span class="k">身份证号</span><b>${esc(c.id_card) || "—"}</b></div>
          <div class="card"><span class="k">联系电话</span><b>${esc(c.phone) || "—"}</b></div>
          <div class="card"><span class="k">出生日期</span><b>${esc(c.birth_date) || "—"}</b></div>
          <div class="card"><span class="k">死亡日期</span><b>${esc(c.death_date)}</b></div>
          <div class="card"><span class="k">死因诊断</span><b>${esc(c.cause_of_death) || "—"}</b></div>
          <div class="card"><span class="k">签发机构</span><b>${esc(c.org_name) || c.org_id}</b></div>
          <div class="card"><span class="k">签发人</span><b>${esc(c.issued_by) || "—"}</b></div>
          <div class="card"><span class="k">签发时间</span><b>${
            esc(c.issued_at.slice(0, 16).replace("T", " "))}</b></div></div>
        <p class="desc">按人口死亡信息登记管理系统的字段集导出<b>平台已存字段</b>；
          身份证号与电话按调用者角色脱敏，本次调阅已落 AccessLog。</p>`;
        return;
      }
      if (chkitems) return await showItems(chkitems, null);
      if (chkreview) {
        // 框自己提交（P2-607）：结论写超了、留空时报错写在框里、框不关，写好的总检结论不用重写（原先留空就关框、什么也不发生）
        const r = await spdModal(`体检 ${chkreview} 总检`, [
          { name: "final_conclusion", label: "总检结论（必填，后端上限 1024 字）", type: "textarea" },
          { name: "final_doctor", label: "总检医师（留空则署当前登录医师）", type: "text" },
        ], { submit: (picked) => api(`/api/checkups/${chkreview}/review`, { method: "POST", body: JSON.stringify(picked) }) });
        if (!r) return;
        // 不走 route()：清单里没有结论全文，整页重画只会把下面这段回执冲掉；只把这一行的「总检」列改成已总检，
        // 回执连同分项摆进详情容器，人才看得见自己刚写的结论落成了什么
        const state = $(`[data-chkstate="${chkreview}"]`);
        if (state) state.innerHTML = statusTag(CHK_REVIEW, "done");
        return await showItems(chkreview, r);
      }
      // 两条打印各写一处字面量地址（P2-490）：写成一个三元，调用点解析推不出地址，读动词棘轮把两个打印接口都记成没有入口
      if (printcert) return await openPrintPage(`/api/print/certs/${printcert}`);
      if (printchk) return await openPrintPage(`/api/print/checkups/${printchk}`);
    } catch (err) { setMsg(msgSel, err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
}

/* 块3：数据质控（管理员）——规则驱动扫描存量数据，看违规明细与汇总 */
/* 块1：集成平台 ESB——端点注册、消息队列（筛选/重试/错误）、流程编排、统计看板 */

const ESB_SYSTEMS = { his: "医院信息系统", lis: "检验系统", pacs: "影像系统", insurance: "医保系统", provincial: "省级平台" };
const ESB_MSG_STATUS = { queued: ["待处理", "orange"], processing: ["处理中", ""], succeeded: ["成功", "green"], failed: ["失败待重试", "orange"], dead: ["死信", "red"] };
// 执行记录只有两种终态（esb.py 的 `status="failed" if error else "succeeded"`）——与消息状态不是一套
const ESB_RUN_STATUS = { succeeded: ["成功", "green"], failed: ["失败", "red"] };
const ESB_FLOW_SAMPLE = JSON.stringify([
  { type: "transform", config: { format: "fhir_patient", source_field: "resource" } },
  { type: "validate", config: { required: ["name", "id_card"] } },
  { type: "persist", config: { entity: "patient" } },
], null, 1);

async function renderEsb() {
  $("#page-desc").textContent = "轻量服务总线：接入方注册与限流、消息队列重试与死信、编排流程逐步执行、成功率与积压监控";
  const [stats, endpoints, flows] = await Promise.all([
    api("/api/esb/stats"), api("/api/esb/endpoints"), api("/api/esb/flows")]);
  // 消息表当前这一页（「查看载荷」就在这一页里找，P2-424）
  let shownMessages = [];
  const drawMessages = async () => {
    const f = new FormData($("#esb-msg-filter"));
    const params = new URLSearchParams({ limit: "50" });
    if (f.get("status")) params.set("status", f.get("status"));
    if (f.get("endpoint_id")) params.set("endpoint_id", f.get("endpoint_id"));
    const messages = await api(`/api/esb/messages?${params}`);
    shownMessages = messages;
    // 停用的出站接入方不给「消费/重试」（P2-180：后端 409，消息留在队里，启用后再消费）
    const stoppedOutbound = new Set(endpoints.filter((ep) => ep.direction === "outbound" && !ep.active).map((ep) => ep.code));
    // 按编排执行失败的消息只能按那条编排重试（P2-820）：默认消费绕过编排，后端 409——原先这里照给「消费/重试」，
    // 透传消息点了就记成功、编排里后面那步的目标再也收不到。那条编排已停用的，执行也是 409，只标出来
    const activeFlows = new Set(flows.filter((f) => f.active).map((f) => f.code));
    const retryOp = (m) => !m.retry_flow ? `<button class="btn secondary" data-esbproc="${m.id}">消费/重试</button>`
      : activeFlows.has(m.retry_flow)
        ? `<button class="btn secondary" data-esbrerun="${m.id}" data-flow="${esc(m.retry_flow)}">按编排「${esc(m.retry_flow)}」重试</button>`
        : `<span class="tag">编排「${esc(m.retry_flow)}」已停用</span>`;
    $("#esb-messages").innerHTML = table(["ID", "接入方", "消息类型", "状态", "重试", "最后错误", "操作"], messages, (m) => {
      const retryable = (m.status === "queued" || m.status === "failed") && !stoppedOutbound.has(m.endpoint_code);
      return `<tr><td>${m.id}</td><td><span class="tag">${esc(m.endpoint_code)}</span></td><td>${esc(m.msg_type)}</td>
        <td>${statusTag(ESB_MSG_STATUS, m.status)}</td><td>${m.retry_count}/${m.max_retries}</td>
        <td style="max-width:280px;font-size:12px;color:#b23c3c">${esc(m.last_error)}</td>
        <td>${retryable ? retryOp(m)
          : stoppedOutbound.has(m.endpoint_code) && m.status !== "succeeded" && m.status !== "dead"
            ? '<span class="tag">接入方已停用</span>' : "—"}
          <button class="btn secondary" data-esbpayload="${m.id}">查看载荷</button></td></tr>`;
    });
  };
  // ADR-0009 第三批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 顶部的统计卡片区不是面板，原样保留。
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">接入方</div><div class="value">${stats.totals.endpoints}</div></div>
      <div class="card"><div class="label">消息总量</div><div class="value">${stats.totals.total || 0}</div></div>
      <div class="card"><div class="label">成功率</div><div class="value">${stats.totals.success_rate_pct || 0}%</div></div>
      <div class="card"><div class="label">积压</div><div class="value${stats.totals.backlog ? " warn" : ""}">${stats.totals.backlog || 0}</div></div>
      <div class="card"><div class="label">死信</div><div class="value${stats.totals.dead ? " warn" : ""}">${stats.totals.dead || 0}</div></div></div>`
    + panel("接入方注册", `
      <form class="inline" id="esb-ep-form">
        <input name="code" placeholder="接入方编码" required>
        <input name="name" placeholder="名称" required style="min-width:180px">
        <select name="system_type">${Object.entries(ESB_SYSTEMS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="direction"><option value="inbound">入站</option><option value="outbound">出站</option></select>
        <input name="rate_limit_per_min" type="number" min="1" value="60" style="width:110px" title="每分钟限流">
        <input name="endpoint_url" placeholder="出站投递地址 https://…（入站留空）" style="min-width:240px">
        <button>注册并生成令牌</button></form>
      <p class="msg" id="esb-ep-msg"></p>
      ${table(["编码", "名称", "系统类型", "方向", "投递地址", "限流/分钟", "状态", "操作"], endpoints, (e) =>
        `<tr><td><span class="tag">${esc(e.code)}</span></td><td>${esc(e.name)}</td><td>${esc(e.system_type_name)}</td>
         <td>${esc(e.direction_name)}</td>
         <td>${e.direction !== "outbound" ? "—" : e.endpoint_url ? esc(e.endpoint_url)
           : '<span class="tag orange">未配（仅登记不投递）</span>'}</td><td>${e.rate_limit_per_min}</td>
         <td>${e.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-esbtoggle="${e.id}" data-active="${e.active ? 1 : 0}">${e.active ? "停用" : "启用"}</button>
           <button class="btn secondary" data-esbrotate="${e.id}">轮换令牌</button>
           ${e.direction === "outbound" ? `<button class="btn secondary" data-esburl="${e.id}"
             data-url="${esc(e.endpoint_url || "")}">改投递地址</button>` : ""}</td></tr>`)}`)
    + panel("消息队列", `
      <form class="inline" id="esb-msg-filter">
        <select name="status"><option value="">全部状态</option>${Object.entries(ESB_MSG_STATUS).map(([v, t]) => `<option value="${v}">${t[0]}</option>`).join("")}</select>
        <select name="endpoint_id"><option value="">全部接入方</option>${endpoints.map((e) => `<option value="${e.id}">${esc(e.code)}</option>`).join("")}</select>
        <button>查询</button></form>
      <p class="msg" id="esb-msg"></p><div id="esb-messages"></div>`)
    + panel("流程编排（步骤 JSON：transform / validate / route / persist）", `
      <form id="esb-flow-form">
        <div class="inline"><input name="code" placeholder="流程编码" required>
          <input name="name" placeholder="流程名称" required style="min-width:180px"></div>
        <textarea name="steps" rows="7" style="width:100%;font-family:monospace;font-size:12px;margin-top:8px">${esc(ESB_FLOW_SAMPLE)}</textarea>
        <div class="inline" style="margin-top:8px"><button>保存流程</button></div></form>
      <p class="msg" id="esb-flow-msg"></p>
      ${table(["编码", "名称", "步骤数", "步骤", "状态", "操作"], flows, (f) =>
        `<tr><td><span class="tag">${esc(f.code)}</span></td><td>${esc(f.name)}</td><td>${f.step_count}</td>
         <td style="max-width:320px;font-size:12px">${esc((f.steps || []).map((s) => s.type).join(" → "))}</td>
         <td>${f.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td>${f.active ? `<button class="btn secondary" data-esbrun="${esc(f.code)}">对消息执行</button>` : ""}
             <button class="btn secondary" data-esbflowedit="${f.id}">编辑</button>
             <button class="btn" data-esbruns="${f.id}">执行记录</button></td></tr>`)}
      <p class="desc">编辑只改<b>此后</b>的执行——已经跑过的那些执行记录里存的是当时的步骤快照，
        流程后来改了步骤，旧记录也不会跟着变。
        停用的流程不摆「对消息执行」：后端仍按 code 找得到它，但会回 409「流程已停用」，
        摆出来只会让人点一次看一句错。停用后要再用，先在这里「编辑」改回启用。</p>
      <div id="esb-runs"></div>`)
    + panel("接入方统计",
      table(["接入方", "总量", "成功", "死信", "积压", "成功率", "失败率"], stats.by_endpoint, (r) =>
        `<tr><td><span class="tag">${esc(r.endpoint_code)}</span> ${esc(r.endpoint_name)}</td><td>${r.total}</td>
         <td><span class="tag green">${r.succeeded}</span></td>
         <td>${r.dead ? `<span class="tag red">${r.dead}</span>` : 0}</td>
         <td>${r.backlog ? `<span class="tag orange">${r.backlog}</span>` : 0}</td>
         <td>${r.success_rate_pct}%</td><td>${r.failure_rate_pct}%</td></tr>`));
  const drawRuns = async (flowId) => {
    try {
      const rows = await api(`/api/esb/flow-runs?flow_id=${encodeURIComponent(flowId)}&limit=50`);
      $("#esb-runs").innerHTML = `<h3 style="margin-top:14px">流程 ${flowId} 的执行记录</h3>
        ${table(["记录", "流程", "消息", "状态", "步骤结果", "错误", "时间"], rows, (r) =>
          `<tr><td>${r.id}</td><td><span class="tag">${esc(r.flow_code)}</span></td><td>${r.message_id}</td>
           <td>${statusTag(ESB_RUN_STATUS, r.status)}</td>
           <td style="font-size:12px">${esc((r.step_results || []).map((x) =>
             `${x.step}.${x.type}=${x.status}`).join("；")) || "—"}</td>
           <td>${esc(r.error) || "—"}</td>
           <td>${esc(r.created_at.slice(0, 16).replace("T", " "))}</td></tr>`)}
        <p class="desc">步骤结果是<b>执行当时的快照</b>：流程后来改了步骤，这里也不会跟着变——
          排查"那天为什么失败"靠的正是这一点。</p>`;
    } catch (err) { $("#esb-runs").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#esb-msg-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawMessages(); } catch (err) { setMsg("#esb-msg", err.message, false); }
  };
  $("#esb-ep-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    // 出站投递地址（P2-860）：原先注册表单与行上操作都不收，界面建的出站接入方全部「仅登记不投递」，消息却记成「已成功」。
    // 签名密钥（可选，仅入库不回显）仍只经接口配
    const body = { code: f.get("code"), name: f.get("name"), system_type: f.get("system_type"),
                   direction: f.get("direction"), rate_limit_per_min: Number(f.get("rate_limit_per_min")) || 60 };
    if (String(f.get("endpoint_url") || "").trim()) body.endpoint_url = String(f.get("endpoint_url")).trim();
    try {
      const created = await api("/api/esb/endpoints", { method: "POST", body: JSON.stringify(body) });
      alert(`接入令牌（仅此一次可见，请妥善保存）：\n${created.auth_token}`);
      route();
    } catch (err) { setMsg("#esb-ep-msg", err.message, false); }
  };
  $("#esb-flow-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let steps;
    try { steps = JSON.parse(f.get("steps")); }
    catch { return setMsg("#esb-flow-msg", "步骤 JSON 格式错误", false); }
    try {
      await api("/api/esb/flows", { method: "POST", body: JSON.stringify({
        code: f.get("code"), name: f.get("name"), steps }) });
      route();
    } catch (err) { setMsg("#esb-flow-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { esbproc, esbpayload, esbtoggle, active, esbrotate, esbrun,
            esbflowedit, esbruns, esbrerun, flow, esburl, url } = e.target.dataset;
    try {
      if (esbruns) return await drawRuns(esbruns);
      if (esbflowedit) {
        const f = flows.find((x) => x.id === Number(esbflowedit));
        // 框自己提交（P2-607）：步骤 JSON 写错、步骤类型不认时报错写在框里、框不关——原先框一关，改了一半的步骤 JSON 就没了
        const ok = await spdModal(`编辑流程 ${f ? f.code : esbflowedit}`, [
          { name: "name", label: "流程名称（留空不改）", type: "text", value: f ? f.name : "" },
          { name: "active", label: "启停", type: "select", value: f && f.active ? "1" : "0",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
          { name: "steps", label: "步骤 JSON 数组（留空不改；type 只认 transform/validate/route/persist）",
            type: "textarea", value: f ? JSON.stringify(f.steps || []) : "" },
        ], { submit: (picked) => {
          // 后端 exclude_unset + `if value is not None`：留空的键不送
          const body = { active: picked.active === "1" };
          if (picked.name) body.name = picked.name;
          if (picked.steps) {
            try { body.steps = JSON.parse(picked.steps); }
            catch (err) { throw new Error(`步骤 JSON 解析失败：${err.message}`); }
          }
          return api(`/api/esb/flows/${esbflowedit}`, { method: "PATCH", body: JSON.stringify(body) });
        } });
        if (ok) route();
        return;
      }
      if (esbrerun) {
        const res = await api(`/api/esb/flows/${encodeURIComponent(flow)}/run?message_id=${encodeURIComponent(esbrerun)}`, { method: "POST" });
        setMsg("#esb-msg", `消息 ${esbrerun} 按编排 ${flow} → ${res.status === "succeeded" ? "全部步骤成功" : `第 ${res.step_results.length} 步失败：${res.error}`}`, res.status === "succeeded");
        await drawMessages();
      } else if (esbproc) {
        const res = await api(`/api/esb/messages/${esbproc}/process`, { method: "POST" });
        setMsg("#esb-msg", `消息 ${esbproc} → ${ESB_MSG_STATUS[res.status][0]}：${res.detail || res.last_error}`, res.status === "succeeded");
        await drawMessages();
      } else if (esbpayload) {
        // 就在消息表当前这一页里找（P2-424）：原先另取「最新 50 条」、不带筛选条件——按状态 / 接入方筛出来的老消息，
        // 点「查看载荷」反倒说「不在当前页，请先按条件筛选」，而它明明就在眼前这一页
        const msg = shownMessages.find((m) => String(m.id) === esbpayload);
        alert(msg ? JSON.stringify(msg.payload, null, 2) : "这条消息已不在当前列表，请重新查询后再看");
      } else if (esbtoggle) {
        await api(`/api/esb/endpoints/${esbtoggle}`, { method: "PATCH", body: JSON.stringify({ active: active !== "1" }) });
        route();
      } else if (esburl) {
        // 留空即「仅登记不投递」（P2-1085，后端改档照收空串）：原先必填，下游停用或维护时界面上改不回仅登记，
        // 只能停用整个接入方或直接调接口
        const form = await spdModal("改出站投递地址", [
          { name: "endpoint_url", label: "投递地址（http / https；留空 = 仅登记不投递）", value: url || "" }]);
        if (!form) return;
        await api(`/api/esb/endpoints/${esburl}`, { method: "PATCH",
          body: JSON.stringify({ endpoint_url: String(form.endpoint_url).trim() }) });
        route();
      } else if (esbrotate) {
        const res = await api(`/api/esb/endpoints/${esbrotate}/rotate-token`, { method: "POST" });
        alert(`新令牌（旧令牌已失效）：\n${res.auth_token}`);
      } else if (esbrun) {
        // P2-38：弹窗换成页内表单（消息 ID 见上方消息列表）
        const form = await spdModal(`执行编排：${esbrun}`, [
          { name: "message_id", label: "对哪条消息执行（消息 ID，见上方消息列表）", type: "number", required: true }]);
        if (!form) return;
        const res = await api(`/api/esb/flows/${encodeURIComponent(esbrun)}/run?message_id=${encodeURIComponent(form.message_id)}`, { method: "POST" });
        setMsg("#esb-flow-msg", `编排 ${esbrun} → ${res.status === "succeeded" ? "全部步骤成功" : `第 ${res.step_results.length} 步失败：${res.error}`}`, res.status === "succeeded");
        await drawMessages();
      }
    } catch (err) { setMsg("#esb-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawMessages();
}

const QC_SEVERITY = { error: ["错误", "red"], warn: ["警告", "orange"] };
// 规则类型（措辞照抄后端 dataquality.RULE_TYPES）与各类型的配置示例（结构见 app/data/qc_rules_seed.py 的 docstring，
// 后端 rule_config_problem 逐项校验：字段不在被检表上、区间的界与列类型对不上都 422 并说清楚）
const QC_RULE_TYPES = { required: "必填项", range: "数值区间", enum: "取值枚举", cross_ref: "引用校验", logic: "逻辑校验" };
const QC_CONFIG_EXAMPLES = {
  required: '{"field": "phone"}',
  range: '{"field": "age", "min": 0, "max": 120}',
  enum: '{"field": "gender", "values": ["男", "女"]}',
  cross_ref: '{"field": "diagnosis_code", "ref_code_system": "diagnosis", "skip_empty": true}',
  logic: '{"check": "date_not_future", "field": "birth_date"}',
};
// 配置写坏、本次没扫的规则（P2-81）：原先任何一条都让整次扫描 500，现在跳过并点名
const qcSkippedNote = (skipped) => (skipped || []).length
  ? `<p class="msg err">⚠ ${skipped.length} 条规则配置有误，本次没有参与扫描：${skipped.map((r) =>
    `${esc(r.rule_code)} ${esc(r.rule_name)}（${esc(r.problem)}）`).join("；")}</p>`
  : "";

async function renderDataQuality() {
  $("#page-desc").textContent = "规则引擎按启用规则扫描存量数据：必填/区间/枚举/引用/逻辑五类校验，停用规则不参与扫描";
  const [summary, rules] = await Promise.all([
    api("/api/dataquality/summary"), api("/api/dataquality/rules")]);
  const canRule = currentRole() === "admin";   // 建规则仅管理员（后端 require_admin）
  const drawViolations = async (params = "?limit=200") => {
    const data = await api(`/api/dataquality/run${params}`);
    $("#qc-violations").innerHTML = qcSkippedNote(data.skipped_rules) + `<p class="desc" style="font-size:12.5px">共 ${data.total} 条违规（错误 ${data.error_total} / 警告 ${data.warn_total}），本页展示 ${data.items.length} 条</p>` +
      table(["规则", "规则名称", "表", "记录ID", "问题描述", "严重度"], data.items, (v) => {
        return `<tr><td><span class="tag">${esc(v.rule_code)}</span></td><td>${esc(v.rule_name)}</td>
          <td>${esc(v.table)}</td><td>${v.record_id}</td><td>${esc(v.message)}</td>
          <td>${statusTag(QC_SEVERITY, v.severity)}</td></tr>`;
      });
  };
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">参与扫描规则</div><div class="value">${summary.rules_checked}</div></div>
      <div class="card"><div class="label">违规总数</div><div class="value${summary.total ? " warn" : ""}">${summary.total}</div></div>
      <div class="card"><div class="label">错误级</div><div class="value${summary.by_severity.error ? " warn" : ""}">${summary.by_severity.error || 0}</div></div>
      <div class="card"><div class="label">警告级</div><div class="value">${summary.by_severity.warn || 0}</div></div></div>
    ${qcSkippedNote(summary.skipped_rules)}
    ${panel("违规明细", `
      <form class="inline" id="qc-run-form">
        <select name="rule_code"><option value="">全部规则</option>${rules.map((r) =>
          `<option value="${esc(r.code)}">${esc(r.code)} ${esc(r.name)}</option>`).join("")}</select>
        <select name="severity"><option value="">全部严重度</option><option value="error">错误</option><option value="warn">警告</option></select>
        <button>运行检查</button></form>
      <p class="msg" id="qc-msg"></p><div id="qc-violations">点击「运行检查」开始扫描</div>`)}
    ${panel("规则汇总", `${table(["规则", "名称", "类型", "表", "严重度", "违规数"], summary.by_rule, (r) => {
      const [text, color] = QC_SEVERITY[r.severity] || [r.severity, ""];
      return `<tr><td><span class="tag">${esc(r.rule_code)}</span></td><td>${esc(r.rule_name)}</td>
        <td>${esc(r.rule_type_name)}</td><td>${esc(r.table)}</td><td><span class="tag ${color}">${esc(text)}</span></td>
        <td>${r.violations ? `<span class="tag ${color}">${r.violations}</span>` : 0}</td></tr>`;
    })}`)}
    ${panel("规则库（管理员可新增、停用/启用、调整严重度，自建规则可删除）", `
      ${canRule ? `<form class="inline" id="qc-rule-form" style="margin-bottom:8px">
        <input name="code" placeholder="规则编码" required style="width:100px">
        <input name="name" placeholder="规则名称" required style="min-width:200px">
        <input name="target_table" placeholder="被检表" list="qc-tables" required style="width:150px">
        <datalist id="qc-tables">${[...new Set(rules.map((r) => r.target_table))].map((t) =>
          `<option value="${esc(t)}">`).join("")}</datalist>
        <select name="rule_type">${Object.entries(QC_RULE_TYPES).map(([k, v]) => `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="config" placeholder="配置 JSON，如 ${esc(QC_CONFIG_EXAMPLES.required)}" style="min-width:320px">
        <select name="severity"><option value="error">错误</option><option value="warn">警告</option></select>
        <button>新增规则</button>
      </form><p class="msg" id="qc-rule-msg"></p>` : ""}
      ${table(["编码", "名称", "类型", "被检表", "严重度", "状态", "操作"], rules, (r) => {
        // 操作按钮只给管理员（后端三个写接口都是 require_admin，别的角色点了只会 403）；
        // 删除只给自建规则——内置规则删了下次启动会按种子补回，只能停用（P2-564）
        return `<tr><td><span class="tag">${esc(r.code)}</span>${r.builtin ? ' <span class="tag">内置</span>' : ""}</td>
          <td>${esc(r.name)}</td><td>${esc(r.rule_type_name)}</td>
          <td>${esc(r.target_table)}</td><td>${statusTag(QC_SEVERITY, r.severity)}</td>
          <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
          <td>${canRule ? `<button class="btn secondary" data-qctoggle="${r.id}" data-active="${r.active ? 1 : 0}">${r.active ? "停用" : "启用"}</button>
            <button class="btn secondary" data-qcsev="${r.id}" data-sev="${esc(r.severity)}">切换严重度</button>${r.builtin ? ""
              : ` <button class="btn secondary" data-qcdel="${r.id}" data-code="${esc(r.code)}">删除</button>`}` : "—"}</td></tr>`;
      })}`)}`;
  // 规则库原先只能启停、切严重度，不能新增（P2-93 动词级孤儿）；种子 docstring 写着「落地时由质控办经接口增删调整」
  const ruleForm = $("#qc-rule-form");
  if (ruleForm) {
    // placeholder 是 DOM 属性、不是 HTML：原样赋文本，不经 esc（经了反倒显示出 &quot;）
    ruleForm.rule_type.onchange = (e) => {
      ruleForm.config.placeholder = "配置 JSON，如 " + (QC_CONFIG_EXAMPLES[e.target.value] || "{}");
    };
    ruleForm.onsubmit = (e) => {
      e.preventDefault();
      const body = formJson(e.target);
      try { body.config = body.config ? JSON.parse(body.config) : {}; }
      catch (err) { return setMsg("#qc-rule-msg", `配置 JSON 解析失败：${err.message}`, false); }
      return postAction("/api/dataquality/rules", body, "#qc-rule-msg");
    };
  }
  $("#qc-run-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const params = new URLSearchParams({ limit: "200" });
    if (f.get("rule_code")) params.set("rule_code", f.get("rule_code"));
    if (f.get("severity")) params.set("severity", f.get("severity"));
    try { await drawViolations(`?${params}`); }
    catch (err) { setMsg("#qc-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { qctoggle, active, qcsev, sev, qcdel, code } = e.target.dataset;
    try {
      if (qcdel) {
        // 删了就没了（违规明细按规则现扫，不留历史）；只是暂时不想扫的，用「停用」
        if (!confirm(`删除规则 ${code}？删除后不可恢复；只是暂时不扫请用「停用」。`)) return;
        await api(`/api/dataquality/rules/${qcdel}`, { method: "DELETE" });
        route();
      } else if (qctoggle) {
        await api(`/api/dataquality/rules/${qctoggle}`, { method: "PATCH", body: JSON.stringify({ active: active !== "1" }) });
        route();
      } else if (qcsev) {
        await api(`/api/dataquality/rules/${qcsev}`, { method: "PATCH", body: JSON.stringify({ severity: sev === "error" ? "warn" : "error" }) });
        route();
      }
    } catch (err) { setMsg("#qc-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawViolations();
}

/* 块1：打印模板维护（管理员）——抬头机构名、页脚说明与二维码开关按单据类型配置 */
async function renderPrintTemplates() {
  $("#page-desc").textContent = "按单据类型配置打印抬头、页脚与验真二维码；抬头留空时回落到单据所属机构名";
  const templates = await api("/api/print/templates");
  $("#page-body").innerHTML = `
    ${panel("打印模板", `
      ${table(["单据类型", "抬头机构名", "页脚说明", "二维码"], templates, (t) =>
        `<tr><td>${esc(t.doc_type_name)}</td><td>${esc(t.header_org_name) || "（用机构名）"}</td>
         <td>${esc(t.footer_note) || "（默认页脚）"}</td>
         <td>${t.show_qr ? '<span class="tag green">显示</span>' : '<span class="tag">隐藏</span>'}</td></tr>`)}
      <form class="inline" id="tpl-form">
        <select name="doc_type">${templates.map((t) => `<option value="${esc(t.doc_type)}">${esc(t.doc_type_name)}</option>`).join("")}</select>
        <input name="header_org_name" placeholder="抬头机构名（可空）" style="min-width:200px">
        <input name="footer_note" placeholder="页脚说明（可空）" style="min-width:220px">
        <label style="font-size:13px"><input type="checkbox" name="show_qr" checked> 显示验真二维码</label>
        <button>保存模板</button></form>
      <p class="msg" id="tpl-msg"></p>`)}`;
  // 选单据类型时回填这一类的现值（P2-993，与 P2-313 / P2-960「按现值预填」同一条规矩）：原先抬头、页脚空白、二维码恒勾选，
  // 保存时三项整条送出、后端整条覆盖——只想改一句页脚，抬头就被清空、关掉的验真二维码又打开了
  const tplForm = $("#tpl-form");
  const fillTemplate = () => {
    const t = templates.find((x) => x.doc_type === tplForm.doc_type.value);
    if (!t) return;
    tplForm.header_org_name.value = t.header_org_name || "";
    tplForm.footer_note.value = t.footer_note || "";
    tplForm.show_qr.checked = !!t.show_qr;
  };
  tplForm.doc_type.onchange = fillTemplate;
  fillTemplate();
  $("#tpl-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/print/templates", { method: "PUT", body: JSON.stringify({
        doc_type: f.get("doc_type"), header_org_name: f.get("header_org_name") || "",
        footer_note: f.get("footer_note") || "", show_qr: f.get("show_qr") === "on" }) });
      route();
    } catch (err) { setMsg("#tpl-msg", err.message, false); }
  };
}

const KB_CATEGORIES = { drug_policy: "药物政策", clinical_guideline: "临床指南", referral: "转诊知识", regulation: "质量制度规范", tcm_health: "中医养生" };

async function renderKnowledge() {
  $("#page-desc").textContent = "五分类统一知识库：发布（管理层/公卫）、检索、有效期管理与临期提醒";
  const expiring = await api("/api/knowledge/expiring?days=30");
  const canEdit = ["director", "public_health", "admin"].includes(currentRole());
  const catOpts = Object.entries(KB_CATEGORIES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  const draw = async (params = "") => {
    const entries = await api(`/api/knowledge${params}`);
    $("#kb-table").innerHTML = table(["ID", "分类", "标题", "有效期至", "状态", "操作"], entries, (k) =>
      `<tr><td>${k.id}</td><td><span class="tag">${esc(k.category_name)}</span></td><td>${esc(k.title)}</td>
       <td>${esc(k.expire_date) || "长期"}</td>
       <td>${k.expired ? '<span class="tag red">已过期</span>' : '<span class="tag green">有效</span>'}</td>
       <td>${canEdit ? `<button class="btn secondary" data-renew="${k.id}">续期</button>
            <button class="btn danger" data-deact="${k.id}">停用</button>` : "—"}</td></tr>`);
  };
  $("#page-body").innerHTML = `
    ${canEdit ? panel("发布知识条目（管理层/公卫）", `
      <form class="inline" id="kb-form">
        <select name="category">${catOpts}</select>
        <input name="title" placeholder="标题" required style="min-width:240px">
        <input name="body" placeholder="正文/摘要" style="min-width:220px">
        <input name="expire_date" placeholder="有效期至 YYYY-MM-DD（可空）">
        <button>发布</button></form><p class="msg" id="kb-msg"></p>`) : '<p class="msg" id="kb-msg"></p>'}
    ${expiring.length ? panel(`⚠ 30天内到期资料（${expiring.length}）`,
      table(["ID", "分类", "标题", "到期日"], expiring, (k) =>
        `<tr><td>${k.id}</td><td>${KB_CATEGORIES[k.category] || esc(k.category)}</td><td>${esc(k.title)}</td>
         <td><span class="tag orange">${esc(k.expire_date)}</span></td></tr>`), { accent: "#b26a00" }) : ""}
    ${panel("知识检索", `
      <form class="inline" id="kb-search">
        <select name="category"><option value="">全部分类</option>${catOpts}</select>
        <input name="q" placeholder="标题关键字">
        <label style="font-size:13px"><input type="checkbox" name="include_expired"> 含过期</label>
        <button>检索</button></form>
      <div id="kb-table"></div>`)}`;
  const kb = $("#kb-form");
  if (kb) kb.onsubmit = (e) => { e.preventDefault(); postAction("/api/knowledge", formJson(e.target), "#kb-msg"); };
  $("#kb-search").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const params = new URLSearchParams();
    if (f.get("category")) params.set("category", f.get("category"));
    if (f.get("q")) params.set("q", f.get("q"));
    if (f.get("include_expired")) params.set("include_expired", "true");
    const qs = params.toString();
    // 查询失败要说出来（P2-378）：原先 draw 抛错没人接，列表还是上一次的结果
    try { await draw(qs ? `?${qs}` : ""); } catch (err) { setMsg("#kb-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.renew) {
        // P2-38：弹窗换成页内表单；日期写错由后端 OptionalDateStr 报人话
        const form = await spdModal("续期", [
          { name: "expire_date", label: "新有效期", required: true, placeholder: "YYYY-MM-DD" }]);
        if (!form) return;
        await api(`/api/knowledge/${d.renew}`, { method: "PATCH", body: JSON.stringify({ expire_date: form.expire_date }) });
        route();
      }
      if (d.deact) {
        // 停用原先点一下就生效、没有任何确认；停用后条目从检索里消失，页面上没有恢复入口
        if (!await spdModal("停用知识条目", [], { intro: "停用后该条目不再出现在检索与临期提醒里，页面上不能恢复。" })) return;
        await api(`/api/knowledge/${d.deact}`, { method: "PATCH", body: JSON.stringify({ active: false }) });
        route();
      }
    } catch (err) { setMsg("#kb-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
}

/* ================= 块4：细目补齐（合并进既有页面的追加面板） ================= */

/* 在当前页面尾部追加一个面板容器，返回该容器（事件在容器内自绑，不干扰原页面） */
function appendSection(html) {
  const holder = document.createElement("div");
  holder.innerHTML = html;
  $("#page-body").appendChild(holder);
  return holder;
}

/* ⑭ 中药制剂管理：配方 / 批次 / 效期预警（挂中医药服务页） */
const DOSAGE_FORMS = { pill: "丸剂", powder: "散剂", paste: "膏剂", granule: "颗粒剂", decoction: "合剂/汤剂" };

async function drawTcmPreparations() {
  // 待发放的批次单独取一遍、排在最前（P2-457，同 P2-456）：清单只回最新 200 批，挤出窗口的就没有「发放」
  const [formulas, recentBatches, produced, expiring] = await Promise.all([
    api("/api/tcm/formulas"), api("/api/tcm/preparation-batches"), api("/api/tcm/preparation-batches?status=produced"),
    api("/api/tcm/preparation-batches/expiring?days=60")]);
  const batches = actionableFirst(recentBatches, produced);
  // 批次表与效期预警的「配方 / 制剂」列显示配方名（P2-1411）：原先印 formula_id 数字，药剂科得拿编号回上面的配方表对。
  // 按本页已取到的配方清单映射；映射不到的（配方清单只回最新 200 个）回显编号。接口不动
  const formulaNames = new Map(formulas.map((f) => [f.id, f.name]));
  const formulaOf = (b) => (formulaNames.has(b.formula_id) ? formulaNames.get(b.formula_id) : b.formula_id);
  const holder = appendSection(`
    ${panel("⑭ 中药制剂配方（药师/中医师维护）", `
      <form class="inline" id="tf-form">
        <input name="code" placeholder="制剂编码" required>
        <input name="name" placeholder="制剂名称" required>
        <select name="dosage_form">${Object.entries(DOSAGE_FORMS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="composition" placeholder="处方组成" style="min-width:200px">
        <input name="indication" placeholder="适应症">
        <input name="shelf_life_months" type="number" value="12" min="1" style="min-width:90px" title="有效期（月）">
        <button>新增配方</button></form>
      ${table(["ID", "编码", "名称", "剂型", "组成", "适应症", "有效期(月)"], formulas, (f) =>
        `<tr><td>${f.id}</td><td><span class="tag">${esc(f.code)}</span></td><td>${esc(f.name)}</td>
         <td>${esc(f.dosage_form_name)}</td><td>${esc(f.composition) || "—"}</td><td>${esc(f.indication) || "—"}</td>
         <td>${f.shelf_life_months}</td></tr>`)}`)}
    ${expiring.length ? panel(`⚠ 制剂效期预警（60天内到期/已过期 ${expiring.length}）`, `${
      table(["批号", "制剂", "效期", "状态"], expiring, (b) =>
        `<tr><td>${esc(b.batch_no)}</td><td>${esc(formulaOf(b))}</td>
         <td><span class="tag ${b.expired ? "red" : "orange"}">${esc(b.expire_date)}</span></td><td>${esc(b.status_name)}</td></tr>`)}`, { accent: "#b26a00" }) : ""}
    ${panel("制剂批次（效期缺省按配方有效期推算；过期批次禁止发放）", `
      <form class="inline" id="tb-form">
        <input name="formula_id" type="number" placeholder="配方ID" required>
        <input name="batch_no" placeholder="批号" required>
        <input name="org_id" type="number" placeholder="生产机构ID" required>
        <input name="quantity" type="number" placeholder="产量" required min="1">
        <input name="produced_date" placeholder="生产日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="expire_date" placeholder="效期（可空）">
        <button>投产建批</button></form>
      <p class="msg" id="tp-msg"></p>
      ${table(["ID", "批号", "配方", "数量", "生产日期", "效期", "状态", "操作"], batches, (b) =>
        `<tr><td>${b.id}</td><td>${esc(b.batch_no)}</td><td>${esc(formulaOf(b))}</td><td>${b.quantity}${esc(b.unit)}</td>
         <td>${esc(b.produced_date)}</td><td><span class="tag ${b.expired ? "red" : ""}">${esc(b.expire_date)}</span></td>
         <td>${esc(b.status_name)}</td>
         <td>${b.status === "produced" ? `<button class="btn secondary" data-release="${b.id}">发放</button>` : "—"}</td></tr>`)}`)}`);
  holder.querySelector("#tf-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/tcm/formulas", formJson(e.target, ["shelf_life_months"]), "#tp-msg");
  };
  holder.querySelector("#tb-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/tcm/preparation-batches", formJson(e.target, ["formula_id", "org_id", "quantity"]), "#tp-msg");
  };
  holder.onclick = async (e) => {
    const { release } = e.target.dataset;
    if (!release) return;
    try { await api(`/api/tcm/preparation-batches/${release}/release`, { method: "POST" }); route(); }
    catch (err) { setMsg("#tp-msg", err.message, false); }
  };
}

/* ⑥ 消毒供应成本核算（挂消毒供应页） */
const CSSD_COST_TYPES = { labor: "人工", material: "耗材", energy: "能耗", equipment: "设备折旧", other: "其他" };

async function drawCssdCosts() {
  const stats = await api("/api/cssd/cost-stats");
  const holder = appendSection(`
    ${panel("⑥ 消毒供应成本核算", `
      <div class="cards">
        <div class="card"><div class="label">成本合计</div><div class="value">${stats.total_cost}</div></div>
        <div class="card"><div class="label">灭菌件数</div><div class="value">${stats.total_quantity}</div></div>
        <div class="card"><div class="label">整体单件成本</div><div class="value">${stats.overall_unit_cost}</div></div></div>
      <form class="inline" id="cost-form">
        <input name="batch_id" type="number" placeholder="批次ID" required>
        <select name="cost_type">${Object.entries(CSSD_COST_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="amount" type="number" step="any" placeholder="金额" required>
        <input name="note" placeholder="备注">
        <button>登记成本项</button></form>
      <p class="msg" id="cost-msg"></p>
      <p style="font-size:13px">成本构成：${Object.entries(stats.by_cost_type).map(([k, v]) =>
        `<span class="tag" style="margin-right:6px">${esc(v.name)} ${v.amount}</span>`).join("") || "暂无"}</p>
      ${table(["批次", "批号", "物品", "件数", "总成本", "单件成本", "明细"], stats.batches, (b) =>
        `<tr><td>${b.batch_id}</td><td>${esc(b.batch_no)}</td><td>${esc(b.item_name)}</td><td>${b.quantity}</td>
         <td>${b.total_cost}</td><td><span class="tag">${b.unit_cost}</span></td>
         <td><button class="btn secondary" data-costitems="${esc(b.batch_id)}">成本项</button></td></tr>`)}
      <div id="cost-items"></div>`)}`);
  holder.querySelector("#cost-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/cssd/cost-items", formJson(e.target, ["batch_id", "amount"]), "#cost-msg");
  };
  // 逐条成本项（P2-495）：原先只看得到按批次的合计与构成，登记的每一项（金额、备注）录进去就没处核对，录错一笔只能从
  // 单件成本的异常上倒推
  holder.onclick = async (e) => {
    const batchId = e.target.dataset.costitems;
    if (!batchId) return;
    try {
      const rows = await api(`/api/cssd/cost-items?batch_id=${encodeURIComponent(batchId)}`);
      holder.querySelector("#cost-items").innerHTML = `<h3 style="margin-top:12px">批次 ${esc(batchId)} 的成本项（${rows.length} 项）</h3>
        ${table(["ID", "类型", "金额", "备注"], rows, (i) =>
          `<tr><td>${i.id}</td><td>${esc(i.cost_type_name)}</td><td>${esc(i.amount)}</td><td>${esc(i.note) || "—"}</td></tr>`)}`;
    } catch (err) { setMsg("#cost-msg", err.message, false); }
  };
}

/* ⑳ 课件资源 + ㉑ 适宜技术实训（挂远程医学教育页） */
const MATERIAL_TYPES = { slide: "课件", video: "视频", doc: "文档", link: "外链" };

/* 课件「点播」= 打开并计一次（P2-1428）：有 http(s) 外链的先开外链、再发计数。开窗必须在点击手势里同步做（await 之后再开
 * 会被弹窗拦截，与 openPrintPage 同一口径），所以单列成函数、window.open 写在发请求之前。只认 http(s)（isHttpUrl，同收银页
 * pay_url）——javascript: 链接打开是在本站执行；noopener：外链页拿不到本页的 window.opener */
function playMaterial(id, url) {
  if (isHttpUrl(url)) window.open(url, "_blank", "noopener");
  return api(`/api/education/materials/${id}/play`, { method: "POST" });
}

async function drawEduGaps() {
  // 实训计划的适宜技术从技术库里选（P2-1430）：原先手填编号，而技术库页面不显示编号——填一个别的、但确实存在的编号照样
  // 201，计划挂到了另一项技术上。技术库只要登录就能读；万一没取到，下拉只剩「不挂适宜技术」并说一句，别把整页掀掉
  const [mstats, plans, techniques] = await Promise.all([
    api("/api/education/material-stats"), api("/api/education/training-plans"),
    api("/api/tcm/techniques").catch(() => null)]);
  const holder = appendSection(`
    ${panel(`⑳ 课件资源管理（点播总量 ${mstats.total_plays}，课件 ${mstats.total_materials} 个）`, `
      <form class="inline" id="cm-form">
        <input name="course_id" type="number" placeholder="课程ID" required>
        <input name="title" placeholder="课件标题" required style="min-width:200px">
        <select name="material_type">${Object.entries(MATERIAL_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="url" placeholder="外链地址（可空）">
        <button>新增课件</button></form>
      <form class="inline" id="cm-query"><input name="course_id" type="number" placeholder="课程ID" required><button>查课件</button></form>
      <form class="inline" id="cm-att"><input name="material_id" type="number" placeholder="课件ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传附件</button></form>
      <p class="msg" id="cm-msg"></p><div id="cm-list"></div><div id="cm-att-list"></div>
      <h4 style="margin-top:10px">点播排行</h4>
      ${table(["课件ID", "标题", "类型", "点播量"], mstats.top, (m) =>
        `<tr><td>${m.id}</td><td>${esc(m.title)}</td><td>${esc(m.material_type_name)}</td>
         <td><span class="tag">${m.play_count}</span></td></tr>`)}`)}
    ${panel("㉑ 适宜技术实训（计划 → 报名 → 考核）", `
      <form class="inline" id="tp-plan-form">
        <input name="title" placeholder="实训主题" required style="min-width:180px">
        <input name="org_id" type="number" placeholder="承办机构ID" required>
        <select name="technique_id"><option value="">不挂适宜技术</option>${(techniques || []).map((t) =>
          `<option value="${t.id}">${esc(t.name)}</option>`).join("")}</select>${
          techniques ? "" : '<span class="desc">适宜技术库没取到，本次只能不挂适宜技术</span>'}
        <input name="plan_date" placeholder="实训日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="capacity" type="number" value="30" min="1" style="min-width:80px">
        <input name="trainer" placeholder="带教老师">
        <button>发布计划</button></form>
      <p class="msg" id="tplan-msg"></p>
      ${table(["ID", "主题", "适宜技术", "日期", "带教", "名额", "已报", "余额", "状态", "操作"], plans, (p) =>
        // 挂的适宜技术写名称（P2-1430，取出参的 technique_name）：原先表里没有这一列，挂没挂、挂的是哪项都看不出
        `<tr><td>${p.id}</td><td>${esc(p.title)}</td><td>${esc(p.technique_name) || "—"}</td><td>${esc(p.plan_date)}</td>
         <td>${esc(p.trainer) || "—"}</td>
         <td>${p.capacity}</td><td>${p.enrolled}</td><td>${p.remaining}</td><td>${esc(p.status_name)}</td>
         <td><button class="btn secondary" data-enroll="${p.id}">报名</button>
             <button class="btn secondary" data-unenroll="${p.id}">退报</button>
             <button class="btn secondary" data-assess="${p.id}">录考核</button>
             <button class="btn secondary" data-roster="${p.id}">名单成绩</button></td></tr>`)}
      <div id="tp-roster"></div>`)}`);
  const drawMaterials = async (courseId) => {
    const list = await api(`/api/education/courses/${courseId}/materials`);
    holder.querySelector("#cm-list").innerHTML = table(["ID", "标题", "类型", "外链", "附件", "点播", "操作"], list, (m) => {
      // 外链只给 http(s) 画成链接（P2-1428）：原先清单根本不读 m.url——视频、PPT 只能填外链（附件只收图片与 PDF），填进去
      // 哪儿都看不到。存量里别的协议照原样转义成文字、不做 href：CSP 放行了 'unsafe-inline'，javascript: 链接点了会在本站执行
      const link = isHttpUrl(m.url);
      // 「点播」= 打开并计一次：有外链的开外链，没有外链但有附件的展开附件清单；两样都没有，没有可点播的
      const play = link || m.attachments
        ? `<button class="btn secondary" data-play="${m.id}"${link ? ` data-url="${esc(m.url)}"` : ""}>点播</button>` : "—";
      // 附件原先只给个数、看不到也下不了（P2-431）：上传了课件附件，页面上再没有入口取回来
      return `<tr><td>${m.id}</td><td>${esc(m.title)}</td><td>${esc(m.material_type_name)}</td>
       <td>${link ? `<a href="${esc(m.url)}" target="_blank" rel="noopener">${esc(m.url)}</a>` : esc(m.url) || "—"}</td>
       <td>${m.attachments ? `<button class="btn secondary" data-cmatt="${m.id}">${m.attachments} 个 · 查看</button>` : "0"}</td>
       <td data-plays="${m.id}">${m.play_count}</td><td>${play}</td></tr>`;
    });
  };
  holder.querySelector("#cm-form").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    postAction(`/api/education/courses/${f.get("course_id")}/materials`, {
      title: f.get("title"), material_type: f.get("material_type"), url: f.get("url") || "" }, "#cm-msg");
  };
  holder.querySelector("#cm-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawMaterials(new FormData(e.target).get("course_id")); }
    catch (err) { setMsg("#cm-msg", err.message, false); }
  };
  holder.querySelector("#cm-att").onsubmit = async (e) => {
    e.preventDefault();
    const materialId = new FormData(e.target).get("material_id");
    try {
      await uploadAttachment("course_material", materialId, e.target.querySelector("input[type=file]"));
      setMsg("#cm-msg", "课件附件已上传");
      await drawAttachments("course_material", materialId, "#cm-att-list", "#cm-msg");
    } catch (err) { setMsg("#cm-msg", err.message, false); }
  };
  holder.querySelector("#tp-plan-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/education/training-plans", formJson(e.target, ["org_id", "technique_id", "capacity"]), "#tplan-msg");
  };
  holder.onclick = async (e) => {
    const { play, url, enroll, unenroll, assess, roster, cmatt } = e.target.dataset;
    try {
      if (cmatt) return await drawAttachments("course_material", cmatt, "#cm-att-list", "#cm-msg");
      if (play) {
        // 打开并计一次（P2-1428）：原先只发计数再整页 route()——什么也不打开，刚查出来的课件清单也被重画冲掉。
        // 开窗在 playMaterial 里同步做；计完只改这一行的点播数；没有外链的（有附件才摆按钮）展开附件清单
        const counted = await playMaterial(play, url);
        holder.querySelector(`[data-plays="${play}"]`).textContent = counted.play_count;
        if (!url) await drawAttachments("course_material", play, "#cm-att-list", "#cm-msg");
        return;
      }
      if (enroll) { await api(`/api/education/training-plans/${enroll}/enroll`, { method: "POST" }); return route(); }
      if (unenroll) { await api(`/api/education/training-plans/${unenroll}/cancel-enroll`, { method: "POST" }); return route(); }
      if (assess) {
        // P2-38：原先三连问——学员要手打用户 ID（后端只收本计划已报名的，打错就是 409），
        // 评语框点取消照样提交。改成表单：学员从本计划的报名名单里选，取消就是不录。
        const enrolled = (await api(`/api/education/training-plans/${assess}/enrollments`))
          .filter((r) => r.status === "enrolled");
        if (!enrolled.length) { setMsg("#tplan-msg", "该计划还没有在报名的学员，无人可录考核", false); return; }
        // 框自己提交（P2-607）：分数越界、评语写超了时报错写在框里、框不关，选的学员与写的评语都在
        const ok = await spdModal("录考核（60 分及格；同一学员重录即更新成绩）", [
          { name: "user_id", label: "学员", type: "select", options: enrolled.map((r) =>
            ({ value: r.user_id, label: `${r.full_name || r.username}（${r.username}）` })) },
          { name: "score", label: "考核得分（0-100）", type: "number", required: true },
          { name: "comment", label: "评语", type: "textarea" },
        ], { submit: (form) => api(`/api/education/training-plans/${assess}/assessments`, { method: "POST",
          body: JSON.stringify({ user_id: Number(form.user_id), score: form.score, comment: form.comment }) }) });
        if (ok) route();
        return;
      }
      if (roster) {
        const [list, scores] = await Promise.all([
          api(`/api/education/training-plans/${roster}/enrollments`),
          api(`/api/education/training-plans/${roster}/assessments`)]);
        holder.querySelector("#tp-roster").innerHTML =
          // 合格率只算还在报名的（P2-625）：退了报名的成绩照列，标「不计入」
          `<h4>计划 ${esc(roster)} 报名名单（合格率 ${scores.pass_rate_pct}%，按在报名的 ${scores.total} 人算）</h4>` +
          table(["用户ID", "账号", "姓名", "报名状态", "成绩", "是否合格"], list, (r) => {
            const s = scores.items.find((i) => i.user_id === r.user_id);
            return `<tr><td>${r.user_id}</td><td>${esc(r.username)}</td><td>${esc(r.full_name) || "—"}</td>
              <td>${esc(r.status_name)}</td><td>${s ? s.score : "—"}</td>
              <td>${s ? (s.passed ? '<span class="tag green">合格</span>' : '<span class="tag red">不合格</span>') : "—"}${
                s && !s.enrolled ? ' <span class="desc">不计入</span>' : ""}</td></tr>`;
          });
      }
    } catch (err) { setMsg("#tplan-msg", err.message, false); }
  };
}

/* ㉔ 产前筛查与诊断（挂妇幼保健页） */
const SCREEN_TYPES = { down: "唐氏血清学筛查", nipt: "无创产前基因检测", ultrasound: "超声结构筛查", diagnosis: "产前诊断" };
const SCREEN_RESULTS = { low_risk: ["低风险", "green"], high_risk: ["高风险", "red"], critical: ["临界风险", "orange"] };

async function drawPrenatalScreenings() {
  const [screenings, stats] = await Promise.all([
    api("/api/maternal/screenings"), api("/api/maternal/screening-stats")]);
  const holder = appendSection(`
    ${panel("㉔ 产前筛查与诊断（高风险/临界风险自动标记孕产妇高危）", `
      <div class="cards">
        <div class="card"><div class="label">筛查总数</div><div class="value">${stats.total}</div></div>
        <div class="card"><div class="label">高危检出率</div><div class="value${stats.high_risk_detect_rate_pct > 0 ? " warn" : ""}">${stats.high_risk_detect_rate_pct}%</div></div></div>
      <form class="inline" id="ps-form">
        <input name="record_id" type="number" placeholder="孕产妇档案ID" required>
        <select name="screen_type">${Object.entries(SCREEN_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="screen_date" placeholder="筛查日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="gest_week" type="number" placeholder="孕周" style="min-width:80px">
        <select name="result">${Object.entries(SCREEN_RESULTS).map(([v, [t]]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="indicator" placeholder="指标值">
        <input name="conclusion" placeholder="结论建议" style="min-width:160px">
        <button>登记筛查</button></form>
      <p class="msg" id="ps-msg"></p>
      ${table(["ID", "档案", "项目", "日期", "孕周", "结论", "指标", "高危标记"], screenings, (s) => {
        return `<tr><td>${s.id}</td><td>${s.record_id}</td><td>${esc(s.screen_type_name)}</td><td>${esc(s.screen_date)}</td>
          <td>${s.gest_week ?? "—"}</td><td>${statusTag(SCREEN_RESULTS, s.result)}</td><td>${esc(s.indicator) || "—"}</td>
          <td>${s.flagged_high_risk ? '<span class="tag red">已标记高危</span>' : "—"}</td></tr>`;
      })}`)}`);
  holder.querySelector("#ps-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/maternal/screenings", formJson(e.target, ["record_id", "gest_week"]), "#ps-msg");
  };
}

/* ㉟ 绩效自评改进（挂绩效考核页） */
const TASK_STATUS = { open: ["待整改", "orange"], in_progress: ["整改中", ""], completed: ["已完成待确认", "orange"], verified: ["已确认关闭", "green"] };

/** 措施 / 结果一格按状态取（P2-462）：整改中的显示当前措施，被退回过的带上退回人与理由，已关闭的带上确认人与意见。
 *  原先一律 `completion_note || measures`——退回后那条被驳回的整改结果说明还挂在整改中的任务上，
 *  退回理由（接口里一直有 verify_comment / verified_by）页面上哪儿也看不到。 */
function improvementNote(t) {
  const submitted = t.status === "completed" || t.status === "verified";
  const note = esc(submitted ? (t.completion_note || t.measures) : t.measures) || "—";
  if (!t.verified_by) return note;
  const [label, color] = t.status === "verified" ? ["确认关闭", "green"]
    : t.status === "completed" ? ["上次退回", "orange"] : ["已退回", "red"];
  return `${note}<br><span class="tag ${color}">${label}</span> ${esc(t.verified_by)}${
    t.verify_comment ? `：${esc(t.verify_comment)}` : ""}`;
}

async function drawImprovementTasks() {
  // 待确认、待整改、整改中的单独取一遍、排在最前（P2-462，同 P2-456）：清单只回最新 100 条，挤出窗口的那条就没有
  // 「确认关闭 / 退回」「登记进展 / 提交完成」可点
  const [recent, stats, ...open] = await Promise.all([
    api("/api/performance/improvements"), api("/api/performance/improvement-stats"),
    ...["completed", "open", "in_progress"].map((st) => api(`/api/performance/improvements?status=${st}`))]);
  const tasks = actionableFirst(recent, ...open);
  const holder = appendSection(`
    ${panel("㉟ 绩效自评改进（问题 → 责任人 → 期限 → 完成确认）", `
      <div class="cards">
        <div class="card"><div class="label">整改任务</div><div class="value">${stats.total}</div></div>
        <div class="card"><div class="label">超期未办</div><div class="value${stats.overdue ? " warn" : ""}">${stats.overdue}</div></div>
        <div class="card"><div class="label">闭环率</div><div class="value">${stats.closed_rate_pct}%</div></div></div>
      <form class="inline" id="imp-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="indicator_key" placeholder="关联指标key（可空）">
        <input name="problem" placeholder="发现问题" required style="min-width:200px">
        <input name="owner_name" placeholder="责任人" required>
        <input name="due_date" placeholder="整改期限 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>下达整改</button></form>
      <p class="msg" id="imp-msg"></p>
      ${table(["ID", "机构", "问题", "责任人", "期限", "状态", "措施/结果", "操作"], tasks, (t) => {
        const actions = t.status === "completed"
          ? `<button class="btn secondary" data-impok="${t.id}">确认关闭</button>
             <button class="btn danger" data-impno="${t.id}">退回</button>`
          : t.status === "verified" ? "—"
          : `<button class="btn secondary" data-impprog="${t.id}">登记进展</button>
             <button class="btn secondary" data-impdone="${t.id}">提交完成</button>`;
        return `<tr><td>${t.id}</td><td>${t.org_id}</td><td>${esc(t.problem)}</td><td>${esc(t.owner_name)}</td>
          <td>${t.overdue ? `<span class="tag red">${esc(t.due_date)} 超期</span>` : esc(t.due_date)}</td>
          <td>${statusTag(TASK_STATUS, t.status)}</td>
          <td>${improvementNote(t)}</td><td>${actions}</td></tr>`;
      })}`)}`);
  holder.querySelector("#imp-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/performance/improvements", formJson(e.target, ["org_id"]), "#imp-msg");
  };
  // P2-38：四处原生弹窗换成页内表单。原先在弹窗上点"取消"照样提交——"确认关闭"点了取消，
  // 任务照样关了；"退回"点了取消，照样退回且没有理由；"登记进展"点了取消，落一条空措施。
  // 表单里取消就是放弃。
  holder.onclick = async (e) => {
    const { impprog, impdone, impok, impno } = e.target.dataset;
    try {
      // 登记进展与确认 / 退回两张多行框由框自己提交（P2-607）：写超了、任务状态已变（409）时报错写在框里、框不关
      if (impprog) {
        const ok = await spdModal("登记整改进展", [{ name: "measures", label: "整改措施", type: "textarea" }],
          { submit: (v) => api(`/api/performance/improvements/${impprog}/progress`, { method: "POST",
            body: JSON.stringify(v) }) });
        if (ok) route();
        return;
      }
      if (impdone) {
        const v = await spdModal("提交整改完成", [
          { name: "completion_note", label: "整改结果说明（必填）", required: true }]);
        if (!v) return;
        return postAction(`/api/performance/improvements/${impdone}/progress`, { complete: true, ...v }, "#imp-msg");
      }
      if (impok || impno) {
        const ok = await spdModal(impok ? "确认关闭整改任务" : "退回整改", [
          { name: "comment", label: impok ? "确认意见（可空）" : "退回理由", type: "textarea" }],
        { submit: (v) => api(`/api/performance/improvements/${impok || impno}/verify`, { method: "POST",
          body: JSON.stringify({ approve: Boolean(impok), comment: v.comment }) }) });
        if (ok) route();
        return;
      }
    } catch (err) { setMsg("#imp-msg", err.message, false); }
  };
}

/* ⑨ 上门服务调度（挂家医签约页） */
const VISIT_SERVICES = { nursing: "上门护理", doctor: "上门诊疗", rehab: "康复指导", sampling: "上门采样" };
const VISIT_STATUS = { applied: ["待派单", "orange"], dispatched: ["已派单", ""], completed: ["已完成", "green"], cancelled: ["已取消", "red"] };

async function drawHomeVisits() {
  // 待派单、待完成的单独取一遍、排在最前（P2-408，同审方 P1-148）：工单清单只回最新 100 条，挤出窗口的申请
  // 页面上就再没有「派单 / 取消」「完成」可点
  const [recent, applied, dispatched, stats] = await Promise.all([api("/api/homevisits"),
    api("/api/homevisits?status=applied"), api("/api/homevisits?status=dispatched"), api("/api/homevisits/stats")]);
  const actionableIds = new Set([...applied, ...dispatched].map((o) => o.id));
  const orders = [...applied, ...dispatched, ...recent.filter((o) => !actionableIds.has(o.id))];
  // 按钮只给接口收的角色（P2-429）：派单 / 取消限经办 / 医师，完成另收公卫；管理员都放行。原先公卫人员也看得到
  // 「派单」「取消」，点下去一次 403
  const role = currentRole();
  const canDispatch = ["operator", "doctor", "admin"].includes(role);
  const canComplete = ["operator", "doctor", "public_health", "admin"].includes(role);
  const holder = appendSection(`
    ${panel("⑨ 送医送护上门（申请 → 派单 → 完成；自动关联履约中家医签约）", `
      <div class="cards">
        <div class="card"><div class="label">上门工单</div><div class="value">${stats.total}</div></div>
        <div class="card"><div class="label">签约关联率</div><div class="value">${stats.contract_linked_ratio_pct}%</div></div></div>
      <form class="inline" id="hv-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="服务机构ID" required>
        <select name="service_type">${Object.entries(VISIT_SERVICES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="demand" placeholder="服务需求" style="min-width:180px">
        <input name="address" placeholder="上门地址" style="min-width:160px">
        <input name="expect_date" placeholder="期望日期 YYYY-MM-DD">
        <button>提交申请</button></form>
      <p class="msg" id="hv-msg"></p>
      ${table(["ID", "患者", "签约", "服务", "需求", "状态", "上门人员", "操作"], orders, (o) => {
        // 已派单的也能取消（P2-595）：接口只挡已完成的，页面原先只给「完成」——派出去才知道去不成（住院了、搬走了）的
        // 工单就一直挂在待完成里
        const cancel = canDispatch ? `<button class="btn danger" data-hvcancel="${o.id}">取消</button>` : "";
        const actions = o.status === "applied" && canDispatch
          ? `<button class="btn secondary" data-hvdis="${o.id}">派单</button>
             ${cancel}`
          : o.status === "dispatched" && canComplete
          ? `<button class="btn secondary" data-hvdone="${o.id}">完成</button>
             ${cancel}` : "—";
        return `<tr><td>${o.id}</td><td>${o.patient_id}</td><td>${o.contract_id ?? "—"}</td>
          <td>${esc(o.service_type_name)}</td><td>${esc(o.demand) || "—"}</td>
          <td>${statusTag(VISIT_STATUS, o.status)}</td><td>${esc(o.assignee_name) || "—"}</td><td>${actions}</td></tr>`;
      })}`)}`);
  holder.querySelector("#hv-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/homevisits", formJson(e.target, ["patient_id", "org_id"]), "#hv-msg");
  };
  holder.onclick = async (e) => {
    const { hvdis, hvdone, hvcancel } = e.target.dataset;
    try {
      // P2-38：派单 / 完成换成页内表单——服务记录是一整段文字（做了什么、患者情况），
      // 弹窗只有一行、粘不了长文本；留空由后端报人话。
      if (hvdis) {
        const form = await spdModal("派单", [{ name: "assignee_name", label: "上门人员姓名", required: true }]);
        if (!form) return;
        return postAction(`/api/homevisits/${hvdis}/dispatch`, { assignee_name: form.assignee_name }, "#hv-msg");
      }
      if (hvdone) {
        // 框自己提交（P2-607）：服务记录写超了、留空、工单已被别人完成或取消时报错写在框里、框不关，写好的服务记录不用重写
        const ok = await spdModal("完成上门服务", [
          { name: "service_note", label: "服务记录（必填）", type: "textarea" }],
        { submit: (form) => api(`/api/homevisits/${hvdone}/complete`, { method: "POST",
          body: JSON.stringify({ service_note: form.service_note }) }) });
        if (ok) route();
        return;
      }
      if (hvcancel) {
        // 取消工单原先点一下就生效、没有任何确认：居民提的上门申请一次误点就作废了，页面上也恢复不了。
        if (!await spdModal("取消上门工单", [], { intro: "点「确定」作废该工单，不能恢复；点「取消」保留。" })) return;
        return postAction(`/api/homevisits/${hvcancel}/cancel`, null, "#hv-msg");
      }
    } catch (err) { setMsg("#hv-msg", err.message, false); }
  };
}

/* ---------------- 启动 ---------------- */

function buildNav() {
  $("#nav").innerHTML = PAGES.filter(pageAllowed).map((p) =>
    p.group
      ? `<div class="nav-group">${p.group}</div>`
      : `<a href="#${p.id}" data-page="${p.id}">${p.title}</a>`).join("");
}

function enterApp() {
  $("#login-view").classList.add("hidden");
  $("#app-view").classList.remove("hidden");
  buildNav();
  startTodoPolling();
  route();
}

$("#todo-bell").onclick = (e) => {
  if (e.target.closest("#todo-panel")) return;
  $("#todo-panel").classList.toggle("hidden");
};

$("#logout").onclick = logout;
const NOTE_TYPES = { first: "首次病程", daily: "日常病程", ward_round: "上级查房",
  rescue: "抢救记录", consultation: "会诊记录", discharge: "出院记录" };
// 桌面病程表单的缺省类型（P2-1306）：下拉原先按键序、没有预选，医生不动下拉就送首项「首次病程」——新入院先写的抢救记录
// 落成首次病程，真正的首次病程随后 409、且改不回来（住院文书没有更正入口）。预选日常病程，与医生移动端查房的首项一致：
// 首次病程漏写由文书完整性自查报出、还能补，误写成首次病程不可逆。键序不动（清单按类型取名也用这张表）
const PROGRESS_NOTE_DEFAULT = "daily";
const NURSING_LEVELS = { special: "特级护理", level1: "一级护理", level2: "二级护理", level3: "三级护理" };
// 住院护理记录的缺省级别与接口（`clinical_docs.NursingIn`）、表列缺省同一个（P2-988）：下拉原先按键序、没有预选，护士不动
// 就送首项「特级护理」，二级、三级护理的患者在护理记录上一律成了特级；门急诊那张表单本就预选了它自己接口的缺省
const INPATIENT_NURSING_DEFAULT = "level2";
