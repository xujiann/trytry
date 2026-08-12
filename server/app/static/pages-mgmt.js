/* 管理端 · 页面（三）：运营管理、财务资产、绩效基金、平台配置等。 */

const SURGERY_STATUS = { requested: ["待审批", "orange"], approved: ["已审批", ""],
  scheduled: ["已排班", ""], completed: ["已完成", "green"], cancelled: ["已取消", "red"] };
const ANESTHESIA = { general: "全麻", spinal: "椎管内", local: "局麻", nerve_block: "神经阻滞" };
const URGENCY = { elective: "择期", urgent: "限期", emergency: "急诊" };
const FOLLOWUP_STATUS = { pending: ["待随访", "orange"], done: ["已完成", "green"], cancelled: ["已取消", ""] };
const COST_TYPES = { labor: "人员经费", drug: "药品", consumable: "卫生材料",
  depreciation: "折旧", overhead: "其他运行" };
const PURCHASE_STATUS = { requested: ["待审批", "orange"], approved: ["已审批", ""],
  contracted: ["已签合同", ""], received: ["已验收", "green"], cancelled: ["已取消", "red"] };
const CONSUMABLE_STATUS = { in_stock: ["在库", "green"], used: ["已使用", ""],
  returned: ["已退回", "orange"], scrapped: ["已报废", "red"] };
const SEVERITY = { info: ["提示", ""], warning: ["警告", "orange"], error: ["拦截", "red"] };
const UNIFIED_STATUS = { pending: ["待处理", "orange"], processing: ["处理中", ""],
  done: ["已完成", "green"], cancelled: ["已取消", "red"] };

/* ---------------- 住院临床文书 ---------------- */

async function renderClinicalDocs() {
  $("#page-desc").textContent = "病程记录 / 护理记录 / 体温单 / 交接班；出院前可做文书完整性自查";
  const admissions = await api("/api/inpatient/admissions");
  const inHospital = admissions.filter((a) => a.status === "admitted");
  const current = Number(localStorage.getItem("medplat_doc_adm") || 0)
    || (inHospital[0] && inHospital[0].id) || 0;
  const [notes, nursing, vitals, completeness] = current
    ? await Promise.all([
        api(`/api/inpatient/admissions/${current}/progress-notes`),
        api(`/api/inpatient/admissions/${current}/nursing-records`),
        api(`/api/inpatient/admissions/${current}/vitals`),
        api(`/api/inpatient/admissions/${current}/document-completeness`),
      ])
    : [[], [], [], null];

  $("#page-body").innerHTML = `
    <div class="panel"><h3>选择住院记录</h3>
      <form class="inline" id="doc-pick"><select name="admission_id">${
        inHospital.map((a) => `<option value="${a.id}" ${a.id === current ? "selected" : ""}>#${a.id} 患者${a.patient_id} ${esc(a.diagnosis_name || "")}</option>`).join("")
      }</select><button>切换</button></form>
      ${completeness ? `<p class="msg ${completeness.complete ? "ok" : "err"}">${
        completeness.complete ? "文书完整" : "缺项：" + completeness.missing.join("、")}</p>` : '<p class="msg">暂无在院患者</p>'}
    </div>
    ${current ? `
    <div class="panel"><h3>病程记录（${notes.length}）</h3>
      <form class="inline" id="note-form">
        <select name="note_type">${Object.entries(NOTE_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="doctor_name" placeholder="记录医师">
        <input name="content" placeholder="病程内容" required style="min-width:320px">
        <button>书写</button></form>
      <p class="msg" id="doc-msg"></p>
      ${table(["时间", "类型", "医师", "内容"], notes, (n) =>
        `<tr><td>${esc(n.recorded_at)}</td><td>${esc(NOTE_TYPES[n.note_type] || n.note_type)}</td>
         <td>${esc(n.doctor_name)}</td><td>${esc(n.content)}</td></tr>`)}</div>
    <div class="panel"><h3>护理记录（${nursing.length}）</h3>
      <form class="inline" id="nursing-form">
        <select name="nursing_level">${Object.entries(NURSING_LEVELS).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="nurse_name" placeholder="护士">
        <input name="content" placeholder="护理内容" style="min-width:280px"><button>记录</button></form>
      ${table(["时间", "级别", "护士", "内容"], nursing, (r) =>
        `<tr><td>${esc(r.recorded_at)}</td><td>${esc(NURSING_LEVELS[r.nursing_level] || r.nursing_level)}</td>
         <td>${esc(r.nurse_name)}</td><td>${esc(r.content)}</td></tr>`)}</div>
    <div class="panel"><h3>体温单（${vitals.length}）</h3>
      <form class="inline" id="vital-form">
        <input name="measured_at" placeholder="YYYY-MM-DD HH:MM" required>
        <input name="temperature" type="number" step="0.1" placeholder="体温℃">
        <input name="pulse" type="number" placeholder="脉搏"><input name="respiration" type="number" placeholder="呼吸">
        <input name="sbp" type="number" placeholder="收缩压"><input name="dbp" type="number" placeholder="舒张压">
        <button>录入</button></form>
      ${vitals.length ? lineChart(vitals.map((v) => v.measured_at.slice(5, 10)),
        { "体温": vitals.map((v) => v.temperature || 0), "脉搏": vitals.map((v) => v.pulse || 0) },
        ["#c0392b", "#0b6e6e"]) : ""}
      ${table(["测量时刻", "体温", "脉搏", "呼吸", "血压", "记录人"], vitals, (v) =>
        `<tr><td>${esc(v.measured_at)}</td><td>${v.temperature ?? "—"}</td><td>${v.pulse ?? "—"}</td>
         <td>${v.respiration ?? "—"}</td><td>${v.sbp ?? "—"}/${v.dbp ?? "—"}</td><td>${esc(v.recorder)}</td></tr>`)}</div>
    <div class="panel"><h3>交接班</h3>
      <form class="inline" id="handover-form">
        <input name="ward_id" type="number" placeholder="病区ID" required>
        <select name="shift"><option value="day">白班</option><option value="evening">小夜</option><option value="night">大夜</option></select>
        <input name="handover_date" placeholder="YYYY-MM-DD" required>
        <input name="from_staff" placeholder="交班"><input name="to_staff" placeholder="接班">
        <input name="critical_count" type="number" placeholder="危重数">
        <input name="content" placeholder="交班内容" style="min-width:240px"><button>交班</button></form>
      <p class="desc">在院人数由系统按当前住院数据快照，不接受人工填写。</p></div>` : ""}`;

  $("#doc-pick").onsubmit = (e) => {
    e.preventDefault();
    localStorage.setItem("medplat_doc_adm", new FormData(e.target).get("admission_id"));
    route();
  };
  if (!current) return;
  $("#note-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/progress-notes`, formJson(e.target), "#doc-msg"); };
  $("#nursing-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/nursing-records`, formJson(e.target), "#doc-msg"); };
  $("#vital-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/vitals`,
      formJson(e.target, ["temperature", "pulse", "respiration", "sbp", "dbp"]), "#doc-msg"); };
  $("#handover-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/inpatient/handovers", formJson(e.target, ["ward_id", "critical_count"]), "#doc-msg"); };
}

/* ---------------- 手术麻醉 ---------------- */

async function renderSurgery() {
  $("#page-desc").textContent = "申请 → 审批（申请人不得自批）→ 手术间排班（区间重叠拦截）→ 术中记录；病案首页手术栏自动取术式";
  const [requests, rooms, schedules, stats] = await Promise.all([
    api("/api/surgery/requests"), api("/api/surgery/rooms"),
    api("/api/surgery/schedules"), api("/api/surgery/stats")]);
  const roomName = Object.fromEntries(rooms.map((r) => [r.id, r.name]));
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="panel"><h3>手术量统计</h3>${
      table(["机构", "台次", "切口构成", "麻醉构成", "并发症"], stats, (s) =>
        `<tr><td>${esc(s.org_name)}</td><td>${s.total}</td>
         <td>${Object.entries(s.by_incision).map(([k, v]) => `${k}类:${v}`).join(" ")}</td>
         <td>${Object.entries(s.by_anesthesia).map(([k, v]) => `${ANESTHESIA[k] || k}:${v}`).join(" ")}</td>
         <td>${s.complications}</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>手术间（admin 建档）</h3>
      <form class="inline" id="room-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="手术间名称" required><button>新建</button></form>
      <p class="msg" id="surg-msg"></p>
      ${table(["ID", "机构", "名称", "状态"], rooms, (r) =>
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${esc(r.name)}</td>
         <td><span class="tag ${r.active ? "green" : ""}">${r.active ? "启用" : "停用"}</span></td></tr>`)}</div>
    <div class="panel"><h3>提出手术申请（医师）</h3>
      <form class="inline" id="surg-form"><input name="admission_id" type="number" placeholder="住院ID" required>
        <input name="surgery_name" placeholder="拟施手术" required>
        <select name="incision_level"><option value="I">I类切口</option><option value="II" selected>II类切口</option>
          <option value="III">III类切口</option><option value="IV">IV类切口</option></select>
        <select name="anesthesia_type">${Object.entries(ANESTHESIA).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <select name="urgency">${Object.entries(URGENCY).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="planned_date" placeholder="拟手术日 YYYY-MM-DD"><button>提出申请</button></form></div>
    <div class="panel"><h3>手术申请（${requests.length}）</h3>${
      table(["ID", "住院", "术式", "切口", "麻醉", "急缓", "状态", "操作"], requests, (r) => {
        const [text, color] = SURGERY_STATUS[r.status] || [r.status, ""];
        let ops = "—";
        if (r.status === "requested") ops = `<button class="btn secondary" data-approve="${r.id}">审批通过</button>
          <button class="btn danger" data-reject="${r.id}">否决</button>`;
        else if (r.status === "approved") ops = `<button class="btn secondary" data-schedule="${r.id}">排班</button>`;
        else if (r.status === "scheduled") ops = `<button class="btn secondary" data-record="${r.id}">术中记录</button>`;
        else if (r.status === "completed") ops = `<button class="btn" data-view="${r.id}">查看记录</button>`;
        return `<tr><td>${r.id}</td><td>${r.admission_id}</td><td>${esc(r.surgery_name)}</td>
          <td>${esc(r.incision_level)}</td><td>${esc(ANESTHESIA[r.anesthesia_type] || "")}</td>
          <td>${esc(URGENCY[r.urgency] || "")}</td><td><span class="tag ${color}">${text}</span></td><td>${ops}</td></tr>`;
      })}</div>
    <div class="panel"><h3>手术排班表</h3>${
      table(["日期", "手术间", "时段", "术式", "术者", "麻醉", "急缓"], schedules, (s) =>
        `<tr><td>${esc(s.scheduled_date)}</td><td>${esc(s.room_name)}</td><td>${esc(s.start_time)}-${esc(s.end_time)}</td>
         <td>${esc(s.surgery_name)}</td><td>${esc(s.surgeon_name)}</td>
         <td>${esc(ANESTHESIA[s.anesthesia_type] || "")}</td><td>${esc(URGENCY[s.urgency] || "")}</td></tr>`)}</div>
    <div class="panel hidden" id="surg-detail"><h3>术中记录</h3><div id="surg-detail-body"></div></div>`;

  $("#room-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/surgery/rooms", formJson(e.target, ["org_id"]), "#surg-msg"); };
  $("#surg-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/surgery/requests", formJson(e.target, ["admission_id"]), "#surg-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.approve) await api(`/api/surgery/requests/${d.approve}/approve`,
        { method: "POST", body: JSON.stringify({ approved: true }) });
      else if (d.reject) await api(`/api/surgery/requests/${d.reject}/approve`,
        { method: "POST", body: JSON.stringify({ approved: false }) });
      else if (d.schedule) {
        const room = prompt("手术间ID"); if (!room) return;
        const date = prompt("手术日期 YYYY-MM-DD"); if (!date) return;
        const start = prompt("开始时间 HH:MM"); if (!start) return;
        const end = prompt("结束时间 HH:MM"); if (!end) return;
        await api(`/api/surgery/requests/${d.schedule}/schedule`, { method: "POST",
          body: JSON.stringify({ room_id: Number(room), scheduled_date: date, start_time: start, end_time: end }) });
      } else if (d.record) {
        const name = prompt("实际术式"); if (!name) return;
        await api(`/api/surgery/requests/${d.record}/record`, { method: "POST",
          body: JSON.stringify({ actual_surgery_name: name,
            anesthetist_name: prompt("麻醉医师") || "", findings: prompt("术中所见") || "",
            blood_loss_ml: Number(prompt("出血量 ml") || 0), outcome: "好转" }) });
      } else if (d.view) {
        const rec = await api(`/api/surgery/requests/${d.view}/record`);
        $("#surg-detail").classList.remove("hidden");
        $("#surg-detail-body").innerHTML = table(["项", "值"],
          [["实际术式", rec.actual_surgery_name], ["术者", rec.surgeon_name], ["麻醉医师", rec.anesthetist_name],
           ["麻醉方式", ANESTHESIA[rec.anesthesia_type] || rec.anesthesia_type], ["切口等级", rec.incision_level],
           ["起止", `${rec.start_at} ~ ${rec.end_at}`], ["出血量", `${rec.blood_loss_ml} ml`],
           ["术中所见", rec.findings], ["并发症", rec.complications || "无"], ["转归", rec.outcome]],
          ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`);
        return;
      } else return;
      route();
    } catch (err) { setMsg("#surg-msg", err.message, false); }
  };
}

