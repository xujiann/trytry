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

/** 交接班清单的筛选（P2-476）：只留在内存里、不进存储——病区 / 日期是这一次查看的条件。 */
const HANDOVER_FILTER = { ward_id: "", handover_date: "" };

/** 体温单测量时刻 → 毫秒数，给折线图按时间比例摆点（P2-1336）。形状照后端 `datetypes.DATETIME_SHAPE`（日期后可跟时刻，
    空格或 `T` 分隔，秒可选）；按墙上时间算（`Date.UTC` 只当算术用，不涉时区）。形状不对的（P1-100 之前的存量自由文本）
    回 NaN，折线图整张回落成按条目等距 */
function vitalTimeMs(measuredAt) {
  const m = /^([0-9]{4})-([0-9]{2})-([0-9]{2})(?:[ T]([0-9]{2}):([0-9]{2})(?::([0-9]{2}))?)?$/.exec(measuredAt || "");
  return m ? Date.UTC(+m[1], +m[2] - 1, +m[3], +(m[4] || 0), +(m[5] || 0), +(m[6] || 0)) : NaN;
}

async function renderClinicalDocs() {
  $("#page-desc").textContent = "病程记录 / 护理记录 / 体温单 / 交接班；出院前可做文书完整性自查";
  // 只取在院的（P2-154）：原先不带条件取「最新 200 条住院」再在页面上挑在院的——住得久的患者被新入院的挤出前 200 条，
  // 从这张选择框里消失，病程、护理、体温单都写不了；最新 200 条碰巧都出院了，页面就说「暂无在院患者」。
  // 续页取全（P2-1333）：一页最多 500 条，原先只取第一页，在院过 500 人时住得最久的那几位照样不在框里
  const admissions = await fetchAllPages(api, "/api/inpatient/admissions?status=admitted");
  const inHospital = admissions.filter((a) => a.status === "admitted");
  // 存量选择必须落在**这张在院列表里**：出院之后 `inHospital` 不再包含它，
  // 而下面的 <select> 只列在院记录——于是没有一个 option 带 selected，浏览器
  // 显示第一条，四个面板和三个写入表单却仍然指向那条已出院的记录。
  const current = pickedId("medplat_doc_adm", inHospital)
    || (inHospital[0] && inHospital[0].id) || 0;
  // 在用医嘱给护理记录的「关联医嘱」下拉（P2-863）：执行某条医嘱产生的护理记录挂到那条医嘱上，医嘱执行视图的
  // 「关联护理记录 N 条」才数得到——原先表单没有这一项，按界面用法恒为 0
  const [notes, nursing, vitals, completeness, activeOrders] = current
    ? await Promise.all([
        api(`/api/inpatient/admissions/${current}/progress-notes`),
        api(`/api/inpatient/admissions/${current}/nursing-records`),
        api(`/api/inpatient/admissions/${current}/vitals`),
        api(`/api/inpatient/admissions/${current}/document-completeness`),
        api(`/api/inpatient/orders?admission_id=${current}&status=active`),
      ])
    : [[], [], [], null, []];
  // 交接班清单（P2-476）：原先只记得进、没有一个页面看得见——接班的人无从读起。交接班按病区、不挂某次住院，
  // 所以不跟着上面的住院记录走，没有在院患者时照样能看、能交
  const handoverQuery = new URLSearchParams(Object.entries(HANDOVER_FILTER).filter(([, v]) => v !== ""));
  const [wards, handoverResult] = await Promise.all([
    api("/api/inpatient/wards"),
    api(`/api/inpatient/handovers${handoverQuery.toString() ? `?${handoverQuery}` : ""}`)
      .catch((err) => ({ error: err.message })),
  ]);
  // 条件被拒（如换了账号、那个病区不再可见）：说清楚、回到全部病区，不把整页掀掉——条件留在内存里，掀掉就一直是这一行错
  let handovers = handoverResult;
  let handoverError = "";
  if (handovers.error) {
    handoverError = handovers.error;
    Object.keys(HANDOVER_FILTER).forEach((k) => { HANDOVER_FILTER[k] = ""; });
    handovers = await api("/api/inpatient/handovers");
  }
  const wardName = Object.fromEntries(wards.map((w) => [w.id, w.name]));
  // 交班表单默认选正在看的病区，其次是当前住院记录所在的病区
  const handoverWard = String(HANDOVER_FILTER.ward_id || (inHospital.find((a) => a.id === current) || {}).ward_id || "");

  // 选择框写「病区 床号 姓名」（P2-1335，与医生移动端查房同一句）：原先只有「#住院号 患者ID 诊断」，同诊断的几位分不开，
  // 病程、护理、体温单容易写到别人名下
  $("#page-body").innerHTML = `
    ${panel("选择住院记录", `
      <form class="inline" id="doc-pick"><select name="admission_id">${
        inHospital.map((a) => `<option value="${a.id}" ${a.id === current ? "selected" : ""}>${esc(a.ward_name)} ${esc(a.bed_no)} ${esc(a.patient_name)} · ${esc(a.diagnosis_name || "住院")}（住院号 ${a.id}）</option>`).join("")
      }</select><button>切换</button></form>
      ${completeness ? `<p class="msg ${completeness.complete ? "ok" : "err"}">${
        completeness.complete ? "文书完整" : "缺项：" + completeness.missing.join("、")}</p>` : '<p class="msg">暂无在院患者</p>'}
    `)}
    ${current ? `
    ${panel(`病程记录（${notes.length}）`, `
      <form class="inline" id="note-form">
        <select name="note_type">${Object.entries(NOTE_TYPES).map(([k, v]) =>
          `<option value="${k}"${k === PROGRESS_NOTE_DEFAULT ? " selected" : ""}>${v}</option>`).join("")}</select>
        <input name="doctor_name" placeholder="记录医师">
        <input name="content" placeholder="病程内容" required style="min-width:320px">
        <button>书写</button></form>
      <p class="msg" id="doc-msg"></p>
      ${table(["时间", "类型", "医师", "内容"], notes, (n) =>
        `<tr><td>${esc(n.recorded_at)}</td><td>${esc(NOTE_TYPES[n.note_type] || n.note_type)}</td>
         <td>${esc(n.doctor_name)}</td><td>${esc(n.content)}</td></tr>`)}`)}
    ${panel(`护理记录（${nursing.length}）`, `
      <form class="inline" id="nursing-form">
        <select name="nursing_level">${Object.entries(NURSING_LEVELS).map(([k, v]) =>
          `<option value="${k}"${k === INPATIENT_NURSING_DEFAULT ? " selected" : ""}>${v}</option>`).join("")}</select>
        <input name="nurse_name" placeholder="护士">
        <select name="inpatient_order_id"><option value="">关联医嘱（执行某条医嘱时选，可空）</option>${
          activeOrders.map((o) => `<option value="${o.id}">#${o.id} ${o.order_type === "long" ? "长期" : "临时"} ${
            esc(o.content.slice(0, 30))}</option>`).join("")}</select>
        <input name="content" placeholder="护理内容" required style="min-width:280px"><button>记录</button></form>
      ${table(["时间", "级别", "护士", "内容"], nursing, (r) =>
        `<tr><td>${esc(r.recorded_at)}</td><td>${esc(NURSING_LEVELS[r.nursing_level] || r.nursing_level)}</td>
         <td>${esc(r.nurse_name)}</td><td>${esc(r.content)}</td></tr>`)}`)}
    ${panel(`体温单（${vitals.length}）`, `
      <form class="inline" id="vital-form">
        <label style="font-size:13px">测量时刻 <input name="measured_at" type="datetime-local" required></label>
        <input name="temperature" type="number" step="0.1" placeholder="体温℃">
        <input name="pulse" type="number" placeholder="脉搏"><input name="respiration" type="number" placeholder="呼吸">
        <input name="sbp" type="number" placeholder="收缩压"><input name="dbp" type="number" placeholder="舒张压">
        <input name="intake_ml" type="number" min="0" placeholder="入量 ml"><input name="output_ml" type="number" min="0" placeholder="出量 ml">
        <input name="weight_kg" type="number" step="any" min="0" placeholder="体重 kg">
        <button>录入</button></form>
      ${vitals.length ? lineChart(vitals.map((v) => v.measured_at.slice(5, 10)),
        // 未测的给 null、不给 0（P2-158）：接口的注释与用户手册都说「未测项留空不要填 0，填 0 会污染体温单趋势曲线」
        { "体温": vitals.map((v) => v.temperature ?? null), "脉搏": vitals.map((v) => v.pulse ?? null) },
        ["#c0392b", "#0b6e6e"],
        // 按测量时刻定横坐标（P2-1336）：原先按条目等距，一天测 6 次与之后几天每天测 1 次占一样宽，热型曲线被压变形
        vitals.map((v) => vitalTimeMs(v.measured_at))) : ""}
      ${table(["测量时刻", "体温", "脉搏", "呼吸", "血压", "入量 ml", "出量 ml", "体重 kg", "记录人"], vitals, (v) =>
        `<tr><td>${esc(v.measured_at)}</td><td>${v.temperature ?? "—"}</td><td>${v.pulse ?? "—"}</td>
         <td>${v.respiration ?? "—"}</td><td>${v.sbp ?? "—"}/${v.dbp ?? "—"}</td>
         <td>${v.intake_ml ?? "—"}</td><td>${v.output_ml ?? "—"}</td><td>${v.weight_kg ?? "—"}</td><td>${esc(v.recorder)}</td></tr>`)}`)}` : ""}
    ${panel(`交接班（${handovers.length}）`, `
      <form class="inline" id="handover-form">
        <select name="ward_id" required>${wards.map((w) =>
          `<option value="${w.id}"${String(w.id) === handoverWard ? " selected" : ""}>${esc(w.name)}</option>`).join("")}</select>
        <select name="shift"><option value="day">白班</option><option value="evening">小夜</option><option value="night">大夜</option></select>
        <input name="handover_date" placeholder="YYYY-MM-DD" required>
        <input name="from_staff" placeholder="交班"><input name="to_staff" placeholder="接班">
        <input name="critical_count" type="number" placeholder="危重数">
        <input name="content" placeholder="交班内容" style="min-width:240px"><button>交班</button></form>
      <p class="desc">在院人数由系统按当前住院数据快照，不接受人工填写。</p>
      <form class="inline" id="handover-filter">
        <select name="ward_id"><option value="">全部病区</option>${wards.map((w) =>
          `<option value="${w.id}"${String(w.id) === HANDOVER_FILTER.ward_id ? " selected" : ""}>${esc(w.name)}</option>`).join("")}</select>
        <input name="handover_date" type="date" value="${esc(HANDOVER_FILTER.handover_date)}">
        <button>查看交接班</button></form>
      <p class="msg" id="handover-msg"></p>
      ${table(["日期", "班次", "病区", "交班 → 接班", "在院", "危重", "交班内容"], handovers, (h) =>
        `<tr><td>${esc(h.handover_date)}</td><td>${esc(h.shift_name)}</td><td>${esc(wardName[h.ward_id] || `#${h.ward_id}`)}</td>
         <td>${esc(h.from_staff || "—")} → ${esc(h.to_staff || "—")}</td><td>${esc(h.patient_count)}</td>
         <td>${esc(h.critical_count)}</td><td>${esc(h.content)}</td></tr>`)}
      ${handovers.length >= 50 ? '<p class="desc"><b>只列最新 50 条</b>，按病区 / 日期筛看更早的。</p>' : ""}`)}`;

  const pickAdmission = (admissionId) => { localStorage.setItem("medplat_doc_adm", admissionId); route(); };
  $("#doc-pick").onsubmit = (e) => {
    e.preventDefault();
    pickAdmission(new FormData(e.target).get("admission_id"));
  };
  // 下拉一改就切换（P1-231）：原先只有点「切换」才记住——下拉显示乙、没点切换，下面的病程 / 护理 / 体温单
  // 照旧按甲写，回执照样「已记录」（core.js `pickedId` 注释里要防的「屏幕上写着甲，病程记录写进了乙」同一种坏法）
  $("#doc-pick select").onchange = (e) => pickAdmission(e.target.value);
  $("#handover-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/inpatient/handovers", formJson(e.target, ["ward_id", "critical_count"]), "#handover-msg"); };
  $("#handover-filter").onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    Object.keys(HANDOVER_FILTER).forEach((k) => { HANDOVER_FILTER[k] = String(f.get(k) ?? "").trim(); });
    route();
  };
  if (handoverError) setMsg("#handover-msg", `${handoverError}（已回到全部病区）`, false);
  if (!current) return;
  $("#note-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/progress-notes`, formJson(e.target), "#doc-msg"); };
  $("#nursing-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/nursing-records`, formJson(e.target, ["inpatient_order_id"]),
      "#doc-msg"); };
  $("#vital-form").onsubmit = (e) => { e.preventDefault();
    postAction(`/api/inpatient/admissions/${current}/vitals`,
      // 出入量、体重（P2-473）：接口与体温单模型一直有这三项，页面原先录不进、也看不见
      formJson(e.target, ["temperature", "pulse", "respiration", "sbp", "dbp", "intake_ml", "output_ml", "weight_kg"]),
      "#doc-msg"); };
}

/* ---------------- 手术麻醉 ---------------- */

