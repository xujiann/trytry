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
  const cases = await api("/api/emergency/cases");
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
       <td>${esc(c.status)}</td>
       <td><button class="btn secondary" data-mile="${c.id}">录节点</button>
           <button class="btn" data-timeline="${c.id}">时间轴</button></td></tr>`))}
    <div class="panel hidden" id="gc-tl-panel"><h3>绿道时间轴</h3><div id="gc-tl"></div></div>`;
  $("#gc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id"]), "#gc-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.mile) {
        const keys = Object.keys(MILESTONES);
        const pick = prompt(`节点（${keys.map((k, i) => `${i + 1}=${MILESTONES[k]}`).join("，")}）输入序号`);
        const key = keys[Number(pick) - 1];
        if (!key) return;
        const at = prompt("发生时刻（如 2026-08-11 14:30）");
        if (!at) return;
        await api(`/api/emergency/cases/${d.mile}/milestones`, { method: "POST",
          body: JSON.stringify({ milestone: key, occurred_at: at }) });
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
  const [groups, stats] = await Promise.all([api("/api/drgs/groups"), api("/api/drgs/stats")]);
  // ADR-0009 第五批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 三个统计面板"有数据才渲染"，条件仍留在调用点。
  $("#page-body").innerHTML = `
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
    ${panel("分组目录（admin 可调权）", `<p class="msg" id="drg-msg"></p>${
      table(["编码", "MDC", "名称", "基准权重", "主诊断关键词", "主手术关键词", "状态", "操作"], groups, (g) =>
        `<tr><td>${esc(g.code)}</td><td>${esc(g.mdc) || "—"}</td><td>${esc(g.name)}</td><td>${g.base_weight}</td>
         <td>${esc(g.keywords) || "—"}</td>
         <td>${esc(g.procedure_keywords) || "—"}${g.require_procedure ? ' <span class="tag orange">必须</span>' : ""}</td>
         <td><span class="tag ${g.active ? "green" : "red"}">${g.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-drg-weight="${g.id}">调权</button></td></tr>`)}`)}`;
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.drgWeight;
    if (!id) return;
    const w = prompt("新基准权重（>0）");
    if (!w) return;
    try { await api(`/api/drgs/groups/${id}`, { method: "PATCH", body: JSON.stringify({ base_weight: Number(w) }) }); route(); }
    catch (err) { setMsg("#drg-msg", err.message, false); }
  };
}

/* ---------------- 终审轮新增页面 ---------------- */

const BLOOD_COMPONENTS = { rbc: "红细胞", plasma: "血浆", platelet: "血小板" };
const BLOOD_REQ_STATUS = { pending: ["待审批", "orange"], approved: ["已审批", ""], rejected: ["已驳回", "red"], issued: ["已发血", "green"] };