/* ---------------- 随访中心 ---------------- */

async function renderFollowups() {
  $("#page-desc").textContent = "慢病 / 出院 / 术后 / 妇幼四类随访统一任务；出院与手术结案自动派生";
  const [pending, overdue, stats] = await Promise.all([
    api("/api/followups?status=pending"), api("/api/followups/overdue"), api("/api/followups/stats")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>随访完成情况</h3>${
      table(["类别", "待随访", "已完成", "已取消", "超期", "完成率"], stats, (s) =>
        `<tr><td>${esc(s.category_name)}</td><td>${s.pending}</td><td>${s.done}</td>
         <td>${s.cancelled}</td><td><span class="tag ${s.overdue ? "red" : "green"}">${s.overdue}</span></td>
         <td>${s.completion_rate_pct}%</td></tr>`)}
      <p class="desc">完成率分母排除已取消项——取消的任务不该拉低随访绩效。</p></div>
    <div class="panel"><h3>超期未随访（${overdue.length}）</h3>${
      table(["ID", "患者", "类别", "事项", "应随访日", "操作"], overdue, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.patient_name)}</td><td>${esc(t.category_name)}</td>
         <td>${esc(t.title)}</td><td><span class="tag red">${esc(t.due_date)}</span></td>
         <td><button class="btn secondary" data-done="${t.id}">完成随访</button></td></tr>`)}</div>
    <div class="panel"><h3>待随访任务（${pending.length}）</h3>
      <form class="inline" id="fu-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="category"><option value="chronic">慢病随访</option><option value="discharge">出院随访</option>
          <option value="surgery">术后随访</option><option value="maternal">妇幼访视</option></select>
        <input name="due_date" placeholder="应随访日 YYYY-MM-DD" required>
        <input name="assigned_to" placeholder="负责人"><button>补建任务</button></form>
      <p class="msg" id="fu-msg"></p>
      ${table(["ID", "患者", "机构", "类别", "事项", "应随访日", "操作"], pending, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.patient_name)}</td><td>${esc(t.org_name)}</td>
         <td>${esc(t.category_name)}</td><td>${esc(t.title)}</td><td>${esc(t.due_date)}</td>
         <td><button class="btn secondary" data-done="${t.id}">完成</button>
             <button class="btn danger" data-cancel="${t.id}">取消</button></td></tr>`)}</div>`;
  $("#fu-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/followups", formJson(e.target, ["patient_id", "org_id"]), "#fu-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.done) {
        const result = prompt("随访结果"); if (!result) return;
        await api(`/api/followups/${d.done}/complete`, { method: "POST", body: JSON.stringify({ result }) });
      } else if (d.cancel) await api(`/api/followups/${d.cancel}/cancel`, { method: "POST" });
      else return;
      route();
    } catch (err) { setMsg("#fu-msg", err.message, false); }
  };
}

/* ---------------- 会计核算 ---------------- */

async function renderAccounting() {
  $("#page-desc").textContent = "会计科目 + 记账凭证（借贷必平强校验）→ 过账锁定 → 试算平衡表；作废而不删除";
  const period = localStorage.getItem("medplat_acc_period") || new Date().toISOString().slice(0, 7);
  const [subjects, vouchers, balance] = await Promise.all([
    api("/api/accounting/subjects"), api(`/api/accounting/vouchers?period=${period}`),
    api(`/api/accounting/trial-balance?period=${period}`)]);
  const VS = { draft: ["草稿", "orange"], posted: ["已过账", "green"], void: ["已作废", "red"] };
  const options = subjects.map((s) => `<option value="${s.code}">${s.code} ${s.name}</option>`).join("");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>会计期间</h3>
      <form class="inline" id="acc-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM"><button>切换</button></form></div>
    <div class="panel"><h3>录入凭证</h3>
      <form id="voucher-form">
        <div class="inline"><input name="org_id" type="number" placeholder="机构ID" required>
          <input name="voucher_no" placeholder="凭证号" required>
          <input name="voucher_date" placeholder="凭证日期 YYYY-MM-DD" required>
          <input name="summary" placeholder="摘要" style="min-width:220px"></div>
        <div id="entry-rows"></div>
        <div class="inline"><button type="button" id="add-entry" class="btn secondary">加一行分录</button>
          <span id="entry-total" class="desc"></span></div>
        <button>保存凭证（草稿）</button></form>
      <p class="msg" id="acc-msg"></p></div>
    <div class="panel"><h3>${esc(period)} 凭证（${vouchers.length}）</h3>${
      table(["ID", "凭证号", "日期", "摘要", "借方", "贷方", "状态", "操作"], vouchers, (v) => {
        const [text, color] = VS[v.status] || [v.status, ""];
        const ops = v.status === "draft"
          ? `<button class="btn secondary" data-post="${v.id}">过账</button>`
          : (v.status === "posted" ? `<button class="btn danger" data-void="${v.id}">作废</button>` : "—");
        return `<tr><td>${v.id}</td><td>${esc(v.voucher_no)}</td><td>${esc(v.voucher_date)}</td>
          <td>${esc(v.summary)}</td><td>${v.total_debit.toFixed(2)}</td><td>${v.total_credit.toFixed(2)}</td>
          <td><span class="tag ${color}">${text}</span></td>
          <td><button class="btn" data-detail="${v.id}">明细</button> ${ops}</td></tr>`;
      })}</div>
    <div class="panel"><h3>试算平衡表（仅统计已过账）</h3>
      <p class="msg ${balance.balanced ? "ok" : "err"}">借方合计 ${balance.total_debit.toFixed(2)}　贷方合计 ${
        balance.total_credit.toFixed(2)}　${balance.balanced ? "平衡" : "不平衡"}</p>
      ${table(["科目", "名称", "类别", "借方", "贷方"], balance.lines, (l) =>
        `<tr><td>${esc(l.subject_code)}</td><td>${esc(l.subject_name)}</td><td>${esc(l.category)}</td>
         <td>${l.debit.toFixed(2)}</td><td>${l.credit.toFixed(2)}</td></tr>`)}</div>
    <div class="panel hidden" id="voucher-detail"><h3>凭证明细</h3><div id="voucher-detail-body"></div></div>`;

  const addEntryRow = () => {
    const row = document.createElement("div");
    row.className = "inline entry-row";
    row.innerHTML = `<select class="e-subject">${options}</select>
      <input class="e-summary" placeholder="分录摘要">
      <input class="e-debit" type="number" step="0.01" placeholder="借方">
      <input class="e-credit" type="number" step="0.01" placeholder="贷方">`;
    $("#entry-rows").appendChild(row);
    row.oninput = refreshTotal;
  };
  const refreshTotal = () => {
    let debit = 0, credit = 0;
    document.querySelectorAll(".entry-row").forEach((r) => {
      debit += Number(r.querySelector(".e-debit").value || 0);
      credit += Number(r.querySelector(".e-credit").value || 0);
    });
    const el = $("#entry-total");
    // 借贷是否相等实时提示，避免提交后才被 422 打回
    el.textContent = `借方合计 ${debit.toFixed(2)}　贷方合计 ${credit.toFixed(2)}　${
      Math.abs(debit - credit) < 0.005 ? "✓ 平衡" : "✗ 不平"}`;
    el.style.color = Math.abs(debit - credit) < 0.005 ? "#1e7e34" : "#c0392b";
  };
  addEntryRow(); addEntryRow(); refreshTotal();
  $("#add-entry").onclick = addEntryRow;
  $("#acc-period").onsubmit = (e) => { e.preventDefault();
    localStorage.setItem("medplat_acc_period", new FormData(e.target).get("period")); route(); };
  $("#voucher-form").onsubmit = async (e) => {
    e.preventDefault();
    const head = formJson(e.target, ["org_id"]);
    const entries = [...document.querySelectorAll(".entry-row")].map((r) => ({
      subject_code: r.querySelector(".e-subject").value,
      summary: r.querySelector(".e-summary").value,
      debit: Number(r.querySelector(".e-debit").value || 0),
      credit: Number(r.querySelector(".e-credit").value || 0),
    })).filter((x) => x.debit > 0 || x.credit > 0);
    postAction("/api/accounting/vouchers", { ...head, period, entries }, "#acc-msg");
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.post) await api(`/api/accounting/vouchers/${d.post}/post`, { method: "POST" });
      else if (d.void) { if (!confirm("作废该凭证？")) return;
        await api(`/api/accounting/vouchers/${d.void}/void`, { method: "POST" }); }
      else if (d.detail) {
        const v = await api(`/api/accounting/vouchers/${d.detail}`);
        $("#voucher-detail").classList.remove("hidden");
        $("#voucher-detail-body").innerHTML = table(["科目", "摘要", "借方", "贷方"], v.entries, (x) =>
          `<tr><td>${esc(x.subject_code)}</td><td>${esc(x.summary)}</td>
           <td>${x.debit.toFixed(2)}</td><td>${x.credit.toFixed(2)}</td></tr>`);
        return;
      } else return;
      route();
    } catch (err) { setMsg("#acc-msg", err.message, false); }
  };
}

/* ---------------- 成本核算 ---------------- */