async function renderSurgery() {
  $("#page-desc").textContent = "申请 → 审批（申请人不得自批）→ 手术间排班（区间重叠拦截）→ 术中记录；填病案首页时手术栏留空即取已完成的术式（先记术中记录、再填首页）";
  // 还要办的三个状态单独取、并进最新 100 条（P2-361）：原先只取最新 100 条，提前申请的择期手术排到 100 条之外，
  // 审批 / 排班 / 术中记录的按钮跟着消失
  // 手术量统计只给管理层（后端 require_roles("director")），原先也在这个 Promise.all 里：医师、经办一进这页，统计一个 403
  // 整页只剩一句报错——提申请（只收医师）、排班（经办 / 管理层）、术中记录都只在这一页（P1-220，P1-175 / P2-459 同形）。
  // 统计只在管理层登录时取；各个按钮摆给谁是 P2-447 的事
  const canStats = ["admin", "director"].includes(currentRole());
  const [recent, rooms, schedules, stats, orgs, ...open] = await Promise.all([
    api("/api/surgery/requests"), api("/api/surgery/rooms"),
    api("/api/surgery/schedules"), canStats ? api("/api/surgery/stats") : Promise.resolve([]),
    api("/api/organizations"),
    ...["requested", "approved", "scheduled"].map((s) => api(`/api/surgery/requests?status=${s}`))]);
  const requests = [...new Map([...recent, ...open.flat()].map((r) => [r.id, r])).values()]
    .sort((a, b) => b.id - a.id);
  // 手术间名前带所属医院（P2-1402，与「手术已安排」站内信、居民端同一个叫法，后端 `surgery.room_labels`）：手术间名只在一家
  // 医院里唯一，管理员 / 管理层的排班下拉列着全县的手术间，原先只印名字，两家的「1号手术间」分不清。机构名从机构清单取
  const orgName = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const roomLabel = (r) => (orgName[r.org_id] ? `${orgName[r.org_id]} · ${r.name}` : r.name);
  $("#page-body").innerHTML = `
    ${stats.length ? panel("手术量统计",
      table(["机构", "台次", "切口构成", "麻醉构成", "并发症"], stats, (s) =>
        `<tr><td>${esc(s.org_name)}</td><td>${s.total}</td>
         <td>${Object.entries(s.by_incision).map(([k, v]) => `${esc(k)}类:${v}`).join(" ")}</td>
         <td>${Object.entries(s.by_anesthesia).map(([k, v]) => `${esc(ANESTHESIA[k] || k)}:${v}`).join(" ")}</td>
         <td>${s.complications}</td></tr>`)) : ""}
    ${panel("手术间（admin 建档）", `
      <form class="inline" id="room-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="手术间名称" required><button>新建</button></form>
      <p class="msg" id="surg-msg"></p>
      ${table(["ID", "机构", "名称", "状态"], rooms, (r) =>
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${esc(r.name)}</td>
         <td><span class="tag ${r.active ? "green" : ""}">${r.active ? "启用" : "停用"}</span></td></tr>`)}`)}
    ${panel("提出手术申请（医师）", `
      <form class="inline" id="surg-form"><input name="admission_id" type="number" placeholder="住院ID" required>
        <input name="surgery_name" placeholder="拟施手术" required>
        <input name="surgeon_name" placeholder="拟施术者（空着为申请人）">
        <select name="incision_level"><option value="I">I类切口</option><option value="II" selected>II类切口</option>
          <option value="III">III类切口</option><option value="IV">IV类切口</option></select>
        <select name="anesthesia_type">${Object.entries(ANESTHESIA).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <select name="urgency">${Object.entries(URGENCY).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="planned_date" placeholder="拟手术日 YYYY-MM-DD">
        <label style="font-size:13px"><input type="checkbox" name="unplanned_return">
          非计划重返手术室（本次住院因并发症等再次手术；分期手术、计划内二次探查不勾）</label>
        <button>提出申请</button></form>`)}
    ${panel(`手术申请（${requests.length}）`,
      table(["ID", "住院", "术式", "切口", "麻醉", "急缓", "状态", "操作"], requests, (r) => {
        let ops = "—";
        if (r.status === "requested") ops = `<button class="btn secondary" data-approve="${r.id}">审批通过</button>
          <button class="btn danger" data-reject="${r.id}">否决</button>`;
        else if (r.status === "approved") ops = `<button class="btn secondary" data-schedule="${r.id}">排班</button>`;
        else if (r.status === "scheduled") ops = `<button class="btn secondary" data-record="${r.id}">术中记录</button>`;
        else if (r.status === "completed") ops = `<button class="btn" data-view="${r.id}">查看记录</button>`;
        return `<tr><td>${r.id}</td><td>${r.admission_id}</td><td>${esc(r.surgery_name)}${
          r.unplanned_return ? ' <span class="tag red">非计划重返</span>' : ""}</td>
          <td>${esc(r.incision_level)}</td><td>${esc(ANESTHESIA[r.anesthesia_type] || "")}</td>
          <td>${esc(URGENCY[r.urgency] || "")}</td><td>${statusTag(SURGERY_STATUS, r.status)}</td><td>${ops}</td></tr>`;
      }))}
    ${panel("手术排班表",
      table(["日期", "手术间", "时段", "术式", "术者", "麻醉", "急缓"], schedules, (s) =>
        `<tr><td>${esc(s.scheduled_date)}</td><td>${esc(s.room_name)}</td><td>${esc(s.start_time)}-${esc(s.end_time)}</td>
         <td>${esc(s.surgery_name)}</td><td>${esc(s.surgeon_name)}</td>
         <td>${esc(ANESTHESIA[s.anesthesia_type] || "")}</td><td>${esc(URGENCY[s.urgency] || "")}</td></tr>`))}
    <div class="panel hidden" id="surg-detail"><h3>术中记录</h3><div id="surg-detail-body"></div></div>`;

  $("#room-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/surgery/rooms", formJson(e.target, ["org_id"]), "#surg-msg"); };
  $("#surg-form").onsubmit = (e) => { e.preventDefault();
    // 拟施术者（P2-1307）：原先表单没有这一项，申请单的术者恒为申请人——住院医提的申请，术中记录缺省带出的就是住院医。
    // 选填：空着 formJson 不送，后端照旧取申请人
    const body = formJson(e.target, ["admission_id"]);
    // 非计划重返手术室由医师显式勾选（P2-172）：原先表单里没有这一项——手册叫人「提手术申请时如实勾选」，
    // 页面上却无处可勾，质量指标「非计划重返手术室率」恒为 0
    body.unplanned_return = e.target.unplanned_return.checked;
    postAction("/api/surgery/requests", body, "#surg-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.approve) await api(`/api/surgery/requests/${d.approve}/approve`,
        { method: "POST", body: JSON.stringify({ approved: true }) });
      else if (d.reject) await api(`/api/surgery/requests/${d.reject}/approve`,
        { method: "POST", body: JSON.stringify({ approved: false }) });
      // P2-38 / P1-66：排班四连问、术中记录四连问换成页内表单。术中记录原先只问四项、
      // **转归写死成"好转"**——质量指标的治愈率与死亡数取的正是它；术前/术后诊断（诊断
      // 符合率的数据源）从界面根本录不进去。现在后端收的这些都能填。
      else if (d.schedule) {
        if (!rooms.length) return setMsg("#surg-msg", "还没有手术间，请先在上方新增", false);
        const v = await spdModal("手术排班", [
          // 选项文字带所属医院（P2-1402，见上 `roomLabel`），由 spdModal 过 esc()
          { name: "room_id", label: "手术间", type: "select",
            options: rooms.map((r) => ({ value: r.id, label: roomLabel(r) })) },
          { name: "scheduled_date", label: "手术日期", placeholder: "YYYY-MM-DD", required: true },
          { name: "start_time", label: "开始时间", placeholder: "HH:MM", required: true },
          { name: "end_time", label: "结束时间", placeholder: "HH:MM", required: true },
        ]);
        if (!v) return;
        await api(`/api/surgery/requests/${d.schedule}/schedule`, { method: "POST",
          body: JSON.stringify({ ...v, room_id: Number(v.room_id) }) });
      } else if (d.record) {
        const req = requests.find((r) => r.id === Number(d.record));
        // 手术起止时刻（P2-1116）：原先不录，做手术那天（术后随访起算、手术质量指标归月，`surgery.operation_day`）恒取
        // 排班日——顺延、提前的手术都跟着原排班日走，「查看记录」的「起止」一行恒空。缺省带出这台的排班日期与时段、按实际
        // 改；排班表只列今天及以后，更早的排班带不出来就留空（后端按排班日）
        const slot = schedules.find((s) => s.request_id === Number(d.record));
        // 框自己提交（P2-607）：术中所见写超了、单子状态已变时报错写在框里、框不关，填了一整张的术中记录不用重填
        const ok = await spdModal("术中记录", [
          { name: "actual_surgery_name", label: "实际术式", value: req ? req.surgery_name : "", required: true },
          // 术者 / 助手（P2-1307）：原先不录，术者恒取申请单上的拟施术者（缺省即申请人）——住院医提申请、外科医生主刀并录入，
          // 手术记录（居民端、排班表、「查看记录」都当实际术者）署的是住院医。缺省带出申请单上的、按实际改；留空照旧取申请单上的
          { name: "surgeon_name", label: "术者", value: req ? req.surgeon_name : "" },
          { name: "assistants", label: "助手（可空）" },
          { name: "start_at", label: "手术开始时刻（留空按排班日）", placeholder: "YYYY-MM-DD HH:MM",
            value: slot ? `${slot.scheduled_date} ${slot.start_time}` : "" },
          { name: "end_at", label: "手术结束时刻", placeholder: "YYYY-MM-DD HH:MM",
            value: slot ? `${slot.scheduled_date} ${slot.end_time}` : "" },
          { name: "anesthetist_name", label: "麻醉医师" },
          // 麻醉方式与切口等级缺省带出申请时填的（P2-179）：原先麻醉恒缺省第一项、切口恒 II 类
          { name: "anesthesia_type", label: "麻醉方式", type: "select", value: req ? req.anesthesia_type : "general",
            options: Object.entries(ANESTHESIA).map(([value, label]) => ({ value, label })) },
          { name: "incision_level", label: "切口等级", type: "select", value: req ? req.incision_level : "II",
            options: ["I", "II", "III", "IV"].map((x) => ({ value: x, label: `${x} 类` })) },
          { name: "blood_loss_ml", label: "出血量（ml）", type: "number" },
          { name: "findings", label: "术中所见", type: "textarea" },
          { name: "complications", label: "并发症（无则留空）" },
          { name: "outcome", label: "转归", type: "select", value: "好转",
            options: ["治愈", "好转", "未愈", "死亡"].map((x) => ({ value: x, label: x })) },
          { name: "preop_diagnosis", label: "术前诊断（诊断符合率的数据源，可空）" },
          { name: "postop_diagnosis", label: "术后诊断（可空）" },
        ], { submit: (v) => api(`/api/surgery/requests/${d.record}/record`, { method: "POST", body: JSON.stringify(v) }) });
        if (!ok) return;
      } else if (d.view) {
        const rec = await api(`/api/surgery/requests/${d.view}/record`);
        $("#surg-detail").classList.remove("hidden");
        // 助手（P2-1307）：术中记录表单起录得进，出参一直带着，这里一并列出
        $("#surg-detail-body").innerHTML = table(["项", "值"],
          [["实际术式", rec.actual_surgery_name], ["术者", rec.surgeon_name], ["助手", rec.assistants || "—"],
           ["麻醉医师", rec.anesthetist_name],
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
  // 超期清单是全县的，完成却以任务机构的名义写（P2-1313）：原先每行都摆「完成随访」，别家的点了必 403。
  // `can_handle` 由后端按完成接口的同一判据现算，只给能办的行摆，标题写明其中几条能办
  const handleable = overdue.filter((t) => t.can_handle).length;
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML =
    panel("随访完成情况",
      table(["类别", "待随访", "已完成", "已取消", "超期", "完成率"], stats, (s) =>
        `<tr><td>${esc(s.category_name)}</td><td>${s.pending}</td><td>${s.done}</td>
         <td>${s.cancelled}</td><td><span class="tag ${s.overdue ? "red" : "green"}">${s.overdue}</span></td>
         <td>${s.completion_rate_pct}%</td></tr>`)
      + '<p class="desc">完成率分母排除已取消项——取消的任务不该拉低随访绩效。</p>')
    + panel(`超期未随访（${overdue.length}，其中本机构可办 ${handleable}）`,
      table(["ID", "患者", "机构", "类别", "事项", "应随访日", "操作"], overdue, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.patient_name)}</td><td>${esc(t.org_name)}</td><td>${esc(t.category_name)}</td>
         <td>${esc(t.title)}</td><td><span class="tag red">${esc(t.due_date)}</span></td>
         <td>${t.can_handle ? `<button class="btn secondary" data-done="${t.id}">完成随访</button>` : "—"}</td></tr>`))
    + panel(`待随访任务（${pending.length}）`, `
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
             <button class="btn danger" data-cancel="${t.id}">取消</button></td></tr>`)}`);
  $("#fu-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/followups", formJson(e.target, ["patient_id", "org_id"]), "#fu-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.done) {
        // P2-38：随访结果是一段话（症状、用药、下次安排），单行弹窗写不下也换不了行；留空由后端报人话。
        // 框自己提交（P2-607）：写超了、留空、任务已被别人办了时报错写在框里、框不关，写好的随访结果不用重写
        const ok = await spdModal("完成随访", [{ name: "result", label: "随访结果", type: "textarea" }],
          { submit: (form) => api(`/api/followups/${d.done}/complete`, { method: "POST",
            body: JSON.stringify({ result: form.result }) }) });
        if (!ok) return;
      } else if (d.cancel) {
        // 取消原先点一下就生效、没有任何确认：误点一下，该随访的患者就从待随访清单里消失了。
        if (!await spdModal("取消随访任务", [], { intro: "取消后该任务不再出现在待随访清单里，不能恢复。" })) return;
        await api(`/api/followups/${d.cancel}/cancel`, { method: "POST" });
      } else return;
      route();
    } catch (err) { setMsg("#fu-msg", err.message, false); }
  };
}

/* ---------------- 会计核算 ---------------- */

// 科目类别（措辞照抄 AccountSubject.category 列注释；后端 CATEGORY_NAMES 同此）
const ACC_CATEGORIES = { asset: "资产", liability: "负债", net_asset: "净资产", income: "收入", expense: "费用" };

/** 会计页的机构筛选（P2-1438）：只留在内存里、不进存储（同交接班的 `HANDOVER_FILTER`）——看哪一家是这一次查看的条件。
    本页只给管理层（全域角色），选哪家后端都收，不会因换了账号被拒。 */
const ACC_FILTER = { org_id: "" };

async function renderAccounting() {
  $("#page-desc").textContent = "会计科目 + 记账凭证（借贷必平强校验）→ 过账锁定 → 试算平衡表；作废而不删除";
  const thisMonth = localToday().slice(0, 7);
  // 本期草稿续页取全、排在最前（P1-250，同 P2-456 / P2-1310 的 actionableFirst）：凭证清单一页只回最新 50 张（按 id 倒序），
  // 「过账」「明细」只挂在清单行上——一个月过了 50 张，最早录的那批草稿就被挤出窗口，过不了账、看不了明细，月末集中过账时
  // 它们一直进不了试算平衡与合并报表，页面也不提示。草稿是等着过账的待办，过一张少一张，取全有数；整期不封顶（全域账号看的
  // 是全县各家的凭证），已过账 / 已作废的照旧只取最新一页。页长写明而不靠后端缺省：标题按「这一页取没取满」判截断
  const RECENT = 50;
  // 选了机构，凭证清单与试算平衡表都只看这一家（P2-1438）：原先不带机构，试算平衡只看得到全县合计，看不了单独一家的账
  // （指引㉛「独立建账」）。合并报表本来就是各家分列 + 合计，不跟着筛
  const orgQuery = ACC_FILTER.org_id ? `&org_id=${encodeURIComponent(ACC_FILTER.org_id)}` : "";
  const load = (p) => Promise.all([
    api("/api/accounting/subjects"),
    api(`/api/accounting/vouchers?period=${encodeURIComponent(p)}&limit=${RECENT}${orgQuery}`),
    fetchAllPages(api, `/api/accounting/vouchers?period=${encodeURIComponent(p)}&status=draft${orgQuery}`),
    api(`/api/accounting/trial-balance?period=${encodeURIComponent(p)}${orgQuery}`),
    api(`/api/accounting/consolidated-statements?period=${encodeURIComponent(p)}`),
    api("/api/organizations")]);
  let period = localStorage.getItem("medplat_acc_period") || thisMonth;
  let loaded;
  try {
    loaded = await load(period);
  } catch (err) {
    // 存下的期间被后端拒了（P1-62：切换框是自由文本，收严之前存进去的 `2026-9` 之类）。
    // 整页的数据都在这一个 Promise.all 里、切换框也画在它之后——不兜底就是一张
    // 连改正入口都没有的白页。只对 422 回落本月并清掉坏值，别的失败照常抛。
    if (err.status !== 422 || period === thisMonth) throw err;
    localStorage.removeItem("medplat_acc_period");
    period = thisMonth;
    loaded = await load(period);
  }
  const [subjects, recentVouchers, drafts, balance, consolidated, orgs] = loaded;
  const vouchers = actionableFirst(recentVouchers, drafts);
  // 凭证行写明是哪家（P2-1438）：全域账号看的是全县各家的凭证，两家都有「记-1」，作废时分不清。凭证只给机构编号，机构名
  // 从机构清单取，映射不到回显编号；选了机构的，凭证与试算平衡两个面板的标题也写上是哪家
  const orgName = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const orgOf = (id) => orgName[id] || id;
  const scope = ACC_FILTER.org_id ? `${period} ${orgOf(ACC_FILTER.org_id)}` : period;
  // 标题写实数（P1-250）：原先写的是这一页的行数「凭证（50）」——截断后的数，看着像这个月只录了 50 张。最新一页没取满，
  // 整期就都在这里；取满了，草稿是全的，其余只是最新的那几张
  const voucherTitle = recentVouchers.length < RECENT ? `${scope} 凭证（${vouchers.length}）`
    : `${scope} 凭证：本期草稿 ${drafts.length} 张（全列）、其余显示最新 ${vouchers.length - drafts.length} 张`;
  const canSubject = currentRole() === "admin";   // 建科目仅管理员（后端 require_admin）
  const VS = { draft: ["草稿", "orange"], posted: ["已过账", "green"], void: ["已作废", "red"] };
  const options = subjects.map((s) => `<option value="${esc(s.code)}">${esc(s.code)} ${esc(s.name)}</option>`).join("");
  $("#page-body").innerHTML = `
    ${panel("会计期间与机构", `
      <form class="inline" id="acc-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM">
        <select name="org_id"><option value="">全部机构</option>${orgs.map((o) =>
          `<option value="${o.id}"${String(o.id) === ACC_FILTER.org_id ? " selected" : ""}>${esc(o.name)}</option>`).join("")}</select>
        <button>切换</button></form>
      <p class="msg" id="acc-period-msg"></p>
      <p class="desc">选了机构，凭证清单与试算平衡表只看这一家；下方各家分列的叠加汇总不跟着筛。</p>`)}
    ${panel("录入凭证", `
      <form id="voucher-form">
        <div class="inline"><input name="org_id" type="number" placeholder="机构ID" required>
          <input name="voucher_no" placeholder="凭证号" required>
          <input name="voucher_date" placeholder="凭证日期 YYYY-MM-DD" required>
          <input name="summary" placeholder="摘要" style="min-width:220px"></div>
        <div id="entry-rows"></div>
        <div class="inline"><button type="button" id="add-entry" class="btn secondary">加一行分录</button>
          <span id="entry-total" class="desc"></span></div>
        <button>保存凭证（草稿）</button></form>
      <p class="msg" id="acc-msg"></p>`)}
    ${panel(voucherTitle,
      table(["ID", "凭证号", "机构", "日期", "摘要", "借方", "贷方", "状态", "操作"], vouchers, (v) => {
        const ops = v.status === "draft"
          ? `<button class="btn secondary" data-post="${v.id}">过账</button>`
          : (v.status === "posted" ? `<button class="btn danger" data-void="${v.id}">作废</button>` : "—");
        return `<tr><td>${v.id}</td><td>${esc(v.voucher_no)}</td><td>${esc(orgOf(v.org_id))}</td><td>${esc(v.voucher_date)}</td>
          <td>${esc(v.summary)}</td><td>${v.total_debit.toFixed(2)}</td><td>${v.total_credit.toFixed(2)}</td>
          <td>${statusTag(VS, v.status)}</td>
          <td><button class="btn" data-detail="${v.id}">明细</button> ${ops}</td></tr>`;
      }))}
    ${panel(`合并报表（${period}）`, `
      <p class="hint">${esc(consolidated.caliber.note)}</p>
      <div class="cards">
        ${[["合并收入", consolidated.consolidated.income_statement.income],
           ["合并费用", consolidated.consolidated.income_statement.expense],
           ["本期结余", consolidated.consolidated.income_statement.surplus],
           ["资产", consolidated.consolidated.balance_sheet.assets],
           ["负债", consolidated.consolidated.balance_sheet.liabilities],
           ["净资产", consolidated.consolidated.balance_sheet.net_assets]]
          .map(([label, value]) =>
            `<div class="card"><div class="label">${esc(label)}</div><div class="value">${esc(value)}</div></div>`).join("")}
      </div>
      ${table(["机构", "收入", "费用", "结余", "资产", "负债", "净资产"], consolidated.orgs, (o) =>
        `<tr><td>${esc(o.org_name || o.org_id)}</td>
         <td>${o.income_statement.income}</td><td>${o.income_statement.expense}</td>
         <td>${o.income_statement.surplus}</td><td>${o.balance_sheet.assets}</td>
         <td>${o.balance_sheet.liabilities}</td><td>${o.balance_sheet.net_assets}</td></tr>`)}
      ${consolidated.unknown_subject_codes.length
        ? `<p class="msg err">科目表里查不到的编码：${esc(consolidated.unknown_subject_codes.join("、"))}</p>` : ""}
      ${consolidated.voucher_totals.balanced ? "" :
        `<p class="msg err">本期凭证借贷不平，差额 ${consolidated.voucher_totals.difference} 元——报表照出，差额在此标明</p>`}
    `)}
    ${panel(`试算平衡表（${ACC_FILTER.org_id ? `${orgOf(ACC_FILTER.org_id)}，` : ""}仅统计已过账）`, `
      <p class="msg ${balance.balanced ? "ok" : "err"}">借方合计 ${balance.total_debit.toFixed(2)}　贷方合计 ${
        balance.total_credit.toFixed(2)}　${balance.balanced ? "平衡" : "不平衡"}</p>
      ${table(["科目", "名称", "类别", "借方", "贷方"], balance.lines, (l) =>
        `<tr><td>${esc(l.subject_code)}</td><td>${esc(l.subject_name)}</td><td>${esc(l.category_name)}</td>
         <td>${l.debit.toFixed(2)}</td><td>${l.credit.toFixed(2)}</td></tr>`)}`)}
    ${panel(`会计科目（${subjects.length}）`, `
      <p class="desc">种子只放一级科目，明细科目各院口径不一，由管理员按需增建；凭证分录只能选这里有的科目。</p>
      ${canSubject ? `<form class="inline" id="acc-subject-form" style="margin-bottom:8px">
        <input name="code" placeholder="科目编码" required style="width:110px">
        <input name="name" placeholder="科目名称" required>
        <select name="category">${Object.entries(ACC_CATEGORIES).map(([k, v]) =>
          `<option value="${k}">${v}</option>`).join("")}</select>
        <select name="direction"><option value="debit">余额在借方（资产 / 费用类）</option>
          <option value="credit">余额在贷方（负债 / 净资产 / 收入类、累计折旧）</option></select>
        <button>新增科目</button>
      </form><p class="msg" id="acc-subject-msg"></p>` : ""}
      <details><summary>科目表</summary>
      ${table(["编码", "名称", "类别", "余额方向"], subjects, (s) =>
        `<tr><td>${esc(s.code)}</td><td>${esc(s.name)}</td><td>${esc(ACC_CATEGORIES[s.category] || s.category)}</td>
         <td>${s.direction === "credit" ? "贷" : "借"}</td></tr>`)}</details>`)}
    <div class="panel hidden" id="voucher-detail"><h3>凭证明细</h3><div id="voucher-detail-body"></div></div>`;

  const addEntryRow = () => {
    const row = document.createElement("div");
    row.className = "inline entry-row";
    row.innerHTML = `<select class="e-subject">${options}</select>
      <input class="e-summary" placeholder="分录摘要">
      <input class="e-debit" type="number" step="0.01" min="0" placeholder="借方">
      <input class="e-credit" type="number" step="0.01" min="0" placeholder="贷方">`;
    $("#entry-rows").appendChild(row);
    row.oninput = refreshTotal;
  };
  // 分录行只从这一处取（P2-1442）：实时合计与提交原先各取各的——合计把负数算进去，提交却只留借或贷大于 0 的行，只填了负数
  // 的分录行被悄悄丢掉，存下的凭证与屏幕上「✓ 平衡」的不是同一张（借 1000 / 贷 1000 / 借 −200 / 贷 −200：屏幕 800 / 800，
  // 存成 1000 / 1000；全是负数时行被丢光，后端只报「entries 至少 2 项」）。借贷都没填的空行不算；填了负数的照留，下面拦住说清
  const entryRows = () => [...document.querySelectorAll(".entry-row")].map((r) => ({
    subject_code: r.querySelector(".e-subject").value,
    summary: r.querySelector(".e-summary").value,
    debit: Number(r.querySelector(".e-debit").value || 0),
    credit: Number(r.querySelector(".e-credit").value || 0),
  })).filter((x) => x.debit !== 0 || x.credit !== 0);
  // 分录金额后端只收非负（accounting.EntryIn 的 ge=0）：冲销走作废，不收红字负数
  const NEGATIVE_AMOUNT = "金额不能为负，红字更正请走作废后重录";
  const hasNegative = (rows) => rows.some((x) => x.debit < 0 || x.credit < 0);
  const refreshTotal = () => {
    const rows = entryRows();
    const debit = rows.reduce((sum, x) => sum + x.debit, 0);
    const credit = rows.reduce((sum, x) => sum + x.credit, 0);
    const negative = hasNegative(rows);
    const balanced = !negative && Math.abs(debit - credit) < 0.005;
    const el = $("#entry-total");
    // 借贷是否相等实时提示，避免提交后才被 422 打回
    el.textContent = `借方合计 ${debit.toFixed(2)}　贷方合计 ${credit.toFixed(2)}　${
      negative ? `✗ ${NEGATIVE_AMOUNT}` : balanced ? "✓ 平衡" : "✗ 不平"}`;
    el.style.color = balanced ? "#1e7e34" : "#c0392b";
  };
  addEntryRow(); addEntryRow(); refreshTotal();
  $("#add-entry").onclick = addEntryRow;
  $("#acc-period").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const value = String(f.get("period") || "").trim();
    // 先让后端判这个期间合不合法，合法才记住：校验只有后端一份（require_month），
    // 前端不另抄一遍规则；坏值存进去，下次进页面就要走上面那条回落。
    try {
      await api(`/api/accounting/trial-balance?period=${encodeURIComponent(value)}`);
    } catch (err) { setMsg("#acc-period-msg", err.message, false); return; }
    localStorage.setItem("medplat_acc_period", value);
    ACC_FILTER.org_id = String(f.get("org_id") ?? "");   // 机构筛选（P2-1438），缺省全部
    route();
  };
  // 科目原先只能在凭证下拉里看、不能建（P2-93 动词级孤儿）：建科目的接口一直在，明细科目只能靠接口调用方
  const subjectForm = $("#acc-subject-form");
  if (subjectForm) subjectForm.onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/accounting/subjects", formJson(e.target), "#acc-subject-msg");
  };
  $("#voucher-form").onsubmit = async (e) => {
    e.preventDefault();
    const entries = entryRows();   // 与实时合计同一组行（P2-1442）
    if (hasNegative(entries)) return setMsg("#acc-msg", NEGATIVE_AMOUNT, false);   // 提交前说清，不再悄悄丢行
    const head = formJson(e.target, ["org_id"]);
    postAction("/api/accounting/vouchers", { ...head, period, entries }, "#acc-msg");
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.post) await api(`/api/accounting/vouchers/${d.post}/post`, { method: "POST" });
      else if (d.void) {
        // 作废要写明原因（P2-522）：原先 confirm() 一下就作废，谁作废的、为什么都不留
        const form = await spdModal("作废凭证", [
          { name: "reason", label: "作废原因", required: true, placeholder: "如：科目记错，已另开更正凭证" },
        ], { intro: "作废后该凭证不再计入试算平衡与报表，不能恢复；作废人、时间与原因留在凭证上可查。" });
        if (!form) return;
        await api(`/api/accounting/vouchers/${d.void}/void`, { method: "POST", body: JSON.stringify({ reason: form.reason }) });
      }
      else if (d.detail) {
        const v = await api(`/api/accounting/vouchers/${d.detail}`);
        $("#voucher-detail").classList.remove("hidden");
        const trail = v.status === "void"
          ? `<p class="desc">已作废：${esc(v.voided_by_name || "—")} ${esc((v.voided_at || "").replace("T", " ").slice(0, 16))}，原因：${esc(v.void_reason || "（未写明）")}</p>`
          : "";
        $("#voucher-detail-body").innerHTML = trail + table(["科目", "摘要", "借方", "贷方"], v.entries, (x) =>
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
  const thisMonth = localToday().slice(0, 7);
  const orgId = Number(localStorage.getItem("medplat_cost_org") || 0);
  const load = (p) => Promise.all([
    api("/api/mgmt/departments"), api(`/api/cost/departments?period=${encodeURIComponent(p)}`),
    api("/api/cost/allocation-rules"), api("/api/organizations")]);
  let period = localStorage.getItem("medplat_cost_period") || thisMonth;
  let loaded;
  try {
    loaded = await load(period);
  } catch (err) {
    // 与会计页同一个坑（P1-62）：切换框是自由文本、存进 localStorage 不校验，存下 `2026/09`
    // 之后整页那个 Promise.all 422，切换框又画在它之后——一张连改正入口都没有的白页。
    // 只对 422 回落本月并清掉坏值，别的失败照常抛。
    if (err.status !== 422 || period === thisMonth) throw err;
    localStorage.removeItem("medplat_cost_period");
    period = thisMonth;
    loaded = await load(period);
  }
  const [depts, costs, rules, orgs] = loaded;
  const unit = orgId ? await api(`/api/cost/unit-cost?period=${encodeURIComponent(period)}&org_id=${orgId}`).catch(() => null) : null;
  const deptName = Object.fromEntries(depts.map((d) => [d.id, d.name]));
  // 写明是哪家（P2-1438，同 P2-1402 手术间的「机构名 · 名称」）：本页给管理层看，科室、分摊规则、科室成本都是全县各家混在
  // 一起的，原先哪里都不写机构——两家都有「内科」，归集下拉里是一模一样的两条，选错了成本就记到别家（接口按所选科室的机构
  // 照收 201）。科室清单与规则只给机构编号，机构名从机构清单取，映射不到回显编号；科室成本行后端本来就给 org_name
  const orgName = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const orgOf = (id) => orgName[id] || id;
  const deptLabel = (d) => `${orgOf(d.org_id)} · ${d.name}`;
  // ADR-0009 第五批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 单位成本面板按有无 unit 条件渲染；"期间"进标题时不再自己 esc()——组件转义标题。
  $("#page-body").innerHTML = `
    ${panel("期间与机构", `
      <form class="inline" id="cost-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM">
        <input name="org_id" type="number" value="${orgId || ""}" placeholder="机构ID（算单位成本）"><button>切换</button></form>
      <p class="msg" id="cost-period-msg"></p>`)}
    ${unit ? panel("单位成本", `${
      table(["总成本", "门诊人次", "占用床日", "门诊成本", "住院成本", "诊次成本", "床日成本"], [unit], (u) =>
        `<tr><td>${u.total_cost.toFixed(2)}</td><td>${u.outpatient_visits}</td><td>${u.occupied_bed_days}</td>
         <td>${u.outpatient_cost.toFixed(2)}</td><td>${u.inpatient_cost.toFixed(2)}</td>
         <td><b>${u.cost_per_visit.toFixed(2)}</b></td><td><b>${u.cost_per_bed_day.toFixed(2)}</b></td></tr>`)}
      <p class="desc">床日成本分母是实际占用床日，不是床位数×天数——后者是可用床日，混用会把成本算低。</p>`) : ""}
    ${panel("归集科室直接成本", `
      <form class="inline" id="cost-form"><select name="dept_id">${
        depts.map((d) => `<option value="${d.id}">${esc(deptLabel(d))}（${esc(d.category_name)}）</option>`).join("")}</select>
        <input name="period" value="${esc(period)}" placeholder="YYYY-MM" required>
        <select name="cost_type">${Object.entries(COST_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="amount" type="number" step="0.01" placeholder="金额" required><button>归集</button></form>
      <p class="msg" id="cost-msg"></p>
      <p class="desc">同科室同期间同成本项重复提交按覆盖处理——月末成本反复调整是常态。</p>`)}
    ${panel("分摊规则", `
      <form class="inline" id="alloc-form">
        <select name="from_dept_id">${depts.map((d) => `<option value="${d.id}">${esc(deptLabel(d))}</option>`).join("")}</select>
        →<select name="to_dept_id">${depts.map((d) => `<option value="${d.id}">${esc(deptLabel(d))}</option>`).join("")}</select>
        <input name="ratio_pct" type="number" step="0.01" placeholder="比例%" required><button>新增规则</button></form>
      ${table(["机构", "来源科室", "目标科室", "比例", "操作"], rules, (r) =>
        `<tr><td>${esc(orgOf(r.org_id))}</td><td>${esc(deptName[r.from_dept_id] || r.from_dept_id)}</td>
         <td>${esc(deptName[r.to_dept_id] || r.to_dept_id)}</td><td>${r.ratio_pct}%</td>
         <td><button class="btn secondary" data-alloc-edit="${r.id}" data-ratio="${esc(r.ratio_pct)}">改比例</button>
             <button class="btn danger" data-alloc-del="${r.id}">删除</button></td></tr>`)}
      <p class="desc">同一来源科室的比例合计不能超过 100%；不足 100% 的部分在科室成本里记为「未分摊」。
        规则不分期间：改比例、删规则后，任一期的科室成本汇总都按现行规则重算。科室填错了就删掉重建。</p>`)}
    ${panel(`${period} 科室成本`, `${
      table(["机构", "科室", "类别", "直接成本", "分摊转入", "分摊转出", "总成本", "未分摊"], costs, (c) =>
        // 类别列写后端给的文案（P2-1439）：原先印 dept_category 英文码 clinical / medtech / admin
        `<tr><td>${esc(c.org_name || "—")}</td><td>${esc(c.dept_name)}</td><td>${esc(c.dept_category_name)}</td><td>${c.direct_cost.toFixed(2)}</td>
         <td>${c.allocated_in.toFixed(2)}</td><td>${c.allocated_out.toFixed(2)}</td>
         <td><b>${c.total_cost.toFixed(2)}</b></td>
         <td>${c.unallocated_ratio_amount ? `<span class="tag orange">${c.unallocated_ratio_amount.toFixed(2)}</span>` : "—"}</td></tr>`)}
      ${costs.length ? barChart(costs.map((c) => [c.dept_name, c.total_cost])) : ""}`)}`;
  $("#cost-period").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const value = String(f.get("period") || "").trim();
    // 先让后端判这个期间合不合法，合法才记住（校验只有后端一份，前端不另抄规则）
    try {
      await api(`/api/cost/departments?period=${encodeURIComponent(value)}`);
    } catch (err) { setMsg("#cost-period-msg", err.message, false); return; }
    localStorage.setItem("medplat_cost_period", value);
    localStorage.setItem("medplat_cost_org", f.get("org_id") || "");
    route();
  };
  $("#cost-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/cost/departments", formJson(e.target, ["dept_id", "amount"]), "#cost-msg"); };
  $("#alloc-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/cost/allocation-rules", formJson(e.target, ["from_dept_id", "to_dept_id", "ratio_pct"]), "#cost-msg"); };
  // P1-116：规则原先只能建、不能改也不能删——比例或科室填错就每一期都照错的分
  $("#page-body").onclick = async (e) => {
    const edit = e.target.closest("[data-alloc-edit]");
    if (edit) {
      const form = await spdModal("改分摊比例", [
        { name: "ratio_pct", label: "比例（%，同一来源科室合计不超过 100）", type: "number",
          value: edit.dataset.ratio, required: true },
      ]);
      if (!form) return;
      return postAction(`/api/cost/allocation-rules/${edit.dataset.allocEdit}`,
        { ratio_pct: Number(form.ratio_pct) }, "#cost-msg", "PATCH");
    }
    const del = e.target.closest("[data-alloc-del]");
    if (del) {
      if (!confirm("删除这条分摊规则？删除后任一期的科室成本汇总都按剩下的规则重算。")) return;
      return postAction(`/api/cost/allocation-rules/${del.dataset.allocDel}`, null, "#cost-msg", "DELETE");
    }
  };
}

/* ---------------- 物资采购与高值耗材 ---------------- */

/* 采购流程的三列金额（P2-1362）：审批人点「审批」时得看得到这笔要花多少——清单行本来就带预估单价与合同金额，原先表里一个
   都不画。预估单价没填（0）的，单价与总额显示 —；合同还没签的，合同金额显示 —（签了 0 元的照写 0.00）。预估总额 = 数量 ×
   预估单价。金额照本页会计、成本两页的两位小数写法，调用处一律 esc()。 */
function materialPurchaseAmounts(p) {
  const yuan = (v) => Number(v).toFixed(2);
  return {
    price: p.estimated_price ? yuan(p.estimated_price) : "—",
    total: p.estimated_price ? yuan(p.quantity * p.estimated_price) : "—",
    contract: p.contract_no ? yuan(p.contract_amount) : "—",
  };
}

async function renderMaterials() {
  $("#page-desc").textContent = "非药品物资：申请 → 审批 → 合同 → 验收（自动入库流水）；高值耗材一物一码正反向追溯";
  // 在库耗材按状态续页取全、排在台账最前（P2-1358，同 P2-456 的 actionableFirst）：台账只取最新一页（100 件、按登记倒序），
  // 「使用登记」只挂在这一页上——更早入库、先到效期的在库耗材被后登记的挤出窗口，页面上就登记不了。
  // 采购流程同一写法（P2-1506）：待审批、已审批（待签合同）、已签合同（待验收）三态按状态续页取全、排在最前——清单缺省只回
  // 最新 50 张（按编号倒序），审批、签合同、验收三个按钮只挂在这张表上，第 51 张起最早那批没办完的单子被挤出窗口，页面上就
  // 办不了。已验收 / 已取消的照旧只取最新一页
  const [recentPurchases, requested, approved, contracted, recentConsumables, inStock] = await Promise.all([
    api("/api/materials/purchases"), fetchAllPages(api, "/api/materials/purchases?status=requested"),
    fetchAllPages(api, "/api/materials/purchases?status=approved"),
    fetchAllPages(api, "/api/materials/purchases?status=contracted"), api("/api/materials/consumables"),
    fetchAllPages(api, "/api/materials/consumables?status=in_stock")]);
  const purchases = actionableFirst(recentPurchases, requested, approved, contracted);
  const consumables = actionableFirst(recentConsumables, inStock);
  // 标题写实数（P2-1506，同 P1-250 / P2-1478）：原先写的是这一页的行数「采购流程（50）」——截断后的数，看着像一共只有 50 张。
  // 最新一页（接口缺省 50 张）没取满，全部单子都在表里；取满了，三种待办是全的，其余只是最新的那几张
  const openPurchases = requested.length + approved.length + contracted.length;
  const purchaseCount = recentPurchases.length < 50 ? `${purchases.length}`
    : `待审批 / 待签合同 / 待验收 ${openPurchases} 张排在最前、其余只列最新 ${purchases.length - openPurchases} 张`;
  // 审批（批准 / 驳回）限管理层（后端 require_roles("director")，管理员放行）：别的角色摆出按钮只会点出一次 403
  const canApprove = ["director", "admin"].includes(currentRole());
  // ADR-0009 第三批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = `
    ${panel("提出采购申请", `
      <form class="inline" id="mp-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="dept_id" type="number" placeholder="科室ID">
        <input name="item_name" placeholder="物资名称" required><input name="spec" placeholder="规格">
        <input name="unit" placeholder="单位" value="件"><input name="quantity" type="number" placeholder="数量" required>
        <input name="estimated_price" type="number" step="0.01" placeholder="预估单价">
        <input name="reason" placeholder="事由"><button>提交申请</button></form>
      <p class="msg" id="mat-msg"></p>`)}
    ${panel(`采购流程（${purchaseCount}）`,
      table(["ID", "物资", "规格", "数量", "预估单价", "预估总额", "状态", "合同", "合同金额", "已验收", "操作"], purchases, (p) => {
        let ops = "—";
        // 驳回原先没有入口（P2-425）：接口收 approved=false 早就置「已取消」，页面只给「审批」，不该买的申请只能一直挂着。
        // 本人提的申请不摆这两个按钮（P2-1506）：审批接口对申请人本人 403「不得审批本人提出的采购申请」，原先照样摆、点了必
        // 403；是不是本人取清单行的 requested_by_me（页面不知道自己是谁，由后端按同一判据现算）
        if (p.status === "requested") ops = !canApprove ? "待管理层审批"
          : p.requested_by_me ? "本人提出，待其他管理层审批"
          : `<button class="btn secondary" data-approve="${p.id}">审批</button>
             <button class="btn danger" data-reject="${p.id}">驳回</button>`;
        else if (p.status === "approved") ops = `<button class="btn secondary" data-contract="${p.id}">签合同</button>`;
        else if (p.status === "contracted") ops = `<button class="btn secondary" data-receive="${p.id}">验收</button>`;
        const amt = materialPurchaseAmounts(p);
        return `<tr><td>${p.id}</td><td>${esc(p.item_name)}</td><td>${esc(p.spec)}</td><td>${p.quantity}${esc(p.unit)}</td>
          <td>${esc(amt.price)}</td><td>${esc(amt.total)}</td>
          <td>${statusTag(PURCHASE_STATUS, p.status)}</td><td>${esc(p.contract_no || "—")}</td><td>${esc(amt.contract)}</td>
          <td>${p.received_quantity || "—"}</td><td>${ops}</td></tr>`;
      }))}
    ${panel("高值耗材登记（一物一码）", `
      <form class="inline" id="hv-form"><input name="barcode" placeholder="条码" required>
        <input name="name" placeholder="耗材名称" required><input name="spec" placeholder="规格">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="supplier_id" type="number" placeholder="供应商ID"><input name="batch_no" placeholder="批号">
        <input name="expire_date" placeholder="效期 YYYY-MM-DD">
        <input name="unit_price" type="number" step="0.01" placeholder="单价"><button>入库登记</button></form>
      <form class="inline" id="trace-form"><input name="barcode" placeholder="按条码追溯" required><button>追溯</button></form>
      <div id="trace-result"></div>`)}
    ${panel(`耗材台账（${consumables.length}）`,
      table(["条码", "名称", "批号", "效期", "状态", "用于患者", "关联手术", "操作"], consumables, (c) => {
        return `<tr><td>${esc(c.barcode)}</td><td>${esc(c.name)}</td><td>${esc(c.batch_no)}</td>
          <td>${esc(c.expire_date)}</td><td>${statusTag(CONSUMABLE_STATUS, c.status)}</td>
          <td>${esc(c.used_patient_name || "—")}</td><td>${esc(c.used_surgery_name || "—")}</td>
          <td>${c.status === "in_stock" ? `<button class="btn secondary" data-use="${esc(c.barcode)}">使用登记</button>` : "—"}</td></tr>`;
      }))}`;
  $("#mp-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/materials/purchases", formJson(e.target, ["org_id", "dept_id", "quantity", "estimated_price"]), "#mat-msg"); };
  $("#hv-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/materials/consumables", formJson(e.target, ["org_id", "supplier_id", "unit_price"]), "#mat-msg"); };
  $("#trace-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const c = await api(`/api/materials/consumables/trace/${encodeURIComponent(new FormData(e.target).get("barcode"))}`);
      $("#trace-result").innerHTML = table(["项", "值"],
        [["条码", c.barcode], ["名称", c.name], ["规格", c.spec], ["供应商", c.supplier_name],
         ["批号", c.batch_no], ["效期", c.expire_date], ["状态", CONSUMABLE_STATUS[c.status][0]],
         ["用于患者", c.used_patient_name || "—"], ["关联手术", c.used_surgery_name || "—"], ["使用时间", c.used_at || "—"]],
        ([k, v]) => `<tr><td>${esc(k)}</td><td>${esc(v)}</td></tr>`);
    } catch (err) { setMsg("#mat-msg", err.message, false); }
  };
  // P2-38：签合同三连问（手输供应商 ID）、验收两连问、使用登记两连问换成页内表单。
  // 供应商从在用清单里选——后端只认在用的，停用的摆出来只会点出一次 404。
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.approve) await api(`/api/materials/purchases/${d.approve}/approve`,
        { method: "POST", body: JSON.stringify({ approved: true }) });
      else if (d.reject) {
        const p = purchases.find((x) => x.id === Number(d.reject));
        const ok = await spdModal(`驳回采购申请：${p ? `${p.item_name}（${p.quantity}${p.unit}）` : d.reject}`, [],
          { intro: "驳回后这条申请即作废（已取消），不能恢复；要买得重新提出申请。" });
        if (!ok) return;
        await api(`/api/materials/purchases/${d.reject}/approve`,
          { method: "POST", body: JSON.stringify({ approved: false }) });
      }
      else if (d.contract) {
        const p = purchases.find((x) => x.id === Number(d.contract));
        const suppliers = (await api("/api/pharmacy/suppliers")).filter((s) => s.active);
        if (!suppliers.length) return setMsg("#mat-msg", "还没有在用的供应商，请先到「采购与盘点」页建档", false);
        const v = await spdModal(`签合同：${p ? `${p.item_name}（${p.quantity}${p.unit}）` : d.contract}`, [
          { name: "supplier_id", label: "供应商", type: "select",
            options: suppliers.map((s) => ({ value: s.id, label: s.name })) },
          { name: "contract_no", label: "合同号", required: true },
          { name: "contract_amount", label: "合同金额（元）", type: "number", required: true },
        ]);
        if (!v) return;
        await api(`/api/materials/purchases/${d.contract}/contract`, { method: "POST",
          body: JSON.stringify({ ...v, supplier_id: Number(v.supplier_id) }) });
      } else if (d.receive) {
        const p = purchases.find((x) => x.id === Number(d.receive));
        // 框自己提交（P2-607）：验收数量超了采购数量、备注写超了时报错写在框里、框不关，填的都在
        const ok = await spdModal(`到货验收：${p ? p.item_name : d.receive}`, [
          { name: "received_quantity", label: `验收数量（不得超过采购数量${p ? ` ${p.quantity}` : ""}）`,
            type: "number", value: p ? p.quantity : "", required: true },
          { name: "note", label: "验收备注", type: "textarea" },
        ], { submit: (v) => api(`/api/materials/purchases/${d.receive}/receive`, { method: "POST", body: JSON.stringify(v) }) });
        if (!ok) return;
      } else if (d.use) {
        const c = consumables.find((x) => x.barcode === d.use);
        const v = await spdModal(
          `使用登记：${c ? `${c.name}（条码 ${c.barcode}，效期 ${c.expire_date || "未采集"}）` : d.use}`, [
            { name: "patient_id", label: "患者ID", type: "number", required: true },
            { name: "surgery_id", label: "关联手术申请ID（可空；须是该患者的手术）", type: "number" },
          ]);
        if (!v) return;
        await api(`/api/materials/consumables/${encodeURIComponent(d.use)}/use`, { method: "POST",
          body: JSON.stringify({ patient_id: v.patient_id, surgery_id: v.surgery_id || null }) });
      } else return;
      route();
    } catch (err) { setMsg("#mat-msg", err.message, false); }
  };
}

/* ---------------- 决策指标扩展 ---------------- */

async function renderAnalytics() {
  $("#page-desc").textContent = "县域就诊率与就医流向 / 运行效率 / 自定义绩效公式与综合报告";
  const thisMonth = localToday().slice(0, 7);
  // 就医流向按所选期间取（P2-1082）：原先不带 start / end，卡片是建库以来的累计，标题又不写期间——选 2026-09 看到的
  // 县域就诊率混着去年的县外就诊。接口的 end 不含当天，传次月 1 日
  const flowRange = (p) => {
    const [y, m] = p.split("-").map(Number);
    const next = m === 12 ? `${y + 1}-01` : `${y}-${String(m + 1).padStart(2, "0")}`;
    return `start=${encodeURIComponent(`${p}-01`)}&end=${encodeURIComponent(`${next}-01`)}`;
  };
  const load = (p) => Promise.all([
    api(`/api/analytics/patient-flow?${flowRange(p)}`), api(`/api/analytics/efficiency?period=${encodeURIComponent(p)}`),
    api("/api/analytics/formulas"), api("/api/analytics/formula-variables")]);
  let period = localStorage.getItem("medplat_ana_period") || thisMonth;
  let loaded;
  try {
    loaded = await load(period);
  } catch (err) {
    // 与会计 / 成本页同一个坑（P1-62，本页 P2-586）：切换框是自由文本，存下 `2026` / `2026/09` 之后整页那个
    // Promise.all 422，切换框又画在它之后——每次进来都只剩一行报错、连改正入口都没有。只对 422 回落本月并清掉坏值
    if (err.status !== 422 || period === thisMonth) throw err;
    localStorage.removeItem("medplat_ana_period");
    period = thisMonth;
    loaded = await load(period);
  }
  const [flow, eff, formulas, vars] = loaded;
  const report = currentRole() === "admin" || currentRole() === "director"
    ? await api(`/api/analytics/performance-report?period=${encodeURIComponent(period)}`).catch(() => null) : null;
  $("#page-body").innerHTML = `
    ${panel("期间", `
      <form class="inline" id="ana-period"><input name="period" value="${esc(period)}" placeholder="YYYY-MM"><button>切换</button></form>
      <p class="msg" id="ana-period-msg"></p>`)}
    ${panel(`就医流向（${period}）`, `
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
      <p class="msg" id="ana-msg"></p>`)}
    ${panel(`运行效率（${period}）`,
      table(["机构", "床位", "出院", "占用床日", "平均住院日", "床位周转", "使用率", "诊疗量", "医师", "日均担负"], eff, (r) =>
        `<tr><td>${esc(r.org_name)}</td><td>${r.beds}</td><td>${r.discharges}</td><td>${r.occupied_bed_days}</td>
         <td>${r.avg_length_of_stay}</td><td>${r.bed_turnover}</td><td>${r.bed_occupancy_rate_pct}%</td>
         <td>${r.visits}</td><td>${r.doctors}</td><td>${r.visits_per_doctor_per_day}</td></tr>`))}
    ${panel("自定义绩效公式", `
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
         <td>${f.active ? `<button class="btn danger" data-off="${esc(f.key)}">停用</button>` : "—"}</td></tr>`)}`)}
    ${report ? panel(`期末综合绩效报告（${period}）`, `
      <p class="desc">⚠️ 口径提示：本表的「加权得分」由上面的<b>自定义公式</b>算出，
        公式与权重管理员可随时增删改，同一机构换套公式就是另一个分数。
        它与「绩效考核」页的<b>机构绩效评分不是同一套口径，两个分数不可直接比较</b>，
        也不要相互印证。对上考核用「绩效考核」页。</p>${
      table(["排名", "机构", "层级", ...report.orgs[0] ? report.orgs[0].items.map((i) => i.name) : [], "加权得分"],
        report.orgs, (o, idx) =>
        `<tr><td>${idx + 1}</td><td>${esc(o.org_name)}</td><td>${esc(o.level_name)}</td>
         ${o.items.map((i) => `<td>${i.value === null ? `<span class="tag red" title="${esc(i.error || "")}">错误</span>` : i.value}</td>`).join("")}
         <td><b>${o.weighted_score}</b></td></tr>`)}`) : ""}`;
  $("#ana-period").onsubmit = async (e) => {
    e.preventDefault();
    const value = String(new FormData(e.target).get("period") || "").trim();
    // 先让后端判这个期间合不合法，合法才记住（与会计 / 成本页同一句）：校验只有后端一份，坏值不进 localStorage
    try {
      await api(`/api/analytics/efficiency?period=${encodeURIComponent(value)}`);
    } catch (err) { setMsg("#ana-period-msg", err.message, false); return; }
    localStorage.setItem("medplat_ana_period", value); route();
  };
  $("#ob-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/analytics/outbound-visits",
      formJson(e.target, ["patient_id", "total_amount", "insurance_pay", "referral_id"]), "#ana-msg"); };
  $("#formula-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/analytics/formulas", formJson(e.target, ["weight"]), "#ana-msg"); };
  $("#page-body").onclick = async (e) => {
    if (!e.target.dataset.off) return;
    // P2-43：原先点一下就停用；停用的公式页面上没有启用入口，考核口径随之改变
    if (!await spdModal("停用考核公式", [], { intro: "停用后该公式不再参与计分，页面上不能重新启用。" })) return;
    try { await api(`/api/analytics/formulas/${encodeURIComponent(e.target.dataset.off)}`, { method: "DELETE" }); route(); }
    catch (err) { setMsg("#ana-msg", err.message, false); }
  };
}