async function renderBlood() {
  $("#page-desc").textContent = "血库台账（经办登记）→ 用血申请（医师）→ 审批（管理层）→ 发血（经办，库存不足拦截）";
  const [stocks, requests] = await Promise.all([api("/api/blood/stocks"), api("/api/blood/requests")]);
  const role = currentRole();
  const typeOpts = ["A", "B", "AB", "O"].map((t) => `<option>${t}</option>`).join("");
  const compOpts = Object.entries(BLOOD_COMPONENTS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("");
  // ADR-0009 第四批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 两个表单面板按角色条件渲染，条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${["operator", "admin"].includes(role) ? panel("血库入库登记（经办）", `
      <form class="inline" id="bs-form">
        <select name="blood_type">${typeOpts}</select>
        <select name="component">${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <button>入库</button></form>`) : ""}
    ${["doctor", "admin"].includes(role) ? panel("临床用血申请（医师）", `
      <form class="inline" id="br-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="用血机构ID" required>
        <select name="blood_type">${typeOpts}</select>
        <select name="component">${compOpts}</select>
        <input name="quantity_ml" type="number" min="1" placeholder="数量(ml)" required>
        <input name="reason" placeholder="用血原因">
        <button>申请</button></form>`) : ""}
    <p class="msg" id="blood-msg"></p>
    ${panel("血液库存台账", table(["血型", "成分", "库存(ml)"], stocks, (s) =>
      `<tr><td><span class="tag">${esc(s.blood_type)}</span></td><td>${BLOOD_COMPONENTS[s.component] || esc(s.component)}</td><td>${s.quantity_ml}</td></tr>`))}
    ${panel("用血申请队列", table(["ID", "患者", "机构", "血型/成分", "数量", "状态", "操作"], requests, (r) => {
      const actions = r.status === "pending" && ["director", "admin"].includes(role)
        ? `<button class="btn secondary" data-brev="${r.id}" data-ok="true">批准</button>
           <button class="btn danger" data-brev="${r.id}" data-ok="false">驳回</button>`
        : r.status === "approved" && ["operator", "admin"].includes(role)
        ? `<button class="btn secondary" data-bissue="${r.id}">发血</button>` : "—";
      return `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${r.org_id}</td>
        <td>${esc(r.blood_type)} / ${BLOOD_COMPONENTS[r.component] || esc(r.component)}</td><td>${r.quantity_ml}ml</td>
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
          ? `<button class="btn secondary" data-porec="${o.id}">验收入库</button>` : "—";
        return `<tr><td>${o.id}</td><td>${o.org_id}</td><td>${esc(supNames[o.supplier_id] || o.supplier_id)}</td>
          <td>${o.item_type === "drug" ? "药品" : "物资"}</td><td>${esc(o.item_name)}（${esc(o.item_code)}）</td>
          <td>${o.quantity}</td><td>${statusTag(PO_STATUS, o.status)}</td><td>${actions}</td></tr>`;
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
  $("#st-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pharmacy/stock-takes", formJson(e.target, ["org_id", "actual_qty"]), "#po-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.poap) return postAction(`/api/pharmacy/purchase-orders/${d.poap}/approve${d.reject ? "?reject=true" : ""}`, null, "#po-msg");
    if (d.porec) return postAction(`/api/pharmacy/purchase-orders/${d.porec}/receive`, null, "#po-msg");
  };
}

const CERT_TYPES = { birth: "出生医学证明", death: "死亡医学证明", defect: "出生缺陷儿登记" };

async function renderCerts() {
  $("#page-desc").textContent = "出生/死亡医学证明签发与出生缺陷登记（限医师/公卫）；成人健康体检记录与异常清单";
  const [stats, checkups, abnormal] = await Promise.all([
    api("/api/certs/stats"), api("/api/checkups"), api("/api/checkups/abnormal")]);
  const draw = async (certType = "") => {
    const certs = await api(`/api/certs${certType ? `?cert_type=${certType}` : ""}`);
    $("#cert-table").innerHTML = table(["编号", "类型", "姓名", "性别", "日期", "诊断/说明", "机构", "操作"], certs, (c) =>
      `<tr><td><span class="tag">${esc(c.cert_no)}</span></td><td>${CERT_TYPES[c.cert_type] || esc(c.cert_type)}</td>
       <td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.event_date)}</td><td>${esc(c.detail) || "—"}</td><td>${c.org_id}</td>
       <td><button class="btn secondary" data-printcert="${c.id}">打印</button></td></tr>`);
  };
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
    ${panel("成人健康体检登记（医师/公卫，异常项自动标记并入360档案）", `
      <form class="inline" id="chk-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="体检机构ID" required>
        <input name="package_name" placeholder="套餐（默认常规体检）">
        <input name="exam_date" placeholder="体检日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="summary" placeholder="体检结论" style="min-width:160px">
        <input name="abnormal_items" placeholder="异常项（有则填）" style="min-width:160px">
        <button>登记</button></form>`)}
    ${abnormal.length ? panel(`⚠ 体检异常清单（${abnormal.length}，供慢病筛查建档衔接）`,
      table(["体检ID", "患者", "日期", "异常项"], abnormal, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.exam_date)}</td><td><span class="tag red">${esc(a.abnormal_items)}</span></td></tr>`)) : ""}
    ${panel("体检记录", table(["ID", "患者", "套餐", "日期", "结论", "异常"], checkups, (c) =>
      `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${esc(c.package_name)}</td><td>${esc(c.exam_date)}</td>
       <td>${esc(c.summary) || "—"}</td><td>${c.has_abnormal ? `<span class="tag red">${esc(c.abnormal_items)}</span>` : '<span class="tag green">正常</span>'}</td></tr>`))}`;
  $("#cert-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/certs", formJson(e.target, ["org_id", "patient_id"]), "#cert-msg"); };
  $("#cert-filter").onsubmit = async (e) => { e.preventDefault(); await draw(new FormData(e.target).get("cert_type")); };
  $("#chk-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/checkups", formJson(e.target, ["patient_id", "org_id"]), "#cert-msg"); };
  $("#page-body").onclick = async (e) => {
    const { printcert } = e.target.dataset;
    if (!printcert) return;
    try { await openPrintPage(`/api/print/certs/${printcert}`); }
    catch (err) { setMsg("#cert-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
}

/* 块3：数据质控（管理员）——规则驱动扫描存量数据，看违规明细与汇总 */
/* 块1：集成平台 ESB——端点注册、消息队列（筛选/重试/错误）、流程编排、统计看板 */

const ESB_SYSTEMS = { his: "医院信息系统", lis: "检验系统", pacs: "影像系统", insurance: "医保系统", provincial: "省级平台" };
const ESB_MSG_STATUS = { queued: ["待处理", "orange"], processing: ["处理中", ""], succeeded: ["成功", "green"], failed: ["失败待重试", "orange"], dead: ["死信", "red"] };
const ESB_FLOW_SAMPLE = JSON.stringify([
  { type: "transform", config: { format: "fhir_patient", source_field: "resource" } },
  { type: "validate", config: { required: ["name", "id_card"] } },
  { type: "persist", config: { entity: "patient" } },
], null, 1);

async function renderEsb() {
  $("#page-desc").textContent = "轻量服务总线：接入方注册与限流、消息队列重试与死信、编排流程逐步执行、成功率与积压监控";
  const [stats, endpoints, flows] = await Promise.all([
    api("/api/esb/stats"), api("/api/esb/endpoints"), api("/api/esb/flows")]);
  const drawMessages = async () => {
    const f = new FormData($("#esb-msg-filter"));
    const params = new URLSearchParams({ limit: "50" });
    if (f.get("status")) params.set("status", f.get("status"));
    if (f.get("endpoint_id")) params.set("endpoint_id", f.get("endpoint_id"));
    const messages = await api(`/api/esb/messages?${params}`);
    $("#esb-messages").innerHTML = table(["ID", "接入方", "消息类型", "状态", "重试", "最后错误", "操作"], messages, (m) => {
      const retryable = m.status === "queued" || m.status === "failed";
      return `<tr><td>${m.id}</td><td><span class="tag">${esc(m.endpoint_code)}</span></td><td>${esc(m.msg_type)}</td>
        <td>${statusTag(ESB_MSG_STATUS, m.status)}</td><td>${m.retry_count}/${m.max_retries}</td>
        <td style="max-width:280px;font-size:12px;color:#b23c3c">${esc(m.last_error)}</td>
        <td>${retryable ? `<button class="btn secondary" data-esbproc="${m.id}">消费/重试</button>` : "—"}
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
        <button>注册并生成令牌</button></form>
      <p class="msg" id="esb-ep-msg"></p>
      ${table(["编码", "名称", "系统类型", "方向", "限流/分钟", "状态", "操作"], endpoints, (e) =>
        `<tr><td><span class="tag">${esc(e.code)}</span></td><td>${esc(e.name)}</td><td>${esc(e.system_type_name)}</td>
         <td>${esc(e.direction_name)}</td><td>${e.rate_limit_per_min}</td>
         <td>${e.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-esbtoggle="${e.id}" data-active="${e.active ? 1 : 0}">${e.active ? "停用" : "启用"}</button>
           <button class="btn secondary" data-esbrotate="${e.id}">轮换令牌</button></td></tr>`)}`)
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
         <td><button class="btn secondary" data-esbrun="${esc(f.code)}">对消息执行</button></td></tr>`)}`)
    + panel("接入方统计",
      table(["接入方", "总量", "成功", "死信", "积压", "成功率", "失败率"], stats.by_endpoint, (r) =>
        `<tr><td><span class="tag">${esc(r.endpoint_code)}</span> ${esc(r.endpoint_name)}</td><td>${r.total}</td>
         <td><span class="tag green">${r.succeeded}</span></td>
         <td>${r.dead ? `<span class="tag red">${r.dead}</span>` : 0}</td>
         <td>${r.backlog ? `<span class="tag orange">${r.backlog}</span>` : 0}</td>
         <td>${r.success_rate_pct}%</td><td>${r.failure_rate_pct}%</td></tr>`));
  $("#esb-msg-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawMessages(); } catch (err) { setMsg("#esb-msg", err.message, false); }
  };
  $("#esb-ep-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const created = await api("/api/esb/endpoints", { method: "POST", body: JSON.stringify({
        code: f.get("code"), name: f.get("name"), system_type: f.get("system_type"),
        direction: f.get("direction"), rate_limit_per_min: Number(f.get("rate_limit_per_min")) || 60 }) });
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
    const { esbproc, esbpayload, esbtoggle, active, esbrotate, esbrun } = e.target.dataset;
    try {
      if (esbproc) {
        const res = await api(`/api/esb/messages/${esbproc}/process`, { method: "POST" });
        setMsg("#esb-msg", `消息 ${esbproc} → ${ESB_MSG_STATUS[res.status][0]}：${res.detail || res.last_error}`, res.status === "succeeded");
        await drawMessages();
      } else if (esbpayload) {
        const rows = await api(`/api/esb/messages?limit=50`);
        const msg = rows.find((m) => String(m.id) === esbpayload);
        alert(msg ? JSON.stringify(msg.payload, null, 2) : "载荷不在当前页，请先按条件筛选");
      } else if (esbtoggle) {
        await api(`/api/esb/endpoints/${esbtoggle}`, { method: "PATCH", body: JSON.stringify({ active: active !== "1" }) });
        route();
      } else if (esbrotate) {
        const res = await api(`/api/esb/endpoints/${esbrotate}/rotate-token`, { method: "POST" });
        alert(`新令牌（旧令牌已失效）：\n${res.auth_token}`);
      } else if (esbrun) {
        const messageId = prompt("对哪条消息执行该编排？填写消息ID");
        if (!messageId) return;
        const res = await api(`/api/esb/flows/${encodeURIComponent(esbrun)}/run?message_id=${encodeURIComponent(messageId)}`, { method: "POST" });
        setMsg("#esb-flow-msg", `编排 ${esbrun} → ${res.status === "succeeded" ? "全部步骤成功" : `第 ${res.step_results.length} 步失败：${res.error}`}`, res.status === "succeeded");
        await drawMessages();
      }
    } catch (err) { setMsg("#esb-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawMessages();
}

const QC_SEVERITY = { error: ["错误", "red"], warn: ["警告", "orange"] };

async function renderDataQuality() {
  $("#page-desc").textContent = "规则引擎按启用规则扫描存量数据：必填/区间/枚举/引用/逻辑五类校验，停用规则不参与扫描";
  const [summary, rules] = await Promise.all([
    api("/api/dataquality/summary"), api("/api/dataquality/rules")]);
  const drawViolations = async (params = "?limit=200") => {
    const data = await api(`/api/dataquality/run${params}`);
    $("#qc-violations").innerHTML = `<p class="desc" style="font-size:12.5px">共 ${data.total} 条违规（错误 ${data.error_total} / 警告 ${data.warn_total}），本页展示 ${data.items.length} 条</p>` +
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
    ${panel("规则库（管理员可停用/启用与调整严重度）", `
      ${table(["编码", "名称", "类型", "被检表", "严重度", "状态", "操作"], rules, (r) => {
        return `<tr><td><span class="tag">${esc(r.code)}</span></td><td>${esc(r.name)}</td><td>${esc(r.rule_type_name)}</td>
          <td>${esc(r.target_table)}</td><td>${statusTag(QC_SEVERITY, r.severity)}</td>
          <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
          <td><button class="btn secondary" data-qctoggle="${r.id}" data-active="${r.active ? 1 : 0}">${r.active ? "停用" : "启用"}</button>
            <button class="btn secondary" data-qcsev="${r.id}" data-sev="${esc(r.severity)}">切换严重度</button></td></tr>`;
      })}`)}`;
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
    const { qctoggle, active, qcsev, sev } = e.target.dataset;
    try {
      if (qctoggle) {
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
        <select name="doc_type">${templates.map((t) => `<option value="${t.doc_type}">${esc(t.doc_type_name)}</option>`).join("")}</select>
        <input name="header_org_name" placeholder="抬头机构名（可空）" style="min-width:200px">
        <input name="footer_note" placeholder="页脚说明（可空）" style="min-width:220px">
        <label style="font-size:13px"><input type="checkbox" name="show_qr" checked> 显示验真二维码</label>
        <button>保存模板</button></form>
      <p class="msg" id="tpl-msg"></p>`)}`;
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
    await draw(qs ? `?${qs}` : "");
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.renew) {
        const dateStr = prompt("新有效期 YYYY-MM-DD"); if (!dateStr) return;
        await api(`/api/knowledge/${d.renew}`, { method: "PATCH", body: JSON.stringify({ expire_date: dateStr }) });
        route();
      }
      if (d.deact) {
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
  const [formulas, batches, expiring] = await Promise.all([
    api("/api/tcm/formulas"), api("/api/tcm/preparation-batches"),
    api("/api/tcm/preparation-batches/expiring?days=60")]);
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
        `<tr><td>${esc(b.batch_no)}</td><td>${b.formula_id}</td>
         <td><span class="tag ${b.expired ? "red" : "orange"}">${esc(b.expire_date)}</span></td><td>${esc(b.status)}</td></tr>`)}`, { accent: "#b26a00" }) : ""}
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
        `<tr><td>${b.id}</td><td>${esc(b.batch_no)}</td><td>${b.formula_id}</td><td>${b.quantity}${esc(b.unit)}</td>
         <td>${esc(b.produced_date)}</td><td><span class="tag ${b.expired ? "red" : ""}">${esc(b.expire_date)}</span></td>
         <td>${esc(b.status)}</td>
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
      ${table(["批次", "批号", "物品", "件数", "总成本", "单件成本"], stats.batches, (b) =>
        `<tr><td>${b.batch_id}</td><td>${esc(b.batch_no)}</td><td>${esc(b.item_name)}</td><td>${b.quantity}</td>
         <td>${b.total_cost}</td><td><span class="tag">${b.unit_cost}</span></td></tr>`)}`)}`);
  holder.querySelector("#cost-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/cssd/cost-items", formJson(e.target, ["batch_id", "amount"]), "#cost-msg");
  };
}

/* ⑳ 课件资源 + ㉑ 适宜技术实训（挂远程医学教育页） */
const MATERIAL_TYPES = { slide: "课件", video: "视频", doc: "文档", link: "外链" };

async function drawEduGaps() {
  const [mstats, plans] = await Promise.all([
    api("/api/education/material-stats"), api("/api/education/training-plans")]);
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
      <p class="msg" id="cm-msg"></p><div id="cm-list"></div>
      <h4 style="margin-top:10px">点播排行</h4>
      ${table(["课件ID", "标题", "类型", "点播量"], mstats.top, (m) =>
        `<tr><td>${m.id}</td><td>${esc(m.title)}</td><td>${esc(m.material_type_name)}</td>
         <td><span class="tag">${m.play_count}</span></td></tr>`)}`)}
    ${panel("㉑ 适宜技术实训（计划 → 报名 → 考核）", `
      <form class="inline" id="tp-plan-form">
        <input name="title" placeholder="实训主题" required style="min-width:180px">
        <input name="org_id" type="number" placeholder="承办机构ID" required>
        <input name="technique_id" type="number" placeholder="适宜技术ID（可空）">
        <input name="plan_date" placeholder="实训日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <input name="capacity" type="number" value="30" min="1" style="min-width:80px">
        <input name="trainer" placeholder="带教老师">
        <button>发布计划</button></form>
      <p class="msg" id="tplan-msg"></p>
      ${table(["ID", "主题", "日期", "带教", "名额", "已报", "余额", "状态", "操作"], plans, (p) =>
        `<tr><td>${p.id}</td><td>${esc(p.title)}</td><td>${esc(p.plan_date)}</td><td>${esc(p.trainer) || "—"}</td>
         <td>${p.capacity}</td><td>${p.enrolled}</td><td>${p.remaining}</td><td>${esc(p.status)}</td>
         <td><button class="btn secondary" data-enroll="${p.id}">报名</button>
             <button class="btn secondary" data-unenroll="${p.id}">退报</button>
             <button class="btn secondary" data-assess="${p.id}">录考核</button>
             <button class="btn secondary" data-roster="${p.id}">名单成绩</button></td></tr>`)}
      <div id="tp-roster"></div>`)}`);
  const drawMaterials = async (courseId) => {
    const list = await api(`/api/education/courses/${courseId}/materials`);
    holder.querySelector("#cm-list").innerHTML = table(["ID", "标题", "类型", "附件", "点播", "操作"], list, (m) =>
      `<tr><td>${m.id}</td><td>${esc(m.title)}</td><td>${esc(m.material_type_name)}</td><td>${m.attachments}</td>
       <td>${m.play_count}</td><td><button class="btn secondary" data-play="${m.id}">点播</button></td></tr>`);
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
    } catch (err) { setMsg("#cm-msg", err.message, false); }
  };
  holder.querySelector("#tp-plan-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/education/training-plans", formJson(e.target, ["org_id", "technique_id", "capacity"]), "#tplan-msg");
  };
  holder.onclick = async (e) => {
    const { play, enroll, unenroll, assess, roster } = e.target.dataset;
    try {
      if (play) { await api(`/api/education/materials/${play}/play`, { method: "POST" }); return route(); }
      if (enroll) { await api(`/api/education/training-plans/${enroll}/enroll`, { method: "POST" }); return route(); }
      if (unenroll) { await api(`/api/education/training-plans/${unenroll}/cancel-enroll`, { method: "POST" }); return route(); }
      if (assess) {
        const userId = prompt("学员用户ID"); if (!userId) return;
        const score = prompt("考核得分（0-100）"); if (score === null) return;
        return postAction(`/api/education/training-plans/${assess}/assessments`, {
          user_id: Number(userId), score: Number(score), comment: prompt("评语") || "" }, "#tplan-msg");
      }
      if (roster) {
        const [list, scores] = await Promise.all([
          api(`/api/education/training-plans/${roster}/enrollments`),
          api(`/api/education/training-plans/${roster}/assessments`)]);
        holder.querySelector("#tp-roster").innerHTML =
          `<h4>计划 ${esc(roster)} 报名名单（合格率 ${scores.pass_rate_pct}%）</h4>` +
          table(["用户ID", "账号", "姓名", "报名状态", "成绩", "是否合格"], list, (r) => {
            const s = scores.items.find((i) => i.user_id === r.user_id);
            return `<tr><td>${r.user_id}</td><td>${esc(r.username)}</td><td>${esc(r.full_name) || "—"}</td>
              <td>${esc(r.status)}</td><td>${s ? s.score : "—"}</td>
              <td>${s ? (s.passed ? '<span class="tag green">合格</span>' : '<span class="tag red">不合格</span>') : "—"}</td></tr>`;
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

async function drawImprovementTasks() {
  const [tasks, stats] = await Promise.all([
    api("/api/performance/improvements"), api("/api/performance/improvement-stats")]);
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
          <td>${esc(t.completion_note || t.measures) || "—"}</td><td>${actions}</td></tr>`;
      })}`)}`);
  holder.querySelector("#imp-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/performance/improvements", formJson(e.target, ["org_id"]), "#imp-msg");
  };
  holder.onclick = async (e) => {
    const { impprog, impdone, impok, impno } = e.target.dataset;
    try {
      if (impprog) return postAction(`/api/performance/improvements/${impprog}/progress`, { measures: prompt("整改措施") || "" }, "#imp-msg");
      if (impdone) {
        const note = prompt("整改结果说明（必填）"); if (!note) return;
        return postAction(`/api/performance/improvements/${impdone}/progress`, { complete: true, completion_note: note }, "#imp-msg");
      }
      if (impok) return postAction(`/api/performance/improvements/${impok}/verify`, { approve: true, comment: prompt("确认意见") || "" }, "#imp-msg");
      if (impno) return postAction(`/api/performance/improvements/${impno}/verify`, { approve: false, comment: prompt("退回理由") || "" }, "#imp-msg");
    } catch (err) { setMsg("#imp-msg", err.message, false); }
  };
}