async function renderCost() {
  $("#page-desc").textContent = "科室直接成本归集 → 分摊（行政/医技→临床）→ 诊次成本与床日成本（分母为实际占用床日）";
  const period = localStorage.getItem("medplat_cost_period") || new Date().toISOString().slice(0, 7);
  const orgId = Number(localStorage.getItem("medplat_cost_org") || 0);
  const [depts, costs, rules] = await Promise.all([
    api("/api/mgmt/departments"), api(`/api/cost/departments?period=${period}`),
    api("/api/cost/allocation-rules")]);
  const unit = orgId ? await api(`/api/cost/unit-cost?period=${period}&org_id=${orgId}`).catch(() => null) : null;
  const deptName = Object.fromEntries(depts.map((d) => [d.id, d.name]));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>期间与机构</h3>
      <form class="inline" id="cost-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM">
        <input name="org_id" type="number" value="${orgId || ""}" placeholder="机构ID（算单位成本）"><button>切换</button></form></div>
    ${unit ? `<div class="panel"><h3>单位成本</h3>${
      table(["总成本", "门诊人次", "占用床日", "门诊成本", "住院成本", "诊次成本", "床日成本"], [unit], (u) =>
        `<tr><td>${u.total_cost.toFixed(2)}</td><td>${u.outpatient_visits}</td><td>${u.occupied_bed_days}</td>
         <td>${u.outpatient_cost.toFixed(2)}</td><td>${u.inpatient_cost.toFixed(2)}</td>
         <td><b>${u.cost_per_visit.toFixed(2)}</b></td><td><b>${u.cost_per_bed_day.toFixed(2)}</b></td></tr>`)}
      <p class="desc">床日成本分母是实际占用床日，不是床位数×天数——后者是可用床日，混用会把成本算低。</p></div>` : ""}
    <div class="panel"><h3>归集科室直接成本</h3>
      <form class="inline" id="cost-form"><select name="dept_id">${
        depts.map((d) => `<option value="${d.id}">${esc(d.name)}（${d.category}）</option>`).join("")}</select>
        <input name="period" value="${esc(period)}" placeholder="YYYY-MM" required>
        <select name="cost_type">${Object.entries(COST_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="amount" type="number" step="0.01" placeholder="金额" required><button>归集</button></form>
      <p class="msg" id="cost-msg"></p>
      <p class="desc">同科室同期间同成本项重复提交按覆盖处理——月末成本反复调整是常态。</p></div>
    <div class="panel"><h3>分摊规则</h3>
      <form class="inline" id="alloc-form">
        <select name="from_dept_id">${depts.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("")}</select>
        →<select name="to_dept_id">${depts.map((d) => `<option value="${d.id}">${esc(d.name)}</option>`).join("")}</select>
        <input name="ratio_pct" type="number" step="0.01" placeholder="比例%" required><button>新增规则</button></form>
      ${table(["来源科室", "目标科室", "比例"], rules, (r) =>
        `<tr><td>${esc(deptName[r.from_dept_id] || r.from_dept_id)}</td>
         <td>${esc(deptName[r.to_dept_id] || r.to_dept_id)}</td><td>${r.ratio_pct}%</td></tr>`)}</div>
    <div class="panel"><h3>${esc(period)} 科室成本</h3>${
      table(["科室", "类别", "直接成本", "分摊转入", "分摊转出", "总成本", "未分摊"], costs, (c) =>
        `<tr><td>${esc(c.dept_name)}</td><td>${esc(c.dept_category)}</td><td>${c.direct_cost.toFixed(2)}</td>
         <td>${c.allocated_in.toFixed(2)}</td><td>${c.allocated_out.toFixed(2)}</td>
         <td><b>${c.total_cost.toFixed(2)}</b></td>
         <td>${c.unallocated_ratio_amount ? `<span class="tag orange">${c.unallocated_ratio_amount.toFixed(2)}</span>` : "—"}</td></tr>`)}
      ${costs.length ? barChart(costs.map((c) => [c.dept_name, c.total_cost])) : ""}</div>`;
  $("#cost-period").onsubmit = (e) => { e.preventDefault();
    const f = new FormData(e.target);
    localStorage.setItem("medplat_cost_period", f.get("period"));
    localStorage.setItem("medplat_cost_org", f.get("org_id") || "");
    route(); };
  $("#cost-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/cost/departments", formJson(e.target, ["dept_id", "amount"]), "#cost-msg"); };
  $("#alloc-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/cost/allocation-rules", formJson(e.target, ["from_dept_id", "to_dept_id", "ratio_pct"]), "#cost-msg"); };
}

/* ---------------- 物资采购与高值耗材 ---------------- */

async function renderMaterials() {
  $("#page-desc").textContent = "非药品物资：申请 → 审批 → 合同 → 验收（自动入库流水）；高值耗材一物一码正反向追溯";
  const [purchases, consumables] = await Promise.all([
    api("/api/materials/purchases"), api("/api/materials/consumables")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>提出采购申请</h3>
      <form class="inline" id="mp-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="dept_id" type="number" placeholder="科室ID">
        <input name="item_name" placeholder="物资名称" required><input name="spec" placeholder="规格">
        <input name="unit" placeholder="单位" value="件"><input name="quantity" type="number" placeholder="数量" required>
        <input name="estimated_price" type="number" step="0.01" placeholder="预估单价">
        <input name="reason" placeholder="事由"><button>提交申请</button></form>
      <p class="msg" id="mat-msg"></p></div>
    <div class="panel"><h3>采购流程（${purchases.length}）</h3>${
      table(["ID", "物资", "规格", "数量", "状态", "合同", "已验收", "操作"], purchases, (p) => {
        const [text, color] = PURCHASE_STATUS[p.status] || [p.status, ""];
        let ops = "—";
        if (p.status === "requested") ops = `<button class="btn secondary" data-approve="${p.id}">审批</button>`;
        else if (p.status === "approved") ops = `<button class="btn secondary" data-contract="${p.id}">签合同</button>`;
        else if (p.status === "contracted") ops = `<button class="btn secondary" data-receive="${p.id}">验收</button>`;
        return `<tr><td>${p.id}</td><td>${esc(p.item_name)}</td><td>${esc(p.spec)}</td><td>${p.quantity}${esc(p.unit)}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${esc(p.contract_no || "—")}</td>
          <td>${p.received_quantity || "—"}</td><td>${ops}</td></tr>`;
      })}</div>
    <div class="panel"><h3>高值耗材登记（一物一码）</h3>
      <form class="inline" id="hv-form"><input name="barcode" placeholder="条码" required>
        <input name="name" placeholder="耗材名称" required><input name="spec" placeholder="规格">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="supplier_id" type="number" placeholder="供应商ID"><input name="batch_no" placeholder="批号">
        <input name="expire_date" placeholder="效期 YYYY-MM-DD">
        <input name="unit_price" type="number" step="0.01" placeholder="单价"><button>入库登记</button></form>
      <form class="inline" id="trace-form"><input name="barcode" placeholder="按条码追溯" required><button>追溯</button></form>
      <div id="trace-result"></div></div>
    <div class="panel"><h3>耗材台账（${consumables.length}）</h3>${
      table(["条码", "名称", "批号", "效期", "状态", "用于患者", "关联手术", "操作"], consumables, (c) => {
        const [text, color] = CONSUMABLE_STATUS[c.status] || [c.status, ""];
        return `<tr><td>${esc(c.barcode)}</td><td>${esc(c.name)}</td><td>${esc(c.batch_no)}</td>
          <td>${esc(c.expire_date)}</td><td><span class="tag ${color}">${text}</span></td>
          <td>${esc(c.used_patient_name || "—")}</td><td>${esc(c.used_surgery_name || "—")}</td>
          <td>${c.status === "in_stock" ? `<button class="btn secondary" data-use="${c.barcode}">使用登记</button>` : "—"}</td></tr>`;
      })}</div>`;
  $("#mp-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/materials/purchases", formJson(e.target, ["org_id", "dept_id", "quantity", "estimated_price"]), "#mat-msg"); };
  $("#hv-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/materials/consumables", formJson(e.target, ["org_id", "supplier_id", "unit_price"]), "#mat-msg"); };
  $("#trace-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const c = await api(`/api/materials/consumables/trace/${new FormData(e.target).get("barcode")}`);
      $("#trace-result").innerHTML = table(["项", "值"],
        [["条码", c.barcode], ["名称", c.name], ["规格", c.spec], ["供应商", c.supplier_name],
         ["批号", c.batch_no], ["效期", c.expire_date], ["状态", CONSUMABLE_STATUS[c.status][0]],
         ["用于患者", c.used_patient_name || "—"], ["关联手术", c.used_surgery_name || "—"], ["使用时间", c.used_at || "—"]],
        ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`);
    } catch (err) { setMsg("#mat-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.approve) await api(`/api/materials/purchases/${d.approve}/approve`,
        { method: "POST", body: JSON.stringify({ approved: true }) });
      else if (d.contract) {
        const supplier = prompt("供应商ID"); if (!supplier) return;
        await api(`/api/materials/purchases/${d.contract}/contract`, { method: "POST",
          body: JSON.stringify({ supplier_id: Number(supplier), contract_no: prompt("合同号") || "",
            contract_amount: Number(prompt("合同金额") || 0) }) });
      } else if (d.receive) {
        const qty = prompt("验收数量"); if (!qty) return;
        await api(`/api/materials/purchases/${d.receive}/receive`, { method: "POST",
          body: JSON.stringify({ received_quantity: Number(qty), note: prompt("验收备注") || "" }) });
      } else if (d.use) {
        const pid = prompt("使用患者ID"); if (!pid) return;
        const sid = prompt("关联手术申请ID（可留空）");
        await api(`/api/materials/consumables/${d.use}/use`, { method: "POST",
          body: JSON.stringify({ patient_id: Number(pid), surgery_id: sid ? Number(sid) : null }) });
      } else return;
      route();
    } catch (err) { setMsg("#mat-msg", err.message, false); }
  };
}

/* ---------------- 决策指标扩展 ---------------- */

async function renderAnalytics() {
  $("#page-desc").textContent = "县域就诊率与就医流向 / 运行效率 / 自定义绩效公式与综合报告";
  const period = localStorage.getItem("medplat_ana_period") || new Date().toISOString().slice(0, 7);
  const [flow, eff, formulas, vars] = await Promise.all([
    api("/api/analytics/patient-flow"), api(`/api/analytics/efficiency?period=${period}`),
    api("/api/analytics/formulas"), api("/api/analytics/formula-variables")]);
  const report = currentRole() === "admin" || currentRole() === "director"
    ? await api(`/api/analytics/performance-report?period=${period}`).catch(() => null) : null;
  $("#page-body").innerHTML = `
    <div class="panel"><h3>期间</h3>
      <form class="inline" id="ana-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM"><button>切换</button></form></div>
    <div class="panel"><h3>就医流向</h3>
      <div class="cards">
        <div class="card"><span class="k">县域就诊率</span><b>${flow.county_visit_rate_pct}%</b></div>
        <div class="card"><span class="k">外转率</span><b>${flow.outbound_rate_pct}%</b></div>
        <div class="card"><span class="k">有序转诊率</span><b>${flow.ordered_referral_rate_pct}%</b></div>
        <div class="card"><span class="k">县外就诊人次</span><b>${flow.outside_visits}</b></div>
        <div class="card"><span class="k">县外费用</span><b>${flow.outside_amount.toFixed(0)}</b></div>
      </div>
      <form class="inline" id="ob-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="visit_date" placeholder="就诊日 YYYY-MM-DD" required>
        <input name="external_org_name" placeholder="县外机构名称" required>
        <select name="external_org_level"><option value="city">市级</option><option value="province">省级</option><option value="other">其他</option></select>
        <select name="visit_type"><option value="outpatient">门急诊</option><option value="inpatient">住院</option></select>
        <input name="total_amount" type="number" step="0.01" placeholder="总费用">
        <input name="insurance_pay" type="number" step="0.01" placeholder="医保支付">
        <input name="referral_id" type="number" placeholder="转诊单ID（有则为有序转诊）">
        <button>登记县外就诊</button></form>
      <p class="msg" id="ana-msg"></p></div>
    <div class="panel"><h3>运行效率（${esc(period)}）</h3>${
      table(["机构", "床位", "出院", "占用床日", "平均住院日", "床位周转", "使用率", "诊疗量", "医师", "日均担负"], eff, (r) =>
        `<tr><td>${esc(r.org_name)}</td><td>${r.beds}</td><td>${r.discharges}</td><td>${r.occupied_bed_days}</td>
         <td>${r.avg_length_of_stay}</td><td>${r.bed_turnover}</td><td>${r.bed_occupancy_rate_pct}%</td>
         <td>${r.visits}</td><td>${r.doctors}</td><td>${r.visits_per_doctor_per_day}</td></tr>`)}</div>
    <div class="panel"><h3>自定义绩效公式</h3>
      <form class="inline" id="formula-form"><input name="key" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <input name="expression" placeholder="表达式，如 round(referrals_up / encounters * 100, 2)" required style="min-width:320px">
        <input name="unit" placeholder="单位"><input name="weight" type="number" step="0.1" placeholder="权重(0=只观测)">
        <button>新增公式</button></form>
      <p class="desc">可用变量：${vars.map((v) => `<code>${esc(v.name)}</code>=${esc(v.description)}`).join("　")}</p>
      ${table(["编码", "名称", "表达式", "单位", "权重", "状态", "操作"], formulas, (f) =>
        `<tr><td>${esc(f.key)}</td><td>${esc(f.name)}</td><td><code>${esc(f.expression)}</code></td>
         <td>${esc(f.unit)}</td><td>${f.weight}</td>
         <td><span class="tag ${f.active ? "green" : ""}">${f.active ? "启用" : "停用"}</span></td>
         <td>${f.active ? `<button class="btn danger" data-off="${f.key}">停用</button>` : "—"}</td></tr>`)}</div>
    ${report ? `<div class="panel"><h3>期末综合绩效报告（${esc(period)}）</h3>${
      table(["排名", "机构", "层级", ...report.orgs[0] ? report.orgs[0].items.map((i) => i.name) : [], "加权得分"],
        report.orgs, (o, idx) =>
        `<tr><td>${idx + 1}</td><td>${esc(o.org_name)}</td><td>${esc(o.level)}</td>
         ${o.items.map((i) => `<td>${i.value === null ? `<span class="tag red" title="${esc(i.error || "")}">错误</span>` : i.value}</td>`).join("")}
         <td><b>${o.weighted_score}</b></td></tr>`)}</div>` : ""}`;
  $("#ana-period").onsubmit = (e) => { e.preventDefault();
    localStorage.setItem("medplat_ana_period", new FormData(e.target).get("period")); route(); };
  $("#ob-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/analytics/outbound-visits",
      formJson(e.target, ["patient_id", "total_amount", "insurance_pay", "referral_id"]), "#ana-msg"); };
  $("#formula-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/analytics/formulas", formJson(e.target, ["weight"]), "#ana-msg"); };
  $("#page-body").onclick = async (e) => {
    if (!e.target.dataset.off) return;
    try { await api(`/api/analytics/formulas/${e.target.dataset.off}`, { method: "DELETE" }); route(); }
    catch (err) { setMsg("#ana-msg", err.message, false); }
  };
}

/* ---------------- 统一规则引擎 ---------------- */

async function renderRules() {
  $("#page-desc").textContent = "条件 DSL 新增规则免改代码；目录并入四套既有规则（engine 列标明执行路径）";
  const [catalog, domains, rules] = await Promise.all([
    api("/api/rules/catalog"), api("/api/rules/domains"), api("/api/rules")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>全平台规则总目录（${catalog.total}）</h3>
      <div class="cards">${Object.entries(catalog.by_source).map(([k, v]) =>
        `<div class="card"><span class="k">${esc(k)}</span><b>${v}</b></div>`).join("")}</div>
      ${table(["来源", "执行引擎", "域", "编码", "名称", "定义", "状态"], catalog.entries, (e) =>
        `<tr><td>${esc(e.source)}</td>
         <td><span class="tag ${e.engine === "unified" ? "green" : ""}">${e.engine === "unified" ? "统一引擎" : "既有实现"}</span></td>
         <td>${esc(e.domain)}</td><td>${esc(e.key)}</td><td>${esc(e.name)}</td>
         <td><code>${esc(e.detail)}</code></td>
         <td>${e.active ? "启用" : "停用"}</td></tr>`)}
      <p class="desc">目录已统一，执行路径尚未统一——engine 列如实标出，不含糊其辞。</p></div>
    <div class="panel"><h3>新增统一规则（admin）</h3>
      <form class="inline" id="rule-form"><input name="key" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="domain">${domains.map((d) => `<option value="${d.domain}">${esc(d.domain)}</option>`).join("")}</select>
        <input name="condition" placeholder="条件，如 daily_dose > max_daily_dose and age >= 65" required style="min-width:340px">
        <input name="message" placeholder="命中提示">
        <select name="severity"><option value="info">提示</option><option value="warning" selected>警告</option><option value="error">拦截</option></select>
        <input name="deduct_points" type="number" placeholder="扣分"><button>新增</button></form>
      <p class="msg" id="rule-msg"></p>
      ${domains.map((d) => `<p class="desc"><b>${esc(d.domain)}</b>：${
        d.variables.map((v) => `<code>${esc(v.name)}</code>(${esc(v.type)})`).join("　")}</p>`).join("")}
      ${table(["编码", "名称", "域", "条件", "严重度", "扣分", "状态", "操作"], rules, (r) => {
        const [text, color] = SEVERITY[r.severity] || [r.severity, ""];
        return `<tr><td>${esc(r.key)}</td><td>${esc(r.name)}</td><td>${esc(r.domain)}</td>
          <td><code>${esc(r.condition)}</code></td><td><span class="tag ${color}">${text}</span></td>
          <td>${r.deduct_points}</td><td>${r.active ? "启用" : "停用"}</td>
          <td>${r.active ? `<button class="btn danger" data-off="${r.key}">停用</button>` : "—"}</td></tr>`;
      })}</div>
    <div class="panel"><h3>在线试算</h3>
      <form id="eval-form"><div class="inline">
        <select name="domain">${domains.map((d) => `<option value="${d.domain}">${esc(d.domain)}</option>`).join("")}</select>
        <input name="variables" placeholder='变量 JSON，如 {"daily_dose": 3000, "age": 78}' required style="min-width:420px">
        <button>试算</button></div></form>
      <div id="eval-result"></div></div>`;
  $("#rule-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/rules", formJson(e.target, ["deduct_points"]), "#rule-msg"); };
  $("#eval-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let variables;
    try { variables = JSON.parse(f.get("variables")); }
    catch { setMsg("#rule-msg", "变量必须是合法 JSON", false); return; }
    try {
      const r = await api("/api/rules/evaluate", { method: "POST",
        body: JSON.stringify({ domain: f.get("domain"), variables }) });
      $("#eval-result").innerHTML = `
        <p class="msg ${r.blocked ? "err" : "ok"}">求值 ${r.evaluated} 条，命中 ${r.hits.length} 条，
          合计扣分 ${r.total_deduction}${r.blocked ? "，存在拦截级命中" : ""}</p>
        ${table(["规则", "严重度", "提示", "扣分"], r.hits, (h) =>
          `<tr><td>${esc(h.name)}</td><td>${esc((SEVERITY[h.severity] || [h.severity])[0])}</td>
           <td>${esc(h.message)}</td><td>${h.deduct_points}</td></tr>`)}
        ${r.errors.length ? `<p class="msg err">求值失败的规则：${
          r.errors.map((x) => `${esc(x.key)}（${esc(x.error)}）`).join("、")}</p>` : ""}`;
    } catch (err) { setMsg("#rule-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    if (!e.target.dataset.off) return;
    try { await api(`/api/rules/${e.target.dataset.off}`, { method: "DELETE" }); route(); }
    catch (err) { setMsg("#rule-msg", err.message, false); }
  };
}

/* ---------------- 流程引擎与统一申请单中心 ---------------- */

async function renderWorkflows() {
  $("#page-desc").textContent = "流程定义 JSON 化、节点角色守卫、流转留痕；我的待办按角色过滤";
  const [definitions, instances, tasks] = await Promise.all([
    api("/api/workflows/definitions"), api("/api/workflows/instances?status=running"),
    api("/api/workflows/my-tasks")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>我的待办（${tasks.count}）</h3>${
      table(["实例", "流程", "事项", "当前节点", "需要角色", "操作"], tasks.tasks, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.definition_key)}</td><td>${esc(t.title)}</td>
         <td>${esc(t.current_node_name || t.current_node)}</td><td>${esc(t.current_node_role || "任意")}</td>
         <td><button class="btn secondary" data-advance="${t.id}">推进</button>
             <button class="btn danger" data-cancel="${t.id}">终止</button></td></tr>`)}
      <p class="msg" id="wf-msg"></p></div>
    <div class="panel"><h3>流程定义（admin）</h3>
      <form class="inline" id="def-form"><input name="key" placeholder="流程编码" required>
        <input name="name" placeholder="流程名称" required>
        <input name="nodes" placeholder='节点 JSON，如 [{"key":"apply","name":"申请","role":"doctor","next":"approve"},{"key":"approve","name":"审批","role":"director","next":""}]'
          required style="min-width:420px"><button>新建定义</button></form>
      ${table(["编码", "名称", "节点链", "状态"], definitions, (d) =>
        `<tr><td>${esc(d.key)}</td><td>${esc(d.name)}</td>
         <td>${d.nodes.map((n) => `${esc(n.name)}${n.role ? `(${esc(n.role)})` : ""}`).join(" → ")}</td>
         <td>${d.active ? "启用" : "停用"}</td></tr>`)}</div>
    <div class="panel"><h3>发起流程</h3>
      <form class="inline" id="inst-form">
        <select name="definition_key">${definitions.map((d) => `<option value="${d.key}">${esc(d.name)}</option>`).join("")}</select>
        <input name="business_type" placeholder="业务类型" required><input name="business_id" type="number" placeholder="业务ID">
        <input name="title" placeholder="事项标题" style="min-width:220px">
        <input name="org_id" type="number" placeholder="机构ID"><button>发起</button></form></div>
    <div class="panel"><h3>流转中实例（${instances.length}）</h3>${
      table(["ID", "流程", "事项", "当前节点", "更新时间", "操作"], instances, (i) =>
        `<tr><td>${i.id}</td><td>${esc(i.definition_key)}</td><td>${esc(i.title)}</td>
         <td>${esc(i.current_node_name || i.current_node)}</td><td>${esc(i.updated_at.slice(0, 16).replace("T", " "))}</td>
         <td><button class="btn" data-history="${i.id}">流转记录</button></td></tr>`)}</div>
    <div class="panel hidden" id="wf-history"><h3>流转记录</h3><div id="wf-history-body"></div></div>`;
  $("#def-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    let nodes;
    try { nodes = JSON.parse(f.get("nodes")); }
    catch { setMsg("#wf-msg", "节点必须是合法 JSON 数组", false); return; }
    postAction("/api/workflows/definitions", { key: f.get("key"), name: f.get("name"), nodes }, "#wf-msg");
  };
  $("#inst-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/workflows/instances", formJson(e.target, ["business_id", "org_id"]), "#wf-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.advance) await api(`/api/workflows/instances/${d.advance}/advance`, { method: "POST",
        body: JSON.stringify({ comment: prompt("处理意见") || "" }) });
      else if (d.cancel) { if (!confirm("终止该流程？")) return;
        await api(`/api/workflows/instances/${d.cancel}/cancel`, { method: "POST",
          body: JSON.stringify({ comment: prompt("终止原因") || "" }) }); }
      else if (d.history) {
        const rows = await api(`/api/workflows/instances/${d.history}/history`);
        $("#wf-history").classList.remove("hidden");
        $("#wf-history-body").innerHTML = table(["从", "到", "动作", "意见", "操作人", "时间"], rows, (h) =>
          `<tr><td>${esc(h.from_node)}</td><td>${esc(h.to_node || "终态")}</td><td>${esc(h.action)}</td>
           <td>${esc(h.comment)}</td><td>${esc(h.actor)}</td><td>${esc(h.created_at.slice(0, 16).replace("T", " "))}</td></tr>`);
        return;
      } else return;
      route();
    } catch (err) { setMsg("#wf-msg", err.message, false); }
  };
}

async function renderServiceRequests() {
  $("#page-desc").textContent = "预约 / 检查 / 会诊 / 用血 / 手术五类单据聚合视图，状态映射到统一口径";
  const pid = localStorage.getItem("medplat_sr_patient") || "";
  const data = await api(`/api/service-requests${pid ? `?patient_id=${pid}` : ""}`);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>筛选</h3>
      <form class="inline" id="sr-form"><input name="patient_id" type="number" value="${esc(pid)}" placeholder="患者ID（留空看全部）">
        <button>查询</button></form>
      <div class="cards">
        ${Object.entries(data.by_status).map(([k, v]) =>
          `<div class="card"><span class="k">${esc((UNIFIED_STATUS[k] || [k])[0])}</span><b>${v}</b></div>`).join("")}
        ${Object.entries(data.by_type).map(([k, v]) =>
          `<div class="card"><span class="k">${esc(k)}</span><b>${v}</b></div>`).join("")}
      </div>
      <p class="desc">刻意不建第六张单据表：五类单据各有必要的领域字段与状态机，这里做的是聚合视图。</p></div>
    <div class="panel"><h3>在办事项（${data.total}）</h3>${
      table(["类型", "单号", "患者", "机构", "事项", "统一状态", "原生状态", "时间"], data.items, (i) => {
        const [text, color] = UNIFIED_STATUS[i.status] || [i.status, ""];
        return `<tr><td>${esc(i.request_type_name)}</td><td>${i.id}</td><td>${esc(i.patient_name)}</td>
          <td>${esc(i.org_name)}</td><td>${esc(i.title)}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${esc(i.raw_status)}</td>
          <td>${esc(i.created_at.slice(0, 16).replace("T", " "))}</td></tr>`;
      })}</div>`;
  $("#sr-form").onsubmit = (e) => { e.preventDefault();
    localStorage.setItem("medplat_sr_patient", new FormData(e.target).get("patient_id") || ""); route(); };
}

/* ---------------- 定时任务 ---------------- */

async function renderJobs() {
  $("#page-desc").textContent = "任务注册表与执行留痕；多实例下靠 Redis 抢锁保证只跑一次";
  const [jobs, runs] = await Promise.all([api("/api/jobs"), api("/api/jobs/runs")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>任务清单（${jobs.length}）</h3>${
      table(["任务", "说明", "间隔", "启停", "上次执行", "上次结果", "下次到期", "操作"], jobs, (j) =>
        `<tr><td><code>${esc(j.name)}</code></td><td>${esc(j.title)}</td>
         <td>${Math.round(j.interval_seconds / 60)} 分钟</td>
         <td><span class="tag ${j.enabled ? "green" : "red"}">${j.enabled ? "启用" : "停用"}</span></td>
         <td>${esc((j.last_run_at || "—").slice(0, 16).replace("T", " "))}</td>
         <td>${j.last_status ? `<span class="tag ${j.last_status === "succeeded" ? "green" : "red"}">${
           j.last_status === "succeeded" ? "成功" : "失败"}</span>` : "—"}</td>
         <td>${esc((j.next_run_at || "—").slice(0, 16).replace("T", " "))}</td>
         <td><button class="btn secondary" data-run="${j.name}">立即执行</button>
             <button class="btn" data-toggle="${j.name}" data-enabled="${j.enabled}">${j.enabled ? "停用" : "启用"}</button>
             <button class="btn" data-interval="${j.name}">改间隔</button>
             ${j.implemented ? "" : '<span class="tag red">无实现</span>'}</td></tr>`)}
      <p class="msg" id="job-msg"></p></div>
    <div class="panel"><h3>执行历史（最近 ${runs.length} 条）</h3>${
      table(["时间", "任务", "触发", "结果", "处理数", "耗时", "摘要"], runs, (r) =>
        `<tr><td>${esc(r.created_at.slice(0, 19).replace("T", " "))}</td><td><code>${esc(r.job_name)}</code></td>
         <td>${r.trigger === "manual" ? "人工" : "计划"}</td>
         <td><span class="tag ${r.status === "succeeded" ? "green" : "red"}">${
           r.status === "succeeded" ? "成功" : "失败"}</span></td>
         <td>${r.affected}</td><td>${r.duration_ms} ms</td><td>${esc(r.message)}</td></tr>`)}</div>`;
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.run) await api(`/api/jobs/${d.run}/run`, { method: "POST" });
      else if (d.toggle) await api(`/api/jobs/${d.toggle}`, { method: "PATCH",
        body: JSON.stringify({ enabled: d.enabled !== "true" }) });
      else if (d.interval) {
        const minutes = prompt("执行间隔（分钟，最小 1）"); if (!minutes) return;
        await api(`/api/jobs/${d.interval}`, { method: "PATCH",
          body: JSON.stringify({ interval_seconds: Number(minutes) * 60 }) });
      } else return;
      route();
    } catch (err) { setMsg("#job-msg", err.message, false); }
  };
}