/* ---------------- 统一规则引擎 ---------------- */

async function renderRules() {
  $("#page-desc").textContent = "条件 DSL 新增规则免改代码；目录并入四套既有规则（engine 列标明执行路径）";
  const [catalog, domains, rules] = await Promise.all([
    api("/api/rules/catalog"), api("/api/rules/domains"), api("/api/rules")]);
  $("#page-body").innerHTML = `
    ${panel(`全平台规则总目录（${catalog.total}）`, `
      <div class="cards">${Object.entries(catalog.by_source).map(([k, v]) =>
        `<div class="card"><span class="k">${esc(k)}</span><b>${v}</b></div>`).join("")}</div>
      ${table(["来源", "执行引擎", "域", "编码", "名称", "定义", "状态"], catalog.entries, (e) =>
        `<tr><td>${esc(e.source)}</td>
         <td><span class="tag ${e.engine === "unified" ? "green" : ""}">${e.engine === "unified" ? "统一引擎" : "既有实现"}</span></td>
         <td>${esc(e.domain)}</td><td>${esc(e.key)}</td><td>${esc(e.name)}</td>
         <td><code>${esc(e.detail)}</code></td>
         <td>${e.active ? "启用" : "停用"}</td></tr>`)}
      ${catalog.entries.length < catalog.total ? `<p class="desc">共 ${catalog.total} 条，表中列出 ${catalog.entries.length} 条：
        用药规则只列按药品编码排的前 500 条，上面的分来源计数是全量。</p>` : ""}
      <p class="desc">目录已统一，执行路径尚未统一——engine 列如实标出，不含糊其辞。</p>`)}
    ${panel("新增统一规则（admin）", `
      <form class="inline" id="rule-form"><input name="key" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="domain">${domains.map((d) => `<option value="${esc(d.domain)}">${esc(d.domain)}</option>`).join("")}</select>
        <input name="condition" placeholder="条件，如 daily_dose > max_daily_dose and age >= 65" required style="min-width:340px">
        <input name="message" placeholder="命中提示">
        <select name="severity"><option value="info">提示</option><option value="warning" selected>警告</option><option value="error">拦截</option></select>
        <input name="deduct_points" type="number" placeholder="扣分"><button>新增</button></form>
      <p class="msg" id="rule-msg"></p>
      ${domains.map((d) => `<p class="desc"><b>${esc(d.domain)}</b>：${
        d.variables.map((v) => `<code>${esc(v.name)}</code>(${esc(v.type)})`).join("　")}</p>`).join("")}
      ${table(["编码", "名称", "域", "条件", "严重度", "扣分", "状态", "操作"], rules, (r) => {
        return `<tr><td>${esc(r.key)}</td><td>${esc(r.name)}</td><td>${esc(r.domain)}</td>
          <td><code>${esc(r.condition)}</code></td><td>${statusTag(SEVERITY, r.severity)}</td>
          <td>${r.deduct_points}</td><td>${r.active ? "启用" : "停用"}</td>
          <td>${r.active ? `<button class="btn danger" data-off="${esc(r.key)}">停用</button>` : "—"}</td></tr>`;
      })}`)}
    ${panel("在线试算", `
      <form id="eval-form"><div class="inline">
        <select name="domain">${domains.map((d) => `<option value="${esc(d.domain)}">${esc(d.domain)}</option>`).join("")}</select>
        <input name="variables" placeholder='变量 JSON，如 {"daily_dose": 3000, "age": 78}' required style="min-width:420px">
        <button>试算</button></div></form>
      <div id="eval-result"></div>`)}`;
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
    // P2-43：原先点一下就停用；停用的规则页面上没有启用入口，依赖它的判定随之失效
    if (!await spdModal("停用规则", [], { intro: "停用后该规则不再参与求值，页面上不能重新启用。" })) return;
    try { await api(`/api/rules/${encodeURIComponent(e.target.dataset.off)}`, { method: "DELETE" }); route(); }
    catch (err) { setMsg("#rule-msg", err.message, false); }
  };
}