/* ⑨ 上门服务调度（挂家医签约页） */
const VISIT_SERVICES = { nursing: "上门护理", doctor: "上门诊疗", rehab: "康复指导", sampling: "上门采样" };
const VISIT_STATUS = { applied: ["待派单", "orange"], dispatched: ["已派单", ""], completed: ["已完成", "green"], cancelled: ["已取消", "red"] };

async function drawHomeVisits() {
  const [orders, stats] = await Promise.all([api("/api/homevisits"), api("/api/homevisits/stats")]);
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
        const actions = o.status === "applied"
          ? `<button class="btn secondary" data-hvdis="${o.id}">派单</button>
             <button class="btn danger" data-hvcancel="${o.id}">取消</button>`
          : o.status === "dispatched"
          ? `<button class="btn secondary" data-hvdone="${o.id}">完成</button>` : "—";
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
      if (hvdis) {
        const name = prompt("上门人员姓名"); if (!name) return;
        return postAction(`/api/homevisits/${hvdis}/dispatch`, { assignee_name: name }, "#hv-msg");
      }
      if (hvdone) {
        const note = prompt("服务记录（必填）"); if (!note) return;
        return postAction(`/api/homevisits/${hvdone}/complete`, { service_note: note }, "#hv-msg");
      }
      if (hvcancel) return postAction(`/api/homevisits/${hvcancel}/cancel`, null, "#hv-msg");
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
const NURSING_LEVELS = { special: "特级护理", level1: "一级护理", level2: "二级护理", level3: "三级护理" };