/* ---------------- 满意度分析 ---------------- */

const SURVEY_TARGETS = { contract: "家医签约服务", encounter: "就诊体验", consultation: "远程会诊" };

async function renderSurveys() {
  $("#page-desc").textContent = "满意度均分、分值分布与差评清单；只看均分会把个别差评稀释掉";
  const [stats, recent, negative] = await Promise.all([
    api("/api/surveys/stats"), api("/api/surveys?limit=50"), api("/api/surveys?max_score=2&limit=50")]);
  const totalCount = stats.reduce((s, x) => s + x.count, 0);
  const totalNegative = stats.reduce((s, x) => s + x.negative, 0);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>总览</h3>
      <div class="cards">
        <div class="card"><span class="k">评价总数</span><b>${totalCount}</b></div>
        <div class="card"><span class="k">差评（≤2分）</span><b>${totalNegative}</b></div>
        <div class="card"><span class="k">差评率</span><b>${
          totalCount ? (totalNegative * 100 / totalCount).toFixed(2) : 0}%</b></div>
      </div>
      ${table(["评价对象", "评价数", "均分", "1分", "2分", "3分", "4分", "5分", "差评数", "差评率"], stats, (s) =>
        `<tr><td>${esc(SURVEY_TARGETS[s.target_type] || s.target_type)}</td><td>${s.count}</td>
         <td><b>${s.avg_score}</b></td>
         ${[1, 2, 3, 4, 5].map((n) => `<td>${s.distribution[String(n)]}</td>`).join("")}
         <td><span class="tag ${s.negative ? "red" : "green"}">${s.negative}</span></td>
         <td>${s.negative_rate_pct}%</td></tr>`)}
      ${stats.length ? barChart(stats.map((s) => [SURVEY_TARGETS[s.target_type] || s.target_type, s.avg_score])) : ""}</div>
    <div class="panel"><h3>差评清单（${negative.length}）</h3>
      <p class="desc">带评语的差评最有改进价值——这是投诉发生前唯一的信号。</p>
      ${table(["日期", "对象", "患者", "评分", "评语"], negative, (s) =>
        `<tr><td>${esc(s.date)}</td><td>${esc(SURVEY_TARGETS[s.target_type] || s.target_type)}</td>
         <td>${esc(s.patient_name)}</td><td><span class="tag red">${s.score}</span></td>
         <td>${esc(s.comment || "（未填写）")}</td></tr>`)}</div>
    <div class="panel"><h3>代录评价（经办）</h3>
      <form class="inline" id="survey-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="target_type">${Object.entries(SURVEY_TARGETS).map(([k, v]) =>
          `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="target_id" type="number" placeholder="对象ID" required>
        <input name="score" type="number" min="1" max="5" placeholder="评分1-5" required>
        <input name="comment" placeholder="评语" style="min-width:240px"><button>提交</button></form>
      <p class="msg" id="survey-msg"></p>
      <p class="desc">居民本人评价走居民端 /m，此处仅供窗口代录。</p></div>
    <div class="panel"><h3>最近评价（${recent.length}）</h3>${
      table(["日期", "对象", "患者", "评分", "评语"], recent, (s) =>
        `<tr><td>${esc(s.date)}</td><td>${esc(SURVEY_TARGETS[s.target_type] || s.target_type)}</td>
         <td>${esc(s.patient_name)}</td>
         <td><span class="tag ${s.score >= 4 ? "green" : s.score <= 2 ? "red" : "orange"}">${s.score}</span></td>
         <td>${esc(s.comment || "—")}</td></tr>`)}</div>`;
  $("#survey-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/surveys", formJson(e.target, ["patient_id", "target_id", "score"]), "#survey-msg"); };
}