/* ---------------- 流程引擎与统一申请单中心 ---------------- */

/* 流程实例面板的筛选（P2-1474）：原先只取 status=running——办完或被终止的单子整个从页面消失，谁批的、意见、终止原因都
   打不开（引擎不回写业务表，这一页是唯一看审批结论的地方），发起人也查不到自己的单子批没批。状态缺省仍是「流转中」（与原先
   一致），可换已完成 / 已终止 / 全部；「只看我发起的」送接口的 mine=true。改筛选只重画实例表、不整页 route()——画布上没存的
   节点、表单里填了一半的不会被冲掉。筛选只留在内存里、不进存储（同 JOB_RUN_FILTER）。 */
const WF_INSTANCE_FILTER = { status: "running", mine: "" };

function wfInstanceQuery() {
  const query = new URLSearchParams(Object.entries(WF_INSTANCE_FILTER).filter(([, v]) => v));
  return query.toString() ? `?${query}` : "";
}

/* 实例表：每一行都给「流转记录」；状态文案取后端的 status_name，发起人、发起时间是 P2-1474 补的三键里的另两个；
   「流程」印流程名（P2-1475，原先印 definition_key），定义不在了回显编码 */
function wfInstanceTable(instances) {
  return `<p class="desc">列出 ${instances.length} 条（新的在前）</p>`
    + table(["ID", "流程", "事项", "发起人", "发起时间", "当前节点", "状态", "更新时间", "操作"], instances, (i) =>
      `<tr><td>${i.id}</td><td>${esc(i.definition_name || i.definition_key)}</td><td>${esc(i.title)}</td>
       <td>${esc(i.created_by_name)}</td><td>${esc(i.created_at.slice(0, 16).replace("T", " "))}</td>
       <td>${esc(i.current_node_name || i.current_node)}</td><td>${esc(i.status_name)}</td>
       <td>${esc(i.updated_at.slice(0, 16).replace("T", " "))}</td>
       <td><button class="btn" data-history="${i.id}">流转记录</button></td></tr>`);
}

async function renderWorkflows() {
  $("#page-desc").textContent = "流程定义 JSON 化、节点角色守卫、流转留痕；我的待办按角色过滤";
  const [definitions, instances, tasks] = await Promise.all([
    api("/api/workflows/definitions"), api(`/api/workflows/instances${wfInstanceQuery()}`),
    api("/api/workflows/my-tasks")]);
  $("#page-body").innerHTML = `
    ${panel(`我的待办（${tasks.count}）`, `${
      table(["实例", "流程", "机构", "事项", "当前节点", "需要角色", "操作"], tasks.tasks, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.definition_name || t.definition_key)}</td>
         <td>${t.org_id == null ? "全县流程" : esc(t.org_name || t.org_id)}</td><td>${esc(t.title)}</td>
         <td>${esc(t.current_node_name || t.current_node)}</td><td>${esc(t.current_node_role || "任意")}</td>
         <td><button class="btn secondary" data-advance="${t.id}">推进</button>
             <button class="btn danger" data-cancel="${t.id}">终止</button></td></tr>`)}
      <p class="msg" id="wf-msg"></p>`)}
    ${panel("流程图形化编排（admin）", `
      <p class="hint">画布产出的就是下面那份 JSON——<b>后端一行没改</b>，接口仍是
        <code>POST /api/workflows/definitions</code>。手写 JSON 的入口保留在下方，两条路等价。</p>
      <form class="inline" id="wfc-meta">
        <input name="key" placeholder="流程编码" required><input name="name" placeholder="流程名称" required>
        <select id="wfc-load"><option value="">— 载入现有定义改编 —</option>${
          definitions.map((d) => `<option value="${esc(d.key)}">${esc(d.name)}</option>`).join("")}</select>
      </form>
      <div class="inline" style="margin:8px 0">
        <button class="btn" id="wfc-add">加节点</button>
        <button class="btn secondary" id="wfc-link">连线（选中→目标）</button>
        <button class="btn secondary" id="wfc-end">设为终态</button>
        <button class="btn danger" id="wfc-del">删节点</button>
        <button class="btn" id="wfc-save">保存为定义</button>
      </div>
      <div id="wfc-canvas" style="overflow-x:auto"></div>
      <p class="msg" id="wfc-msg"></p>
      <details><summary>产出的 JSON（提交给后端的就是它）</summary>
        <pre id="wfc-json" style="white-space:pre-wrap"></pre></details>`)}
    ${panel("流程定义（手写 JSON）", `
      <form class="inline" id="def-form"><input name="key" placeholder="流程编码" required>
        <input name="name" placeholder="流程名称" required>
        <input name="nodes" placeholder='节点 JSON，如 [{"key":"apply","name":"申请","role":"doctor","next":"approve"},{"key":"approve","name":"审批","role":"director","next":""}]'
          required style="min-width:420px"><button>新建定义</button></form>
      ${table(["编码", "名称", "节点链", "状态"], definitions, (d) =>
        `<tr><td>${esc(d.key)}</td><td>${esc(d.name)}</td>
         <td>${wfChainHtml(d.nodes)}</td>
         <td>${d.active ? "启用" : "停用"}</td></tr>`)}`)}
    ${panel("发起流程", `
      <form class="inline" id="inst-form">
        <select name="definition_key">${definitions.map((d) => `<option value="${esc(d.key)}">${esc(d.name)}</option>`).join("")}</select>
        <input name="business_type" placeholder="业务类型" required><input name="business_id" type="number" placeholder="业务ID">
        <input name="title" placeholder="事项标题" style="min-width:220px">
        <input name="org_id" type="number" placeholder="机构ID"><button>发起</button></form>`)}
    ${panel("流程实例", `
      <form class="inline" id="wf-inst-filter">
        <select name="status">${[["running", "流转中"], ["completed", "已完成"], ["cancelled", "已终止"], ["", "全部状态"]].map(([v, t]) =>
          `<option value="${v}"${v === WF_INSTANCE_FILTER.status ? " selected" : ""}>${t}</option>`).join("")}</select>
        <label style="font-size:13px"><input type="checkbox" name="mine" value="true"${
          WF_INSTANCE_FILTER.mine ? " checked" : ""}> 只看我发起的</label></form>
      <p class="msg" id="wf-inst-msg"></p>
      <div id="wf-inst-list">${wfInstanceTable(instances)}</div>`)}
    <div class="panel hidden" id="wf-history"><h3>流转记录</h3><div id="wf-history-body"></div></div>`;
  wfCanvasInit(definitions);
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
  $("#wf-inst-filter").onchange = async (e) => {
    WF_INSTANCE_FILTER[e.target.name] = e.target.type === "checkbox" ? (e.target.checked ? "true" : "") : e.target.value;
    try {
      $("#wf-inst-list").innerHTML = wfInstanceTable(await api(`/api/workflows/instances${wfInstanceQuery()}`));
      setMsg("#wf-inst-msg", "");
    } catch (err) { setMsg("#wf-inst-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      // P2-38：原先弹窗输入框点"取消"照样提交——推进照样推到下一节点（意见记空）；终止在确认框
      // 之后再问原因，原因框点取消照样终止。表单里取消就是不推进 / 不终止。
      // 两张框都由框自己提交（P2-607）：意见 / 原因写超了、流程已被别人推进或终止时报错写在框里、框不关，写好的不用重写
      if (d.advance) {
        const ok = await spdModal("推进流程", [{ name: "comment", label: "处理意见", type: "textarea" }],
          { submit: (form) => api(`/api/workflows/instances/${d.advance}/advance`, { method: "POST",
            body: JSON.stringify({ comment: form.comment }) }) });
        if (!ok) return;
      } else if (d.cancel) {
        const ok = await spdModal("终止流程", [{ name: "comment", label: "终止原因", type: "textarea" }],
          { intro: "终止后该事项不再流转，不能恢复。", submit: (form) => api(`/api/workflows/instances/${d.cancel}/cancel`, {
            method: "POST", body: JSON.stringify({ comment: form.comment }) }) });
        if (!ok) return;
      }
      else if (d.history) {
        const rows = await api(`/api/workflows/instances/${d.history}/history`);
        $("#wf-history").classList.remove("hidden");
        // 节点、动作、操作人印后端给的名称（P2-1475）：原先印节点编码与 advance / cancel，没填姓名的操作人一格空白；
        // 终止那一行的 to_node 是空串，「到」不再印「终态」（单子没走到终态，是在这一步被终止的）
        $("#wf-history-body").innerHTML = table(["从", "到", "动作", "意见", "操作人", "时间"], rows, (h) =>
          `<tr><td>${esc(h.from_node_name || h.from_node)}</td>
           <td>${h.to_node ? esc(h.to_node_name || h.to_node) : h.action === "cancel" ? "（已终止）" : "终态"}</td>
           <td>${esc(h.action_name)}</td><td>${esc(h.comment)}</td><td>${esc(h.actor_name)}</td>
           <td>${esc(h.created_at.slice(0, 16).replace("T", " "))}</td></tr>`);
        return;
      } else return;
      route();
    } catch (err) { setMsg("#wf-msg", err.message, false); }
  };
}

/* 统一申请单中心按状态 / 类型筛（P2-1311）：后端早就收 status / request_type（P2-162 只改了后端），页面原先只有患者号一格——
   卡片上数得出「待处理 1」，列出的最新 200 条里一条待处理都没有，又无处按状态筛。筛选只留在内存里、不进存储（同 JOB_RUN_FILTER）。
   状态的取值与文案用本页的 UNIFIED_STATUS（卡片、统一状态列同一张表；回执的 by_status 只有计数没有文案）；类型名取自回执的
   type_names，见过的记下来——筛到某一类为空时它不在这次回执里，下拉框照样认得出选中的是哪一类。 */
const SR_FILTER = { status: "", request_type: "" };
const SR_TYPE_NAMES = {};

async function renderServiceRequests() {
  $("#page-desc").textContent = "预约 / 检查 / 会诊 / 用血 / 手术五类单据聚合视图，状态映射到统一口径";
  const pid = localStorage.getItem("medplat_sr_patient") || "";
  // 患者 ID 是手输的，而后端对它走 `assert_patient_visible`——本机构与该患者
  // 没有就诊/签约/转诊关系就是 403（写错一个数字必然如此）。这是本页**唯一**
  // 一次取数，且排在 `#page-body` 赋值之前：抛出去会被 route() 的 catch 换成
  // 一行错误，连那个筛选框都没了，而 pid 存在 localStorage 里不会自己消失，
  // 于是这一页对这个用户每次进来都是同一行错误，连清空筛选都做不到。
  // 失败退化成"筛选那一段报错 + 空聚合"，页面本身照常渲染。
  let data = { by_status: {}, by_type: {}, type_names: {}, total: 0, items: [] };
  let pidError = "";
  const query = new URLSearchParams(Object.entries({ patient_id: pid, ...SR_FILTER }).filter(([, v]) => v));
  try { data = await api(`/api/service-requests${query.toString() ? `?${query}` : ""}`); }
  catch (err) { pidError = err.message; }
  Object.assign(SR_TYPE_NAMES, data.type_names);
  const options = (pairs, picked) => pairs.map(([v, text]) =>
    `<option value="${esc(v)}"${v === picked ? " selected" : ""}>${esc(text)}</option>`).join("");
  // 标题照实写（P2-1311）：total 数的是所有状态（P2-162 的口径），原先叫「在办」，已完成、已取消也数在里面
  const filtered = Boolean(SR_FILTER.status || SR_FILTER.request_type);
  // ADR-0009 第二步的**第一页**：面板外壳改用 `panel()`（定义见 core.js）。
  // 迁移范围只限本函数——组件与手写可以共存，ADR 的节奏就是"迁一页、人工过一页"。
  $("#page-body").innerHTML = panel("筛选", `
      <form class="inline" id="sr-form"><input name="patient_id" type="number" value="${esc(pid)}" placeholder="患者ID（留空看全部）">
        <select name="status"><option value="">全部状态</option>${
          options(Object.entries(UNIFIED_STATUS).map(([k, [text]]) => [k, text]), SR_FILTER.status)}</select>
        <select name="request_type"><option value="">全部类型</option>${
          options(Object.entries(SR_TYPE_NAMES), SR_FILTER.request_type)}</select>
        <button>查询</button></form>
      ${pidError ? `<p class="msg err">${esc(pidError)}</p>` : ""}
      <div class="cards">
        ${Object.entries(data.by_status).map(([k, v]) =>
          `<div class="card"><span class="k">${esc((UNIFIED_STATUS[k] || [k])[0])}</span><b>${v}</b></div>`).join("")}
        ${Object.entries(data.by_type).map(([k, v]) =>
          `<div class="card"><span class="k">${esc(data.type_names[k])}</span><b>${v}</b></div>`).join("")}
      </div>
      <p class="desc">刻意不建第六张单据表：五类单据各有必要的领域字段与状态机，这里做的是聚合视图。</p>`)
    // total 是全部命中项，清单只列最新的 returned 条（P2-162）——截断了就明说，别让人拿清单行数当总数
    + panel(`${filtered ? "筛选结果" : "全部事项"}（${data.total}${data.truncated ? `，列出最新 ${data.returned} 条` : ""}）`,
      table(["类型", "单号", "患者", "机构", "事项", "统一状态", "原生状态", "时间"], data.items, (i) => {
        // `text` 在 UNIFIED_STATUS 里查不到时会**回落成后端原始状态码**，
        // 那是服务端数据，必须转义——迁移这一页时才看出来它一直是裸插值。
        // （`color` 是本文件写死的 class 名，不是数据。）
        return `<tr><td>${esc(i.request_type_name)}</td><td>${i.id}</td><td>${esc(i.patient_name)}</td>
          <td>${esc(i.org_name)}</td><td>${esc(i.title)}</td>
          <td>${statusTag(UNIFIED_STATUS, i.status)}</td><td>${esc(i.raw_status)}</td>
          <td>${esc(i.created_at.slice(0, 16).replace("T", " "))}</td></tr>`;
      }));
  $("#sr-form").onsubmit = (e) => { e.preventDefault();
    const f = new FormData(e.target);
    localStorage.setItem("medplat_sr_patient", f.get("patient_id") || "");
    SR_FILTER.status = f.get("status") || "";
    SR_FILTER.request_type = f.get("request_type") || "";
    route(); };
}