/* ---------------- 站内消息 ---------------- */

const NOTIFY_CATEGORIES = {
  critical_value: "危急值",
  exam_report: "检查报告",
  surgery: "手术安排",
  followup: "随访提醒",
};

/* 消息落到对应业务页面即可，不做深链定位——列表页自己带筛选，
   再造一套 URL 深链只会多一份要同步维护的约定。 */
const NOTIFY_LINK_PAGE = {
  exam_report: "exams",
  surgery: "surgery",
  admission: "inpatient",
};

let notifyUnreadOnly = false;

async function renderNotifications() {
  $("#page-desc").textContent =
    "站内消息解决「人不在线也不丢」；求快仍看铃铛与实时广播，此处是可追溯的留存";
  const rows = await api(`/api/notifications?limit=100${notifyUnreadOnly ? "&unread_only=true" : ""}`);
  const { unread } = await api("/api/notifications/unread-count");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>我的消息</h3>
      <div class="cards">
        <div class="card"><span class="k">未读</span><b>${unread}</b></div>
        <div class="card"><span class="k">本页条数</span><b>${rows.length}</b></div>
      </div>
      <form class="inline"><button type="button" id="nt-readall">全部标记已读</button>
        <button type="button" id="nt-toggle">${notifyUnreadOnly ? "查看全部" : "只看未读"}</button></form>
      <p class="msg" id="nt-msg"></p>
      ${table(["时间", "分类", "标题", "内容", "状态", "操作"], rows, (n) =>
        `<tr><td>${esc(n.created_at.slice(0, 16).replace("T", " "))}</td>
         <td>${esc(NOTIFY_CATEGORIES[n.category] || n.category)}</td>
         <td><b>${esc(n.title)}</b></td><td>${esc(n.body || "—")}</td>
         <td><span class="tag ${n.read ? "green" : "orange"}">${n.read ? "已读" : "未读"}</span></td>
         <td>${n.read ? "" : `<button data-ntread="${n.id}">标记已读</button>`}
             ${NOTIFY_LINK_PAGE[n.link_type]
               ? `<button data-ntgo="${NOTIFY_LINK_PAGE[n.link_type]}">前往处理</button>` : ""}</td></tr>`)}
    </div>`;
  $("#nt-toggle").onclick = () => { notifyUnreadOnly = !notifyUnreadOnly; route(); };
  $("#nt-readall").onclick = async () => {
    await postAction("/api/notifications/read-all", null, "#nt-msg");
    pollTodos();
  };
  $("#page-body").onclick = async (e) => {
    const { ntread, ntgo } = e.target.dataset;
    if (ntgo) return nav(ntgo);
    if (!ntread) return;
    await postAction(`/api/notifications/${ntread}/read`, null, "#nt-msg");
    pollTodos();
  };
}

/* ---------------- 医疗质量指标与用药结构（指南 #14/#48/#49/#52） ---------------- */

async function renderClinicalIndicators() {
  $("#page-desc").textContent =
    "分子分母与口径随指标一起给出——只看一个百分比既没法核对，也看不出样本量小到不该看";
  const period = new Date().toISOString().slice(0, 7);
  const [quality, drug] = await Promise.all([
    api("/api/quality/clinical-indicators"),
    api(`/api/analytics/drug-use?period=${period}`),
  ]);
  const byDimension = {};
  quality.indicators.forEach((i) => (byDimension[i.dimension] ||= []).push(i));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>医疗质量指标（${esc(quality.period)}）</h3>
      ${Object.entries(byDimension).map(([dim, items]) => `<h4>${esc(dim)}</h4>${
        table(["指标", "分子", "分母", "比率", "口径"], items, (i) =>
          `<tr><td><b>${esc(i.name)}</b></td><td>${i.numerator}</td>
           <td>${i.denominator || "—"}${i.uncollected
             ? `<span class="tag orange">未采集 ${i.uncollected}</span>` : ""}</td>
           <td>${i.denominator ? `${i.rate_pct}%` : "样本为空"}</td>
           <td class="muted">${esc(i.caliber)}</td></tr>`)}`).join("")}
    </div>
    <div class="panel"><h3>用药结构（${esc(drug.period)}）</h3>
      <p class="desc">${esc(drug.caliber.drug_ratio)}；${esc(drug.caliber.antibiotic_intensity)}</p>
      ${(drug.warnings || []).map((w) => `<p class="msg err">${esc(w)}</p>`).join("")}
      ${table(["机构", "住院药占比", "门诊药占比", "抗菌药 DDDs", "收治人天", "使用强度", "未维护DDD"],
        drug.orgs, (o) =>
          `<tr><td>${esc(o.org_name)}</td>
           <td>${o.inpatient_drug_ratio_pct}%</td><td>${o.outpatient_drug_ratio_pct}%</td>
           <td>${o.antibiotic_ddds}</td><td>${o.bed_days}</td>
           <td>${o.intensity_unstable
             ? `<span class="muted">${o.antibiotic_intensity}</span><span class="tag orange">样本不足</span>`
             : `<b>${o.antibiotic_intensity}</b>`}</td>
           <td>${o.ddd_uncovered_items
             ? `<span class="tag red">${o.ddd_uncovered_items}</span>` : "—"}</td></tr>`)}
    </div>
    ${barChart(drug.orgs.filter((o) => o.antibiotic_intensity > 0 && !o.intensity_unstable)
      .map((o) => [o.org_name, o.antibiotic_intensity]), { unit: " DDDs/百人天" })}`;
}

/* ---------------- 运行监控（浙#47 / #46） ---------------- */

async function renderMonitor() {
  $("#page-desc").textContent =
    "调用统计是本实例进程内的数据，进程重启即清零；审计统计才是跨实例可追溯的";
  const [ov, stats, nodes, audit] = await Promise.all([
    api("/api/monitor/overview"), api("/api/monitor/api-stats"),
    api("/api/monitor/nodes"), api("/api/audit/stats?days=30"),
  ]);
  const dot = (ok) => `<span class="tag ${ok ? "green" : "red"}">${ok ? "正常" : "异常"}</span>`;
  $("#page-body").innerHTML = `
    <div class="panel"><h3>运行环境</h3>
      <div class="cards">
        <div class="card"><span class="k">实例</span><b>${esc(ov.instance_id)}</b></div>
        <div class="card"><span class="k">运行时长</span><b>${
          Math.floor(ov.uptime_seconds / 3600)}小时${Math.floor(ov.uptime_seconds % 3600 / 60)}分</b></div>
        <div class="card"><span class="k">环境</span><b>${esc(ov.environment)}</b></div>
        <div class="card"><span class="k">数据库</span><b>${dot(ov.database.connected)} ${
          esc(ov.database.dialect)} ${ov.database.latency_ms ?? "—"}ms</b></div>
        <div class="card"><span class="k">Redis</span><b>${
          ov.redis.configured ? dot(ov.redis.connected) : '<span class="tag orange">未配置</span>'}</b></div>
      </div>
      ${ov.redis.note ? `<p class="msg err">${esc(ov.redis.note)}</p>` : ""}
    </div>

    <div class="panel"><h3>集群节点</h3>
      ${nodes.instances
        ? table(["实例", "本机", "运行时长(秒)"], nodes.instances, (n) =>
            `<tr><td>${esc(n.instance_id)}</td><td>${n.self ? "是" : ""}</td>
             <td>${n.uptime_seconds ?? "—"}</td></tr>`)
        : `<p class="msg err">${esc(nodes.note)}</p>`}
    </div>

    <div class="panel"><h3>接口调用（${esc(stats.scope)}）</h3>
      <div class="cards">
        <div class="card"><span class="k">请求总数</span><b>${stats.total_requests}</b></div>
        <div class="card"><span class="k">平均耗时</span><b>${stats.avg_duration_ms}ms</b></div>
        ${Object.entries(stats.by_status_class).map(([k, v]) =>
          `<div class="card"><span class="k">${esc(k)}</span><b>${v}</b></div>`).join("")}
      </div>
      ${table(["模块", "调用数", "平均耗时"], stats.top_modules, (m) =>
        `<tr><td>${esc(m.module)}</td><td>${m.count}</td>
         <td>${m.avg_duration_ms > stats.slow_threshold_ms
           ? `<span class="tag red">${m.avg_duration_ms}ms</span>` : `${m.avg_duration_ms}ms`}</td></tr>`)}
      <h4>慢请求样本（≥${stats.slow_threshold_ms}ms，最近 ${stats.slow_samples.length} 条）</h4>
      ${table(["时间", "方法", "路径", "耗时"], stats.slow_samples, (r) =>
        `<tr><td>${esc(r.at)}</td><td>${esc(r.method)}</td><td>${esc(r.path)}</td>
         <td>${r.duration_ms}ms</td></tr>`)}
      <h4>错误样本（最近 ${stats.error_samples.length} 条）</h4>
      ${table(["时间", "方法", "路径", "状态"], stats.error_samples, (r) =>
        `<tr><td>${esc(r.at)}</td><td>${esc(r.method)}</td><td>${esc(r.path)}</td>
         <td><span class="tag ${r.status >= 500 ? "red" : "orange"}">${r.status}</span></td></tr>`)}
    </div>

    <div class="panel"><h3>审计统计（近 ${audit.days} 天 · ${esc(audit.scope)}）</h3>
      <div class="cards">
        <div class="card"><span class="k">写操作</span><b>${audit.total}</b></div>
        <div class="card"><span class="k">失败</span><b>${audit.failed}</b></div>
        <div class="card"><span class="k">失败率</span><b>${audit.failed_ratio_pct}%</b></div>
      </div>
      ${audit.daily.length
        ? lineChart(audit.daily.map((d) => d.date.slice(5)),
            [audit.daily.map((d) => d.ok), audit.daily.map((d) => d.failed)],
            ["#0b6e6e", "#c0392b"])
        : '<p class="desc">暂无审计数据</p>'}
      <div class="two-col">
        <div><h4>高频操作</h4>${table(["路径", "次数"], audit.top_paths, (r) =>
          `<tr><td>${esc(r.key)}</td><td>${r.count}</td></tr>`)}</div>
        <div><h4>失败最多的操作</h4>${table(["路径", "次数"], audit.top_failed_paths, (r) =>
          `<tr><td>${esc(r.key)}</td><td>${r.count}</td></tr>`)}</div>
      </div>
      <h4>操作最多的用户</h4>
      ${table(["用户", "次数"], audit.top_users, (r) =>
        `<tr><td>${esc(r.key)}</td><td>${r.count}</td></tr>`)}
    </div>`;
}

/* ---------------- 就诊凭据（浙#27） ---------------- */

async function renderCredentials() {
  $("#page-desc").textContent =
    "凭据是介质（卡会丢、码会过期），电子健康卡号才是身份——换卡不换号";
  const rows = await api("/api/credentials?limit=100");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>发放凭据</h3>
      <form id="cred-form" class="inline">
        <input name="patient_id" placeholder="患者ID" required>
        <select name="credential_type">
          <option value="card">实体就诊卡</option>
          <option value="qrcode">电子二维码</option>
          <option value="temp">临时凭据</option>
        </select>
        <input name="credential_no" placeholder="凭据号（留空自动生成）">
        <input name="reason" placeholder="换发原因（如原卡遗失）">
        <button>发放</button>
      </form>
      <p class="desc">该患者原有的有效凭据会自动作废——挂失换发后旧卡必须立刻失效。</p>
      <p class="msg" id="cred-msg"></p>
    </div>
    <div class="panel"><h3>凭据核验</h3>
      <form id="cred-lookup" class="inline">
        <input name="credential_no" placeholder="扫码或输入凭据号" required>
        <button>核验</button>
      </form>
      <div id="cred-result"></div>
    </div>
    <div class="panel"><h3>凭据台账</h3>
      ${table(["凭据号", "患者ID", "类型", "状态", "发放时间", "结束原因", "操作"], rows, (c) =>
        `<tr><td>${esc(c.credential_no)}</td><td>${c.patient_id}</td>
         <td>${esc(c.credential_type_name)}</td>
         <td><span class="tag ${c.status === "active" ? "green" : c.status === "recycled" ? "" : "red"}">${
           esc(c.status_name)}</span></td>
         <td>${esc(c.issued_at.slice(0, 16).replace("T", " "))}</td>
         <td>${esc(c.close_reason || "—")}</td>
         <td>${c.status === "active"
           ? `<button data-crec="${c.id}">回收</button><button data-cvoid="${c.id}">作废</button>` : ""}</td></tr>`)}
    </div>`;
  $("#cred-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/credentials", formJson(e.target, ["patient_id"]), "#cred-msg"); };
  $("#cred-lookup").onsubmit = async (e) => {
    e.preventDefault();
    const no = e.target.credential_no.value.trim();
    try {
      const c = await api(`/api/credentials/lookup/${encodeURIComponent(no)}`);
      $("#cred-result").innerHTML = `<div class="cards">
        <div class="card"><span class="k">持有人</span><b>${esc(c.patient?.name || "—")}</b></div>
        <div class="card"><span class="k">健康卡号</span><b>${esc(c.patient?.ehc_no || "—")}</b></div>
        <div class="card"><span class="k">是否可用</span><b><span class="tag ${
          c.valid ? "green" : "red"}">${c.valid ? "有效" : esc(c.status_name)}</span></b></div>
        ${c.close_reason ? `<div class="card"><span class="k">失效原因</span><b>${esc(c.close_reason)}</b></div>` : ""}
      </div>`;
    } catch (err) {
      // 查无此卡与卡已作废是两回事，前者才是 404
      $("#cred-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
    }
  };
  $("#page-body").onclick = (e) => {
    const { crec, cvoid } = e.target.dataset;
    if (crec) return postAction(`/api/credentials/${crec}/recycle`, {}, "#cred-msg");
    if (cvoid) {
      const reason = prompt("作废原因（必填，如挂失/损坏）");
      if (!reason) return;
      return postAction(`/api/credentials/${cvoid}/void`, { reason }, "#cred-msg");
    }
  };
}

/* ---------------- 门急诊文书（浙#3） ---------------- */

const CONSENT_TYPES = {
  surgery: "手术", anesthesia: "麻醉", transfusion: "输血",
  exam: "特殊检查", treatment: "特殊治疗", other: "其他",
};

async function renderOutpatientDocs() {
  $("#page-desc").textContent =
    "知情告知书签署时冻结正文——模板日后修订不会改动已签的那一份；拒签是独立状态，不是「没签」";
  const encounterId = Number(localStorage.getItem("medplat_od_encounter") || 0);
  const [templates, consents] = await Promise.all([
    api("/api/outpatient/consent-templates?active=true"),
    api("/api/outpatient/consents?limit=50"),
  ]);
  let scoped = { treatments: [], nursing: [], completeness: null };
  if (encounterId) {
    scoped = {
      treatments: await api(`/api/outpatient/encounters/${encounterId}/treatments`),
      nursing: await api(`/api/outpatient/encounters/${encounterId}/nursing-records`),
      completeness: await api(`/api/outpatient/encounters/${encounterId}/completeness`),
    };
  }
  $("#page-body").innerHTML = `
    <div class="panel"><h3>选择就诊</h3>
      <form class="inline" id="od-pick">
        <input name="encounter_id" type="number" placeholder="就诊ID" value="${encounterId || ""}" required>
        <button>载入该次就诊的文书</button>
      </form>
      ${scoped.completeness ? `<div class="cards">
        <div class="card"><span class="k">处置记录</span><b>${scoped.completeness.treatment_records}</b></div>
        <div class="card"><span class="k">护理记录</span><b>${scoped.completeness.nursing_records}</b></div>
        <div class="card"><span class="k">告知书</span><b>${scoped.completeness.consents_total}</b></div>
        <div class="card"><span class="k">待签署</span><b>${
          scoped.completeness.consents_pending
            ? `<span class="tag orange">${scoped.completeness.consents_pending}</span>`
            : 0}</b></div>
      </div><p class="desc">${esc(scoped.completeness.note)}</p>` : ""}
    </div>

    ${encounterId ? `
    <div class="panel"><h3>治疗处置记录</h3>
      <form class="inline" id="od-treat">
        <input name="treatment_name" placeholder="处置名称（如雾化吸入）" required>
        <input name="site" placeholder="部位"><input name="dose" placeholder="剂量/参数">
        <input name="executor_name" placeholder="执行人">
        <input name="reaction" placeholder="反应（留空=未记录，不等于无不适）">
        <button>记录</button>
      </form>
      <p class="msg" id="od-msg"></p>
      ${table(["处置", "部位", "剂量", "执行人", "反应", "时间"], scoped.treatments, (t) =>
        `<tr><td>${esc(t.treatment_name)}</td><td>${esc(t.site || "—")}</td>
         <td>${esc(t.dose || "—")}</td><td>${esc(t.executor_name || "—")}</td>
         <td>${t.reaction ? esc(t.reaction) : '<span class="tag orange">未记录</span>'}</td>
         <td>${esc(t.created_at.slice(0, 16).replace("T", " "))}</td></tr>`)}
    </div>

    <div class="panel"><h3>门急诊护理记录</h3>
      <form class="inline" id="od-nurse">
        <select name="nursing_level"><option value="level3">三级护理</option>
          <option value="level2">二级护理</option><option value="level1">一级护理</option>
          <option value="special">特级护理</option></select>
        <input name="content" placeholder="观察内容（如输液中滴速40滴/分）" required style="min-width:280px">
        <input name="nurse_name" placeholder="护士">
        <button>记录</button>
      </form>
      ${table(["级别", "内容", "护士", "时间"], scoped.nursing, (n) =>
        `<tr><td>${esc(NURSING_LEVELS[n.nursing_level] || n.nursing_level)}</td>
         <td>${esc(n.content)}</td><td>${esc(n.nurse_name || "—")}</td>
         <td>${esc(n.recorded_at || "—")}</td></tr>`)}
    </div>` : ""}

    <div class="panel"><h3>开具知情告知书</h3>
      <form class="inline" id="od-consent">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="consent_type">${Object.entries(CONSENT_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="template_id"><option value="">不用模板（自带正文）</option>
          ${templates.map((t) => `<option value="${t.id}">${esc(t.title)} ${esc(t.version)}</option>`).join("")}</select>
        <input name="title" placeholder="标题（不用模板时必填）">
        <input name="content" placeholder="正文（不用模板时必填）" style="min-width:260px">
        <button>生成待签</button>
      </form>
      <p class="msg" id="od-cmsg"></p>
      ${table(["患者", "类型", "标题", "状态", "签署人", "时间", "操作"], consents, (c) =>
        `<tr><td>${c.patient_id}</td><td>${esc(c.consent_type_name)}</td>
         <td>${esc(c.title)}${c.template_version ? `<span class="tag">${esc(c.template_version)}</span>` : ""}</td>
         <td><span class="tag ${c.status === "signed" ? "green" : c.status === "refused" ? "red" : "orange"}">${
           esc(c.status_name)}</span>${c.refuse_reason ? `<br><small>${esc(c.refuse_reason)}</small>` : ""}</td>
         <td>${esc(c.signer_name || "—")}${c.signer_name ? `（${esc(c.signer_relation_name)}）` : ""}</td>
         <td>${esc((c.signed_at || c.created_at).slice(0, 16).replace("T", " "))}</td>
         <td>${c.status === "pending"
           ? `<button data-csign="${c.id}">签署</button><button data-crefuse="${c.id}">拒签</button>` : ""}</td></tr>`)}
    </div>`;

  $("#od-pick").onsubmit = (e) => { e.preventDefault();
    localStorage.setItem("medplat_od_encounter", e.target.encounter_id.value.trim()); route(); };
  if (encounterId) {
    $("#od-treat").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/outpatient/encounters/${encounterId}/treatments`, formJson(e.target), "#od-msg"); };
    $("#od-nurse").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/outpatient/encounters/${encounterId}/nursing-records`, formJson(e.target), "#od-msg"); };
  }
  $("#od-consent").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "org_id"]);
    // 空字符串的 template_id 要去掉，否则后端按"选了模板"处理
    if (!body.template_id) delete body.template_id;
    else body.template_id = Number(body.template_id);
    postAction("/api/outpatient/consents", body, "#od-cmsg");
  };
  $("#page-body").onclick = (e) => {
    const { csign, crefuse } = e.target.dataset;
    if (csign) {
      const signer_name = prompt("签署人姓名");
      if (!signer_name) return;
      const signer_relation = prompt("与患者关系：self/spouse/parent/child/other", "self") || "self";
      return postAction(`/api/outpatient/consents/${csign}/sign`,
        { signer_name, signer_relation }, "#od-cmsg");
    }
    if (crefuse) {
      const signer_name = prompt("拒签人姓名");
      if (!signer_name) return;
      const refuse_reason = prompt("拒签原因（必填，这是机构「已告知」的证据）");
      if (!refuse_reason) return;
      return postAction(`/api/outpatient/consents/${crefuse}/refuse`,
        { signer_name, signer_relation: "self", refuse_reason }, "#od-cmsg");
    }
  };
}

/* ---------------- 机构协作分组（阶段六） ---------------- */

const GROUP_TYPES = { zone: "片区/分片", alliance: "专科联盟", grid: "网格", other: "其他" };

async function renderOrgGroups() {
  $("#page-desc").textContent =
    "机构的横向分组，与上下级隶属正交——一家机构可以既在某片区，又在某专科联盟";
  const typeFilter = localStorage.getItem("medplat_group_type") || "zone";
  const [groups, orgs, coverage] = await Promise.all([
    api("/api/org-groups"), api("/api/organizations"),
    api(`/api/org-groups/coverage?group_type=${typeFilter}`),
  ]);
  const orgName = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const selected = Number(localStorage.getItem("medplat_group_id") || 0);
  const members = selected ? await api(`/api/org-groups/${selected}/members`) : [];
  $("#page-body").innerHTML = `
    <div class="panel"><h3>新建分组</h3>
      <form class="inline" id="og-form">
        <input name="name" placeholder="分组名称" required>
        <select name="group_type">${Object.entries(GROUP_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="lead_org_id"><option value="">牵头机构（可留空）</option>
          ${orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <input name="note" placeholder="备注">
        <button>创建</button>
      </form>
      <p class="msg" id="og-msg"></p>
      ${table(["名称", "类型", "牵头机构", "成员数", "状态", "操作"], groups, (g) =>
        `<tr><td><b>${esc(g.name)}</b></td><td>${esc(g.group_type_name)}</td>
         <td>${esc(orgName[g.lead_org_id] || "—")}</td><td>${g.member_count}</td>
         <td><span class="tag ${g.active ? "green" : "red"}">${g.active ? "启用" : "停用"}</span></td>
         <td><button data-ogpick="${g.id}">管理成员</button>
             <button data-ogtoggle="${g.id}" data-active="${g.active}">${
               g.active ? "停用" : "启用"}</button></td></tr>`)}
    </div>

    ${selected ? `<div class="panel"><h3>成员机构（分组 #${selected}）</h3>
      <form class="inline" id="og-member">
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>加入分组</button>
      </form>
      ${table(["机构", "层级", "加入时间", "操作"], members, (m) =>
        `<tr><td>${esc(m.org_name)}</td><td>${esc(m.level)}</td>
         <td>${esc(m.joined_at.slice(0, 10))}</td>
         <td><button data-ogdrop="${m.org_id}">移出</button></td></tr>`)}
    </div>` : ""}

    <div class="panel"><h3>覆盖情况（${esc(coverage.group_type_name)}）</h3>
      <form class="inline"><select id="og-type">${Object.entries(GROUP_TYPES).map(([v, t]) =>
        `<option value="${v}"${v === typeFilter ? " selected" : ""}>${t}</option>`).join("")}</select></form>
      <div class="cards">
        <div class="card"><span class="k">分组数</span><b>${coverage.groups}</b></div>
        <div class="card"><span class="k">机构总数</span><b>${coverage.orgs_total}</b></div>
        <div class="card"><span class="k">已入组</span><b>${coverage.orgs_grouped}</b></div>
        <div class="card"><span class="k">未入组</span><b>${
          coverage.ungrouped.length
            ? `<span class="tag orange">${coverage.ungrouped.length}</span>`
            : 0}</b></div>
      </div>
      ${coverage.ungrouped.length
        ? `<p class="msg err">${esc(coverage.note)}</p>${
            table(["机构", "层级"], coverage.ungrouped, (o) =>
              `<tr><td>${esc(o.org_name)}</td><td>${esc(o.level)}</td></tr>`)}`
        : '<p class="desc">全部机构均已入组，按分组统计之和等于全域总数。</p>'}
    </div>`;

  $("#og-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target);
    // 空字符串要去掉，否则后端按"指定了牵头机构 0"处理
    if (!body.lead_org_id) delete body.lead_org_id;
    else body.lead_org_id = Number(body.lead_org_id);
    postAction("/api/org-groups", body, "#og-msg");
  };
  $("#og-type").onchange = (e) => {
    localStorage.setItem("medplat_group_type", e.target.value); route();
  };
  if (selected) {
    $("#og-member").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/org-groups/${selected}/members`, formJson(e.target, ["org_id"]), "#og-msg"); };
  }
  $("#page-body").onclick = async (e) => {
    const { ogpick, ogtoggle, active, ogdrop } = e.target.dataset;
    if (ogpick) { localStorage.setItem("medplat_group_id", ogpick); return route(); }
    if (ogtoggle) {
      try {
        await api(`/api/org-groups/${ogtoggle}`, { method: "PATCH",
          body: JSON.stringify({ active: active !== "true" }) });
        route();
      } catch (err) { setMsg("#og-msg", err.message, false); }
      return;
    }
    if (ogdrop) {
      try {
        await api(`/api/org-groups/${selected}/members/${ogdrop}`, { method: "DELETE" });
        route();
      } catch (err) { setMsg("#og-msg", err.message, false); }
    }
  };
}