/* ---------------- 定时任务 ---------------- */

/* 执行历史的筛选（P2-467）：默认任务每小时跑百余次（60 秒、300 秒的各一批），只看最新 50 条的话，日跑任务的失败记录
   半小时就滚出这一页——接口早就收 job_name / status，页面没给入口。筛选只留在内存里、不进存储：任务名指向的任务会改名下线，
   存下来的旧名字下次进来就成了「下拉框显示全部任务、查的却是那个旧任务」（见 pickedId 的说明）。 */
const JOB_RUN_FILTER = { job_name: "", status: "" };

async function renderJobs() {
  $("#page-desc").textContent = "任务注册表与执行留痕；多实例下靠 Redis 抢锁保证只跑一次";
  const runQuery = new URLSearchParams(Object.entries(JOB_RUN_FILTER).filter(([, v]) => v));
  const [jobs, runs] = await Promise.all([api("/api/jobs"), api(`/api/jobs/runs${runQuery.toString() ? `?${runQuery}` : ""}`)]);
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML =
    panel(`任务清单（${jobs.length}）`,
      table(["任务", "说明", "间隔", "启停", "上次执行", "上次结果", "下次到期", "操作"], jobs, (j) =>
        `<tr><td><code>${esc(j.name)}</code></td><td>${esc(j.title)}</td>
         <td>${Math.round(j.interval_seconds / 60)} 分钟</td>
         <td><span class="tag ${j.enabled ? "green" : "red"}">${j.enabled ? "启用" : "停用"}</span></td>
         <td>${esc((j.last_run_at || "—").slice(0, 16).replace("T", " "))}</td>
         <td>${j.last_status ? `<span class="tag ${j.last_status === "succeeded" ? "green" : "red"}">${
           j.last_status === "succeeded" ? "成功" : "失败"}</span>` : "—"}</td>
         <td>${esc((j.next_run_at || "—").slice(0, 16).replace("T", " "))}</td>
         <td><button class="btn secondary" data-run="${esc(j.name)}">立即执行</button>
             <button class="btn" data-toggle="${esc(j.name)}" data-enabled="${j.enabled}">${j.enabled ? "停用" : "启用"}</button>
             <button class="btn" data-interval="${esc(j.name)}">改间隔</button>
             ${j.implemented ? "" : '<span class="tag red">无实现</span>'}</td></tr>`)
      + '<p class="msg" id="job-msg"></p>')
    + panel(`执行历史（最近 ${runs.length} 条）`,
      `<form class="inline" id="job-run-filter">
        <select name="job_name"><option value="">全部任务</option>${jobs.map((j) =>
          `<option value="${esc(j.name)}"${j.name === JOB_RUN_FILTER.job_name ? " selected" : ""}>${esc(j.title)}</option>`).join("")}</select>
        <select name="status"><option value="">全部结果</option>${[["failed", "只看失败"], ["succeeded", "只看成功"]].map(([v, t]) =>
          `<option value="${v}"${v === JOB_RUN_FILTER.status ? " selected" : ""}>${t}</option>`).join("")}</select></form>`
      + table(["时间", "任务", "触发", "结果", "处理数", "耗时", "摘要"], runs, (r) =>
        `<tr><td>${esc(r.created_at.slice(0, 19).replace("T", " "))}</td><td><code>${esc(r.job_name)}</code></td>
         <td>${r.trigger === "manual" ? "人工" : "计划"}</td>
         <td><span class="tag ${r.status === "succeeded" ? "green" : "red"}">${
           r.status === "succeeded" ? "成功" : "失败"}</span></td>
         <td>${r.affected}</td><td>${r.duration_ms} ms</td><td>${esc(r.message)}</td></tr>`));
  $("#job-run-filter").onchange = (e) => { JOB_RUN_FILTER[e.target.name] = e.target.value; route(); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.run) await api(`/api/jobs/${d.run}/run`, { method: "POST" });
      else if (d.toggle) await api(`/api/jobs/${d.toggle}`, { method: "PATCH",
        body: JSON.stringify({ enabled: d.enabled !== "true" }) });
      else if (d.interval) {
        // P2-38：弹窗换成页内表单；分钟数必填，写成小数、负数由后端报人话
        const form = await spdModal("改执行间隔", [
          { name: "minutes", label: "执行间隔（分钟，最小 1）", type: "number", required: true }]);
        if (!form) return;
        // 下界在页面上按分钟判（P2-1087）：送的是秒，后端报的是「interval_seconds：不能小于 60」，填分钟的人看不懂
        if (!(form.minutes >= 1)) throw new Error("执行间隔至少 1 分钟");
        await api(`/api/jobs/${d.interval}`, { method: "PATCH",
          body: JSON.stringify({ interval_seconds: Math.round(form.minutes * 60) }) });
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
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML =
    panel("总览", `
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
      ${stats.length ? barChart(stats.map((s) => [SURVEY_TARGETS[s.target_type] || s.target_type, s.avg_score])) : ""}`)
    + panel(`差评清单（${negative.length}）`, `
      <p class="desc">带评语的差评最有改进价值——这是投诉发生前唯一的信号。</p>
      ${table(["日期", "对象", "患者", "评分", "评语"], negative, (s) =>
        `<tr><td>${esc(s.date)}</td><td>${esc(SURVEY_TARGETS[s.target_type] || s.target_type)}</td>
         <td>${esc(s.patient_name)}</td><td><span class="tag red">${s.score}</span></td>
         <td>${esc(s.comment || "（未填写）")}</td></tr>`)}`)
    + panel("代录评价（经办）", `
      <form class="inline" id="survey-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="target_type">${Object.entries(SURVEY_TARGETS).map(([k, v]) =>
          `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="target_id" type="number" placeholder="对象ID" required>
        <input name="score" type="number" min="1" max="5" placeholder="评分1-5" required>
        <input name="comment" placeholder="评语" style="min-width:240px"><button>提交</button></form>
      <p class="msg" id="survey-msg"></p>
      <p class="desc">居民本人评价走居民端 /m，此处仅供窗口代录。</p>`)
    + panel(`最近评价（${recent.length}）`,
      table(["日期", "对象", "患者", "评分", "评语"], recent, (s) =>
        `<tr><td>${esc(s.date)}</td><td>${esc(SURVEY_TARGETS[s.target_type] || s.target_type)}</td>
         <td>${esc(s.patient_name)}</td>
         <td><span class="tag ${s.score >= 4 ? "green" : s.score <= 2 ? "red" : "orange"}">${s.score}</span></td>
         <td>${esc(s.comment || "—")}</td></tr>`));
  $("#survey-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/surveys", formJson(e.target, ["patient_id", "target_id", "score"]), "#survey-msg"); };
}

/* ---------------- 站内消息 ---------------- */

const NOTIFY_CATEGORIES = {
  critical_value: "危急值",
  exam_report: "检查报告",
  surgery: "手术安排",
  followup: "随访提醒",
  spd_task: "慢专病任务",
  spd_path: "专病路径",
  spd_report: "智能报告",
};

/* 消息落到对应业务页面即可，不做深链定位——列表页自己带筛选，
   再造一套 URL 深链只会多一份要同步维护的约定。
   先按分类找（P2-460）：危急值关联的是检查报告，原先按关联对象落到共享诊断中心，那里只有打印 / 修订，
   「确认接收 / 处置反馈」只在危急值操作台；慢专病的催办、路径暂停、报告生成原先没有映射、分类也原样显示代号。 */
const NOTIFY_CATEGORY_PAGE = {
  critical_value: "critical",
  spd_task: "spdpath",
  spd_path: "spdpath",
  spd_report: "spdreport",
};
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
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = panel("我的消息", `
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
             ${NOTIFY_CATEGORY_PAGE[n.category] || NOTIFY_LINK_PAGE[n.link_type]
               ? `<button data-ntgo="${NOTIFY_CATEGORY_PAGE[n.category] || NOTIFY_LINK_PAGE[n.link_type]}">前往处理</button>` : ""}</td></tr>`)}
    `);
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
  const period = localToday().slice(0, 7);
  const [quality, drug] = await Promise.all([
    api("/api/quality/clinical-indicators"),
    api(`/api/analytics/drug-use?period=${period}`),
  ]);
  const byDimension = {};
  quality.indicators.forEach((i) => (byDimension[i.dimension] ||= []).push(i));
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 末尾的 barChart 刻意留在面板**外**——它原本就不在任何 panel 里。
  $("#page-body").innerHTML =
    panel(`医疗质量指标（${quality.period}）`,
      Object.entries(byDimension).map(([dim, items]) => `<h4>${esc(dim)}</h4>${
        table(["指标", "分子", "分母", "比率", "口径"], items, (i) =>
          `<tr><td><b>${esc(i.name)}</b></td><td>${i.numerator}</td>
           <td>${i.denominator || "—"}${i.uncollected
             ? `<span class="tag orange">未采集 ${i.uncollected}</span>` : ""}</td>
           <td>${i.denominator ? `${i.rate_pct}%` : "样本为空"}</td>
           <td class="muted">${esc(i.caliber)}</td></tr>`)}`).join(""))
    + panel(`用药结构（${drug.period}）`, `
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
    `)
    + barChart(drug.orgs.filter((o) => o.antibiotic_intensity > 0 && !o.intensity_unstable)
      .map((o) => [o.org_name, o.antibiotic_intensity]), { unit: " DDDs/百人天" });
}

/* ---------------- 运行监控（浙#47 / #46） ---------------- */

async function renderMonitor() {
  $("#page-desc").textContent = "运行环境、接口调用统计、集群节点与写操作审计";
  const [ov, stats, nodes, audit] = await Promise.all([
    api("/api/monitor/overview"), api("/api/monitor/api-stats"),
    api("/api/monitor/nodes"), api("/api/audit/stats?days=30"),
  ]);
  // 调用统计的口径取自后端（P2-356）：原先写死「本实例进程内、重启即清零」，配了 Redis 之后计数是集群口径、跨重启累计，
  // 同一页的面板标题（stats.scope）说的却是集群——一页自相矛盾
  $("#page-desc").textContent = `调用统计口径：${stats.scope}；审计统计是落库的写操作留痕，跨实例可追溯`;
  const dot = (ok) => `<span class="tag ${ok ? "green" : "red"}">${ok ? "正常" : "异常"}</span>`;
  // 审计按日趋势补齐零值日（P2-417）：后端只回有写操作的日子，没写操作的那天整天不在列表里，折线把前后两天挨着画、
  // 看不出停摆。按统计窗口逐日补 0；后端按 UTC 日分桶，这里也按 UTC 拼日期（不是「今天」，不走 localToday）
  const utcDay = (ms) => {
    const d = new Date(ms);
    return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}-${String(d.getUTCDate()).padStart(2, "0")}`;
  };
  const auditByDay = Object.fromEntries(audit.daily.map((d) => [d.date, d]));
  const auditDaily = Array.from({ length: audit.days + 1 }, (_, i) => utcDay(Date.now() - (audit.days - i) * 86400000))
    .map((day) => auditByDay[day] || { date: day, ok: 0, failed: 0 });
  // ADR-0009 第二步：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 标题里的 `stats.scope` / `audit.scope` 原本手写了 `esc()`，迁移后**必须去掉**——
  // `panel()` 自己转义 title，留着就是转义两遍（`&` 会变成 `&amp;amp;`），那是改字节。
  $("#page-body").innerHTML =
    panel("运行环境", `
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
    `)
    + panel("集群节点", `
      ${nodes.instances
        ? table(["实例", "本机", "运行时长(秒)"], nodes.instances, (n) =>
            `<tr><td>${esc(n.instance_id)}</td><td>${n.self ? "是" : ""}</td>
             <td>${n.uptime_seconds ?? "—"}</td></tr>`)
        : `<p class="msg err">${esc(nodes.note)}</p>`}
    `)
    + panel(`接口调用（${stats.scope}）`, `
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
    `)
    + panel(`审计统计（近 ${audit.days} 天 · ${audit.scope}）`, `
      <div class="cards">
        <div class="card"><span class="k">写操作</span><b>${audit.total}</b></div>
        <div class="card"><span class="k">失败</span><b>${audit.failed}</b></div>
        <div class="card"><span class="k">失败率</span><b>${audit.failed_ratio_pct}%</b></div>
      </div>
      ${audit.daily.length
        ? lineChart(auditDaily.map((d) => d.date.slice(5)),
            [auditDaily.map((d) => d.ok), auditDaily.map((d) => d.failed)],
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
    `);
}

/* ---------------- 就诊凭据（浙#27） ---------------- */

// 取值真源是 credentials.py:resolve_any 的三条 return——按「从具体到一般」的顺序
const MATCHED_BY = { credential_no: "凭据号", ehc_no: "电子健康卡号", id_card: "身份证号" };

async function renderCredentials() {
  $("#page-desc").textContent =
    "凭据是介质（卡会丢、码会过期），电子健康卡号才是身份——换卡不换号";
  const rows = await api("/api/credentials?limit=100");
  $("#page-body").innerHTML = `
    ${panel("发放凭据", `
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
    `)}
    ${panel("凭据核验", `
      <form id="cred-lookup" class="inline">
        <input name="credential_no" placeholder="扫码或输入凭据号" required>
        <button>核验</button>
      </form>
      <div id="cred-result"></div>
    `)}
    ${panel("一码通（动态码：出码与核验）", `
      <form id="onecode-form" class="inline">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="ttl_seconds" type="number" value="60" min="10" max="600" style="min-width:110px"
          title="后端限 10～600 秒">
        <button>出码</button>
      </form>
      <div id="onecode-result"></div>
      <form id="onecode-check" class="inline" style="margin-top:10px">
        <input name="code" placeholder="粘贴或扫入动态码（健康卡号.过期时刻.签名）" required style="min-width:340px">
        <button>核验</button>
      </form>
      <div id="onecode-check-result"></div>
      <p class="desc">动态码<b>不落库</b>：它是「健康卡号.过期时刻.签名」的自包含串，
        过期即失效，换平台密钥即全部作废。默认 60 秒——给太长等于回到静态码。
        核验时<b>过期与签名错误分开报</b>：前者让人重新出码，后者是伪造，处置完全不同。</p>
    `)}
    ${panel("多卡（码）协同：一个入口认全部身份标识", `
      <form id="anyid-form" class="inline">
        <input name="identifier" placeholder="实体卡号 / 电子健康卡号 / 身份证号" required style="min-width:300px">
        <button>认人</button>
      </form>
      <div id="anyid-result"></div>
      <p class="desc">按「从具体到一般」依次试：凭据号 → 电子健康卡号 → 身份证号，
        回执<b>注明命中的是哪一类</b>——不注明的话，同一个人从不同介质进来看不出差别。
        与上面「凭据核验」的分工：那条只认凭据号、回的是这张卡的台账行；这条认三类标识、回的是人。</p>
    `)}
    ${panel("凭据台账", `
      ${table(["凭据号", "患者ID", "类型", "状态", "发放时间", "结束原因", "操作"], rows, (c) =>
        `<tr><td>${esc(c.credential_no)}</td><td>${c.patient_id}</td>
         <td>${esc(c.credential_type_name)}</td>
         <td><span class="tag ${c.status === "active" ? "green" : c.status === "recycled" ? "" : "red"}">${
           esc(c.status_name)}</span></td>
         <td>${esc(c.issued_at.slice(0, 16).replace("T", " "))}</td>
         <td>${esc(c.close_reason || "—")}</td>
         <td>${c.status === "active"
           ? `<button data-crec="${c.id}">回收</button><button data-cvoid="${c.id}">作废</button>` : ""}</td></tr>`)}
    `)}`;
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
  $("#onecode-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api("/api/credentials/one-code", { method: "POST", body: JSON.stringify({
        patient_id: Number(f.get("patient_id")), ttl_seconds: Number(f.get("ttl_seconds")) }) });
      // 码本身要能被选中复制，所以印在 <code> 里而不是塞进提示行
      $("#onecode-result").innerHTML = `<p class="msg ok">健康卡号 ${esc(r.ehc_no)}，
        有效 ${r.expires_in} 秒</p>
        <p><code style="word-break:break-all;user-select:all">${esc(r.code)}</code></p>
        <p class="desc">${esc(r.note)}</p>`;
    } catch (err) { $("#onecode-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#onecode-check").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/credentials/one-code/resolve", { method: "POST",
        body: JSON.stringify({ code: e.target.code.value.trim() }) });
      // 卡片形状照搬同一页的 #cred-result——`kv()` 不是管理端的全局
      // （它只存在于 pages-spd 某个函数内与两个 H5 文件里，index.html 根本不加载后者）
      $("#onecode-check-result").innerHTML = `<div class="cards">
        <div class="card"><span class="k">持有人</span><b>${esc(r.name)}</b></div>
        <div class="card"><span class="k">健康卡号</span><b>${esc(r.ehc_no)}</b></div>
        <div class="card"><span class="k">患者ID</span><b>${r.patient_id}</b></div>
        <div class="card"><span class="k">剩余有效</span><b>${r.remaining_seconds} 秒</b></div></div>`;
    } catch (err) { $("#onecode-check-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#anyid-form").onsubmit = async (e) => {
    e.preventDefault();
    const id = e.target.identifier.value.trim();
    try {
      const r = await api(`/api/credentials/resolve?identifier=${encodeURIComponent(id)}`);
      $("#anyid-result").innerHTML = `<div class="cards">
        <div class="card"><span class="k">命中方式</span><b><span class="tag">${
          esc(MATCHED_BY[r.matched_by] || r.matched_by)}</span></b></div>
        <div class="card"><span class="k">持有人</span><b>${esc(r.patient?.name || "—")}</b></div>
        <div class="card"><span class="k">健康卡号</span><b>${esc(r.patient?.ehc_no || "—")}</b></div>
        <div class="card"><span class="k">是否可用</span><b><span class="tag ${r.valid ? "green" : "red"}">${
          r.valid ? "有效" : esc(`失效（${r.credential_status_name || "未知状态"}）`)}</span></b></div></div>`;
    } catch (err) { $("#anyid-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#page-body").onclick = (e) => {
    const { crec, cvoid } = e.target.dataset;
    if (crec) return postAction(`/api/credentials/${crec}/recycle`, {}, "#cred-msg");
    if (cvoid) {
      // P2-38：弹窗换成页内表单，写明作废的后果（作废后不能再用于挂号，需另行发放）
      return spdModal("作废就诊凭据", [
        { name: "reason", label: "作废原因", required: true, placeholder: "如：挂失、损坏" },
      ], { intro: "作废后该凭据不能再用于就诊识别，不能恢复；如需继续就诊请另行发放。" }).then((form) =>
        form && postAction(`/api/credentials/${cvoid}/void`, { reason: form.reason }, "#cred-msg"));
    }
  };
}

/* ---------------- 门急诊文书（浙#3） ---------------- */

// active 是 bool，没有后端文案可取；映成状态码再走 statusTag（consent_type_name 是后端给的，不自己映）
const TPL_STATUS = { on: ["现行", "green"], off: ["已停用", "red"] };

const CONSENT_TYPES = {
  surgery: "手术", anesthesia: "麻醉", transfusion: "输血",
  exam: "特殊检查", treatment: "特殊治疗", other: "其他",
};
// 与后端 outpatient_docs.RELATION_NAMES 同一份取值（入参 pattern 只认这五个）
const CONSENT_RELATIONS = { self: "本人", spouse: "配偶", parent: "父母", child: "子女", other: "委托人" };

async function renderOutpatientDocs() {
  $("#page-desc").textContent =
    "知情告知书开具时冻结正文——模板日后修订不会改动已开具的（含待签的）；拒签是独立状态，不是「没签」";
  const encounterId = Number(localStorage.getItem("medplat_od_encounter") || 0);
  const [templates, recentConsents, pendingConsents] = await Promise.all([
    // 取全部（不带 active）：停用的模板也要能看到并改回现行版，否则"停错了"就再也捞不回来。
    // 开具那个下拉仍只列启用中的——后端对停用模板直接 409（"请选用现行版本"）。
    api("/api/outpatient/consent-templates"),
    api("/api/outpatient/consents?limit=50"),
    // 待签署的单独取一遍、排在最前（P2-456，同 P2-408）：清单只取最新 50 份，挤出去的待签告知书就没有「签署 / 拒签」，
    // 而那次就诊的完整度还数着它「待签署」
    api("/api/outpatient/consents?status=pending&limit=500"),
  ]);
  const consents = actionableFirst(recentConsents, pendingConsents);
  const activeTemplates = templates.filter((t) => t.active);
  const canTemplate = currentRole() === "admin";
  let scoped = { treatments: [], nursing: [], completeness: null };
  // 就诊 ID 是手输的，打错一次就 404（`/completeness` 还会因不可见 403）。
  // 这三条取数排在 `#page-body` 赋值之前，抛出去会被 route() 的 catch 换成
  // 一行错误——连那个输入框都跟着没了，而 id 存在 localStorage 里不会自己
  // 消失，于是这一页对这个用户每次进来都是同一行错误，改都改不回来。
  // 别的页面是拿列表校验存量选择（`pickedId`），这里没有列表可校验，
  // 只能让取数失败退化成"就诊那一段报错"，页面本身照常渲染。
  let scopeError = "";
  if (encounterId) {
    try {
      scoped = {
        treatments: await api(`/api/outpatient/encounters/${encounterId}/treatments`),
        nursing: await api(`/api/outpatient/encounters/${encounterId}/nursing-records`),
        completeness: await api(`/api/outpatient/encounters/${encounterId}/completeness`),
      };
    } catch (err) { scopeError = err.message; }
  }
  $("#page-body").innerHTML = `
    ${panel("选择就诊", `
      <form class="inline" id="od-pick">
        <input name="encounter_id" type="number" placeholder="就诊ID" value="${encounterId || ""}" required>
        <button>载入该次就诊的文书</button>
      </form>
      ${scopeError ? `<p class="msg err">就诊 #${encounterId}：${esc(scopeError)}</p>` : ""}
      ${scoped.completeness ? `<div class="cards">
        <div class="card"><span class="k">处置记录</span><b>${scoped.completeness.treatment_records}</b></div>
        <div class="card"><span class="k">护理记录</span><b>${scoped.completeness.nursing_records}</b></div>
        <div class="card"><span class="k">告知书</span><b>${scoped.completeness.consents_total}</b></div>
        <div class="card"><span class="k">待签署</span><b>${
          scoped.completeness.consents_pending
            ? `<span class="tag orange">${scoped.completeness.consents_pending}</span>`
            : 0}</b></div>
      </div><p class="desc">${esc(scoped.completeness.note)}</p>` : ""}
    `)}

    ${encounterId ? `
    ${panel("治疗处置记录", `
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
    `)}

    ${panel("门急诊护理记录", `
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
    `)}` : ""}

    ${panel("开具知情告知书", `
      <form class="inline" id="od-consent">
        <input name="patient_id" type="number" placeholder="患者ID" required
          value="${scoped.completeness ? scoped.completeness.patient_id : ""}">
        <input name="org_id" type="number" placeholder="机构ID" required>
        ${scoped.completeness ? `<label style="font-size:13px"><input type="checkbox" name="link_encounter" checked>
          关联本次就诊 #${encounterId}</label>` : ""}
        <select name="consent_type">${Object.entries(CONSENT_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="template_id"><option value="">不用模板（自带正文）</option>
          ${activeTemplates.map((t) => `<option value="${t.id}">${esc(t.title)} ${esc(t.version)}</option>`).join("")}</select>
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
    `)}
    ${panel("告知书模板（改模板只影响此后开具的，已开具的正文是冻结快照）", `
      ${canTemplate ? `<form class="inline" id="od-tpl-form" style="margin-bottom:8px">
        <select name="consent_type">${Object.entries(CONSENT_TYPES).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <input name="title" placeholder="标题" required>
        <input name="version" value="v1" placeholder="版本" style="width:70px">
        <textarea name="body" rows="2" placeholder="告知书正文" required style="min-width:320px"></textarea>
        <button>新增模板</button>
      </form>` : ""}
      ${table(["ID", "类型", "标题", "版本", "状态", "正文"].concat(canTemplate ? ["操作"] : []),
        templates, (t) =>
        `<tr><td>${t.id}</td><td>${esc(t.consent_type_name)}</td><td>${esc(t.title)}</td>
         <td><span class="tag">${esc(t.version)}</span></td>
         <td>${statusTag(TPL_STATUS, t.active ? "on" : "off")}</td>
         <td style="white-space:pre-wrap">${esc(t.body) || "—"}</td>
         ${canTemplate ? `<td><button class="btn secondary" data-tpledit="${t.id}">编辑</button></td>` : ""}</tr>`)}
      <p class="desc">停用的模板<b>不能再用来开具</b>（后端 409「请选用现行版本」），
        但已经开具的那些（含还没签的）一个字都不会变——开具时正文就冻结成了快照。
        所以改模板是"从此往后"，不是"追溯修订"。开具处的下拉只列启用中的（当前 ${
          activeTemplates.length} 个）。</p>
      <p class="msg" id="od-tmsg"></p>`)}
    ${panel("按患者查处置史（跨就诊看一条线）", `
      <form class="inline" id="od-tr-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <button>查询</button>
      </form>
      <p class="desc">换药、雾化这类连续处置要跨就诊看才有意义——上面那张表只列当前选中的那一次就诊。
        查询会按患者维度落调阅留痕。</p>
      <div id="od-tr"></div>`)}`;

  $("#od-pick").onsubmit = (e) => { e.preventDefault();
    localStorage.setItem("medplat_od_encounter", e.target.encounter_id.value.trim()); route(); };
  // 改了就诊ID、没点「载入」就提交的一律拦下（P1-231）：原先下面几张表单按上次载入的那一次就诊写，
  // 框里的号是另一次——处置、护理记录进了上一次就诊，回执照样「已记录」
  const encounterUnloaded = (msgSel) => {
    const typed = $("#od-pick").encounter_id.value.trim();
    if (Number(typed || 0) === encounterId) return false;
    setMsg(msgSel, `就诊ID 改成了 ${typed || "（空）"}，还没载入：先点「载入该次就诊的文书」再提交`, false);
    return true;
  };
  if (encounterId) {
    $("#od-treat").onsubmit = (e) => { e.preventDefault();
      if (encounterUnloaded("#od-msg")) return;
      postAction(`/api/outpatient/encounters/${encounterId}/treatments`, formJson(e.target), "#od-msg"); };
    $("#od-nurse").onsubmit = (e) => { e.preventDefault();
      if (encounterUnloaded("#od-msg")) return;
      postAction(`/api/outpatient/encounters/${encounterId}/nursing-records`, formJson(e.target), "#od-msg"); };
  }
  $("#od-consent").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "org_id"]);
    // 空字符串的 template_id 要去掉，否则后端按"选了模板"处理
    if (!body.template_id) delete body.template_id;
    else body.template_id = Number(body.template_id);
    // 载入了某次就诊时默认挂到这次就诊上（P2-161）：就诊文书完整度按「挂在本次就诊上的告知书」数，原先这张表单从不挂，
    // 那两格「告知书 / 待签署」恒为 0——注释里说「真正该追的是待签」的那一项从来追不到
    delete body.link_encounter;
    if (e.target.link_encounter && e.target.link_encounter.checked) {
      if (encounterUnloaded("#od-cmsg")) return;
      body.related_type = "encounter";
      body.related_id = Number(encounterId);
    }
    postAction("/api/outpatient/consents", body, "#od-cmsg");
  };
  // 告知书模板原先只能改不能建（P2-93 动词级孤儿）：新的告知书类型、新版本只能靠接口调用方登记
  const tplForm = $("#od-tpl-form");
  if (tplForm) tplForm.onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/outpatient/consent-templates", formJson(e.target), "#od-tmsg");
  };
  $("#od-tr-form").onsubmit = async (e) => {
    e.preventDefault();
    const pid = new FormData(e.target).get("patient_id");
    try {
      const rows = await api(`/api/outpatient/treatments?patient_id=${encodeURIComponent(pid)}`);
      $("#od-tr").innerHTML = table(["就诊", "处置", "部位", "剂量", "执行人", "反应", "时间"], rows, (t) =>
        `<tr><td>${t.encounter_id}</td><td>${esc(t.treatment_name)}</td><td>${esc(t.site) || "—"}</td>
         <td>${esc(t.dose) || "—"}</td><td>${esc(t.executor_name) || "—"}</td>
         <td>${t.reaction ? esc(t.reaction) : '<span class="tag orange">未记录</span>'}</td>
         <td>${esc(t.created_at.slice(0, 16).replace("T", " "))}</td></tr>`);
    } catch (err) { $("#od-tr").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#page-body").onclick = async (e) => {
    const { csign, crefuse, tpledit } = e.target.dataset;
    if (tpledit) {
      const t = templates.find((x) => x.id === Number(tpledit));
      // 框自己提交（P2-607）：正文写超了、版本号写超了时报错写在框里、框不关，改了一半的同意书正文不用重敲
      const ok = await spdModal(`编辑模板 ${t ? t.version : tpledit}`, [
        { name: "title", label: "标题（留空不改）", type: "text", value: t ? t.title : "" },
        { name: "version", label: "版本号（留空不改；改版本是为了让已签的那份认得出依据哪版）",
          type: "text", value: t ? t.version : "" },
        { name: "active", label: "启停", type: "select", value: t && t.active ? "1" : "0",
          options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
        { name: "body", label: "正文（留空不改）", type: "textarea", value: t ? t.body : "" },
      ], { submit: (picked) => {
        // 后端 exclude_unset + `if value is not None`：留空的键不送，别拿空串把正文清了
        const body = { active: picked.active === "1" };
        if (picked.title) body.title = picked.title;
        if (picked.version) body.version = picked.version;
        if (picked.body) body.body = picked.body;
        return api(`/api/outpatient/consent-templates/${tpledit}`, { method: "PATCH", body: JSON.stringify(body) });
      } });
      if (ok) route();
      return;
    }
    // P2-38 / P1-70：签署两连问（关系要手打 self/spouse/…）、拒签两连问换成页内表单。
    // 拒签原先把关系**写死成"本人"**——家属或委托人代为拒签的，证据上记成了患者本人拒签。
    const relationOptions = Object.entries(CONSENT_RELATIONS).map(([value, label]) => ({ value, label }));
    if (csign) {
      const v = await spdModal("记录患方签署", [
        { name: "signer_name", label: "签署人姓名", required: true },
        { name: "signer_relation", label: "与患者关系", type: "select", value: "self", options: relationOptions },
      ]);
      if (!v) return;
      return postAction(`/api/outpatient/consents/${csign}/sign`, v, "#od-cmsg");
    }
    if (crefuse) {
      const v = await spdModal("记录拒签（这是机构「已告知」的证据）", [
        { name: "signer_name", label: "拒签人姓名", required: true },
        { name: "signer_relation", label: "与患者关系", type: "select", value: "self", options: relationOptions },
        { name: "refuse_reason", label: "拒签原因（必填）", required: true },
      ]);
      if (!v) return;
      return postAction(`/api/outpatient/consents/${crefuse}/refuse`, v, "#od-cmsg");
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
  // 分组被删/不可见时退回"没有选择"：这行取数排在 `#page-body` 赋值之前，
  // 让它 404 抛出去，连分组列表都渲染不出来，这一页就再也换不了分组。
  const selected = pickedId("medplat_group_id", groups);
  const members = selected ? await api(`/api/org-groups/${selected}/members`) : [];
  $("#page-body").innerHTML = `
    ${panel("新建分组", `
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
             <button data-ogedit="${g.id}">改档</button>
             <button data-ogtoggle="${g.id}" data-active="${g.active}">${
               g.active ? "停用" : "启用"}</button></td></tr>`)}
    `)}

    ${selected ? panel(`成员机构（分组 #${selected}）`, `
      <form class="inline" id="og-member">
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>加入分组</button>
      </form>
      ${table(["机构", "层级", "加入时间", "操作"], members, (m) =>
        `<tr><td>${esc(m.org_name)}</td><td>${esc(m.level_name)}</td>
         <td>${esc(m.joined_at.slice(0, 10))}</td>
         <td><button data-ogdrop="${m.org_id}">移出</button></td></tr>`)}
    `) : ""}

    ${panel(`覆盖情况（${coverage.group_type_name}）`, `
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
              `<tr><td>${esc(o.org_name)}</td><td>${esc(o.level_name)}</td></tr>`)}`
        : '<p class="desc">全部机构均已入组，按分组统计之和等于全域总数。</p>'}
    `)}

    ${panel("按机构查归属分组", `
      <form class="inline" id="og-oforg">
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>查归属</button></form>
      <p class="desc">上面两块都是「分组 → 成员」，这一块是反过来的「机构 → 它在哪些分组」。
        一家机构可以既在某片区、又在某专科联盟，所以这里可能跨<b>多个分组类型</b>返回多条，
        而覆盖情况那一块一次只看一种类型——"某机构没入组"要在这里才查得准。
        分组归属是组织架构拓扑（转诊、调拨、统计口径都要引用），不含经营或诊疗数据，
        所以这条按设计<b>不做横向隔离</b>：查别家机构的分组归属是允许的。</p>
      <p class="msg" id="og-oforg-msg"></p>
      <div id="og-oforg-result"></div>
    `)}`;

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
  $("#og-oforg").onsubmit = async (e) => {
    e.preventDefault();
    const orgId = new FormData(e.target).get("org_id");
    try {
      const rows = await api(`/api/org-groups/of-org/${encodeURIComponent(orgId)}`);
      // of-org 的行按设计不带成员数（见后端 OrgGroupOut），成员数从本页已取的分组列表按 id 对上；
      // 列表只取前 200 个分组，对不上的显示「—」，不显示 undefined
      const memberCount = Object.fromEntries(groups.map((x) => [x.id, x.member_count]));
      $("#og-oforg-result").innerHTML = rows.length
        ? table(["名称", "类型", "牵头机构", "成员数", "状态"], rows, (g) =>
            `<tr><td><b>${esc(g.name)}</b></td><td>${esc(g.group_type_name)}</td>
             <td>${esc(orgName[g.lead_org_id] || "—")}</td><td>${esc(memberCount[g.id] ?? "—")}</td>
             <td><span class="tag ${g.active ? "green" : "red"}">${g.active ? "启用" : "停用"}</span></td></tr>`)
        : `<p class="empty">这家机构不在任何分组里——转诊、调拨与按分组的统计口径都不会把它算进去。</p>`;
      setMsg("#og-oforg-msg", "", true);
    } catch (err) { setMsg("#og-oforg-msg", err.message, false); }
  };
  if (selected) {
    $("#og-member").onsubmit = (e) => { e.preventDefault();
      postAction(`/api/org-groups/${selected}/members`, formJson(e.target, ["org_id"]), "#og-msg"); };
  }
  $("#page-body").onclick = async (e) => {
    const { ogpick, ogedit, ogtoggle, active, ogdrop } = e.target.dataset;
    if (ogpick) { localStorage.setItem("medplat_group_id", ogpick); return route(); }
    // 改档（P2-1533）：PATCH 能改名称、类型（P2-1511）、牵头机构（传 null 即清空，P2-351）和备注，行上原先只有「停用 / 启用」——
    // 类型、名称建错了只能停用，名称唯一、想用原名重建也得先改旧组的名字。框照本文件「编辑基金池」的写法：四项都是单行字段与
    // 下拉（P2-607 棘轮只管多行文本框），点确定关框、由页面发请求，失败写本页消息行。只送和预填值不同的项（照 P2-969）：这里没动
    // 的项不拿进页面时那份写回去；spdModal 交回的值去了首尾空白，原值也去掉再比，没动的名称、备注不会被当成改了
    if (ogedit) {
      const g = groups.find((x) => x.id === Number(ogedit));
      if (!g) return;
      const picked = await spdModal(`改档：${g.name}`, [
        { name: "name", label: "分组名称（必填，最多 64 个字）", type: "text", value: g.name, required: true },
        { name: "group_type", label: "类型", type: "select", value: g.group_type,
          options: Object.entries(GROUP_TYPES).map(([value, label]) => ({ value, label })) },
        { name: "lead_org_id", label: "牵头机构", type: "select", value: g.lead_org_id ?? "",
          options: [{ value: "", label: "无牵头机构" }, ...orgs.map((o) => ({ value: o.id, label: o.name }))] },
        { name: "note", label: "备注（最多 256 个字，可留空）", type: "text", value: g.note },
      ], { intro: "只提交改了的项；牵头机构选「无牵头机构」即清空。启停用行上的「停用 / 启用」。" });
      if (!picked) return;
      const body = {};
      if (picked.name !== g.name.trim()) body.name = picked.name;
      if (picked.group_type !== g.group_type) body.group_type = picked.group_type;
      if (picked.lead_org_id !== String(g.lead_org_id ?? "")) {
        body.lead_org_id = picked.lead_org_id ? Number(picked.lead_org_id) : null;
      }
      if (picked.note !== (g.note || "").trim()) body.note = picked.note;
      // 一项都没变不发请求（后端此时也 422「请至少改一项」）
      if (!Object.keys(body).length) return setMsg("#og-msg", "没有改动：名称、类型、牵头机构、备注都与原来相同，未提交", false);
      // 页面约束与后端 GroupUpdate 同一口径：名称至少有一个看得见的字（texttypes.NON_BLANK：空白、控制字符、零宽这类格式字符
      // 都不算）、最多 64 个字，备注最多 256 个字；字数按字符算，同后端
      if ("name" in body && !/[^\s\p{Cc}\p{Cf}]/u.test(body.name)) return setMsg("#og-msg", "分组名称不能只填空格", false);
      if ("name" in body && [...body.name].length > 64) return setMsg("#og-msg", "分组名称最多 64 个字，超出了", false);
      if ("note" in body && [...body.note].length > 256) return setMsg("#og-msg", "备注最多 256 个字，超出了", false);
      try {
        await api(`/api/org-groups/${ogedit}`, { method: "PATCH", body: JSON.stringify(body) });
        route();
      } catch (err) { setMsg("#og-msg", err.message, false); }
      return;
    }
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

// 取值真源是 fund.py：active/closed 由 PoolUpdate 的 pattern 限定，settled 由清算置。文案取后端的 status_name（P2-598：
// 原先这里自带「在用 / 已关闭」，与模型列注释、409 报错的「执行中 / 已归档」两套说法），这里只管颜色
const POOL_STATUS_TAG = { active: "green", closed: "", settled: "green" };

async function renderFund() {
  $("#page-desc").textContent =
    "预付与清算产生真实资金流，月度预结只是账面对冲；分配依据是冻结的绩效得分快照，事后调权不影响已分结果";
  const [pools, groups, vars] = await Promise.all([
    api("/api/fund/pools"), api("/api/org-groups"), api("/api/fund/formula-variables"),
  ]);
  // 先拿到池子列表再认存量选择——池子被删/不可见时下面那两条取数会 404，
  // 而它们排在 `#page-body` 赋值之前，抛出去就连池子列表都渲染不出来了。
  const picked = pickedId("medplat_fund_pool", pools);
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
  // 只有执行中的池子收预付 / 预结 / 清算（其余 409「基金池状态为 已归档，不可再预付」）：P2-598 之前三张表单照样摆着，
  // 填完点下去才报错。已归档的能在上方「编辑」改回执行中，已清算的不能
  const pickedPool = pools.find((p) => p.id === picked);
  const poolOpen = !pickedPool || pickedPool.status === "active";
  const notOpen = (what) => `<p class="desc">基金池${esc(pickedPool ? pickedPool.status_name : "")}，不能再${what}${
    pickedPool && pickedPool.status === "closed" ? "；要继续先在上方「编辑」把状态改回执行中" : ""}。</p>`;
  $("#page-body").innerHTML = `
    ${panel("基金池", `
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
         <td><span class="tag ${POOL_STATUS_TAG[p.status] ?? ""}">${esc(p.status_name)}</span></td>
         <td><button data-fdpick="${p.id}">打开</button>
             ${p.status === "settled"
               ? `<button class="btn secondary" data-fddist="${p.id}">分配结果</button>`
               : `<button class="btn secondary" data-fdedit="${p.id}">编辑</button>`}</td></tr>`)}
      <p class="desc">已清算的池子<b>不可再改</b>（后端 409）——清算是一次性动作，
        改了筹资总额就等于改了已经分下去的结果。所以「编辑」只在未清算的池子上摆，
        已清算的换成「分配结果」：不切换当前打开的池子，直接看那一池分给了谁多少。</p>
      <div id="fd-dist"></div>
    `)}

    ${picked ? `
    ${panel("预付批次（真实资金流）", `
      ${poolOpen ? `<form class="inline" id="fd-prepay">
        <input name="batch_no" placeholder="批次号">
        <input name="amount" type="number" step="any" placeholder="金额(元)" required>
        <input name="paid_date" type="date"><button>登记预付</button>
      </form>` : notOpen("预付")}
      ${table(["批次", "金额", "拨付日期", "备注"], detail.prepay, (r) =>
        `<tr><td>${esc(r.batch_no || "—")}</td><td>${r.amount}</td>
         <td>${esc(r.paid_date || "—")}</td><td>${esc(r.note || "—")}</td></tr>`)}
    `)}

    ${panel("月度预结（账面对冲，不产生资金流）", `
      ${poolOpen ? `<form class="inline" id="fd-period">
        <input name="period" placeholder="${(pickedPool || {}).year || "YYYY"}-MM（本池年度内）" required style="min-width:150px">
        <input name="actual_amount" type="number" step="any" placeholder="发生额(留空=按结算单归集)">
        <button>预结</button>
      </form>` : notOpen("预结")}
      ${table(["期间", "发生额", "来源", "备注"], detail.periods, (r) =>
        `<tr><td>${esc(r.period)}</td><td>${r.actual_amount}</td>
         <td>${r.source === "auto" ? "系统归集" : "人工核定"}</td>
         <td>${esc(r.note || "—")}</td></tr>`)}
    `)}

    ${panel("年终清算与结余分配", `
      ${detail.settlement ? renderSettlement(detail.settlement, vars) : !poolOpen ? notOpen("清算") : `
        <form class="inline" id="fd-settle">
          <input name="total_expense" type="number" step="any" placeholder="全年发生额(留空=各期之和)">
          <select name="overrun_action"><option value="none">超支不处理（仅记录）</option>
            <option value="share">超支按公式分摊</option>
            <option value="carry">超支挂账结转</option></select>
          <button>清算</button>
        </form>
        <p class="desc">清算每个池只能做一次；超支不会自动扣减任何机构。</p>`}
    `)}` : ""}`;

  $("#fd-pool").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["year", "total_amount", "prepay_ratio_pct"]);
    if (!body.org_group_id) delete body.org_group_id;
    else body.org_group_id = Number(body.org_group_id);
    postAction("/api/fund/pools", body, "#fd-msg");
  };
  if (picked && poolOpen) {
    // 超计划的警告要说出来（P2-1480）：预付只警告不拦截，回执里的 warning 是这道防线唯一的出口；原先走 postAction，成功即
    // 整页重画、回执整个丢掉，警告在页面上从来没出现过。先重画再写（同 P2-1432 / P2-1013）
    $("#fd-prepay").onsubmit = async (e) => {
      e.preventDefault();
      try {
        const r = await api(`/api/fund/pools/${picked}/prepayments`, { method: "POST",
          body: JSON.stringify(formJson(e.target, ["amount"])) });
        await route();
        if (r.warning) setMsg("#fd-msg", r.warning, false);
      } catch (err) { setMsg("#fd-msg", err.message, false); }
    };
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
  $("#page-body").onclick = async (e) => {
    if (e.target.dataset.fdpick) {
      localStorage.setItem("medplat_fund_pool", e.target.dataset.fdpick);
      return route();
    }
    if (e.target.id === "fd-distribute") {
      const formula_expr = $("#fd-formula").value.trim() || "score";
      return postAction(`/api/fund/pools/${picked}/distribute`, { formula_expr }, "#fd-msg");
    }
    const { fddist, fdedit } = e.target.dataset;
    if (fddist) {
      try {
        const rows = await api(`/api/fund/pools/${fddist}/distributions`);
        $("#fd-dist").innerHTML = `<h3 style="margin-top:14px">基金池 ${fddist} 的分配结果</h3>
          ${table(["机构", "绩效得分", "权重", "占比", "金额"], rows, (d) =>
            `<tr><td>${esc(d.org_name) || d.org_id}</td><td>${d.score}</td><td>${d.weight}</td>
             <td>${d.share_pct}%</td><td><b>${d.amount}</b></td></tr>`)}
          <p class="desc">得分是<b>分配当时冻结的快照</b>，此后调整指标权重不影响已分结果。</p>`;
      } catch (err) { $("#fd-dist").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
      return;
    }
    if (fdedit) {
      const pool = pools.find((x) => x.id === Number(fdedit));
      // 金额与比例用 text（P2-590，口径同应急资源编辑）：spdModal 的 number 字段把空串折成 0，原先
      // `if (picked2.prepay_ratio_pct)` 又把 0 当留空——预付比例改成 0（不预付）不送，照样保存成功、仍按原比例预付
      const picked2 = await spdModal(`编辑基金池 ${fdedit}（${pool ? pool.year : ""} 年度）`, [
        { name: "total_amount", label: "筹资总额（元，留空不改）", type: "text",
          value: pool ? pool.total_amount : "" },
        { name: "prepay_ratio_pct", label: "预付比例 %（0-100，0 = 不预付，留空不改）", type: "text",
          value: pool ? pool.prepay_ratio_pct : "" },
        { name: "status", label: "状态（「已清算」由清算置，这里改不了）", type: "select",
          value: pool ? pool.status : "active",
          options: [{ value: "active", label: "执行中" }, { value: "closed", label: "已归档" }] },   // 同后端 POOL_STATUS_NAMES
        { name: "note", label: "备注（留空不改）", type: "text", value: pool ? pool.note : "" },
      ]);
      if (!picked2) return;
      // 后端 exclude_unset + `if value is not None`：留空的键不送
      const body = { status: picked2.status };
      for (const [k, label] of [["total_amount", "筹资总额"], ["prepay_ratio_pct", "预付比例"]]) {
        if (picked2[k] === "") continue;
        if (!Number.isFinite(Number(picked2[k]))) return setMsg("#fd-msg", `${label}要填数值`, false);
        body[k] = Number(picked2[k]);
      }
      if (picked2.note) body.note = picked2.note;
      try {
        await api(`/api/fund/pools/${fdedit}`, { method: "PATCH", body: JSON.stringify(body) });
        return route();
      } catch (err) { return setMsg("#fd-msg", err.message, false); }
    }
  };
}

function renderSettlement(s, vars) {
  // 结余为 0 不给公式框与「按公式分配」（P2-1483）：后端收支相抵一律 409「本池收支相抵、结余为 0，无结余可分配」（P2-261），
  // 原先这里只按 is_overrun 分两支，结余 0 照样摆着公式框和按钮、点了必 409。直接写后端那一句（与 fund.distribute 同一句）
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
      : s.balance === 0
      ? `<p class="desc">本池收支相抵、结余为 0，无结余可分配</p>`
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
// 与后端 staffing.TITLE_LEVELS 同一张表（键与名由 tests/test_staffing_title_level_options.py 钉住）
const TITLE_LEVELS = { none: "未填", junior: "初级", intermediate: "中级", deputy_senior: "副高", senior: "正高" };

/** 派驻台账的筛选（P2-1314）：只留在内存里、不进存储——点了提示条才只看待补职称等级的那几条，「看全部」回去。
    `year` 是下沉指标的统计年度（P2-1509），空串为当年，同样只留在内存里（与绩效页 PERF_FILTER 同一个做法）：原先两次请求都不带
    年度，1 月 1 日一过，上一年度的下沉指标和「待补职称等级」在页面上都看不到了，而下沉指标是按年度上报的。统计与台账带同一个
    年度——提示条报的人次与台账 needs_level 筛出的必须是同一批。 */
const STAFFING_FILTER = { needs_level: false, year: "" };

async function renderStaffing() {
  $("#page-desc").textContent =
    "监测指标只认长期派驻满半年且中级及以上——巡诊不算下沉，职称等级必须显式维护而不从职称文本推断";
  const yearQuery = STAFFING_FILTER.year ? `year=${encodeURIComponent(STAFFING_FILTER.year)}` : "";
  const [rows, stats, orgs] = await Promise.all([
    api(`/api/staffing/secondments?limit=100${STAFFING_FILTER.needs_level ? "&needs_level=true" : ""}${yearQuery ? `&${yearQuery}` : ""}`),
    api(`/api/staffing/dispatch-stats${yearQuery ? `?${yearQuery}` : ""}`),
    api("/api/organizations"),
  ]);
  $("#page-body").innerHTML = `
    ${panel(`下沉指标（${stats.year} 年度）`, `
      <form class="inline" id="st-year">
        <input name="year" placeholder="统计年度 YYYY（默认当年）" value="${esc(STAFFING_FILTER.year)}" pattern="\\d{4}">
        <button>按此年度统计</button></form>
      ${stats.unknown_title_level
        ? `<p class="msg err">有 ${stats.unknown_title_level} 人次满足长期派驻满半年，
            但职称等级未维护，未计入"中级及以上"。请在下方台账补齐等级。
            <button class="btn sm" data-stneeds="1">台账只看这 ${stats.unknown_title_level} 人次</button></p>` : ""}
      ${table(["接收机构", "在派", "累计", "长期满半年", "其中中级及以上"], stats.orgs, (o) =>
        `<tr><td>${esc(o.org_name)}</td><td>${o.ongoing}</td><td>${o.total}</td>
         <td>${o.long_term_6m}</td><td><b>${o.long_term_6m_senior}</b></td></tr>`)}
      <p class="desc">${esc(stats.caliber.long_term_6m)}；${esc(stats.caliber.senior)}</p>
    `)}

    ${panel("新建派驻", `
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
    `)}

    ${panel("派驻台账", `
      ${STAFFING_FILTER.needs_level ? `<p class="desc">只看长期派驻满半年、职称等级未维护的
        <button class="btn sm secondary" data-stneeds="0">看全部</button></p>` : ""}
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
    `)}`;
  $("#st-year").onsubmit = (e) => {
    e.preventDefault();
    STAFFING_FILTER.year = String(new FormData(e.target).get("year") ?? "").trim();
    route();
  };
  $("#st-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/staffing/secondments",
      formJson(e.target, ["employee_id", "from_org_id", "to_org_id"]), "#st-msg"); };
  $("#page-body").onclick = async (e) => {
    const { stend, stlevel, stneeds } = e.target.dataset;
    if (stneeds) {
      // 提示条要人「在下方台账补齐等级」，可要补的恰是早建的行、不在最新 100 条里（P2-1314）：点了台账按同一个判据重取，
      // 补完一条重画时提示条与台账一起少一条
      STAFFING_FILTER.needs_level = stneeds === "1";
      return route();
    }
    if (stend) {
      // P2-43：原先点一下就结束、结束日一律记今天。"长期派驻满半年"按起止日期算，记错了这一人次
      // 就进不了下沉指标，且不能撤回。后端早就收可选的结束日期（补录用），这里一并给出。
      const form = await spdModal("结束派驻", [
        { name: "end_date", label: "结束日期", placeholder: "YYYY-MM-DD，留空为今天" },
      ], { intro: "结束后不能撤回；下沉指标里的「长期派驻满半年」按起止日期计算。" });
      if (!form) return;
      const q = form.end_date ? `?end_date=${encodeURIComponent(form.end_date)}` : "";
      return postAction(`/api/staffing/secondments/${stend}/end${q}`, null, "#st-msg");
    }
    if (stlevel) {
      // P2-38：原先要手打英文代码（junior/intermediate/…），打错被后端 422 拒回；而"中级及以上"
      // 正是下沉指标的判据。换成下拉，默认中级（与原先弹窗的预填一致）。
      const form = await spdModal("维护职称等级", [
        { name: "title_level", label: "职称等级", type: "select", value: "intermediate",
          options: Object.entries(TITLE_LEVELS).map(([value, label]) => ({ value, label })) },
      ], { intro: "下沉监测只认中级及以上（中级 / 副高 / 正高）。" });
      if (!form) return;
      try {
        await api(`/api/staffing/employees/${stlevel}/title-level`,
          { method: "PATCH", body: JSON.stringify({ title_level: form.title_level }) });
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
  const [programs, orgs] = await Promise.all([
    api("/api/disease-programs"), api("/api/organizations"),
  ]);
  // 校验提到取数之前：原先只有下面的 `current` 兜住了渲染，`/{picked}/stats`
  // 仍然会对已删除的专病发出去，404 掀掉整页——包括那张换专病用的目录表。
  const picked = pickedId("medplat_program", programs);
  let enrollments = [], stats = null;
  if (picked) {
    [enrollments, stats] = await Promise.all([
      api(`/api/disease-programs/enrollments?program_id=${picked}&limit=50`),
      api(`/api/disease-programs/${picked}/stats`),
    ]);
  }
  const current = programs.find((p) => p.id === picked);
  $("#page-body").innerHTML = `
    ${panel("专病目录", `
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
         <td><button data-dppick="${p.id}">打开</button>
             <button data-dpedit="${p.id}">编辑</button></td></tr>`)}
      <p class="desc">改路径<b>只影响此后的判定</b>：已经记下的节点一条都不会删，
        但在管病例的「待办节点」会跟着新路径变——把一个必需节点改成选做，
        完成度当场就上去了。这不是 bug，是目录即口径。</p>
    `)}

    ${picked && current ? panel(`${current.name} · 入组管理`, `
      ${current.active === false
        // 停用的专病目录不再摆入组表单（第十五批 S1-7）：后端入组对它 409「该专病目录已停用」，原先照摆、填完才被拒
        ? '<p class="desc">该专病目录已停用，不再入组；已入组的病例照常查看、记录节点。</p>'
        : `<form class="inline" id="dp-enroll">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="org_id">${orgs.map((o) =>
          `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>入组</button>
      </form>`}
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
         <td><button data-dptrace="${e.id}">轨迹</button>
             ${e.status === "enrolled"
               ? `<button data-dpnode="${e.id}">记录节点</button><button data-dpexit="${e.id}">出组</button>`
               : ""}</td></tr>`)}
      <div id="dp-detail"></div>
    `) : ""}`;

  $("#dp-form").onsubmit = (e) => {
    e.preventDefault();
    const raw = formJson(e.target);
    const body = { code: raw.code, name: raw.name, path_nodes: [] };
    if (raw.org_id) body.org_id = Number(raw.org_id);
    // "键:名称,键:名称" → 节点数组。缺省全部按必需处理，选做节点在目录里再改。
    // 全角逗号、顿号、全角冒号也认（P1-137 前端同一族）：原先只认半角，中文输入法填的「a：申请，b：审批」一个节点都拆不出，
    // 建出来是个没有节点的专病，不报错；拆不出「键:名称」的那一段点名报出来，不再悄悄丢
    const bad = [];
    (raw.nodes || "").split(/[,，、]/).map((s) => s.trim()).filter(Boolean).forEach((pair) => {
      const [key, name] = pair.split(/[:：]/).map((x) => (x || "").trim());
      if (key && name) body.path_nodes.push({ key, name, required: true });
      else bad.push(pair);
    });
    if (bad.length) return setMsg("#dp-msg", `路径节点要写成「键:名称」：${bad.join("、")}`, false);
    postAction("/api/disease-programs", body, "#dp-msg");
  };
  const enrollForm = $("#dp-enroll");   // 停用的目录不摆这张表
  if (picked && enrollForm) {
    enrollForm.onsubmit = (e) => { e.preventDefault();
      postAction(`/api/disease-programs/${picked}/enrollments`,
        formJson(e.target, ["patient_id", "org_id"]), "#dp-msg"); };
  }
  const nodeName = (key) => {
    const n = ((current && current.path_nodes) || []).find((x) => x.key === key);
    return n ? `${n.name}${n.required === false ? "（选做）" : ""}` : key;
  };
  const drawTrace = async (enrollmentId) => {
    try {
      // 按行上的 id 取，不提供"输入任意入组ID"的入口：这条端点没有归属校验也不留痕
      // （已登记 test_stage15_horizontal.py::NEWLY_VISIBLE_UNGUARDED_READS），
      // 而行里的 id 是上面那张**按机构收口过**的列表给出来的。
      const d = await api(`/api/disease-programs/enrollments/${encodeURIComponent(enrollmentId)}`);
      $("#dp-detail").innerHTML = `<h3 style="margin-top:14px">入组 ${d.id} 的路径轨迹</h3>
        <div class="cards">
          <div class="card"><span class="k">患者</span><b>${d.patient_id}</b></div>
          <div class="card"><span class="k">入组日</span><b>${esc(d.enrolled_at) || "—"}</b></div>
          <div class="card"><span class="k">出组日</span><b>${esc(d.exited_at) || "—"}</b></div>
          <div class="card"><span class="k">疗效</span><b>${esc(d.outcome_name)}</b></div>
        </div>
        ${d.exit_reason ? `<p class="desc">退出原因：${esc(d.exit_reason)}</p>` : ""}
        ${d.outcome_note ? `<p class="desc">疗效备注：${esc(d.outcome_note)}</p>` : ""}
        ${table(["节点", "完成时间", "经办人", "结果", "备注"], d.records, (r) =>
          `<tr><td>${esc(nodeName(r.node_key))}</td><td>${esc(r.performed_at) || "—"}</td>
           <td>${esc(r.operator_name) || "—"}</td><td>${esc(r.result) || "—"}</td>
           <td>${esc(r.note) || "—"}</td></tr>`)}
        <p class="desc">节点名按<b>当前目录</b>翻译：目录里已经删掉的节点，这里印的是原始 key
          ——记录本身不会因为改目录而消失，这正是"改路径只影响此后的判定"的另一面。</p>`;
    } catch (err) { $("#dp-detail").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#page-body").onclick = async (e) => {
    const { dppick, dpedit, dptrace, dpnode, dpexit } = e.target.dataset;
    try {
      if (dppick) { localStorage.setItem("medplat_program", dppick); return route(); }
      if (dptrace) return await drawTrace(dptrace);
      if (dpedit) {
        const prog = programs.find((x) => x.id === Number(dpedit));
        // 框自己提交（P2-607）：路径节点 JSON 写错、键重复、说明写超了时报错写在框里、框不关——原先框一关，改了一半的节点 JSON 就没了
        const ok = await spdModal(`编辑专病 ${prog ? prog.code : dpedit}`, [
          { name: "name", label: "专病名称（留空不改）", type: "text", value: prog ? prog.name : "" },
          { name: "description", label: "说明（留空不改）", type: "text",
            value: prog ? prog.description || "" : "" },
          { name: "active", label: "启停", type: "select", value: prog && prog.active ? "1" : "0",
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
          { name: "path_nodes", label: "路径节点 JSON（留空不改；key 不得重复）", type: "textarea",
            value: prog ? JSON.stringify(prog.path_nodes || []) : "" },
        ], { submit: (picked) => {
          // 后端 exclude_unset + `if value is not None`：留空的键不送，免得把说明清空
          const body = { active: picked.active === "1" };
          if (picked.name) body.name = picked.name;
          if (picked.description) body.description = picked.description;
          if (picked.path_nodes) {
            try { body.path_nodes = JSON.parse(picked.path_nodes); }
            catch (err) { throw new Error(`路径节点 JSON 解析失败：${err.message}`); }
          }
          return api(`/api/disease-programs/${dpedit}`, { method: "PATCH", body: JSON.stringify(body) });
        } });
        if (ok) route();
        return;
      }
      if (dpnode) {
        const nodes = (current.path_nodes || []);
        if (!nodes.length) return setMsg("#dp-msg", "本专病还没有配置路径节点，先在目录里「编辑」加上", false);
        // 节点键从目录里选：手打一个不在路径里的 key，后端 422，而完成度也永远算不对
        // 框自己提交（P2-607）：日期写错、备注写超了时报错写在框里、框不关，填的都在
        const ok = await spdModal("记录路径节点", [
          { name: "node_key", label: "节点", type: "select", value: nodes[0].key,
            options: nodes.map((n) => ({ value: n.key,
              label: `${n.name}${n.required === false ? "（选做）" : ""}` })) },
          { name: "performed_at", label: "完成日期（留空按业务日期记）", type: "text", value: "" },
          { name: "operator_name", label: "经办人（可留空）", type: "text", value: "" },
          { name: "result", label: "执行结果（可留空）", type: "text", value: "" },
          { name: "note", label: "备注（可留空）", type: "textarea", value: "" },
        ], { submit: (picked) => api(`/api/disease-programs/enrollments/${dpnode}/records`, { method: "POST",
          body: JSON.stringify(picked) }) });
        if (ok) route();
        return;
      }
      if (dpexit) {
        const picked = await spdModal("出组", [
          { name: "status", label: "出组方式", type: "select", value: "completed",
            options: [{ value: "completed", label: "完成出组" }, { value: "exited", label: "中途退出" }] },
          { name: "outcome", label: "疗效（留空=未评价，与「无效」不是一回事）", type: "select", value: "",
            options: [{ value: "", label: "未评价" }, { value: "cured", label: "治愈" },
              { value: "improved", label: "好转" }, { value: "stable", label: "稳定" },
              { value: "worsened", label: "加重" }, { value: "died", label: "死亡" }] },
          { name: "outcome_note", label: "疗效备注（可留空）", type: "text", value: "" },
          { name: "exit_reason", label: "退出原因（中途退出必填）", type: "text", value: "" },
        ]);
        if (!picked) return;
        if (picked.status === "exited" && !picked.exit_reason) {
          return setMsg("#dp-msg", "中途退出必须写退出原因——出组率的分母里，这一条要能解释", false);
        }
        return postAction(`/api/disease-programs/enrollments/${dpexit}/exit`, picked, "#dp-msg");
      }
    } catch (err) { setMsg("#dp-msg", err.message, false); }
  };
}


/* ---------------- 流程图形化编排（阶段十） ----------------
 *
 * 画布只做一件事：**产出与手写 JSON 完全相同的那份定义**。后端一行没改，
 * 提交仍走 `POST /api/workflows/definitions`——图形化是录入方式的差别，
 * 不是另一套模型。若画布产出的结构与手写的不一样，就等于凭空多出一个
 * 只有画布能生成、其它入口都不认的流程格式。
 *
 * 客户端把服务端的三条校验又实现了一遍（键唯一 / next 指向存在 / 至少一个终态），
 * 目的是**在提交前就把话说清楚**——这三种错留到运行期，表现是单据卡死在某个
 * 节点上没人能推，比录入时报错难查得多。但服务端仍然是权威：
 * 客户端校验只管提示，不管放行。
 *
 * 保存前沿 next 从链头重排（P2-1473，见 wfChainOrder）：后端从数组第 0 个节点起步，而画布按数组顺序摆、没有「改节点」，
 * 删了重加的节点排在末尾——存出去就是另一条流程。
 */

/* 节点沿 next 从链头排好的副本（P2-1473）。实例发起时落在 nodes[0]（`start_instance`），画布却没有「改节点」：要改首节点
 * 只能删了重加，新节点 push 到末尾，存出去的首项成了原先的第二步——实例发起后直接落在那里，申请一步从没发生；终态节点
 * 先画的，一推就「已完成」。链头 = 唯一一个没有入边的节点；只有链头唯一、且沿 next 走得到全部节点（就是一条直链）时才
 * 重排，有环、有走不到的、不止一个链头的原样返回、不拦——后端「首节点 / 可达」的口径随待裁定 P2-1033 定，这里只排不拦。 */
function wfChainOrder(nodes) {
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const pointed = new Set(nodes.map((n) => n.next).filter(Boolean));
  const heads = nodes.filter((n) => !pointed.has(n.key));
  if (heads.length !== 1) return nodes;
  const chain = [];
  for (let n = heads[0]; n && !chain.includes(n); n = byKey.get(n.next)) chain.push(n);
  return chain.length === nodes.length ? chain : nodes;
}

/* 定义表「节点链」一栏（P2-1473）：从实例实际的起点 nodes[0] 沿 next 打印，走回已经过的节点写「回到」并停下；起点走不到
 * 的节点另起一行标出来。原先按数组顺序拼接——删了重加首节点的定义印成「药学审核 → 院长审批 → 科室申请」，看上去申请是
 * 最后一步，实际实例从药学审核起步、科室申请永远不经过。 */
function wfChainHtml(nodes) {
  const label = (n) => `${esc(n.name)}${n.role ? `(${esc(n.role)})` : ""}`;
  const byKey = new Map(nodes.map((n) => [n.key, n]));
  const walked = [];
  let n = nodes[0];
  while (n && !walked.includes(n)) { walked.push(n); n = byKey.get(n.next); }
  const unreached = nodes.filter((x) => !walked.includes(x));
  return walked.map(label).join(" → ") + (n ? ` → 回到 ${label(n)}` : "")
    + (unreached.length ? `<br><span class="tag red">起点走不到</span> ${unreached.map(label).join("、")}` : "");
}

let WF_NODES = [];
let WF_SELECTED = "";
let WF_LINKING = false;

function wfCanvasInit(definitions) {
  WF_NODES = [];
  WF_SELECTED = "";
  WF_LINKING = false;
  wfCanvasDraw();

  $("#wfc-load").onchange = (e) => {
    const def = definitions.find((d) => d.key === e.target.value);
    if (!def) return;
    // 载入现有定义改编：深拷贝，避免改画布顺手改了列表里那份
    WF_NODES = def.nodes.map((n) => ({ key: n.key, name: n.name, role: n.role || "", next: n.next || "" }));
    $("#wfc-meta").key.value = def.key;
    $("#wfc-meta").name.value = def.name;
    WF_SELECTED = WF_NODES.length ? WF_NODES[0].key : "";
    wfCanvasDraw();
  };

  $("#wfc-add").onclick = async (e) => {
    e.preventDefault();
    // P2-38：原先三连问，角色要手打英文码——后端不校验角色名，打错一个字母就得到一个除管理员外
    // 谁也推不动的节点，发起之后才发现卡死。合成一个表单，角色从角色字典里选
    // （字典以后端为准，自定义角色也在；取不到时退回内置六个）。
    let roleMap = {};
    try { roleMap = await api("/api/users/roles"); } catch (err) { roleMap = {}; }
    const roles = { ...ROLE_NAMES, ...(roleMap || {}) };
    const form = await spdModal("加节点", [
      { name: "key", label: "节点编码（英文，如 approve）", required: true },
      { name: "name", label: "节点名称（如 审批；留空同编码）" },
      { name: "role", label: "可推进该节点的角色", type: "select", value: "", options: [
        { value: "", label: "任何登录用户" },
        ...Object.entries(roles).map(([value, label]) => ({ value, label: `${label}（${value}）` }))] },
    ]);
    if (!form) return;
    const { key, role } = form;
    if (WF_NODES.some((n) => n.key === key)) { setMsg("#wfc-msg", `节点 ${key} 已存在`, false); return; }
    const name = form.name || key;
    WF_NODES.push({ key, name, role, next: "" });
    WF_SELECTED = key;
    wfCanvasDraw();
  };

  $("#wfc-link").onclick = (e) => {
    e.preventDefault();
    if (!WF_SELECTED) { setMsg("#wfc-msg", "先点一个节点作为起点", false); return; }
    WF_LINKING = !WF_LINKING;
    setMsg("#wfc-msg", WF_LINKING ? `连线中：点击目标节点，作为「${WF_SELECTED}」的下一步` : "已退出连线", true);
    wfCanvasDraw();
  };

  $("#wfc-end").onclick = (e) => {
    e.preventDefault();
    const node = WF_NODES.find((n) => n.key === WF_SELECTED);
    if (!node) { setMsg("#wfc-msg", "先选中一个节点", false); return; }
    node.next = "";
    wfCanvasDraw();
  };

  $("#wfc-del").onclick = (e) => {
    e.preventDefault();
    if (!WF_SELECTED) return;
    WF_NODES = WF_NODES.filter((n) => n.key !== WF_SELECTED);
    // 指向被删节点的连线一并断开，否则会产出一份 next 悬空的定义
    WF_NODES.forEach((n) => { if (n.next === WF_SELECTED) n.next = ""; });
    WF_SELECTED = "";
    wfCanvasDraw();
  };

  $("#wfc-save").onclick = async (e) => {
    e.preventDefault();
    const form = $("#wfc-meta");
    const key = form.key.value.trim(), name = form.name.value.trim();
    if (!key || !name) { setMsg("#wfc-msg", "请先填流程编码与名称", false); return; }
    const problems = wfValidate();
    if (problems.length) { setMsg("#wfc-msg", problems.join("；"), false); return; }
    // 先沿 next 从链头重排（P2-1473）：删了重加的首节点排在数组末尾，原样存出去实例就从第二步起步
    WF_NODES = wfChainOrder(WF_NODES);
    wfCanvasDraw();
    postAction("/api/workflows/definitions", { key, name, nodes: WF_NODES }, "#wfc-msg");
  };
}

function wfValidate() {
  /* 与服务端 `_validate_nodes` 同三条规则。服务端仍是权威，这里只为早点说清楚。 */
  const problems = [];
  if (!WF_NODES.length) problems.push("至少要有一个节点");
  const keys = WF_NODES.map((n) => n.key);
  if (new Set(keys).size !== keys.length) problems.push("节点编码不得重复");
  WF_NODES.forEach((n) => {
    if (n.next && !keys.includes(n.next)) problems.push(`节点 ${n.key} 的下一步指向不存在的节点`);
  });
  if (WF_NODES.length && WF_NODES.every((n) => n.next)) problems.push("流程必须有终态节点（无下一步）");
  return problems;
}

function wfCanvasDraw() {
  const boxW = 150, boxH = 52, gapX = 60, padY = 30;
  const perRow = 4;
  const pos = {};
  WF_NODES.forEach((n, i) => {
    pos[n.key] = { x: (i % perRow) * (boxW + gapX) + 10, y: Math.floor(i / perRow) * 110 + padY };
  });
  const rows = Math.max(Math.ceil(WF_NODES.length / perRow), 1);
  const width = perRow * (boxW + gapX) + 20, height = rows * 110 + padY;

  let svg = `<defs><marker id="wf-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3"
      orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#0b6e6e"></path></marker></defs>`;
  WF_NODES.forEach((n) => {
    if (!n.next || !pos[n.next]) return;
    const a = pos[n.key], b = pos[n.next];
    const x1 = a.x + boxW, y1 = a.y + boxH / 2, x2 = b.x, y2 = b.y + boxH / 2;
    // 同行直连，跨行走折线——直连跨行会从别的节点身上穿过去
    const path = y1 === y2
      ? `M${x1},${y1} L${x2 - 6},${y2}`
      : `M${x1},${y1} L${x1 + 20},${y1} L${x1 + 20},${y2 - 34} L${x2 - 20},${y2 - 34} L${x2 - 20},${y2} L${x2 - 6},${y2}`;
    svg += `<path d="${path}" fill="none" stroke="#0b6e6e" stroke-width="1.6" marker-end="url(#wf-arrow)"></path>`;
  });
  WF_NODES.forEach((n) => {
    const p = pos[n.key];
    const selected = n.key === WF_SELECTED;
    const terminal = !n.next;
    svg += `<g class="wf-node" data-node="${esc(n.key)}" style="cursor:pointer">
      <rect x="${p.x}" y="${p.y}" width="${boxW}" height="${boxH}" rx="6"
        fill="${terminal ? "#eef6f6" : "#ffffff"}" stroke="${selected ? "#b45309" : "#0b6e6e"}"
        stroke-width="${selected ? 2.4 : 1.4}"></rect>
      <text x="${p.x + 10}" y="${p.y + 21}" font-size="13" fill="#24292f">${esc(n.name)}</text>
      <text x="${p.x + 10}" y="${p.y + 39}" font-size="11" fill="#5b6773">${esc(n.key)}${
        n.role ? " · " + esc(n.role) : " · 任意角色"}${terminal ? " · 终态" : ""}</text></g>`;
  });

  $("#wfc-canvas").innerHTML = WF_NODES.length
    ? `<svg width="${width}" height="${height}" role="img">${svg}</svg>`
    : '<p class="empty">点「加节点」开始画，或从上方载入现有定义改编</p>';

  const problems = wfValidate();
  // 预览按保存时的顺序印（P2-1473）：面板上写着「提交给后端的就是它」
  $("#wfc-json").textContent = JSON.stringify(wfChainOrder(WF_NODES), null, 2)
    + (WF_NODES.length && problems.length ? `\n\n// 待修正：${problems.join("；")}` : "");

  $("#wfc-canvas").querySelectorAll(".wf-node").forEach((g) => {
    g.onclick = () => {
      const key = g.dataset.node;
      if (WF_LINKING && WF_SELECTED && key !== WF_SELECTED) {
        const from = WF_NODES.find((n) => n.key === WF_SELECTED);
        from.next = key;
        WF_LINKING = false;
        setMsg("#wfc-msg", `已连：${from.key} → ${key}`, true);
      } else {
        WF_SELECTED = key;
      }
      wfCanvasDraw();
    };
  });
}