/* ---------------- 医保基金总额付费（阶段七） ---------------- */

const INSURANCE_TYPES = { resident: "城乡居民", employee: "城镇职工" };

async function renderFund() {
  $("#page-desc").textContent =
    "预付与清算产生真实资金流，月度预结只是账面对冲；分配依据是冻结的绩效得分快照，事后调权不影响已分结果";
  const picked = Number(localStorage.getItem("medplat_fund_pool") || 0);
  const [pools, groups, vars] = await Promise.all([
    api("/api/fund/pools"), api("/api/org-groups"), api("/api/fund/formula-variables"),
  ]);
  const groupName = Object.fromEntries(groups.map((g) => [g.id, g.name]));
  let detail = null;
  if (picked) {
    const [prepay, periods] = await Promise.all([
      api(`/api/fund/pools/${picked}/prepayments`), api(`/api/fund/pools/${picked}/periods`),
    ]);
    let settlement = null;
    try { settlement = await api(`/api/fund/pools/${picked}/settlement`); } catch (e) { /* 未清算 */ }
    detail = { prepay, periods, settlement };
  }
  const thisYear = new Date().getFullYear();
  $("#page-body").innerHTML = `
    <div class="panel"><h3>基金池</h3>
      <form class="inline" id="fd-pool">
        <input name="year" type="number" value="${thisYear}" required style="min-width:90px">
        <select name="insurance_type">${Object.entries(INSURANCE_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="org_group_id"><option value="">全域（不分片区）</option>
          ${groups.map((g) => `<option value="${g.id}">${esc(g.name)}</option>`).join("")}</select>
        <input name="total_amount" type="number" step="any" placeholder="筹资总额(元)" required>
        <input name="prepay_ratio_pct" type="number" step="any" placeholder="预付比例%" value="70">
        <button>建池</button>
      </form>
      <p class="msg" id="fd-msg"></p>
      ${table(["年度", "险种", "范围", "筹资", "已预付", "已归集", "账面结余", "状态", "操作"], pools, (p) =>
        `<tr><td>${p.year}</td><td>${esc(INSURANCE_TYPES[p.insurance_type] || p.insurance_type)}</td>
         <td>${esc(p.org_group_id ? groupName[p.org_group_id] || `#${p.org_group_id}` : "全域")}</td>
         <td>${p.total_amount}</td><td>${p.prepaid_amount}</td><td>${p.accrued_expense}</td>
         <td>${p.book_balance < 0
           ? `<span class="tag red">${p.book_balance}</span>` : p.book_balance}</td>
         <td><span class="tag ${p.status === "settled" ? "green" : ""}">${esc(p.status)}</span></td>
         <td><button data-fdpick="${p.id}">打开</button></td></tr>`)}
    </div>

    ${picked ? `
    <div class="panel"><h3>预付批次（真实资金流）</h3>
      <form class="inline" id="fd-prepay">
        <input name="batch_no" placeholder="批次号">
        <input name="amount" type="number" step="any" placeholder="金额(元)" required>
        <input name="paid_date" type="date"><button>登记预付</button>
      </form>
      ${table(["批次", "金额", "拨付日期", "备注"], detail.prepay, (r) =>
        `<tr><td>${esc(r.batch_no || "—")}</td><td>${r.amount}</td>
         <td>${esc(r.paid_date || "—")}</td><td>${esc(r.note || "—")}</td></tr>`)}
    </div>

    <div class="panel"><h3>月度预结（账面对冲，不产生资金流）</h3>
      <form class="inline" id="fd-period">
        <input name="period" placeholder="YYYY-MM" required style="min-width:100px">
        <input name="actual_amount" type="number" step="any" placeholder="发生额(留空=按结算单归集)">
        <button>预结</button>
      </form>
      ${table(["期间", "发生额", "来源", "备注"], detail.periods, (r) =>
        `<tr><td>${esc(r.period)}</td><td>${r.actual_amount}</td>
         <td>${r.source === "auto" ? "系统归集" : "人工核定"}</td>
         <td>${esc(r.note || "—")}</td></tr>`)}
    </div>

    <div class="panel"><h3>年终清算与结余分配</h3>
      ${detail.settlement ? renderSettlement(detail.settlement, vars) : `
        <form class="inline" id="fd-settle">
          <input name="total_expense" type="number" step="any" placeholder="全年发生额(留空=各期之和)">
          <select name="overrun_action"><option value="none">超支不处理（仅记录）</option>
            <option value="share">超支按公式分摊</option>
            <option value="carry">超支挂账结转</option></select>
          <button>清算</button>
        </form>
        <p class="desc">清算每个池只能做一次；超支不会自动扣减任何机构。</p>`}
    </div>` : ""}`;

  $("#fd-pool").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["year", "total_amount", "prepay_ratio_pct"]);
    if (!body.org_group_id) delete body.org_group_id;
    else body.org_group_id = Number(body.org_group_id);
    postAction("/api/fund/pools", body, "#fd-msg");
  };
  if (picked) {
    $("#fd-prepay").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/fund/pools/${picked}/prepayments`, formJson(e.target, ["amount"]), "#fd-msg"); };
    $("#fd-period").onsubmit = (e) => {
      e.preventDefault();
      const body = formJson(e.target, ["actual_amount"]);
      // 留空即交给系统按结算单归集，不能传空串
      if (body.actual_amount === null || body.actual_amount === undefined || Number.isNaN(body.actual_amount)) {
        delete body.actual_amount;
      }
      postAction(`/api/fund/pools/${picked}/periods`, body, "#fd-msg");
    };
    const settleForm = $("#fd-settle");
    if (settleForm) settleForm.onsubmit = (e) => {
      e.preventDefault();
      const body = formJson(e.target, ["total_expense"]);
      if (Number.isNaN(body.total_expense) || body.total_expense === null) delete body.total_expense;
      postAction(`/api/fund/pools/${picked}/settle`, body, "#fd-msg");
    };
  }
  $("#page-body").onclick = (e) => {
    if (e.target.dataset.fdpick) {
      localStorage.setItem("medplat_fund_pool", e.target.dataset.fdpick);
      return route();
    }
    if (e.target.id === "fd-distribute") {
      const formula_expr = $("#fd-formula").value.trim() || "score";
      return postAction(`/api/fund/pools/${picked}/distribute`, { formula_expr }, "#fd-msg");
    }
  };
}

function renderSettlement(s, vars) {
  return `
    <div class="cards">
      <div class="card"><span class="k">筹资</span><b>${s.total_income}</b></div>
      <div class="card"><span class="k">发生额</span><b>${s.total_expense}</b></div>
      <div class="card"><span class="k">${s.is_overrun ? "超支" : "结余"}</span><b>${
        s.is_overrun ? `<span class="tag red">${s.balance}</span>` : s.balance}</b></div>
      <div class="card"><span class="k">已分配</span><b>${s.distributed_amount}</b></div>
    </div>
    <p class="desc">${esc(s.caliber.balance)}；${esc(s.caliber.overrun)}</p>
    ${s.is_overrun
      ? `<p class="msg err">本池超支，无结余可分配。当前超支处置方式：${
          esc(s.overrun_action_name)}——平台只记录，不自动扣减任何机构。</p>`
      : `<form class="inline">
          <input id="fd-formula" value="${esc(s.formula_expr || "score")}" style="min-width:200px">
          <button type="button" id="fd-distribute">按公式分配</button></form>
        <p class="desc">${esc(vars.note)}；可用变量：${
          vars.variables.map((v) => `<code>${esc(v.name)}</code>（${esc(v.desc)}）`).join("、")}</p>
        <p class="desc">${esc(s.caliber.score)}${
          s.score_basis ? `；本次快照参数：${esc(s.score_basis)}` : ""}</p>
        ${table(["机构", "绩效得分", "权重", "占比", "金额"], s.distributions, (d) =>
          `<tr><td>${esc(d.org_name)}</td><td>${d.score}</td><td>${d.weight}</td>
           <td>${d.share_pct}%</td><td><b>${d.amount}</b></td></tr>`)}`}`;
}

/* ---------------- 人员下沉调度（阶段八） ---------------- */

const ASSIGN_TYPES = { long_term: "长期派驻", support: "短期支援", rounds: "巡诊", other: "其他" };
const TITLE_LEVELS = {
  none: "未填", junior: "初级", intermediate: "中级", deputy_senior: "副高", senior: "正高",
};

async function renderStaffing() {
  $("#page-desc").textContent =
    "监测指标只认长期派驻满半年且中级及以上——巡诊不算下沉，职称等级必须显式维护而不从职称文本推断";
  const [rows, stats, orgs] = await Promise.all([
    api("/api/staffing/secondments?limit=100"),
    api("/api/staffing/dispatch-stats"),
    api("/api/organizations"),
  ]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>下沉指标（${stats.year} 年度）</h3>
      ${stats.unknown_title_level
        ? `<p class="msg err">有 ${stats.unknown_title_level} 人次满足长期派驻满半年，
            但职称等级未维护，未计入"中级及以上"。请在下方台账补齐等级。</p>` : ""}
      ${table(["接收机构", "在派", "累计", "长期满半年", "其中中级及以上"], stats.orgs, (o) =>
        `<tr><td>${esc(o.org_name)}</td><td>${o.ongoing}</td><td>${o.total}</td>
         <td>${o.long_term_6m}</td><td><b>${o.long_term_6m_senior}</b></td></tr>`)}
      <p class="desc">${esc(stats.caliber.long_term_6m)}；${esc(stats.caliber.senior)}</p>
    </div>

    <div class="panel"><h3>新建派驻</h3>
      <form class="inline" id="st-form">
        <input name="employee_id" type="number" placeholder="员工ID" required>
        <select name="from_org_id">${orgs.map((o) =>
          `<option value="${o.id}">派出：${esc(o.name)}</option>`).join("")}</select>
        <select name="to_org_id">${orgs.map((o) =>
          `<option value="${o.id}">接收：${esc(o.name)}</option>`).join("")}</select>
        <input name="start_date" type="date" required>
        <select name="assignment_type">${Object.entries(ASSIGN_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="position" placeholder="派驻岗位">
        <button>建立</button>
      </form>
      <p class="msg" id="st-msg"></p>
    </div>

    <div class="panel"><h3>派驻台账</h3>
      ${table(["员工", "职称", "等级", "派出", "接收", "类型", "起止", "天数", "操作"], rows, (r) =>
        `<tr><td>${esc(r.employee_name)}</td><td>${esc(r.title || "—")}</td>
         <td>${r.title_level === "none"
           ? '<span class="tag orange">未填</span>' : esc(r.title_level_name)}
             <button data-stlevel="${r.employee_id}">维护</button></td>
         <td>${esc(r.from_org_name)}</td><td>${esc(r.to_org_name)}</td>
         <td>${esc(r.assignment_type_name)}</td>
         <td>${esc(r.start_date)} ~ ${r.ongoing ? "在派" : esc(r.end_date)}</td>
         <td>${r.days}</td>
         <td>${r.ongoing ? `<button data-stend="${r.id}">结束派驻</button>` : ""}</td></tr>`)}
    </div>`;
  $("#st-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/staffing/secondments",
      formJson(e.target, ["employee_id", "from_org_id", "to_org_id"]), "#st-msg"); };
  $("#page-body").onclick = async (e) => {
    const { stend, stlevel } = e.target.dataset;
    if (stend) return postAction(`/api/staffing/secondments/${stend}/end`, null, "#st-msg");
    if (stlevel) {
      const level = prompt("职称等级：junior/intermediate/deputy_senior/senior", "intermediate");
      if (!level) return;
      try {
        await api(`/api/staffing/employees/${stlevel}/title-level`,
          { method: "PATCH", body: JSON.stringify({ title_level: level }) });
        route();
      } catch (err) { setMsg("#st-msg", err.message, false); }
    }
  };
}

/* ---------------- 专病管理（阶段八） ---------------- */

const ENROLL_STATUS = { enrolled: "在管", completed: "完成出组", exited: "中途退出" };

async function renderDiseasePrograms() {
  $("#page-desc").textContent =
    "专病是有始有终的诊疗路径（入组—节点—疗效评价—出组），与慢病的长期随访分级不是一回事；路径节点自行配置，平台不预置任何病种";
  const picked = Number(localStorage.getItem("medplat_program") || 0);
  const [programs, orgs] = await Promise.all([
    api("/api/disease-programs"), api("/api/organizations"),
  ]);
  let enrollments = [], stats = null;
  if (picked) {
    [enrollments, stats] = await Promise.all([
      api(`/api/disease-programs/enrollments?program_id=${picked}&limit=50`),
      api(`/api/disease-programs/${picked}/stats`),
    ]);
  }
  const current = programs.find((p) => p.id === picked);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>专病目录</h3>
      <form class="inline" id="dp-form">
        <input name="code" placeholder="专病编码" required>
        <input name="name" placeholder="专病名称" required>
        <select name="org_id"><option value="">不指定主办机构</option>
          ${orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <input name="nodes" placeholder="路径节点：键:名称,键:名称" style="min-width:260px">
        <button>建目录</button>
      </form>
      <p class="msg" id="dp-msg"></p>
      ${table(["编码", "名称", "路径节点", "状态", "操作"], programs, (p) =>
        `<tr><td>${esc(p.code)}</td><td>${esc(p.name)}</td>
         <td>${(p.path_nodes || []).map((n) =>
           `${esc(n.name)}${n.required === false ? "（选做）" : ""}`).join(" → ") || "—"}</td>
         <td><span class="tag ${p.active ? "green" : "red"}">${p.active ? "启用" : "停用"}</span></td>
         <td><button data-dppick="${p.id}">打开</button></td></tr>`)}
    </div>

    ${picked && current ? `
    <div class="panel"><h3>${esc(current.name)} · 入组管理</h3>
      <form class="inline" id="dp-enroll">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>入组</button>
      </form>
      <div class="cards">
        <div class="card"><span class="k">累计</span><b>${stats.total}</b></div>
        ${Object.entries(stats.by_status).map(([k, v]) =>
          `<div class="card"><span class="k">${esc(v.name)}</span><b>${v.count}</b></div>`).join("")}
        <div class="card"><span class="k">必需节点完成度</span><b>${
          stats.avg_required_completion_pct}%</b></div>
      </div>
      <p class="desc">${esc(stats.caliber)}</p>
      ${Object.keys(stats.by_outcome).length
        ? table(["疗效", "例数"], Object.entries(stats.by_outcome).map(([k, v]) => ({ k, ...v })),
            (r) => `<tr><td>${esc(r.name)}</td><td>${r.count}</td></tr>`)
        : ""}
      ${table(["入组ID", "患者", "状态", "完成度", "待办节点", "疗效", "操作"], enrollments, (e) =>
        `<tr><td>${e.id}</td><td>${e.patient_id}</td>
         <td><span class="tag ${e.status === "enrolled" ? "orange"
           : e.status === "completed" ? "green" : ""}">${esc(e.status_name)}</span></td>
         <td>${e.completion.required_done}/${e.completion.required_total}
             （${e.completion.required_done_pct}%）</td>
         <td>${esc(e.completion.pending_required.join("、") || "—")}</td>
         <td>${esc(e.outcome_name)}</td>
         <td>${e.status === "enrolled"
           ? `<button data-dpnode="${e.id}">记录节点</button><button data-dpexit="${e.id}">出组</button>`
           : ""}</td></tr>`)}
    </div>` : ""}`;

  $("#dp-form").onsubmit = (e) => {
    e.preventDefault();
    const raw = formJson(e.target);
    const body = { code: raw.code, name: raw.name, path_nodes: [] };
    if (raw.org_id) body.org_id = Number(raw.org_id);
    // "键:名称,键:名称" → 节点数组。缺省全部按必需处理，选做节点在目录里再改。
    (raw.nodes || "").split(",").map((s) => s.trim()).filter(Boolean).forEach((pair) => {
      const [key, name] = pair.split(":");
      if (key && name) body.path_nodes.push({ key: key.trim(), name: name.trim(), required: true });
    });
    postAction("/api/disease-programs", body, "#dp-msg");
  };
  if (picked) {
    $("#dp-enroll").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/disease-programs/${picked}/enrollments`,
        formJson(e.target, ["patient_id", "org_id"]), "#dp-msg"); };
  }
  $("#page-body").onclick = (e) => {
    const { dppick, dpnode, dpexit } = e.target.dataset;
    if (dppick) { localStorage.setItem("medplat_program", dppick); return route(); }
    if (dpnode) {
      const keys = (current.path_nodes || []).map((n) => `${n.key}(${n.name})`).join(" ");
      const node_key = prompt(`节点键，可选：${keys}`);
      if (!node_key) return;
      const result = prompt("执行结果（可留空）") || "";
      return postAction(`/api/disease-programs/enrollments/${dpnode}/records`,
        { node_key, result }, "#dp-msg");
    }
    if (dpexit) {
      const status = prompt("出组方式：completed（完成）/ exited（中途退出）", "completed");
      if (!status) return;
      const outcome = prompt("疗效：cured/improved/stable/worsened/died，留空=未评价") || "";
      const exit_reason = status === "exited" ? (prompt("退出原因（必填）") || "") : "";
      if (status === "exited" && !exit_reason) return;
      return postAction(`/api/disease-programs/enrollments/${dpexit}/exit`,
        { status, outcome, exit_reason }, "#dp-msg");
    }
  };
}

