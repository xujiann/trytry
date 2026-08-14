/* 管理端 · 页面（一）：驾驶舱、共享诊断、会诊转诊、预约、处方药事等。 */

async function renderInfectious() {
  $("#page-desc").textContent = "病例报告 + 滑动窗口多点触发预警（多机构同报升级为高风险）";
  const [cases, alerts] = await Promise.all([api("/api/infectious/cases"), api("/api/infectious/alerts")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>病例报告</h3>
      <form class="inline" id="case-form">
        <input name="org_id" type="number" placeholder="报告机构ID" required>
        <input name="disease_code" placeholder="病种编码" required>
        <input name="disease_name" placeholder="病种名称" required>
        <input name="onset_date" placeholder="发病日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>报告</button>
      </form><p class="msg" id="case-msg"></p></div>
    ${alerts.length ? `<div class="panel"><h3>⚠ 当前预警</h3>${
      table(["病种", "7日病例数", "报告机构数", "风险等级"], alerts, (a) =>
        `<tr><td>${esc(a.disease_name)}</td><td>${a.case_count}</td><td>${a.org_count}</td>
         <td><span class="tag ${a.severity === "high" ? "red" : "orange"}">${a.severity === "high" ? "高" : "中"}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>病例列表</h3>${table(["ID", "机构", "病种", "发病日期"], cases, (c) =>
      `<tr><td>${c.id}</td><td>${c.org_id}</td><td>${esc(c.disease_name)}</td><td>${esc(c.onset_date)}</td></tr>`)}</div>`;
  $("#case-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/infectious/cases", { method: "POST", body: JSON.stringify({
        org_id: Number(f.get("org_id")), disease_code: f.get("disease_code"),
        disease_name: f.get("disease_name"), onset_date: f.get("onset_date") }) });
      route();
    } catch (err) { setMsg("#case-msg", err.message, false); }
  };
}

async function renderArchive() {
  $("#page-desc").textContent = "门诊接诊登记；按电子健康卡号汇聚档案、就诊、报告、慢病、处方";
  const encounters = await api("/api/encounters?limit=50");
  $("#page-body").innerHTML = `
    <div class="panel"><h3>门诊接诊登记</h3>
      <form class="inline" id="enc-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="doctor_name" placeholder="接诊医师">
        <input name="diagnosis_code" placeholder="诊断编码"><input name="diagnosis_name" placeholder="诊断名称">
        <input name="summary" placeholder="诊疗摘要" style="min-width:220px"><button>登记</button></form>
      <p class="msg" id="enc-msg"></p>
      <p class="desc">就诊记录是县域就诊率、诊次成本与绩效变量的共同数据源。</p>
      ${table(["ID", "患者", "机构", "类型", "诊断", "医师"], encounters, (e) =>
        `<tr><td>${e.id}</td><td>${e.patient_id}</td><td>${e.org_id}</td>
         <td>${e.encounter_type === "inpatient" ? "住院" : "门诊"}</td>
         <td>${esc(e.diagnosis_name || "—")}</td><td>${esc(e.doctor_name || "—")}</td></tr>`)}</div>
    <div class="panel"><h3>患者 360 视图</h3>
      <form class="inline" id="archive-form">
        <input name="ehc_no" placeholder="电子健康卡号" required>
        <button>查询</button>
      </form>
      <div id="archive-result"></div></div>`;
  $("#enc-form").onsubmit = (e) => { e.preventDefault();
    postAction("/api/encounters", formJson(e.target, ["patient_id", "org_id"]), "#enc-msg"); };
  $("#archive-form").onsubmit = async (e) => {
    e.preventDefault();
    const ehcNo = new FormData(e.target).get("ehc_no");
    try {
      const archive = await api(`/api/archive/${encodeURIComponent(ehcNo)}`);
      const more = Object.entries(archive.has_more || {}).filter(([, v]) => v).map(([k]) => k);
      $("#archive-result").innerHTML = `
        ${more.length ? `<p class="msg">以下分段超过 ${archive.section_limit} 条已截断：${more.join("、")}，
          完整清单请到对应业务页查询。</p>` : ""}
        <pre class="json">${esc(JSON.stringify(archive, null, 2))}</pre>`;
    } catch (err) { $("#archive-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
}

async function renderUsers() {
  $("#page-desc").textContent = "账号开通、角色分配与变更留痕、系统参数配置（仅管理员）";
  const [usersList, orgs, roleChanges, params] = await Promise.all([
    api("/api/users"), api("/api/organizations"), api("/api/users/role-changes"), api("/api/mgmt/params")]);
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>开通账号</h3>
      <form class="inline" id="user-form">
        <input name="username" placeholder="用户名（≥3位）" required minlength="3">
        <input name="password" type="password" placeholder="初始密码（≥6位）" required minlength="6">
        <input name="full_name" placeholder="姓名">
        <select name="role">${Object.entries(ROLE_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="org_id"><option value="">不挂机构</option>${orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>开通</button>
      </form><p class="msg" id="user-msg"></p></div>
    <div class="panel"><h3>修改本人密码</h3>
      <form class="inline" id="pwd-form">
        <input name="current_password" type="password" placeholder="当前密码" required>
        <input name="new_password" type="password" placeholder="新密码（≥6位）" required minlength="6">
        <button>修改</button>
      </form><p class="msg" id="pwd-msg"></p></div>
    <div class="panel">${table(["ID", "用户名", "姓名", "角色", "所属机构", "操作"], usersList, (u) =>
      `<tr><td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.full_name) || "—"}</td>
       <td><span class="tag">${ROLE_NAMES[u.role] || esc(u.role)}</span></td>
       <td>${u.org_id ? esc(orgNames[u.org_id] || u.org_id) : "—"}</td>
       <td><button class="btn secondary" data-chrole="${u.id}">调角色</button></td></tr>`)}</div>
    <div class="panel"><h3>角色变更记录（留痕，变更即吊销旧令牌）</h3>${
      table(["用户ID", "原角色", "新角色", "操作人", "时间"], roleChanges, (r) =>
        `<tr><td>${r.user_id}</td><td><span class="tag">${ROLE_NAMES[r.old_role] || esc(r.old_role)}</span></td>
         <td><span class="tag green">${ROLE_NAMES[r.new_role] || esc(r.new_role)}</span></td>
         <td>${r.changed_by}</td><td>${esc(r.at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>
    <div class="panel"><h3>系统参数配置（键值集中管理）</h3>
      <form class="inline" id="param-form">
        <input name="key" placeholder="参数键（如 portal.verify_lock_seconds）" required style="min-width:240px">
        <input name="value" placeholder="参数值" required>
        <input name="description" placeholder="说明" style="min-width:180px">
        <button>保存</button></form>
      <p class="msg" id="param-msg"></p>
      ${table(["键", "值", "说明", "更新时间"], params, (p) =>
        `<tr><td><span class="tag">${esc(p.key)}</span></td><td>${esc(p.value)}</td><td>${esc(p.description) || "—"}</td>
         <td>${esc(p.updated_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>`;
  $("#user-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/users", { method: "POST", body: JSON.stringify({
        username: f.get("username"), password: f.get("password"), full_name: f.get("full_name"),
        role: f.get("role"), org_id: f.get("org_id") ? Number(f.get("org_id")) : null }) });
      route();
    } catch (err) { setMsg("#user-msg", err.message, false); }
  };
  $("#pwd-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      await api("/api/auth/change-password", { method: "POST", body: JSON.stringify({
        current_password: f.get("current_password"), new_password: f.get("new_password") }) });
      setMsg("#pwd-msg", "密码已修改");
      e.target.reset();
    } catch (err) { setMsg("#pwd-msg", err.message, false); }
  };
  $("#param-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/params", formJson(e.target), "#param-msg"); };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.chrole;
    if (!id) return;
    const keys = Object.keys(ROLE_NAMES);
    const pick = prompt(`新角色（${keys.map((k, i) => `${i + 1}=${ROLE_NAMES[k]}`).join("，")}）输入序号`);
    const role = keys[Number(pick) - 1]; if (!role) return;
    try {
      await api(`/api/users/${id}/role`, { method: "PATCH", body: JSON.stringify({ role }) });
      route();
    } catch (err) { setMsg("#user-msg", err.message, false); }
  };
}

async function renderAudit() {
  $("#page-desc").textContent = "全部写操作留痕（等保三级安全审计），仅管理员可查";
  const draw = async (username = "") => {
    const logs = await api(`/api/audit?limit=200${username ? `&username=${encodeURIComponent(username)}` : ""}`);
    $("#audit-table").innerHTML = table(["时间", "用户", "操作", "接口", "结果"], logs, (l) =>
      `<tr><td>${esc(l.at.replace("T", " ").slice(0, 19))}</td><td>${esc(l.username)}</td>
       <td><span class="tag">${esc(l.method)}</span></td><td>${esc(l.path)}</td>
       <td><span class="tag ${l.status_code < 400 ? "green" : "red"}">${l.status_code}</span></td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="panel">
      <form class="inline" id="audit-search"><input name="username" placeholder="按用户名过滤"><button>查询</button></form>
      <div id="audit-table"></div></div>`;
  await draw();
  $("#audit-search").onsubmit = async (e) => { e.preventDefault(); await draw(new FormData(e.target).get("username")); };
}

async function renderAccessLogs() {
  // 敏感读留痕查询（第十轮）：写审计回答"谁改了什么"，这里回答"谁凭什么看了谁"。
  $("#page-desc").textContent = "档案调阅留痕：谁、什么时候、凭什么依据、看了谁（院长/管理员可查）";
  const draw = async (params = {}) => {
    const q = Object.entries(params).filter(([, v]) => v).map(([k, v]) =>
      `${k}=${encodeURIComponent(v)}`).join("&");
    const rows = await api(`/api/access-logs?limit=200${q ? "&" + q : ""}`);
    $("#al-table").innerHTML = table(
      ["时间", "调阅人", "所属机构", "看了谁", "数据", "依据"], rows, (r) =>
      `<tr><td>${esc((r.at || "").replace("T", " ").slice(0, 19))}</td>
       <td>${esc(r.viewer)}</td><td>${esc(r.viewer_org_name)}</td>
       <td>${esc(r.patient_name)}</td><td>${esc(r.resource_name)}</td>
       <td><span class="tag">${esc(r.basis_name)}</span></td></tr>`);
  };
  $("#page-body").innerHTML = `
    <div class="panel">
      <form class="inline" id="al-search">
        <input name="patient_id" placeholder="患者ID">
        <input name="username" placeholder="调阅人账号">
        <input name="basis" placeholder="依据(encounter/referral/…)">
        <input name="start" placeholder="起 YYYY-MM-DD"><input name="end" placeholder="止 YYYY-MM-DD">
        <button>查询</button></form>
      <p class="desc">按患者查询会一并留痕——查"谁看过某人"本身也是在看这个人的隐私。</p>
      <div id="al-table"></div></div>`;
  await draw();
  $("#al-search").onsubmit = async (e) => {
    e.preventDefault(); await draw(formJson(e.target));
  };
}

/* ---------- 通用小工具：表单序列化 + 动作分派 ---------- */
function formJson(form, numFields = []) {
  const f = new FormData(form), out = {};
  for (const [k, v] of f.entries()) {
    if (v === "") continue;
    out[k] = numFields.includes(k) ? Number(v) : v;
  }
  return out;
}

async function postAction(path, body, msgSel, method) {
  try { await api(path, { method: method || "POST", body: body ? JSON.stringify(body) : undefined }); route(); }
  catch (err) { setMsg(msgSel, err.message, false); }
}

/* ---------- 附件通用控件：multipart 上传 / 鉴权下载 / 按 owner 列表 ---------- */

async function uploadAttachment(ownerType, ownerId, fileInput) {
  const file = fileInput.files[0];
  if (!file) throw new Error("请选择文件（图片或PDF，≤10MB）");
  const fd = new FormData();
  fd.append("file", file);
  fd.append("owner_type", ownerType);
  fd.append("owner_id", ownerId);
  const resp = await fetch("/api/attachments", {
    method: "POST", headers: { Authorization: `Bearer ${token}` }, body: fd });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || `上传失败(${resp.status})`);
  return data;
}

/* 块1：报告打印——服务端渲染的打印页需带令牌拉取，取回后写入新窗口并唤起打印 */
async function openPrintPage(path) {
  const resp = await fetch(path, { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) {
    const data = await resp.json().catch(() => ({}));
    throw new Error(data.detail || `打印页加载失败(${resp.status})`);
  }
  const html = await resp.text();
  const win = window.open("", "_blank");
  if (!win) throw new Error("浏览器拦截了新窗口，请允许弹出后重试");
  win.document.open();
  win.document.write(html);
  win.document.close();
  win.focus();
  setTimeout(() => win.print(), 300);
}

async function downloadAttachment(id, filename) {
  const resp = await fetch(`/api/attachments/${id}`, { headers: { Authorization: `Bearer ${token}` } });
  if (!resp.ok) throw new Error(`下载失败(${resp.status})`);
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement("a");
  a.href = url;
  a.download = filename || `attachment-${id}`;
  a.click();
  URL.revokeObjectURL(url);
}

async function drawAttachments(ownerType, ownerId, containerSel, msgSel) {
  const list = await api(`/api/attachments?owner_type=${ownerType}&owner_id=${ownerId}`);
  const el = $(containerSel);
  el.innerHTML = table(["ID", "文件名", "类型", "大小", "上传时间", "操作"], list, (a) =>
    `<tr><td>${a.id}</td><td>${esc(a.filename)}</td><td><span class="tag">${esc(a.content_type)}</span></td>
     <td>${(a.size / 1024).toFixed(1)} KB</td><td>${esc(a.created_at.slice(0, 16).replace("T", " "))}</td>
     <td><button class="btn secondary" data-attdl="${a.id}" data-fn="${esc(a.filename)}">下载</button></td></tr>`);
  el.onclick = async (e) => {
    const { attdl, fn } = e.target.dataset;
    if (!attdl) return;
    try { await downloadAttachment(attdl, fn); }
    catch (err) { setMsg(msgSel, err.message, false); }
  };
}

async function renderEmergency() {
  $("#page-desc").textContent = "呼救调度→转运（生命体征回传）→到院→收治，上车即入院";
  const cases = await api("/api/emergency/cases");
  const ES = { dispatched: ["已调度", "orange"], en_route: ["转运中", "orange"], arrived: ["已到院", ""], admitted: ["已收治", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>呼救登记</h3>
      <form class="inline" id="em-form">
        <input name="location" placeholder="事发地点" required><input name="symptom" placeholder="主诉">
        <input name="ambulance_no" placeholder="车牌"><input name="dest_org_id" type="number" placeholder="目标医院ID">
        <input name="patient_id" type="number" placeholder="患者ID(可空)"><button>调度</button>
      </form><p class="msg" id="em-msg"></p></div>
    <div class="panel">${table(["ID", "地点", "主诉", "车辆", "状态", "操作"], cases, (c) => {
      const [t, col] = ES[c.status] || [c.status, ""];
      return `<tr><td>${c.id}</td><td>${esc(c.location)}</td><td>${esc(c.symptom)}</td><td>${esc(c.ambulance_no)}</td>
        <td><span class="tag ${col}">${t}</span></td>
        <td>${c.status !== "admitted" ? `<button class="btn secondary" data-adv="${c.id}">流转</button>
          <button class="btn secondary" data-vital="${c.id}">回传体征</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#em-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id", "patient_id"]), "#em-msg"); };
  $("#page-body").onclick = async (e) => {
    const { adv, vital } = e.target.dataset;
    if (adv) return postAction(`/api/emergency/cases/${adv}/advance`, null, "#em-msg");
    if (vital) {
      const hr = prompt("心率"); if (hr === null) return;
      return postAction(`/api/emergency/cases/${vital}/vitals`, { heart_rate: Number(hr) || null, note: prompt("备注") || "" }, "#em-msg");
    }
  };
}

async function renderTelemedicine() {
  $("#page-desc").textContent = "在线咨询、复诊续方（续方须关联已过审处方）";
  const consults = await api("/api/telemedicine/consults");
  const TS = { open: ["待回复", "orange"], replied: ["已回复", "green"], closed: ["已结束", ""] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>发起咨询</h3>
      <form class="inline" id="tm-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="consult_type"><option value="consult">在线咨询</option><option value="repeat_rx">复诊续方</option></select>
        <input name="question" placeholder="咨询内容" required style="min-width:220px"><button>提交</button>
      </form><p class="msg" id="tm-msg"></p></div>
    <div class="panel">${table(["ID", "患者", "类型", "内容", "回复", "关联处方", "状态", "操作"], consults, (c) => {
      const [t, col] = TS[c.status] || [c.status, ""];
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.consult_type === "repeat_rx" ? "续方" : "咨询"}</td>
        <td>${esc(c.question)}</td><td>${esc(c.reply) || "—"}</td><td>${c.prescription_id ?? "—"}</td>
        <td><span class="tag ${col}">${t}</span></td>
        <td>${c.status === "open" ? `<button class="btn secondary" data-reply="${c.id}">回复</button>`
          : c.status === "replied" ? `<button class="btn secondary" data-close="${c.id}">结束</button>` : "—"}</td></tr>`;
    })}</div>`;
  $("#tm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/telemedicine/consults", formJson(e.target, ["patient_id", "org_id"]), "#tm-msg"); };
  $("#page-body").onclick = (e) => {
    const { reply, close } = e.target.dataset;
    if (reply) {
      const text = prompt("回复内容"); if (!text) return;
      const rxid = prompt("关联处方ID（续方时填写，可空）");
      return postAction(`/api/telemedicine/consults/${reply}/reply`, { reply: text, doctor_name: prompt("医师姓名") || "医师", prescription_id: rxid ? Number(rxid) : null }, "#tm-msg");
    }
    if (close) return postAction(`/api/telemedicine/consults/${close}/close`, null, "#tm-msg");
  };
}

async function renderTcm() {
  $("#page-desc").textContent = "智能辅诊（辨证推荐）、共享中药房追溯、适宜技术库";
  const [orders, techniques] = await Promise.all([api("/api/tcm/dispense-orders"), api("/api/tcm/techniques")]);
  const DS = { ordered: "已下单", dispensed: "已调配", decocted: "已煎煮", delivering: "配送中", delivered: "已送达" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>智能辨证</h3>
      <form class="inline" id="tcm-diag"><input name="symptoms" placeholder="症状（逗号分隔，如：乏力,气短）" required style="min-width:280px"><button>辨证</button></form>
      <div id="tcm-diag-result"></div></div>
    <div class="panel"><h3>共享中药房下单</h3>
      <form class="inline" id="tcm-order">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="from_org_id" type="number" placeholder="机构ID" required>
        <input name="herbs" placeholder="处方饮片" required style="min-width:220px"><input name="doses" type="number" value="7" min="1" style="min-width:60px">
        <select name="decoct"><option value="true">代煎</option><option value="false">免煎</option></select><button>下单</button>
      </form><p class="msg" id="tcm-msg"></p>
      ${table(["ID", "患者", "饮片", "剂数", "状态", "操作"], orders, (o) =>
        `<tr><td>${o.id}</td><td>${o.patient_id}</td><td>${esc(o.herbs)}</td><td>${o.doses}</td>
         <td><span class="tag ${o.status === "delivered" ? "green" : "orange"}">${DS[o.status]}</span></td>
         <td>${o.status !== "delivered" ? `<button class="btn secondary" data-adv="${o.id}">流转</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>适宜技术库</h3>
      ${table(["名称", "分类", "适应症"], techniques, (t) =>
        `<tr><td>${esc(t.name)}</td><td>${esc(t.category)}</td><td>${esc(t.indication)}</td></tr>`)}</div>`;
  $("#tcm-diag").onsubmit = async (e) => {
    e.preventDefault();
    const symptoms = new FormData(e.target).get("symptoms").split(/[,，]/).map((s) => s.trim()).filter(Boolean);
    const result = await api("/api/tcm/assist-diagnosis", { method: "POST", body: JSON.stringify({ symptoms }) });
    $("#tcm-diag-result").innerHTML = `<pre class="json">${esc(JSON.stringify(result.recommendations, null, 2))}</pre>`;
  };
  $("#tcm-order").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "from_org_id", "doses"]);
    body.decoct = body.decoct === "true";
    postAction("/api/tcm/dispense-orders", body, "#tcm-msg");
  };
  $("#page-body").onclick = (e) => { if (e.target.dataset.adv) postAction(`/api/tcm/dispense-orders/${e.target.dataset.adv}/advance`, null, "#tcm-msg"); };
  await drawTcmPreparations();  // 块4⑭ 中药制剂管理
}

async function renderMedication() {
  $("#page-desc").textContent = "缺药登记流转、供应风险研判、全县用药地图、居民用药画像";
  const [shortages, stats, risk] = await Promise.all([
    api("/api/medication/shortages"), api("/api/medication/usage-stats"), api("/api/medication/supply-risk")]);
  const SS = { registered: ["已登记", "orange"], purchasing: ["采购中", "orange"], delivered: ["已配送", "green"] };
  $("#page-body").innerHTML = `
    ${risk.total ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 药品供应风险评估（${risk.total}）</h3>${
      table(["药品编码", "药品", "库存告警机构数", "未结案缺药登记", "风险等级"], risk.risks, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${esc(r.drug_name) || "—"}</td><td>${r.low_stock_orgs}</td><td>${r.open_shortages}</td>
         <td><span class="tag ${r.risk_level === "high" ? "red" : "orange"}">${r.risk_level === "high" ? "高" : "中"}</span></td></tr>`)}</div>` : ""}`;
  $("#page-body").innerHTML += `
    <div class="panel"><h3>缺药登记</h3>
      <form class="inline" id="short-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>登记</button>
      </form><p class="msg" id="short-msg"></p>
      ${table(["ID", "机构", "药品", "数量", "状态", "操作"], shortages, (s) => {
        const [t, col] = SS[s.status];
        return `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${esc(s.drug_name)}</td><td>${s.quantity}</td>
          <td><span class="tag ${col}">${t}</span></td>
          <td>${s.status !== "delivered" ? `<button class="btn secondary" data-adv="${s.id}">流转</button>` : "—"}</td></tr>`;
      })}</div>
    <div class="panel"><h3>用药画像查询</h3>
      <form class="inline" id="prof-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="prof-result"></div></div>
    <div class="panel"><h3>全县用药地图（品种排名）</h3>
      ${stats.length ? barChart(stats.slice(0, 8).map((s) => [s.drug_name, s.rx_count]), { unit: " 方" }) : "暂无数据"}</div>`;
  $("#short-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/medication/shortages", formJson(e.target, ["org_id", "quantity"]), "#short-msg"); };
  $("#prof-form").onsubmit = async (e) => {
    e.preventDefault();
    const profile = await api(`/api/medication/profile/${new FormData(e.target).get("patient_id")}`);
    $("#prof-result").innerHTML = `${profile.polypharmacy_warning ? '<p class="msg err">⚠ 多重用药风险</p>' : ""}<pre class="json">${esc(JSON.stringify(profile, null, 2))}</pre>`;
  };
  $("#page-body").onclick = (e) => { if (e.target.dataset.adv) postAction(`/api/medication/shortages/${e.target.dataset.adv}/advance`, null, "#short-msg"); };
}

async function renderInsurance() {
  $("#page-desc").textContent = "结算记录、转诊证明、特殊病种申报、双通道药品申报、基金监测";
  const [fund, settlements, apps, dualApps] = await Promise.all([
    api("/api/insurance/fund-stats"), api("/api/insurance/settlements"),
    api("/api/insurance/special-diseases"), api("/api/insurance/dual-channel")]);
  const canReviewDual = ["director", "admin"].includes(currentRole());
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">医保基金支出总额</div><div class="value">${fund.insurance_pay_total}</div></div>
      <div class="card"><div class="label">县域内结算占比</div><div class="value">${fund.local_ratio_pct}%</div></div>
      <div class="card"><div class="label">基层支出占比</div><div class="value">${fund.grassroots_ratio_pct}%</div></div></div>
    <div class="panel"><h3>结算登记</h3>
      <form class="inline" id="ins-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="settle_type"><option value="local">本地</option><option value="remote">异地</option></select>
        <input name="total_amount" type="number" step="any" placeholder="总额" required><input name="insurance_pay" type="number" step="any" placeholder="医保支付" required>
        <input name="self_pay" type="number" step="any" placeholder="自付" required><button>登记</button>
      </form>
      <h3 style="margin-top:12px">转诊证明 / 特病申报</h3>
      <form class="inline" id="cert-form"><input name="referral_id" type="number" placeholder="转诊记录ID" required><button>签发证明</button></form>
      <form class="inline" id="spec-form"><input name="patient_id" type="number" placeholder="患者ID" required><input name="disease_name" placeholder="病种" required><button>特病申报</button></form>
      <p class="msg" id="ins-msg"></p></div>
    <div class="panel"><h3>特病申报队列</h3>${table(["ID", "患者", "病种", "状态", "操作"], apps, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.disease_name)}</td>
       <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${a.status}</span></td>
       <td>${a.status === "applied" ? `<button class="btn secondary" data-ok="${a.id}">批准</button><button class="btn danger" data-no="${a.id}">驳回</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>双通道药品申报（医师/经办申报 → 管理层审核）</h3>
      <form class="inline" id="dual-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="reason" placeholder="申报理由" style="min-width:180px">
        <button>申报</button></form>
      ${table(["ID", "患者", "药品", "理由", "状态", "审核意见", "操作"], dualApps, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.drug_name)}</td><td>${esc(a.reason) || "—"}</td>
         <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${a.status === "approved" ? "已批准" : a.status === "rejected" ? "已驳回" : "待审核"}</span></td>
         <td>${esc(a.review_comment) || "—"}</td>
         <td>${a.status === "pending" && canReviewDual
           ? `<button class="btn secondary" data-dualok="${a.id}">批准</button><button class="btn danger" data-dualno="${a.id}">驳回</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>结算记录</h3>${table(["ID", "患者", "机构", "类型", "总额", "医保付", "自付"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${s.org_id}</td><td>${s.settle_type === "local" ? "本地" : "异地"}</td>
       <td>${s.total_amount}</td><td>${s.insurance_pay}</td><td>${s.self_pay}</td></tr>`)}</div>`;
  $("#ins-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/settlements", formJson(e.target, ["patient_id", "org_id", "total_amount", "insurance_pay", "self_pay"]), "#ins-msg"); };
  $("#cert-form").onsubmit = async (e) => {
    e.preventDefault();
    try { const c = await api(`/api/insurance/referral-certs/${new FormData(e.target).get("referral_id")}`, { method: "POST" }); setMsg("#ins-msg", `证明号：${c.cert_no}`); }
    catch (err) { setMsg("#ins-msg", err.message, false); }
  };
  $("#spec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/special-diseases", formJson(e.target, ["patient_id"]), "#ins-msg"); };
  $("#dual-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/dual-channel", formJson(e.target, ["patient_id"]), "#ins-msg"); };
  $("#page-body").onclick = (e) => {
    const { ok, no, dualok, dualno } = e.target.dataset;
    if (ok) postAction(`/api/insurance/special-diseases/${ok}/review?approve=true`, null, "#ins-msg");
    if (no) postAction(`/api/insurance/special-diseases/${no}/review?approve=false`, null, "#ins-msg");
    if (dualok) postAction(`/api/insurance/dual-channel/${dualok}/review?approve=true&comment=${encodeURIComponent(prompt("审核意见") || "")}`, null, "#ins-msg");
    if (dualno) postAction(`/api/insurance/dual-channel/${dualno}/review?approve=false&comment=${encodeURIComponent(prompt("驳回理由") || "")}`, null, "#ins-msg");
  };
}

async function renderEducation() {
  $("#page-desc").textContent = "课程管理、培训考核（60分合格）、个人学分；直播申请与排期审核";
  const [courses, mine, lives] = await Promise.all([
    api("/api/education/courses"), api("/api/education/my-records"), api("/api/education/live-sessions")]);
  const role = currentRole();
  const LS = { pending: ["待审核", "orange"], approved: ["已排期", ""], rejected: ["已驳回", "red"], finished: ["已结束", "green"] };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>新建课程（管理员）</h3>
      <form class="inline" id="course-form">
        <input name="title" placeholder="课程名" required style="min-width:220px">
        <select name="course_type"><option value="vod">点播</option><option value="live">直播</option></select>
        <select name="category"><option value="clinical">临床医学</option><option value="tcm">中医适宜技术</option><option value="public_health">公共卫生</option></select>
        <input name="speaker" placeholder="讲者"><button>创建</button>
      </form><p class="msg" id="edu-msg"></p></div>
    <div class="panel"><h3>课程列表</h3>${table(["ID", "课程", "形式", "类别", "讲者", "操作"], courses, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.title)}</td><td>${c.course_type === "live" ? "直播" : "点播"}</td><td>${esc(c.category)}</td><td>${esc(c.speaker)}</td>
       <td><button class="btn secondary" data-exam="${c.id}">提交考核</button></td></tr>`)}</div>
    <div class="panel"><h3>我的学习记录</h3>${table(["课程", "成绩", "结果"], mine, (r) =>
      `<tr><td>${esc(r.title)}</td><td>${r.score}</td><td><span class="tag ${r.passed ? "green" : "red"}">${r.passed ? "合格" : "未合格"}</span></td></tr>`)}</div>
    <div class="panel"><h3>直播管理（申请 → 管理层排期审核 → 结束；音视频通道为对接项）</h3>
      <form class="inline" id="live-form">
        <input name="title" placeholder="直播主题" required style="min-width:220px">
        <input name="speaker" placeholder="主讲人">
        <input name="planned_at" placeholder="计划时间（如 2026-09-01 19:00）">
        <button>申请直播</button></form>
      ${table(["ID", "主题", "主讲", "计划时间", "状态", "审核意见", "操作"], lives, (s) => {
        const [text, color] = LS[s.status] || [s.status, ""];
        const actions = s.status === "pending" && ["director", "admin"].includes(role)
          ? `<button class="btn secondary" data-liveok="${s.id}">排期</button>
             <button class="btn danger" data-liveno="${s.id}">驳回</button>`
          : s.status === "approved" && ["director", "operator", "admin"].includes(role)
          ? `<button class="btn secondary" data-livefin="${s.id}">结束</button>` : "—";
        return `<tr><td>${s.id}</td><td>${esc(s.title)}</td><td>${esc(s.speaker) || "—"}</td><td>${esc(s.planned_at) || "—"}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${esc(s.review_comment) || "—"}</td><td>${actions}</td></tr>`;
      })}</div>`;
  $("#course-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/courses", formJson(e.target), "#edu-msg"); };
  $("#live-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/live-sessions", formJson(e.target), "#edu-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.liveok) return postAction(`/api/education/live-sessions/${d.liveok}/review?approve=true&comment=${encodeURIComponent(prompt("审核意见") || "同意排期")}`, null, "#edu-msg");
    if (d.liveno) return postAction(`/api/education/live-sessions/${d.liveno}/review?approve=false&comment=${encodeURIComponent(prompt("驳回理由") || "")}`, null, "#edu-msg");
    if (d.livefin) return postAction(`/api/education/live-sessions/${d.livefin}/finish`, null, "#edu-msg");
    const id = d.exam;
    if (!id) return;
    const score = prompt("考核得分（0-100）"); if (score === null) return;
    postAction(`/api/education/courses/${id}/exam`, { score: Number(score) }, "#edu-msg");
  };
  await drawEduGaps();  // 块4⑳㉑ 课件资源与适宜技术实训
}

async function renderEldercare() {
  $("#page-desc").textContent = "自理能力评估（Barthel自动分级）、失能老人清单、健康预警（重度失能专案+年度复评到期）";
  const [assessments, disabled, alerts] = await Promise.all([
    api("/api/eldercare/assessments"), api("/api/eldercare/disabled"), api("/api/eldercare/alerts")]);
  $("#page-body").innerHTML = `
    ${alerts.total ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 老年健康预警（${alerts.total}）</h3>${
      table(["患者", "预警类型", "提示", "末次评估"], alerts.alerts, (a) =>
        `<tr><td>${a.patient_id}</td>
         <td><span class="tag ${a.alert_type === "severe_disability" ? "red" : "orange"}">${a.alert_type === "severe_disability" ? "重度失能专案" : "复评到期"}</span></td>
         <td>${esc(a.message)}</td><td>${esc(a.assessed_date) || "—"}</td></tr>`)}</div>` : ""}`;
  $("#page-body").innerHTML += `
    <div class="panel"><h3>新评估</h3>
      <form class="inline" id="eld-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="adl_score" type="number" min="0" max="100" placeholder="ADL(0-100)" required>
        <input name="cognitive_score" type="number" min="0" max="30" placeholder="认知(0-30)"><input name="tcm_constitution" placeholder="体质">
        <input name="assessed_date" placeholder="评估日期 YYYY-MM-DD"><button>评估</button>
      </form><p class="msg" id="eld-msg"></p></div>
    ${disabled.length ? `<div class="panel"><h3>⚠ 失能老人清单（${disabled.length}）</h3>${table(["患者", "分级", "ADL"], disabled, (d) =>
      `<tr><td>${d.patient_id}</td><td><span class="tag red">${esc(d.care_level)}</span></td><td>${d.adl_score}</td></tr>`)}</div>` : ""}
    <div class="panel">${table(["ID", "患者", "ADL", "认知", "分级", "日期"], assessments, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${a.adl_score}</td><td>${a.cognitive_score}</td>
       <td><span class="tag ${a.care_level === "能力完好" ? "green" : "red"}">${esc(a.care_level)}</span></td><td>${esc(a.assessed_date)}</td></tr>`)}</div>`;
  $("#eld-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/eldercare/assessments", formJson(e.target, ["patient_id", "adl_score", "cognitive_score"]), "#eld-msg"); };
}

async function renderMaternal() {
  $("#page-desc").textContent = "孕产妇建册、产检、分娩登记、产后结案；儿童保健、新筛与高危儿；妇女保健四类记录";
  const [records, children, highRisk, womenHealth] = await Promise.all([
    api("/api/maternal/records"), api("/api/maternal/children"),
    api("/api/maternal/children/high-risk"), api("/api/maternal/women-health")]);
  const MS = { registered: "孕期管理", delivered: "已分娩", closed: "已结案" };
  const WH_TYPES = { premarital: "婚前保健", preconception: "孕前保健", gynecology: "妇女病检查", contraception: "避孕节育" };
  const SCREEN_ITEMS = { metabolic: "遗传代谢病", hearing: "听力", chd: "先心病" };
  const hrIds = new Set(highRisk.map((c) => c.id));
  $("#page-body").innerHTML = `
    <div class="panel"><h3>孕产妇建册 / 儿童建档</h3>
      <form class="inline" id="mat-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="lmp" placeholder="末次月经 YYYY-MM-DD">
        <input name="edc" placeholder="预产期 YYYY-MM-DD"><button>建册</button></form>
      <form class="inline" id="child-form">
        <input name="name" placeholder="儿童姓名" required><select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="birth_date" placeholder="出生日期 YYYY-MM-DD" required><input name="guardian_patient_id" type="number" placeholder="监护人患者ID"><button>建档</button></form>
      <p class="msg" id="mat-msg"></p></div>
    <div class="panel"><h3>孕产妇档案</h3>${table(["ID", "患者", "预产期", "孕/产次", "高危", "状态", "操作"], records, (r) =>
      `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${esc(r.edc)}</td><td>G${r.gravidity}P${r.parity}</td>
       <td>${r.high_risk ? `<span class="tag red">高危</span> ${esc(r.risk_factors)}` : '<span class="tag green">正常</span>'}</td>
       <td><span class="tag">${MS[r.status]}</span></td>
       <td>${r.status !== "closed" ? `<button class="btn secondary" data-visit="${r.id}">记录访视</button>
         ${r.status === "registered" ? `<button class="btn secondary" data-delivery="${r.id}">分娩登记</button>` : ""}
         ${r.status === "delivered" ? `<button class="btn secondary" data-close="${r.id}">结案</button>` : ""}` : "—"}</td></tr>`)}</div>
    ${highRisk.length ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 高危儿专案清单（${highRisk.length}）</h3>${
      table(["ID", "姓名", "出生日期", "高危原因"], highRisk, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.birth_date)}</td><td><span class="tag red">${esc(c.risk_note)}</span></td></tr>`)}</div>` : ""}
    <div class="panel"><h3>儿童档案（新筛异常自动纳入高危儿）</h3>${table(["ID", "姓名", "性别", "出生日期", "高危", "操作"], children, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.birth_date)}</td>
       <td>${hrIds.has(c.id) ? '<span class="tag red">高危</span>' : '<span class="tag green">—</span>'}</td>
       <td><button class="btn secondary" data-cvisit="${c.id}">记录访视</button>
           <button class="btn secondary" data-screen="${c.id}">新筛登记</button>
           <button class="btn" data-shist="${c.id}">筛查史</button>
           <button class="btn ${hrIds.has(c.id) ? "" : "danger"}" data-hrtoggle="${c.id}" data-cur="${hrIds.has(c.id)}">${hrIds.has(c.id) ? "解除高危" : "标记高危"}</button></td></tr>`)}</div>
    <div class="panel hidden" id="screen-panel"><h3>新生儿筛查史</h3><div id="screen-list"></div></div>
    <div class="panel"><h3>妇女保健记录（婚前/孕前/妇女病/避孕节育）</h3>
      <form class="inline" id="wh-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="record_type">${Object.entries(WH_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="exam_date" placeholder="检查日期 YYYY-MM-DD">
        <input name="result" placeholder="检查结果">
        <input name="advice" placeholder="指导意见">
        <button>登记</button></form>
      ${table(["ID", "患者", "类型", "日期", "结果", "指导"], womenHealth, (w) =>
        `<tr><td>${w.id}</td><td>${w.patient_id}</td><td><span class="tag">${WH_TYPES[w.record_type] || esc(w.record_type)}</span></td>
         <td>${esc(w.exam_date) || "—"}</td><td>${esc(w.result) || "—"}</td><td>${esc(w.advice) || "—"}</td></tr>`)}</div>`;
  $("#mat-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/records", formJson(e.target, ["patient_id"]), "#mat-msg"); };
  $("#child-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/children", formJson(e.target, ["guardian_patient_id"]), "#mat-msg"); };
  $("#wh-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/women-health", formJson(e.target, ["patient_id"]), "#mat-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.visit) {
      const type = prompt("访视类型：prenatal(产检)/postpartum(产后)", "prenatal"); if (!type) return;
      return postAction(`/api/maternal/records/${d.visit}/visits`, { visit_type: type, bp: prompt("血压(如120/80，可空)") || "", visit_date: prompt("日期 YYYY-MM-DD") || "" }, "#mat-msg");
    }
    if (d.delivery) {
      const orgId = prompt("分娩机构ID"); if (!orgId) return;
      const deliveryDate = prompt("分娩日期 YYYY-MM-DD"); if (!deliveryDate) return;
      const mode = prompt("分娩方式：natural(顺产)/cesarean(剖宫产)", "natural") || "natural";
      return postAction(`/api/maternal/records/${d.delivery}/delivery`,
        { org_id: Number(orgId), delivery_date: deliveryDate, delivery_mode: mode, outcome: prompt("分娩结局") || "" }, "#mat-msg");
    }
    if (d.close) return postAction(`/api/maternal/records/${d.close}/close`, null, "#mat-msg");
    if (d.cvisit) return postAction(`/api/maternal/children/${d.cvisit}/visits`, { visit_type: "checkup", visit_date: prompt("日期 YYYY-MM-DD") || "" }, "#mat-msg");
    if (d.screen) {
      const keys = Object.keys(SCREEN_ITEMS);
      const pick = prompt(`筛查项目（${keys.map((k, i) => `${i + 1}=${SCREEN_ITEMS[k]}`).join("，")}）输入序号`);
      const item = keys[Number(pick) - 1]; if (!item) return;
      const abnormal = confirm("结果是否异常？（确定=异常，将自动纳入高危儿）");
      return postAction(`/api/maternal/children/${d.screen}/screenings`,
        { item, result: abnormal ? "abnormal" : "normal", screen_date: prompt("筛查日期 YYYY-MM-DD") || "" }, "#mat-msg");
    }
    if (d.shist) {
      try {
        const list = await api(`/api/maternal/children/${d.shist}/screenings`);
        $("#screen-panel").classList.remove("hidden");
        $("#screen-list").innerHTML = table(["ID", "项目", "结果", "日期", "备注"], list, (s) =>
          `<tr><td>${s.id}</td><td>${SCREEN_ITEMS[s.item] || esc(s.item)}</td>
           <td><span class="tag ${s.result === "abnormal" ? "red" : "green"}">${s.result === "abnormal" ? "异常" : "正常"}</span></td>
           <td>${esc(s.screen_date) || "—"}</td><td>${esc(s.note) || "—"}</td></tr>`);
      } catch (err) { setMsg("#mat-msg", err.message, false); }
      return;
    }
    if (d.hrtoggle) {
      const toHigh = d.cur !== "true";
      const note = toHigh ? (prompt("高危原因") || "人工标记") : "";
      return postAction(`/api/maternal/children/${d.hrtoggle}/high-risk`, { high_risk: toHigh, risk_note: note }, "#mat-msg");
    }
  };
  await drawPrenatalScreenings();  // 块4㉔ 产前筛查与诊断
}

async function renderVaccination() {
  $("#page-desc").textContent = "接种前综合评估（禁忌硬拦截）、接种登记、禁忌管理";
  $("#page-body").innerHTML = `
    <div class="panel"><h3>接种前评估</h3>
      <form class="inline" id="vac-check"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="vaccine_code" placeholder="疫苗编码" required><button>评估</button></form>
      <div id="vac-check-result"></div></div>
    <div class="panel"><h3>接种登记 / 禁忌登记</h3>
      <form class="inline" id="vac-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="vaccine_name" placeholder="疫苗名称" required><input name="dose_no" type="number" value="1" min="1" style="min-width:60px">
        <input name="vaccinated_date" placeholder="接种日期"><input name="org_id" type="number" placeholder="接种机构ID" required><button>登记接种</button></form>
      <form class="inline" id="contra-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="reason" placeholder="禁忌原因" required>
        <select name="contra_type"><option value="permanent">长期禁忌</option><option value="temporary">暂时禁忌</option></select>
        <input name="valid_until" placeholder="有效期末日（暂时禁忌必填）"><button class="btn danger">登记禁忌</button></form>
      <p class="msg" id="vac-msg"></p></div>
    <div class="panel"><h3>禁忌清单（可解除）</h3>
      <form class="inline" id="contra-list"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="contra-result"></div></div>
    <div class="panel"><h3>接种史查询</h3>
      <form class="inline" id="vac-hist"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="vac-hist-result"></div></div>`;
  $("#vac-check").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const r = await api(`/api/vaccination/pre-check?patient_id=${f.get("patient_id")}&vaccine_code=${encodeURIComponent(f.get("vaccine_code"))}`);
    $("#vac-check-result").innerHTML = r.allowed
      ? `<p class="msg ok">可以接种，本次为第 ${r.next_dose_no} 剂</p>`
      : `<p class="msg err">禁止接种：${esc(r.contraindications.join("；"))}</p>`;
  };
  $("#vac-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/records", formJson(e.target, ["patient_id", "dose_no", "org_id"]), "#vac-msg"); };
  $("#contra-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/contraindications", formJson(e.target, ["patient_id"]), "#vac-msg"); };
  const drawContras = async (pid) => {
    const rows = await api(`/api/vaccination/contraindications?patient_id=${pid}`);
    $("#contra-result").innerHTML = table(["疫苗", "原因", "类型", "有效期至", "当前", "操作"], rows, (r) =>
      `<tr><td>${esc(r.vaccine_code)}</td><td>${esc(r.reason)}</td>` +
      `<td>${r.contra_type === "temporary" ? "暂时" : "长期"}</td><td>${esc(r.valid_until || "—")}</td>` +
      `<td>${r.blocking ? '<span class="tag danger">拦截中</span>' : (r.status === "lifted" ? "已解除" : "已过期")}</td>` +
      `<td>${r.blocking ? `<button class="btn sm" data-lift="${r.id}" data-pid="${pid}">解除</button>` : esc(r.lift_reason || "—")}</td></tr>`);
  };
  $("#contra-list").onsubmit = (e) => { e.preventDefault(); drawContras(new FormData(e.target).get("patient_id")); };
  $("#contra-result").onclick = async (e) => {
    const id = e.target.dataset.lift; if (!id) return;
    const reason = prompt("解除原因（如：体温已恢复正常）"); if (!reason) return;
    await postAction(`/api/vaccination/contraindications/${id}/lift`, { lift_reason: reason }, "#vac-msg");
    drawContras(e.target.dataset.pid);
  };
  $("#vac-hist").onsubmit = async (e) => {
    e.preventDefault();
    const records = await api(`/api/vaccination/records?patient_id=${new FormData(e.target).get("patient_id")}`);
    $("#vac-hist-result").innerHTML = table(["疫苗", "剂次", "日期", "机构"], records, (r) =>
      `<tr><td>${esc(r.vaccine_name)}</td><td>第${r.dose_no}剂</td><td>${esc(r.vaccinated_date)}</td><td>${r.org_id}</td></tr>`);
  };
}


async function renderVaccineSupply() {
  $("#page-desc").textContent = "疫苗批次（批号/厂家/效期）、冷链温度监测、AEFI 上报与统计";
  const [batches, cold, aefi, stats] = await Promise.all([
    api("/api/vaccine-supply/batches"), api("/api/vaccine-supply/cold-chain"),
    api("/api/vaccine-supply/aefi"), api("/api/vaccine-supply/stats"),
  ]);
  const a = stats.aefi, b = stats.batches;
  $("#page-body").innerHTML = `
    <div class="cards">
      ${[["接种剂次", stats.doses, false], ["AEFI 报告", a.total, false],
         ["其中严重", a.severe, a.severe > 0],
         ["十万剂次发生率", a.rate_per_100k_doses === null ? "无接种" : a.rate_per_100k_doses, false],
         ["过期批次", b.expired, b.expired > 0], ["封存批次", b.frozen, b.frozen > 0],
         ["30天内到期", b.expiring_soon, b.expiring_soon > 0],
         ["超温未处置", stats.cold_chain.exceeded_unhandled, stats.cold_chain.exceeded_unhandled > 0]]
        .map(([label, value, warn]) =>
          `<div class="card"><div class="label">${esc(label)}</div>` +
          `<div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}
    </div>
    <div class="panel"><h3>登记批次</h3>
      <form class="inline" id="vb-form">
        <input name="vaccine_code" placeholder="疫苗编码" required><input name="vaccine_name" placeholder="疫苗名称" required>
        <input name="batch_no" placeholder="批号" required><input name="manufacturer" placeholder="厂家">
        <input name="expire_date" placeholder="效期 YYYY-MM-DD" required><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="quantity" type="number" placeholder="数量" value="0"><button>登记</button></form>
      <p class="msg" id="vb-msg"></p>
      <div id="vb-list"></div></div>
    <div class="panel"><h3>冷链录温</h3>
      <form class="inline" id="cc-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="device_name" placeholder="设备名称" required>
        <input name="temperature" type="number" step="0.1" placeholder="温度℃" required>
        <input name="min_allowed" type="number" step="0.1" value="2" style="min-width:70px"><input name="max_allowed" type="number" step="0.1" value="8" style="min-width:70px">
        <input name="recorded_at" placeholder="YYYY-MM-DD HH:MM:SS" required><button>录入</button></form>
      <p class="msg" id="cc-msg"></p>
      ${table(["机构", "设备", "温度", "区间", "状态", "处置"], cold, (r) =>
        `<tr><td>${r.org_id}</td><td>${esc(r.device_name)}</td><td>${r.temperature}</td><td>${esc(r.range)}</td>` +
        `<td>${r.exceeded ? '<span class="tag danger">超温</span>' : "正常"}</td>` +
        `<td>${r.exceeded ? (r.handled ? esc(r.handle_note) : `<button class="btn sm" data-handle="${r.id}">处置</button>`) : "—"}</td></tr>`)}
    </div>
    <div class="panel"><h3>AEFI 报告</h3>
      <form class="inline" id="aefi-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="record_id" type="number" placeholder="接种记录ID（可空）">
        <input name="vaccine_code" placeholder="疫苗编码（未关联记录时必填）">
        <select name="reaction_type"><option value="general">一般反应</option><option value="severe">严重反应</option>
          <option value="psychogenic">心因性</option><option value="coincidental">偶合症</option></select>
        <input name="symptom" placeholder="症状" required><input name="onset_date" placeholder="发生日期" required>
        <input name="org_id" type="number" placeholder="机构ID" required><button class="btn danger">上报</button></form>
      <p class="msg" id="aefi-msg"></p>
      ${table(["患者", "疫苗", "批号", "类型", "症状", "发生日期", "转归"], aefi, (r) =>
        `<tr><td>${r.patient_id}</td><td>${esc(r.vaccine_code)}</td><td>${esc(r.batch_no || "—")}</td>` +
        `<td>${r.reaction_type === "severe" ? '<span class="tag danger">' + esc(r.reaction_type_name) + "</span>" : esc(r.reaction_type_name)}</td>` +
        `<td>${esc(r.symptom)}</td><td>${esc(r.onset_date)}</td><td>${esc(r.outcome_name)}</td></tr>`)}
    </div>`;
  $("#vb-list").innerHTML = table(["疫苗", "批号", "厂家", "效期", "在库", "状态", "操作"], batches, (r) =>
    `<tr><td>${esc(r.vaccine_name)}</td><td>${esc(r.batch_no)}</td><td>${esc(r.manufacturer || "—")}</td>` +
    `<td>${esc(r.expire_date)}${r.expired ? ' <span class="tag danger">已过期</span>' : ""}</td>` +
    `<td>${r.remaining}/${r.quantity}</td>` +
    `<td>${r.usable ? '<span class="tag ok">可用</span>' : esc(r.unusable_reason)}</td>` +
    `<td><button class="btn sm" data-recipients="${r.id}">受种者</button>` +
    (r.status === "frozen" ? `<button class="btn sm" data-unfreeze="${r.id}">解除封存</button>`
                           : `<button class="btn sm danger" data-freeze="${r.id}">封存</button>`) + "</td></tr>");
  $("#vb-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccine-supply/batches", formJson(e.target, ["org_id", "quantity"]), "#vb-msg"); };
  $("#cc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccine-supply/cold-chain", formJson(e.target, ["org_id", "temperature", "min_allowed", "max_allowed"]), "#cc-msg"); };
  $("#aefi-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccine-supply/aefi", formJson(e.target, ["patient_id", "record_id", "org_id"]), "#aefi-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.freeze) {
      const reason = prompt("封存原因"); if (!reason) return;
      return postAction(`/api/vaccine-supply/batches/${d.freeze}/freeze`, { frozen_reason: reason }, "#vb-msg");
    }
    if (d.unfreeze) return postAction(`/api/vaccine-supply/batches/${d.unfreeze}/unfreeze`, {}, "#vb-msg");
    if (d.handle) {
      const note = prompt("处置说明"); if (!note) return;
      return postAction(`/api/vaccine-supply/cold-chain/${d.handle}/handle`, { handle_note: note }, "#cc-msg");
    }
    if (d.recipients) {
      const r = await api(`/api/vaccine-supply/batches/${d.recipients}/recipients`);
      alert(`批号 ${r.batch_no}（${r.vaccine_name}）共 ${r.total} 名受种者\n` +
            r.recipients.slice(0, 20).map((x) => `${x.patient_name}(#${x.patient_id}) 第${x.dose_no}剂 ${x.vaccinated_date}`).join("\n"));
    }
  };
}

async function renderSurveillance() {
  $("#page-desc").textContent = "症候群监测、病原监测、多点触发预警与应急资源保障";
  const [syndromes, pathogens, alerts, ready] = await Promise.all([
    api("/api/surveillance/syndromes"), api("/api/surveillance/pathogens"),
    api("/api/surveillance/alerts"), api("/api/surveillance/resources/readiness"),
  ]);
  const SYN = { fever: "发热", respiratory: "呼吸道", diarrhea: "腹泻", rash: "皮疹", jaundice: "黄疸", neuro: "脑炎脑膜炎" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>多点触发预警（近 ${alerts.window.days} 天）</h3>
      <p class="hint">两路信号分列，不做综合评分：症候群异常查接诊，病原阳性抬头查实验室。</p>
      <h4>症候群超阈值</h4>
      ${table(["机构", "症候群", "例数", "阈值", "日期"], alerts.syndrome_alerts, (r) =>
        `<tr><td>${r.org_id}</td><td>${esc(r.syndrome_name)}</td><td><b>${r.case_count}</b></td><td>${r.threshold}</td><td>${esc(r.record_date)}</td></tr>`)}
      <h4>病原阳性率抬头</h4>
      ${table(["机构", "病原", "标本", "阳性/送检", "阳性率"], alerts.pathogen_alerts, (r) =>
        `<tr><td>${r.org_id}</td><td>${esc(r.pathogen_name)}</td><td>${esc(r.specimen_type || "—")}</td>` +
        `<td>${r.positive_count}/${r.tested_count}</td><td><b>${r.positive_rate_pct}%</b></td></tr>`)}
      <p class="hint">口径：${esc(alerts.caliber.pathogen)}</p></div>
    <div class="panel"><h3>症候群日报</h3>
      <form class="inline" id="syn-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="syndrome">${Object.entries(SYN).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="case_count" type="number" placeholder="例数" required>
        <input name="threshold" type="number" placeholder="阈值（0=不预警）" value="0">
        <input name="record_date" placeholder="日期 YYYY-MM-DD" required><button>上报</button></form>
      <p class="msg" id="syn-msg"></p>
      ${table(["机构", "症候群", "例数", "阈值", "日期", "预警"], syndromes.slice(0, 50), (r) =>
        `<tr><td>${r.org_id}</td><td>${esc(r.syndrome_name)}</td><td>${r.case_count}</td><td>${r.threshold || "不设"}</td>` +
        `<td>${esc(r.record_date)}</td><td>${r.alert ? '<span class="tag danger">超阈值</span>' : "—"}</td></tr>`)}
    </div>
    <div class="panel"><h3>病原监测</h3>
      <form class="inline" id="pat-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="pathogen_name" placeholder="病原名称" required>
        <input name="specimen_type" placeholder="标本类型"><input name="tested_count" type="number" placeholder="送检数" required>
        <input name="positive_count" type="number" placeholder="阳性数" required>
        <input name="record_date" placeholder="日期 YYYY-MM-DD" required><button>上报</button></form>
      <p class="msg" id="pat-msg"></p>
      ${table(["机构", "病原", "标本", "阳性/送检", "阳性率", "日期"], pathogens.slice(0, 50), (r) =>
        `<tr><td>${r.org_id}</td><td>${esc(r.pathogen_name)}</td><td>${esc(r.specimen_type || "—")}</td>` +
        `<td>${r.positive_count}/${r.tested_count}</td><td>${r.positive_rate_pct === null ? "未送检" : r.positive_rate_pct + "%"}</td>` +
        `<td>${esc(r.record_date)}</td></tr>`)}
    </div>
    <div class="panel"><h3>应急资源保障</h3>
      <form class="inline" id="res-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type"><option value="material">应急物资</option><option value="team">应急队伍</option><option value="equipment">应急装备</option></select>
        <input name="name" placeholder="名称" required><input name="quantity" type="number" placeholder="数量" required>
        <input name="unit" placeholder="单位" style="min-width:60px"><input name="min_quantity" type="number" placeholder="储备下限" value="0">
        <input name="expire_date" placeholder="效期（队伍留空）"><input name="contact" placeholder="联系方式">
        <button>登记</button></form>
      <p class="msg" id="res-msg"></p>
      <p class="hint">${esc(ready.caliber)}</p>
      ${table(["机构", "资源数", "低于下限", "已过期"], ready.orgs, (o) =>
        `<tr><td>${esc(o.org_name || o.org_id)}</td><td>${o.total}</td>` +
        `<td>${o.below_min.length ? '<span class="tag danger">' + o.below_min.map((x) => esc(x.name) + `(${x.quantity}/${x.min_quantity})`).join("、") + "</span>" : "—"}</td>` +
        `<td>${o.expired.length ? '<span class="tag warn">' + o.expired.map((x) => esc(x.name) + `(${esc(x.expire_date)})`).join("、") + "</span>" : "—"}</td></tr>`)}
    </div>`;
  $("#syn-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/surveillance/syndromes", formJson(e.target, ["org_id", "case_count", "threshold"]), "#syn-msg"); };
  $("#pat-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/surveillance/pathogens", formJson(e.target, ["org_id", "tested_count", "positive_count"]), "#pat-msg"); };
  $("#res-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/surveillance/resources", formJson(e.target, ["org_id", "quantity", "min_quantity"]), "#res-msg"); };
}


async function renderPathology() {
  $("#page-desc").textContent = "病理标本核收（含拒收）、取材制片阅片流转、冷缺血时间质控";
  const [specimens, stats] = await Promise.all([
    api("/api/pathology/specimens"), api("/api/pathology/specimen-stats"),
  ]);
  const ci = stats.cold_ischemia;
  $("#page-body").innerHTML = `
    <div class="cards">
      ${[["标本总数", stats.total, false], ["已拒收", stats.rejected, stats.rejected > 0],
         ["拒收率", stats.reject_rate_pct === null ? "—" : stats.reject_rate_pct + "%", false],
         ["冷缺血已测", ci.measured, false], ["未记录时间", ci.unmeasured, ci.unmeasured > 0],
         ["平均冷缺血", ci.avg_minutes === null ? "—" : ci.avg_minutes + " 分", false],
         ["超60分钟", ci.over_60min, ci.over_60min > 0]]
        .map(([label, value, warn]) =>
          `<div class="card"><div class="label">${esc(label)}</div>` +
          `<div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}
    </div>
    <div class="panel"><h3>标本送检登记</h3>
      <form class="inline" id="sp-form">
        <input name="request_id" type="number" placeholder="病理申请单ID" required>
        <input name="site" placeholder="送检部位">
        <input name="excised_at" placeholder="离体时间 YYYY-MM-DDTHH:MM:SS">
        <input name="fixed_at" placeholder="固定时间 YYYY-MM-DDTHH:MM:SS">
        <input name="fixative" placeholder="固定液"><button>登记</button></form>
      <p class="msg" id="sp-msg"></p>
      <p class="hint">${esc(stats.caliber)}</p></div>
    <div class="panel"><h3>标本流转</h3>
      ${table(["标本号", "部位", "状态", "冷缺血", "蜡块/切片", "核收人", "操作"], specimens, (s) =>
        `<tr><td>${esc(s.specimen_no)}</td><td>${esc(s.site || "—")}</td>` +
        `<td>${s.status === "rejected" ? '<span class="tag danger">' + esc(s.status_name) + "</span>" : esc(s.status_name)}` +
        `${s.reject_reason ? "<br><small>" + esc(s.reject_reason) + "</small>" : ""}</td>` +
        `<td>${s.cold_ischemia_minutes === null ? "未记录" : s.cold_ischemia_minutes + " 分"}</td>` +
        `<td>${s.block_count}/${s.slide_count}</td><td>${esc(s.received_by || "—")}</td>` +
        `<td>${s.status === "pending"
          ? `<button class="btn sm" data-receive="${s.id}">核收</button><button class="btn sm danger" data-reject="${s.id}">拒收</button>`
          : (s.status === "rejected" || s.status === "read" ? "—" : `<button class="btn sm" data-advance="${s.id}">推进</button>`)}</td></tr>`)}
    </div>`;
  $("#sp-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/pathology/specimens", formJson(e.target, ["request_id"]), "#sp-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.receive) {
      const who = prompt("核收人姓名"); if (!who) return;
      return postAction(`/api/pathology/specimens/${d.receive}/receive`, { received_by: who }, "#sp-msg");
    }
    if (d.reject) {
      const reason = prompt("拒收理由（标本量不足/未加固定液/标识不清/标本破损/申请单信息不符）");
      if (!reason) return;
      return postAction(`/api/pathology/specimens/${d.reject}/reject`, { reject_reason: reason }, "#sp-msg");
    }
    if (d.advance) {
      const n = prompt("蜡块数或切片数（该环节不适用可留空）") || "0";
      return postAction(`/api/pathology/specimens/${d.advance}/advance`,
        { block_count: Number(n), slide_count: Number(n) }, "#sp-msg");
    }
  };
}

async function renderProjects() {
  $("#page-desc").textContent = "行政协同项目管理：立项、里程碑、进度与逾期";
  const [projects, stats] = await Promise.all([
    api("/api/projects"), api("/api/projects/stats/overview"),
  ]);
  $("#page-body").innerHTML = `
    <div class="cards">
      ${[["项目总数", stats.total, false], ["在办", stats.active, false],
         ["逾期未结", stats.overdue, stats.overdue > 0],
         ["在办平均进度", stats.avg_progress_pct_active === null ? "—" : stats.avg_progress_pct_active + "%", false],
         ["预算合计", stats.total_budget, false]]
        .map(([label, value, warn]) =>
          `<div class="card"><div class="label">${esc(label)}</div>` +
          `<div class="value${warn ? " warn" : ""}">${esc(value)}</div></div>`).join("")}
    </div>
    <div class="panel"><h3>立项</h3>
      <form class="inline" id="pj-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="name" placeholder="项目名称" required>
        <input name="category" placeholder="类别" value="general"><input name="owner_name" placeholder="负责人">
        <input name="start_date" placeholder="开始 YYYY-MM-DD"><input name="due_date" placeholder="计划完成 YYYY-MM-DD">
        <input name="budget_amount" type="number" step="0.01" placeholder="预算"><button>立项</button></form>
      <p class="msg" id="pj-msg"></p>
      <p class="hint">${esc(stats.caliber)}</p></div>
    <div class="panel"><h3>项目清单</h3>
      ${table(["名称", "负责人", "状态", "进度", "计划完成", "里程碑", "操作"], projects, (p) =>
        `<tr><td>${esc(p.name)}${p.overdue ? ' <span class="tag danger">逾期</span>' : ""}</td>` +
        `<td>${esc(p.owner_name || "—")}</td><td>${esc(p.status_name)}</td>` +
        `<td>${p.progress_pct}%</td><td>${esc(p.due_date || "—")}</td>` +
        `<td>${p.milestone_done}/${p.milestone_total}` +
        `${p.milestone_overdue ? ' <span class="tag warn">' + p.milestone_overdue + " 逾期</span>" : ""}</td>` +
        `<td><button class="btn sm" data-progress="${p.id}">报进度</button>` +
        `<button class="btn sm" data-ms="${p.id}">加里程碑</button></td></tr>`)}
    </div>`;
  $("#pj-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/projects", formJson(e.target, ["org_id", "budget_amount"]), "#pj-msg"); };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.progress) {
      const pct = prompt("当前进度（0-100）"); if (pct === null) return;
      const status = prompt("状态：planning / ongoing / done / suspended", "ongoing"); if (!status) return;
      return postAction(`/api/projects/${d.progress}`, { progress_pct: Number(pct), status }, "#pj-msg", "PATCH");
    }
    if (d.ms) {
      const name = prompt("里程碑名称"); if (!name) return;
      const due = prompt("到期日 YYYY-MM-DD（可留空）") || "";
      return postAction(`/api/projects/${d.ms}/milestones`, { name, due_date: due }, "#pj-msg");
    }
  };
}


async function renderTcmHeritage() {
  $("#page-desc").textContent = "名老中医医案（四诊/辨证/治法/处方/按语分维度）与模拟诊疗";
  const [cases, stats, sims] = await Promise.all([
    api("/api/tcm-heritage/master-cases?include_draft=true"),
    api("/api/tcm-heritage/master-cases/stats"),
    api("/api/tcm-heritage/simulations"),
  ]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>传承概览</h3>
      ${table(["名老中医", "医案数", "已发布", "涉及病种", "传承人"], stats.masters, (m) =>
        `<tr><td>${esc(m.master_name)}</td><td>${m.total}</td><td>${m.published}</td>` +
        `<td>${esc(m.diseases.join("、") || "—")}</td><td>${esc(m.successors.join("、") || "—")}</td></tr>`)}
    </div>
    <div class="panel"><h3>医案录入</h3>
      <form id="mc-form">
        <div class="inline">
          <input name="master_name" placeholder="名老中医" required><input name="successor_name" placeholder="传承人">
          <input name="title" placeholder="医案标题" required><input name="disease" placeholder="病名">
          <input name="syndrome" placeholder="证型"><input name="visit_date" placeholder="就诊日期 YYYY-MM-DD"></div>
        <div class="inline">
          <input name="four_exams" placeholder="四诊摘要" style="min-width:280px">
          <input name="treatment_method" placeholder="治法"><input name="prescription" placeholder="处方" style="min-width:220px">
          <input name="commentary" placeholder="按语" style="min-width:280px"><button>保存草稿</button></div></form>
      <p class="msg" id="mc-msg"></p></div>
    <div class="panel"><h3>医案库</h3>
      <form class="inline" id="mc-search"><input name="keyword" placeholder="搜方药/按语/标题"><button>检索</button></form>
      <div id="mc-list">${renderCaseTable(cases)}</div></div>
    <div class="panel"><h3>模拟诊疗病例</h3>
      ${table(["标题", "类别", "决策点", "满分", "及格分"], sims, (s) =>
        `<tr><td>${esc(s.title)}</td><td>${esc(s.category)}</td><td>${s.decision_points.length}</td>` +
        `<td>${s.total_score}</td><td>${s.pass_score}</td></tr>`)}
      <p class="hint">模拟诊疗的作答与评分在医师端 H5 完成；此处仅维护病例。列表刻意不含正确答案。</p></div>`;
  $("#mc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/tcm-heritage/master-cases", formJson(e.target, []), "#mc-msg"); };
  $("#mc-search").onsubmit = async (e) => {
    e.preventDefault();
    const kw = new FormData(e.target).get("keyword") || "";
    const rows = await api(`/api/tcm-heritage/master-cases?include_draft=true&keyword=${encodeURIComponent(kw)}`);
    $("#mc-list").innerHTML = renderCaseTable(rows);
  };
  $("#mc-list").onclick = (e) => {
    const d = e.target.dataset;
    if (d.publish) return postAction(`/api/tcm-heritage/master-cases/${d.publish}/publish`, {}, "#mc-msg");
    if (d.unpublish) return postAction(`/api/tcm-heritage/master-cases/${d.unpublish}/unpublish`, {}, "#mc-msg");
  };
}

function renderCaseTable(rows) {
  return table(["名老中医", "标题", "病/证", "治法", "处方", "状态", "操作"], rows, (c) =>
    `<tr><td>${esc(c.master_name)}</td><td>${esc(c.title)}</td>` +
    `<td>${esc(c.disease || "—")} / ${esc(c.syndrome || "—")}</td><td>${esc(c.treatment_method || "—")}</td>` +
    `<td>${esc(c.prescription || "—")}</td>` +
    `<td>${c.published ? '<span class="tag ok">已发布</span>' : "草稿"}</td>` +
    `<td>${c.published ? `<button class="btn sm" data-unpublish="${c.id}">撤回</button>`
                       : `<button class="btn sm" data-publish="${c.id}">发布</button>`}</td></tr>`);
}


async function renderResources() {
  $("#page-desc").textContent = "通用资源登记（登记→发布→撤回）、五类资源统一视图、号源与手术间排程撮合";
  const [resources, catalog, slotMatch] = await Promise.all([
    api("/api/resources"), api("/api/resources/catalog"), api("/api/resources/match/slots"),
  ]);
  $("#page-body").innerHTML = `
    <div class="cards">
      ${Object.entries(catalog.by_kind).map(([k, v]) =>
        `<div class="card"><div class="label">${esc(k)}</div>` +
        `<div class="value">${v.usable}/${v.total}</div></div>`).join("")}
    </div>
    <div class="panel"><h3>通用资源登记</h3>
      <p class="hint">只收没有领域表的资源（工勤/后勤/通用设备/会议室）；号源、检查资源、手术间、血制品各有自己的模块。</p>
      <form class="inline" id="rs-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="resource_type"><option value="logistics">工勤服务</option><option value="facility">后勤设施</option>
          <option value="equipment">通用设备</option><option value="meeting_room">会议室</option></select>
        <input name="code" placeholder="编码" required><input name="name" placeholder="名称" required>
        <input name="capacity" type="number" value="1" min="1" style="min-width:70px"><input name="unit" placeholder="单位" style="min-width:70px">
        <input name="location" placeholder="位置"><button>登记（草稿）</button></form>
      <p class="msg" id="rs-msg"></p>
      ${table(["编码", "名称", "类型", "容量", "位置", "状态", "操作"], resources, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.resource_type_name)}</td>` +
        `<td>${r.capacity}${esc(r.unit)}</td><td>${esc(r.location || "—")}</td>` +
        `<td>${esc(r.status_name)}${r.withdraw_reason ? "<br><small>" + esc(r.withdraw_reason) + "</small>" : ""}</td>` +
        `<td>${r.status === "published" ? `<button class="btn sm danger" data-withdraw="${r.id}">撤回</button>`
                                        : `<button class="btn sm" data-publish="${r.id}">发布</button>`}</td></tr>`)}
    </div>
    <div class="panel"><h3>统一资源视图</h3>
      <p class="hint">${esc(catalog.caliber)}</p>
      ${table(["类别", "名称", "详情", "可用量", "状态"], catalog.items.slice(0, 100), (i) =>
        `<tr><td>${esc(i.kind_name)}</td><td>${esc(i.name)}</td><td>${esc(i.detail || "—")}</td>` +
        `<td>${i.available === null ? "—" : i.available + esc(i.unit)}</td>` +
        `<td>${i.usable ? '<span class="tag ok">可用</span>' : "不可用"}</td></tr>`)}
    </div>
    <div class="panel"><h3>号源撮合（未来 14 天）</h3>
      <p class="hint">${esc(slotMatch.caliber)}</p>
      ${table(["机构", "最早可约", "余量合计", "近期号源"], slotMatch.candidates, (c) =>
        `<tr><td>${esc(c.org_name || c.org_id)}</td><td>${esc(c.earliest)}</td><td>${c.remaining_total}</td>` +
        `<td>${c.slots.map((s) => esc(`${s.slot_date} ${s.slot_time} ${s.resource_name}(余${s.remaining})`)).join("；")}</td></tr>`)}
    </div>
    <div class="panel"><h3>手术间撮合</h3>
      <form class="inline" id="or-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="scheduled_date" placeholder="日期 YYYY-MM-DD">
        <input name="start_time" value="08:00" style="min-width:80px"><input name="end_time" value="18:00" style="min-width:80px">
        <button>查空档</button></form>
      <div id="or-result"></div></div>`;
  $("#rs-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/resources", formJson(e.target, ["org_id", "capacity"]), "#rs-msg"); };
  $("#or-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const qs = new URLSearchParams([...f.entries()].filter(([, v]) => v)).toString();
    const r = await api(`/api/resources/match/or-rooms?${qs}`);
    $("#or-result").innerHTML = table(["手术间", "该窗口", "冲突时段", "空档"], r.rooms, (x) =>
      `<tr><td>${esc(x.room_name)}</td>` +
      `<td>${x.available ? '<span class="tag ok">可用</span>' : '<span class="tag danger">有冲突</span>'}</td>` +
      `<td>${x.conflicts.map((c) => esc(`${c.start_time}-${c.end_time}`)).join("、") || "—"}</td>` +
      `<td>${x.gaps.map((g) => esc(`${g.start_time}-${g.end_time}`)).join("、") || "无"}</td></tr>`);
  };
  $("#page-body").onclick = (e) => {
    const d = e.target.dataset;
    if (d.publish) return postAction(`/api/resources/${d.publish}/publish`, {}, "#rs-msg");
    if (d.withdraw) {
      const reason = prompt("撤回理由"); if (!reason) return;
      return postAction(`/api/resources/${d.withdraw}/withdraw`, { reason }, "#rs-msg");
    }
  };
}


async function renderRbac() {
  $("#page-desc").textContent = "内置六角色（代码声明，不可删停）与自定义角色的权限点授权";
  const [roles, modules] = await Promise.all([api("/api/rbac/roles"), api("/api/rbac/modules")]);
  $("#page-body").innerHTML = `
    <div class="panel"><h3>角色</h3>
      <p class="hint">内置角色的权限来自代码内 require_roles 声明，不走授权表；自定义角色按权限点授权。</p>
      <form class="inline" id="role-form">
        <input name="key" placeholder="角色 key（小写字母数字下划线）" required>
        <input name="name" placeholder="角色名称" required><input name="description" placeholder="说明">
        <button>新建自定义角色</button></form>
      <p class="msg" id="role-msg"></p>
      ${table(["key", "名称", "类型", "权限点", "状态", "操作"], roles, (r) =>
        `<tr><td>${esc(r.key)}</td><td>${esc(r.name)}</td>` +
        `<td>${r.builtin ? '<span class="tag">内置</span>' : "自定义"}</td>` +
        `<td>${r.builtin ? esc(r.permission_source) : r.permission_count}</td>` +
        `<td>${r.active ? "启用" : "停用"}</td>` +
        `<td>${r.builtin ? "—" : `<button class="btn sm" data-grant="${r.id}">授权</button>` +
          `<button class="btn sm" data-view="${r.id}">查看</button>` +
          `<button class="btn sm danger" data-del="${r.id}">删除</button>`}</td></tr>`)}
    </div>
    <div class="panel"><h3>权限点模块（共 ${modules.reduce((a, m) => a + m.permission_count, 0)} 个写接口权限点）</h3>
      <p class="hint">权限点由平台启动时从路由表自动登记，不手工维护——手工清单与真实接口的偏差最难查。</p>
      ${table(["模块", "权限点数"], modules, (m) =>
        `<tr><td>${esc(m.module)}</td><td>${m.permission_count}</td></tr>`)}
    </div>
    <div class="panel"><h3>角色权限明细</h3><div id="rbac-detail"><p class="empty">点上方「查看」</p></div></div>`;
  $("#role-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/rbac/roles", formJson(e.target, []), "#role-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.grant) {
      const mods = prompt("按模块授权，逗号分隔（如 medwaste,medication）；留空则取消");
      if (!mods) return;
      return postAction(`/api/rbac/roles/${d.grant}/permissions`,
        { modules: mods.split(",").map((x) => x.trim()).filter(Boolean) }, "#role-msg");
    }
    if (d.del) {
      if (!confirm("确认删除该自定义角色？")) return;
      return postAction(`/api/rbac/roles/${d.del}`, null, "#role-msg", "DELETE");
    }
    if (d.view) {
      const r = await api(`/api/rbac/roles/${d.view}/permissions`);
      $("#rbac-detail").innerHTML = `<p>${esc(r.role.name)}（${esc(r.role.key)}）共 ${r.permissions.length} 个权限点</p>` +
        table(["模块", "方法", "路径"], r.permissions, (p) =>
          `<tr><td>${esc(p.module)}</td><td>${esc(p.method)}</td><td>${esc(p.path)}</td></tr>`);
    }
  };
}

async function renderPublicHealth() {
  $("#page-desc").textContent = "应急事件指挥（I-IV级）、诊间医防提醒、五域卫生监测";
  const [events, monitors] = await Promise.all([api("/api/publichealth/events"), api("/api/publichealth/monitors")]);
  const DM = { nutrition: "营养", environment: "环境", occupational: "职业", radiation: "放射", school: "学校" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>事件立案</h3>
      <form class="inline" id="ev-form">
        <input name="title" placeholder="事件名称" required style="min-width:220px">
        <select name="level"><option>IV</option><option>III</option><option>II</option><option>I</option></select>
        <input name="disease_name" placeholder="相关病种"><button>立案</button></form>
      <h3 style="margin-top:12px">诊间医防提醒</h3>
      <form class="inline" id="rem-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询提醒</button></form>
      <div id="rem-result"></div><p class="msg" id="ph-msg"></p></div>
    <div class="panel"><h3>事件列表</h3>${table(["ID", "事件", "级别", "病种", "状态", "操作"], events, (ev) =>
      `<tr><td>${ev.id}</td><td>${esc(ev.title)}</td><td><span class="tag ${ev.level === "I" || ev.level === "II" ? "red" : "orange"}">${ev.level}级</span></td>
       <td>${esc(ev.disease_name)}</td><td><span class="tag ${ev.status === "active" ? "red" : "green"}">${ev.status === "active" ? "处置中" : "已结案"}</span></td>
       <td>${ev.status === "active" ? `<button class="btn secondary" data-act="${ev.id}">处置记录</button><button class="btn secondary" data-close="${ev.id}">结案</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>卫生监测（营养/环境/职业/放射/学校）</h3>
      <form class="inline" id="mon-form">
        <select name="domain">${Object.entries(DM).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="org_id" type="number" placeholder="机构ID" required><input name="indicator" placeholder="监测指标" required>
        <input name="value" type="number" step="any" placeholder="监测值" required><input name="threshold" type="number" step="any" placeholder="阈值" required>
        <input name="record_date" placeholder="日期"><button>登记</button></form>
      ${table(["领域", "指标", "值/阈值", "状态"], monitors, (m) =>
        `<tr><td>${DM[m.domain]}</td><td>${esc(m.indicator)}</td><td>${m.value} / ${m.threshold}</td>
         <td>${m.exceeded ? '<span class="tag red">超标</span>' : '<span class="tag green">正常</span>'}</td></tr>`)}</div>`;
  $("#ev-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/events", formJson(e.target), "#ph-msg"); };
  $("#mon-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/monitors", formJson(e.target, ["org_id", "value", "threshold"]), "#ph-msg"); };
  $("#rem-form").onsubmit = async (e) => {
    e.preventDefault();
    const r = await api(`/api/publichealth/reminders/${new FormData(e.target).get("patient_id")}`);
    $("#rem-result").innerHTML = r.reminders.length
      ? `<ul style="margin:8px 0 0 18px;font-size:13px">${r.reminders.map((x) => `<li>${esc(x.detail)}</li>`).join("")}</ul>`
      : '<p class="msg ok">无待办提醒</p>';
  };
  $("#page-body").onclick = (e) => {
    const { act, close } = e.target.dataset;
    if (act) {
      const action = prompt("处置动作"); if (!action) return;
      return postAction(`/api/publichealth/events/${act}/actions`, { action, actor: prompt("执行人") || "" }, "#ph-msg");
    }
    if (close) return postAction(`/api/publichealth/events/${close}/close`, null, "#ph-msg");
  };
}

async function renderHrFinance() {
  $("#page-desc").textContent = "人力资源（科室库/变动/合同/薪酬）、派驻下沉、财务集中核算与预算执行、物资出入库";
  const role = currentRole();
  const isDirector = ["director", "admin"].includes(role);
  const [employees, secStats, finance, assets, departments, expiringContracts] = await Promise.all([
    api("/api/mgmt/employees"), api("/api/mgmt/secondments/stats"), api("/api/mgmt/finance/summary"),
    api("/api/mgmt/assets"), api("/api/mgmt/departments"), api("/api/mgmt/staff-contracts/expiring?days=60")]);
  const payroll = isDirector ? await api("/api/mgmt/payroll").catch(() => null) : null;
  const EST = { active: ["在岗", "green"], seconded: ["派驻中", "orange"], left: ["离职", ""] };
  const CHG_TYPES = { hire: "入职", regularize: "转正", transfer: "调动", leave: "离职" };
  const MV_TYPES = { inbound: "入库", issue: "领用", return: "归还", scrap: "报废" };
  const deptNames = Object.fromEntries(departments.map((d) => [d.id, d.name]));
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">在派人数</div><div class="value">${secStats.active_secondments}</div></div>
      <div class="card"><div class="label">医共体收入合计</div><div class="value">${finance.consolidated.income}</div></div>
      <div class="card"><div class="label">医共体结余</div><div class="value">${finance.consolidated.balance}</div></div>
      ${expiringContracts.length ? `<div class="card"><div class="label">60天内到期合同</div><div class="value warn">${expiringContracts.length}</div></div>` : ""}</div>
    <div class="panel"><h3>员工 / 派驻 / 财务 / 物资录入</h3>
      <form class="inline" id="emp-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="name" placeholder="姓名" required>
        <input name="title" placeholder="职称"><input name="position" placeholder="岗位"><button>登记员工</button></form>
      <form class="inline" id="sec-form"><input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="to_org_id" type="number" placeholder="派驻机构ID" required><input name="start_date" placeholder="开始日期 YYYY-MM-DD" required><button>派驻下沉</button></form>
      <form class="inline" id="fin-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="period" placeholder="期间 YYYY-MM" required>
        <select name="category"><option value="income">收入</option><option value="expense">支出</option></select>
        <input name="item" placeholder="科目"><input name="amount" type="number" step="any" placeholder="金额" required><button>记账</button></form>
      <form class="inline" id="asset-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="code" placeholder="物资编码" required>
        <input name="name" placeholder="名称" required><select name="category"><option value="office">办公用品</option><option value="equipment">非医疗设备</option></select>
        <input name="quantity" type="number" value="1" min="1" style="min-width:60px"><button>物资建档</button></form>
      <p class="msg" id="hrf-msg"></p></div>
    <div class="panel"><h3>科室信息基础库（机构内编码唯一，员工跨机构挂接拦截）</h3>
      <form class="inline" id="dept-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="code" placeholder="科室编码" required><input name="name" placeholder="科室名称" required>
        <select name="category"><option value="clinical">临床</option><option value="medtech">医技</option><option value="admin">行政</option></select>
        <button>科室建档</button></form>
      ${table(["ID", "机构", "编码", "名称", "类别"], departments, (d) =>
        `<tr><td>${d.id}</td><td>${d.org_id}</td><td><span class="tag">${esc(d.code)}</span></td><td>${esc(d.name)}</td><td>${esc(d.category)}</td></tr>`)}</div>
    <div class="panel"><h3>员工（变动留痕联动机构与状态）</h3>${table(["ID", "机构", "姓名", "职称", "科室", "状态", "操作"], employees, (em) => {
      const [t, col] = EST[em.status] || [em.status, ""];
      return `<tr><td>${em.id}</td><td>${em.org_id}</td><td>${esc(em.name)}</td><td>${esc(em.title)}</td>
        <td>${em.dept_id ? esc(deptNames[em.dept_id] || em.dept_id) : "—"}</td><td><span class="tag ${col}">${t}</span></td>
        <td><button class="btn secondary" data-empdept="${em.id}">挂科室</button>
            <button class="btn secondary" data-empchg="${em.id}">登记变动</button>
            <button class="btn" data-emphist="${em.id}">变动史</button>
            <button class="btn secondary" data-empct="${em.id}">签合同</button></td></tr>`;
    })}</div>
    <div class="panel hidden" id="empchg-panel"><h3>人员变动记录</h3><div id="empchg-list"></div></div>
    ${expiringContracts.length ? `<div class="panel" style="border-left:4px solid #b26a00"><h3>⚠ 合同到期提醒（60天内 ${expiringContracts.length} 份，续签管理）</h3>${
      table(["合同号", "员工", "止期"], expiringContracts, (c) =>
        `<tr><td><span class="tag">${esc(c.contract_no)}</span></td><td>${c.employee_id}</td><td><span class="tag orange">${esc(c.end_date)}</span></td></tr>`)}</div>` : ""}
    ${isDirector ? `<div class="panel"><h3>月度薪酬（管理层：基础 + 绩效×系数）</h3>
      <form class="inline" id="pay-form">
        <input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="period" placeholder="期间 YYYY-MM" required pattern="\\d{4}-\\d{2}">
        <input name="base_salary" type="number" step="any" placeholder="基础工资" required>
        <input name="perf_bonus" type="number" step="any" placeholder="绩效奖金" value="0">
        <input name="perf_coefficient" type="number" step="any" placeholder="绩效系数" value="1.0">
        <button>录入</button></form>
      ${payroll ? `<p style="font-size:13px">合计发放：<b>${payroll.total_amount}</b> 元</p>${
        table(["ID", "员工", "期间", "基础", "绩效", "系数", "实发"], payroll.records, (r) =>
          `<tr><td>${r.id}</td><td>${r.employee_id}</td><td>${esc(r.period)}</td><td>${r.base_salary}</td>
           <td>${r.perf_bonus}</td><td>${r.perf_coefficient}</td><td><b>${r.total}</b></td></tr>`)}` : ""}</div>
    <div class="panel"><h3>预算编制与执行（管理层）</h3>
      <form class="inline" id="bud-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="year" placeholder="年度 YYYY" required pattern="\\d{4}">
        <select name="category"><option value="income">收入预算</option><option value="expense">支出预算</option></select>
        <input name="amount" type="number" step="any" placeholder="预算额" required>
        <button>编制/调整</button></form>
      <form class="inline" id="bud-exec-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="year" placeholder="年度 YYYY" required pattern="\\d{4}">
        <button>查执行率</button></form>
      <div id="bud-exec"></div></div>` : ""}
    <div class="panel"><h3>各单位收支（全部期间）</h3>${table(["机构", "收入", "支出", "结余"], finance.orgs, (o) =>
      `<tr><td>${o.org_id}</td><td>${o.income}</td><td>${o.expense}</td><td>${o.balance}</td></tr>`)}</div>
    <div class="panel"><h3>物资（出入库全程留痕）</h3>${table(["ID", "编码", "名称", "机构", "数量", "状态", "操作"], assets, (a) =>
      `<tr><td>${a.id}</td><td>${esc(a.code)}</td><td>${esc(a.name)}</td><td>${a.org_id}</td><td>${a.quantity}</td>
       <td><span class="tag ${a.status === "scrapped" ? "red" : ""}">${a.status === "scrapped" ? "已报废" : a.status}</span></td>
       <td>${a.status !== "scrapped" ? `<button class="btn secondary" data-assetmv="${a.id}">出入库</button>` : ""}
           <button class="btn" data-assethist="${a.id}">记录</button></td></tr>`)}</div>
    <div class="panel hidden" id="assetmv-panel"><h3>物资出入库记录</h3><div id="assetmv-list"></div></div>`;
  $("#emp-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/employees", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  $("#sec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/secondments", formJson(e.target, ["employee_id", "to_org_id"]), "#hrf-msg"); };
  $("#fin-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/finance", formJson(e.target, ["org_id", "amount"]), "#hrf-msg"); };
  $("#asset-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/assets", formJson(e.target, ["org_id", "quantity"]), "#hrf-msg"); };
  $("#dept-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/departments", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  const payForm = $("#pay-form");
  if (payForm) payForm.onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/payroll", formJson(e.target, ["employee_id", "base_salary", "perf_bonus", "perf_coefficient"]), "#hrf-msg"); };
  const budForm = $("#bud-form");
  if (budForm) budForm.onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/budgets", formJson(e.target, ["org_id", "amount"]), "#hrf-msg"); };
  const budExec = $("#bud-exec-form");
  if (budExec) budExec.onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try {
      const r = await api(`/api/mgmt/budgets/execution?org_id=${f.get("org_id")}&year=${f.get("year")}`);
      $("#bud-exec").innerHTML = table(["类别", "预算", "实际", "执行率"], [
        ["收入", r.income], ["支出", r.expense]], ([label, d]) =>
        `<tr><td>${label}</td><td>${d.budget}</td><td>${d.actual}</td>
         <td>${d.execution_pct === null ? "—" : `<span class="tag ${d.execution_pct > 100 ? "red" : "green"}">${d.execution_pct}%</span>`}</td></tr>`);
    } catch (err) { setMsg("#hrf-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.empdept) {
        const deptId = prompt("科室ID（须与员工同机构）"); if (!deptId) return;
        await api(`/api/mgmt/employees/${d.empdept}/department?dept_id=${Number(deptId)}`, { method: "POST" });
        route();
      }
      if (d.empchg) {
        const keys = Object.keys(CHG_TYPES);
        const pick = prompt(`变动类型（${keys.map((k, i) => `${i + 1}=${CHG_TYPES[k]}`).join("，")}）输入序号`);
        const type = keys[Number(pick) - 1]; if (!type) return;
        const body = { change_type: type, detail: prompt("变动说明") || "", effective_date: prompt("生效日期 YYYY-MM-DD") || "" };
        if (type === "transfer") {
          const toOrg = prompt("调入机构ID"); if (!toOrg) return;
          body.to_org_id = Number(toOrg);
        }
        return postAction(`/api/mgmt/employees/${d.empchg}/changes`, body, "#hrf-msg");
      }
      if (d.emphist) {
        const changes = await api(`/api/mgmt/employees/${d.emphist}/changes`);
        $("#empchg-panel").classList.remove("hidden");
        $("#empchg-list").innerHTML = table(["ID", "类型", "调入机构", "说明", "生效日期"], changes, (c) =>
          `<tr><td>${c.id}</td><td><span class="tag">${CHG_TYPES[c.change_type] || esc(c.change_type)}</span></td>
           <td>${c.to_org_id ?? "—"}</td><td>${esc(c.detail) || "—"}</td><td>${esc(c.effective_date) || "—"}</td></tr>`);
      }
      if (d.empct) {
        const no = prompt("合同编号"); if (!no) return;
        const start = prompt("起期 YYYY-MM-DD"); if (!start) return;
        const end = prompt("止期 YYYY-MM-DD"); if (!end) return;
        return postAction("/api/mgmt/staff-contracts",
          { employee_id: Number(d.empct), contract_no: no, start_date: start, end_date: end }, "#hrf-msg");
      }
      if (d.assetmv) {
        const keys = Object.keys(MV_TYPES);
        const pick = prompt(`动作（${keys.map((k, i) => `${i + 1}=${MV_TYPES[k]}`).join("，")}）输入序号`);
        const type = keys[Number(pick) - 1]; if (!type) return;
        const qty = prompt("数量"); if (!qty) return;
        return postAction(`/api/mgmt/assets/${d.assetmv}/movements`,
          { movement_type: type, quantity: Number(qty), note: prompt("备注") || "" }, "#hrf-msg");
      }
      if (d.assethist) {
        const moves = await api(`/api/mgmt/assets/${d.assethist}/movements`);
        $("#assetmv-panel").classList.remove("hidden");
        $("#assetmv-list").innerHTML = table(["ID", "动作", "数量", "备注", "时间"], moves, (m) =>
          `<tr><td>${m.id}</td><td><span class="tag">${MV_TYPES[m.movement_type] || esc(m.movement_type)}</span></td>
           <td>${m.quantity}</td><td>${esc(m.note) || "—"}</td><td>${esc(m.at.slice(0, 16).replace("T", " "))}</td></tr>`);
      }
    } catch (err) { setMsg("#hrf-msg", err.message, false); }
  };
}

async function renderOaQc() {
  $("#page-desc").textContent = "行政公文（起草→发布）、共享中心排班与质控";
  const [docs, rosters, qc] = await Promise.all([api("/api/mgmt/docs"), api("/api/mgmt/rosters"), api("/api/mgmt/qc")]);
  const CN = { imaging: "影像", ecg: "心电", lab: "检验", pathology: "病理" };
  $("#page-body").innerHTML = `
    <div class="panel"><h3>公文起草 / 排班 / 质控登记</h3>
      <form class="inline" id="doc-form"><input name="title" placeholder="公文标题" required style="min-width:240px">
        <select name="doc_type"><option value="notice">通知</option><option value="policy">政策文件</option><option value="minutes">会议纪要</option></select>
        <input name="issuer" placeholder="发文单位"><button>起草</button></form>
      <form class="inline" id="roster-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="duty_date" placeholder="值班日期 YYYY-MM-DD" required><input name="shift" placeholder="班次" value="全天"><input name="doctor_name" placeholder="医师" required><button>排班</button></form>
      <form class="inline" id="qc-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="item" placeholder="质控项目" required><select name="result"><option value="pass">合格</option><option value="fail">不合格</option></select>
        <input name="note" placeholder="备注"><input name="record_date" placeholder="日期"><button>登记质控</button></form>
      <p class="msg" id="oa-msg"></p></div>
    <div class="panel"><h3>公文</h3>${table(["ID", "标题", "类型", "发文单位", "状态", "操作"], docs, (d) =>
      `<tr><td>${d.id}</td><td>${esc(d.title)}</td><td>${esc(d.doc_type)}</td><td>${esc(d.issuer)}</td>
       <td><span class="tag ${d.status === "published" ? "green" : "orange"}">${d.status === "published" ? "已发布" : "草稿"}</span></td>
       <td>${d.status === "draft" ? `<button class="btn secondary" data-pub="${d.id}">发布</button>` : "—"}</td></tr>`)}</div>
    <div class="panel"><h3>排班</h3>${table(["中心", "日期", "班次", "医师"], rosters, (r) =>
      `<tr><td>${CN[r.center_type]}</td><td>${esc(r.duty_date)}</td><td>${esc(r.shift)}</td><td>${esc(r.doctor_name)}</td></tr>`)}</div>
    <div class="panel"><h3>质控记录</h3>${table(["中心", "项目", "结果", "备注"], qc, (q) =>
      `<tr><td>${CN[q.center_type]}</td><td>${esc(q.item)}</td>
       <td><span class="tag ${q.result === "pass" ? "green" : "red"}">${q.result === "pass" ? "合格" : "不合格"}</span></td><td>${esc(q.note)}</td></tr>`)}</div>`;
  $("#doc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/docs", formJson(e.target), "#oa-msg"); };
  $("#roster-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/rosters", formJson(e.target), "#oa-msg"); };
  $("#qc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/qc", formJson(e.target), "#oa-msg"); };
  $("#page-body").onclick = (e) => { if (e.target.dataset.pub) postAction(`/api/mgmt/docs/${e.target.dataset.pub}/publish`, null, "#oa-msg"); };
}

/* ---------------- 第四阶段新增页面 ---------------- */

const CRIT_STATUS = { notified: ["已通知", "orange"], acknowledged: ["已确认", ""], resolved: ["已处置", "green"], "": ["待回填", "orange"] };

async function renderCritical() {
  $("#page-desc").textContent = "危急值闭环：通知 → 医师确认接收 → 处置反馈；超时未确认催办";
  const [critical, unacked] = await Promise.all([
    api("/api/exams/critical"), api("/api/exams/critical/unacknowledged")]);
  $("#page-body").innerHTML = `
    ${unacked.length ? `<div class="panel" style="border-left:4px solid #c62828"><h3>⚠ 超时未确认催办（${unacked.length}）</h3>${
      table(["报告ID", "申请单", "结论", "报告人", "报告时间"], unacked, (r) =>
        `<tr><td>${r.report_id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
         <td>${esc(r.reported_by)}</td><td>${esc(r.reported_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>危急值清单</h3><p class="msg" id="crit-msg"></p>${
      table(["报告ID", "申请单", "结论", "闭环状态", "操作"], critical, (r) => {
        const [text, color] = CRIT_STATUS[r.critical_status] || [r.critical_status, ""];
        const actions = (r.critical_status === "notified" || r.critical_status === "")
          ? `<button class="btn secondary" data-ack="${r.id}">确认接收</button>`
          : r.critical_status === "acknowledged"
          ? `<button class="btn secondary" data-resolve="${r.id}">处置反馈</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
          <td><span class="tag ${color}">${text}</span></td>
          <td>${actions} <button class="btn" data-trail="${r.id}">留痕</button></td></tr>`;
      })}</div>
    <div class="panel hidden" id="crit-trail-panel"><h3>处置留痕轨迹</h3><div id="crit-trail"></div></div>`;
  $("#page-body").onclick = async (e) => {
    const { ack, resolve, trail } = e.target.dataset;
    try {
      if (ack) { await api(`/api/exams/reports/${ack}/acknowledge`, { method: "POST" }); route(); }
      if (resolve) {
        const note = prompt("处置反馈说明（如：已复查、已调整治疗）") || "";
        await api(`/api/exams/reports/${resolve}/resolve`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (trail) {
        const actions = await api(`/api/exams/reports/${trail}/critical-actions`);
        $("#crit-trail-panel").classList.remove("hidden");
        $("#crit-trail").innerHTML = table(["动作", "操作人"], actions, (a) =>
          `<tr><td>${esc(a.action)}</td><td>${esc(a.actor)}</td></tr>`);
      }
    } catch (err) { setMsg("#crit-msg", err.message, false); }
  };
}

async function renderRecognition() {
  $("#page-desc").textContent = "互认项目目录（目录内 active 项目方可互认）、互认率统计与检查资源要素档案";
  const [items, stats, resources] = await Promise.all([
    api("/api/exams/recognition-items"), api("/api/exams/recognition-stats"), api("/api/exams/resources")]);
  const cards = [
    ["互认总次数", stats.recognized_total], ["已报告总数", stats.reported_total],
    ["互认率", stats.recognition_ratio_pct + "%"], ["节约检查次数", stats.saved_exams]];
  $("#page-body").innerHTML = `
    <div class="cards">${cards.map(([l, v]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    ${stats.by_item.length ? `<div class="panel"><h3>按项目互认次数</h3>${
      barChart(stats.by_item.slice(0, 10).map((i) => [i.item_name, i.recognized_count]), { unit: " 次" })}</div>` : ""}
    <div class="panel"><h3>目录维护（admin）</h3>
      <form class="inline" id="rec-form">
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="mutual_scope"><option value="county">县域互认</option><option value="city">市级互认</option></select>
        <button>加入目录</button>
      </form><p class="msg" id="rec-msg"></p>
      ${table(["编码", "名称", "中心", "范围", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.item_code)}</td><td>${esc(i.item_name)}</td><td>${CENTER_NAMES[i.center_type]}</td>
         <td>${i.mutual_scope === "city" ? "市级" : "县域"}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-toggle="${i.id}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button></td></tr>`)}</div>
    <div class="panel"><h3>检查资源要素档案（设备/价格/时长/注意事项，admin 建档）</h3>
      <form class="inline" id="res-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="item_name" placeholder="项目名称" required>
        <input name="device" placeholder="设备">
        <input name="price" type="number" step="any" placeholder="价格(元)">
        <input name="duration_min" type="number" min="1" placeholder="时长(分)">
        <input name="notes" placeholder="注意事项" style="min-width:160px">
        <button>建档</button></form>
      ${table(["ID", "机构", "中心", "项目", "设备", "价格", "时长", "注意事项"], resources, (r) =>
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${CENTER_NAMES[r.center_type]}</td><td>${esc(r.item_name)}</td>
         <td>${esc(r.device) || "—"}</td><td>${r.price} 元</td><td>${r.duration_min} 分</td><td>${esc(r.notes) || "—"}</td></tr>`)}</div>`;
  $("#rec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/exams/recognition-items", formJson(e.target), "#rec-msg"); };
  $("#res-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/exams/resources", formJson(e.target, ["org_id", "price", "duration_min"]), "#rec-msg"); };
  $("#page-body").onclick = async (e) => {
    const id = e.target.dataset.toggle;
    if (!id) return;
    try {
      await api(`/api/exams/recognition-items/${id}`, { method: "PATCH",
        body: JSON.stringify({ active: e.target.dataset.active !== "true" }) });
      route();
    } catch (err) { setMsg("#rec-msg", err.message, false); }
  };
}

async function renderInpatient() {
  $("#page-desc").textContent = "入院登记（床位原子占用）→ 转科/转床 → 医嘱 → 病案首页 → 出院（费用结清校验）";
  const [wards, beds, admissions, stats] = await Promise.all([
    api("/api/inpatient/wards"), api("/api/inpatient/beds"),
    api("/api/inpatient/admissions"), api("/api/inpatient/stats")]);
  const AS = { admitted: ["在院", "orange"], discharged: ["已出院", "green"] };
  const wardName = Object.fromEntries(wards.map((w) => [w.id, w.name]));
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="panel"><h3>床位效率</h3>${table(["机构", "床位", "占用", "使用率", "在院", "累计出院"], stats, (s) =>
      `<tr><td>${esc(s.org_name)}</td><td>${s.beds_total}</td><td>${s.beds_occupied}</td>
       <td>${s.occupancy_pct}%</td><td>${s.in_hospital}</td><td>${s.discharged_total}</td></tr>`)}</div>` : ""}
    <div class="panel"><h3>病区/床位建档（admin）与入院登记</h3>
      <form class="inline" id="ward-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="病区名称" required><button>建病区</button></form>
      <form class="inline" id="bed-form"><input name="ward_id" type="number" placeholder="病区ID" required>
        <input name="bed_no" placeholder="床号" required><button>建床位</button></form>
      <form class="inline" id="adm-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="ward_id" type="number" placeholder="病区ID" required><input name="bed_id" type="number" placeholder="床位ID" required>
        <input name="doctor_name" placeholder="主管医师"><input name="diagnosis_name" placeholder="入院诊断"><button>入院登记</button></form>
      <p class="msg" id="inp-msg"></p></div>
    <div class="panel"><h3>床位（${beds.filter((b) => b.status === "free").length} 空闲 / ${beds.length}）</h3>${
      table(["ID", "病区", "床号", "状态"], beds, (b) =>
        `<tr><td>${b.id}</td><td>${esc(wardName[b.ward_id] || b.ward_id)}</td><td>${esc(b.bed_no)}</td>
         <td><span class="tag ${b.status === "free" ? "green" : "orange"}">${b.status === "free" ? "空闲" : "占用"}</span></td></tr>`)}</div>
    <div class="panel"><h3>住院记录</h3>${
      table(["ID", "患者", "病区/床位", "诊断", "状态", "操作"], admissions, (a) => {
        const [text, color] = AS[a.status] || [a.status, ""];
        const actions = a.status === "admitted"
          ? `<button class="btn secondary" data-transfer="${a.id}">转床</button>
             <button class="btn secondary" data-order="${a.id}">开医嘱</button>
             <button class="btn secondary" data-summary="${a.id}">病案首页</button>
             <button class="btn danger" data-discharge="${a.id}">出院</button>`
          : "—";
        return `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(wardName[a.ward_id] || a.ward_id)} / ${a.bed_id}</td>
          <td>${esc(a.diagnosis_name)}</td><td><span class="tag ${color}">${text}</span></td>
          <td>${actions} <button class="btn" data-orders="${a.id}">医嘱单</button></td></tr>`;
      })}</div>
    <div class="panel hidden" id="inp-orders-panel"><h3>医嘱单</h3><div id="inp-orders"></div></div>`;
  $("#ward-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/wards", formJson(e.target, ["org_id"]), "#inp-msg"); };
  $("#bed-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/beds", formJson(e.target, ["ward_id"]), "#inp-msg"); };
  $("#adm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/admissions", formJson(e.target, ["patient_id", "ward_id", "bed_id"]), "#inp-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.transfer) {
        const wardId = prompt("目标病区ID"), bedId = prompt("目标床位ID");
        if (!wardId || !bedId) return;
        await api(`/api/inpatient/admissions/${d.transfer}/transfer`, { method: "POST",
          body: JSON.stringify({ ward_id: Number(wardId), bed_id: Number(bedId) }) });
        route();
      }
      if (d.order) {
        const content = prompt("医嘱内容");
        if (!content) return;
        const isLong = confirm("长期医嘱？（确定=长期，取消=临时）");
        await api("/api/inpatient/orders", { method: "POST",
          body: JSON.stringify({ admission_id: Number(d.order), order_type: isLong ? "long" : "temp", content }) });
        route();
      }
      if (d.summary) {
        const diagnosis = prompt("出院诊断");
        if (!diagnosis) return;
        await api(`/api/inpatient/admissions/${d.summary}/case-summary`, { method: "POST",
          body: JSON.stringify({
            discharge_diagnosis: diagnosis, operation: prompt("手术名称（无则留空）") || "",
            total_cost: Number(prompt("总费用（元）") || 0), drug_cost: Number(prompt("其中药费（元）") || 0) }) });
        route();
      }
      if (d.discharge) { await api(`/api/inpatient/admissions/${d.discharge}/discharge`, { method: "POST" }); route(); }
      if (d.stopOrder) { await api(`/api/inpatient/orders/${d.stopOrder}/stop`, { method: "POST" }); route(); }
      if (d.orders) {
        const orders = await api(`/api/inpatient/orders?admission_id=${d.orders}`);
        $("#inp-orders-panel").classList.remove("hidden");
        $("#inp-orders").innerHTML = table(["ID", "类型", "内容", "状态", "开立", "操作"], orders, (o) =>
          `<tr><td>${o.id}</td><td>${o.order_type === "long" ? "长期" : "临时"}</td><td>${esc(o.content)}</td>
           <td><span class="tag ${o.status === "active" ? "orange" : "green"}">${o.status === "active" ? "执行中" : "已停止"}</span></td>
           <td>${esc(o.created_by_name)}</td>
           <td>${o.status === "active" ? `<button class="btn danger" data-stop-order="${o.id}">停止</button>` : "—"}</td></tr>`);
      }
    } catch (err) { setMsg("#inp-msg", err.message, false); }
  };
}

/* 块3：支付渠道/状态/对账差异类型 */
const PAY_CHANNELS = { cash: "现金", card: "银行卡", insurance: "医保基金", online: "线上支付" };
const PAY_STATUS = { pending: ["待支付", "orange"], paid: ["已支付", "green"], refunded: ["已退款", ""], failed: ["支付失败", "red"] };
const RECON_DIFF = { missing_local: "通道有本地无", missing_remote: "本地有通道无", amount_mismatch: "金额不一致" };

async function renderBilling() {
  $("#page-desc").textContent = "收费目录 → 计费明细 → 结算（医保分担）→ 统一支付（多渠道/退款）→ 日终对账差异核查";
  const today = new Date().toISOString().slice(0, 10);
  const [items, settlements, stats, payments, batches] = await Promise.all([
    api("/api/billing/charge-items"), api("/api/billing/settlements"), api("/api/billing/stats"),
    api("/api/billing/payments"), api("/api/billing/reconciliation")]);
  const BT = { outpatient: "门诊", inpatient: "住院" };
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="cards">${stats.map((s) =>
      `<div class="card"><div class="label">${BT[s.bill_type]}结算 ${s.count} 笔</div>
       <div class="value">${s.total_amount} 元</div>
       <div class="label">均次 ${s.avg_amount} 元 · 医保 ${s.insurance_ratio_pct}%</div></div>`).join("")}</div>` : ""}
    <div class="panel"><h3>收费项目目录（admin 维护）</h3>
      <form class="inline" id="ci-form"><input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="category"><option value="treatment">治疗处置</option><option value="drug">药品</option>
          <option value="exam">检查检验</option><option value="bed">床位</option><option value="other">其他</option></select>
        <input name="price" type="number" step="any" placeholder="单价(元)" required><button>加入目录</button></form>
      <p class="msg" id="bill-msg"></p>
      ${table(["编码", "名称", "类别", "单价", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td><td>${esc(i.category)}</td><td>${i.price}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-reprice="${i.id}">调价</button>
             <button class="btn secondary" data-history="${i.id}">调价历史</button></td></tr>`)}</div>
    <div class="panel"><h3>计费与结算</h3>
      <form class="inline" id="bd-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="admission_id" type="number" placeholder="住院单ID(住院)"><input name="encounter_id" type="number" placeholder="就诊ID(门诊)">
        <input name="item_code" placeholder="收费编码" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>计费</button></form>
      <form class="inline" id="settle-form">
        <select name="bill_type"><option value="inpatient">住院结算</option><option value="outpatient">门诊结算</option></select>
        <input name="admission_id" type="number" placeholder="住院单ID"><input name="encounter_id" type="number" placeholder="就诊ID">
        <input name="insurance_pay" type="number" step="any" placeholder="医保支付(元)" value="0"><button>结算</button></form>
      <p style="font-size:12.5px;color:#8a939e">住院费用未结清不可出院；结算自动汇总未结清明细并联动医保结算记录</p></div>
    <div class="panel"><h3>结算单</h3>${table(["ID", "患者", "类型", "总额", "医保", "自付", "时间"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${BT[s.bill_type]}</td><td>${s.total_amount}</td>
       <td>${s.insurance_pay}</td><td>${s.self_pay}</td><td>${esc(s.created_at.slice(0, 16).replace("T", " "))}</td></tr>`)}</div>
    <div class="panel"><h3>统一支付（经办）</h3>
      <form class="inline" id="pay-form">
        <input name="settlement_id" type="number" placeholder="结算单ID" required>
        <select name="channel">${Object.entries(PAY_CHANNELS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="amount" type="number" step="any" placeholder="金额(元，空=自付额)">
        <button>发起支付</button></form>
      <p class="msg" id="pay-msg"></p>
      <p style="font-size:12.5px;color:#8a939e">渠道对接经 PaymentGateway 协议实现，演示环境使用内置 Mock 通道；仅已支付单可退款且不超可退余额</p>
      ${table(["ID", "结算单", "渠道", "金额", "已退", "状态", "外部流水号", "操作"], payments.slice(0, 30), (p) => {
        const [text, color] = PAY_STATUS[p.status] || [p.status, ""];
        return `<tr><td>${p.id}</td><td>${p.settlement_id}</td><td>${esc(p.channel_name)}</td><td>${p.amount}</td>
          <td>${p.refunded_amount || 0}</td><td><span class="tag ${color}">${text}</span>${p.fail_reason ? `<div style="font-size:12px;color:#b23c3c">${esc(p.fail_reason)}</div>` : ""}</td>
          <td style="font-size:12px">${esc(p.trade_no) || "—"}</td>
          <td>${p.status === "paid" ? `<button class="btn secondary" data-refund="${p.id}">退款</button>` : "—"}</td></tr>`;
      })}</div>
    <div class="panel"><h3>日终对账</h3>
      <form class="inline" id="recon-form">
        <input name="date" placeholder="对账日期 YYYY-MM-DD" value="${today}" required>
        <button>生成对账单</button></form>
      <p class="msg" id="recon-msg"></p>
      ${batches.map((b) => `<div style="margin-top:10px">
        <p style="font-size:13px"><b>${esc(b.date)}</b>：支付单 ${b.total_orders} 笔 / 合计 ${b.total_amount} 元，
          匹配 ${b.matched} 笔，差异 <span class="tag ${b.unmatched ? "red" : "green"}">${b.unmatched}</span> 笔，
          差异金额 ${b.diff_amount} 元</p>
        ${b.diffs.length ? table(["类型", "支付单", "流水号", "本地金额", "通道金额", "说明"], b.diffs, (d) =>
          `<tr><td><span class="tag red">${esc(RECON_DIFF[d.diff_type] || d.diff_type)}</span></td>
           <td>${d.order_id ?? "—"}</td><td style="font-size:12px">${esc(d.trade_no)}</td>
           <td>${d.local_amount}</td><td>${d.remote_amount}</td><td style="font-size:12px">${esc(d.detail)}</td></tr>`) : ""}
        </div>`).join("") || '<p class="desc">暂无对账单</p>'}</div>`;
  $("#ci-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/charge-items", formJson(e.target, ["price"]), "#bill-msg"); };
  $("#bd-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/details", formJson(e.target, ["patient_id", "admission_id", "encounter_id", "quantity"]), "#bill-msg"); };
  $("#settle-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/settlements", formJson(e.target, ["admission_id", "encounter_id", "insurance_pay"]), "#bill-msg"); };
  $("#pay-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { settlement_id: Number(f.get("settlement_id")), channel: f.get("channel") };
    if (f.get("amount")) body.amount = Number(f.get("amount"));
    try {
      const order = await api("/api/billing/payments", { method: "POST", body: JSON.stringify(body) });
      setMsg("#pay-msg", order.status === "paid"
        ? `支付成功，流水号 ${order.trade_no}` : `支付失败：${order.fail_reason}`, order.status === "paid");
      route();
    } catch (err) { setMsg("#pay-msg", err.message, false); }
  };
  $("#recon-form").onsubmit = async (e) => {
    e.preventDefault();
    const date = new FormData(e.target).get("date");
    try {
      const batch = await api(`/api/billing/reconciliation/run?date=${encodeURIComponent(date)}`, { method: "POST" });
      setMsg("#recon-msg", `对账完成：${batch.total_orders} 笔，差异 ${batch.unmatched} 笔（${batch.diff_amount} 元）`, batch.unmatched === 0);
      route();
    } catch (err) { setMsg("#recon-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { reprice, history, refund } = e.target.dataset;
    try {
      if (reprice) {
        const price = prompt("新单价（元）");
        if (!price) return;
        // 走 reprice 而不是 PATCH：价格要对外公示，调价依据与生效日期必须留下来
        const reason = prompt("调价依据（如：省医保局2026年第3号文，可留空）") || "";
        const effective_date = prompt("生效日期 YYYY-MM-DD（可留空）") || "";
        await api(`/api/billing/charge-items/${reprice}/reprice`, {
          method: "POST",
          body: JSON.stringify({ new_price: Number(price), reason, effective_date }) });
        route();
      } else if (history) {
        const rows = await api(`/api/billing/charge-items/${history}/price-history`);
        setMsg("#bill-msg", rows.length
          ? rows.map((r) => `${r.changed_at.slice(0, 10)} ${r.old_price}→${r.new_price}元${
              r.effective_date ? `（${r.effective_date}起）` : ""}${r.reason ? ` ${r.reason}` : ""}`).join("；")
          : "该项目尚无调价记录", true);
      } else if (refund) {
        const amount = prompt("退款金额（元，留空为全额退款）");
        if (amount === null) return;
        const body = amount ? { amount: Number(amount) } : {};
        const res = await api(`/api/billing/payments/${refund}/refund`, { method: "POST", body: JSON.stringify(body) });
        setMsg("#pay-msg", `退款成功 ${res.refund_amount} 元，退款单号 ${res.refund_no}`);
        route();
      }
    } catch (err) { setMsg("#bill-msg", err.message, false); }
  };
}

/* 块2：环节质控字段与等级配色（甲绿/乙橙/丙红） */
const MR_FIELDS = [
  ["chief_complaint", "主诉", "如：咳嗽发热3天（≤20字）", 1],
  ["present_illness", "现病史", "起病时间、诱因、演变、伴随症状（≥50字）", 3],
  ["past_history", "既往史", "既往疾病、手术、过敏史", 2],
  ["physical_exam", "体格检查", "须含体温/血压/脉搏等生命体征", 2],
  ["diagnosis_basis", "诊断依据", "症状+体征+辅助检查支持依据（≥30字）", 3],
  ["treatment_plan", "治疗方案", "用药、处置、随访安排；危急值须写明处置", 3],
];
const MR_GRADE_COLOR = { 甲: "green", 乙: "orange", 丙: "red" };

async function renderQuality() {
  $("#page-desc").textContent = "不良事件上报（可匿名）→ 审核 → 整改；结构化病历实时环节质控；病历抽检评分；院感上报核实";
  const [events, estats, qstats, infections, mrSummary, mrRecords] = await Promise.all([
    api("/api/quality/adverse-events"), api("/api/quality/adverse-events-stats"),
    api("/api/quality/record-qc-stats"), api("/api/quality/infection-reports"),
    api("/api/quality/records/qc-summary"), api("/api/quality/records")]);
  const AES = { reported: ["已上报", "orange"], reviewed: ["已审核", ""], rectified: ["已整改", "green"] };
  const AET = { medication: "用药", device: "器械", fall: "跌倒", pressure_sore: "压疮", transfusion: "输血", identification: "查对", other: "其他" };
  const SITE = { respiratory: "呼吸道", surgical_site: "手术部位", urinary: "泌尿道", bloodstream: "血流", gastrointestinal: "消化道", other: "其他" };
  const IST = { reported: ["待核实", "orange"], confirmed: ["已确认", "red"], excluded: ["已排除", "green"] };
  const cards = [
    ["不良事件", estats.total], ["整改闭环率", estats.closed_loop_pct + "%"],
    ["病历抽检", qstats.total], ["病历均分", qstats.avg_score], ["病历甲级率", qstats.grade_a_pct + "%"]];
  $("#page-body").innerHTML = `
    <div class="cards">${cards.map(([l, v]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    <div class="panel"><h3>不良事件上报</h3>
      <form class="inline" id="ae-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="event_type">${Object.entries(AET).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="level"><option value="IV">IV级(隐患)</option><option value="III">III级(无后果)</option>
          <option value="II">II级(不良后果)</option><option value="I">I级(警告)</option></select>
        <input name="description" placeholder="事件经过" required style="min-width:220px">
        <label style="font-size:13px"><input type="checkbox" name="anonymous" value="true"> 匿名</label><button>上报</button></form>
      <p class="msg" id="qa-msg"></p>
      ${table(["ID", "类型", "等级", "经过", "报告人", "状态", "操作"], events, (ev) => {
        const [text, color] = AES[ev.status] || [ev.status, ""];
        const actions = ev.status === "reported"
          ? `<button class="btn secondary" data-review="${ev.id}">审核</button>`
          : ev.status === "reviewed"
          ? `<button class="btn secondary" data-rectify="${ev.id}">登记整改</button>` : "—";
        return `<tr><td>${ev.id}</td><td>${AET[ev.event_type] || ev.event_type}</td><td>${ev.level}</td>
          <td>${esc(ev.description)}</td><td>${esc(ev.reporter_name) || "（匿名）"}</td>
          <td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
      })}</div>
    <div class="panel"><h3>不良事件附件（现场照片/佐证PDF，≤10MB）</h3>
      <form class="inline" id="ae-att-form">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传</button></form>
      <form class="inline" id="ae-att-query">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <button>查附件</button></form>
      <p class="msg" id="ae-att-msg"></p><div id="ae-att-list"></div></div>
    <div class="panel"><h3>结构化病历录入（医师）——提交即出环节质控评分</h3>
      <form id="mr-form">
        <div class="inline"><input name="encounter_id" type="number" placeholder="就诊ID" required>
          <span class="desc" style="font-size:12px">同一就诊仅一份病历，再次提交为修正并复评</span></div>
        ${MR_FIELDS.map(([key, label, hint, rows]) =>
          `<div style="margin-top:8px"><label style="font-size:13px">${label}<span class="desc" style="font-size:12px">（${hint}）</span></label>
           <textarea name="${key}" rows="${rows}" style="width:100%"></textarea></div>`).join("")}
        <div class="inline" style="margin-top:8px"><button>提交并质控评分</button></div></form>
      <p class="msg" id="mr-msg"></p><div id="mr-result"></div></div>
    <div class="panel"><h3>环节质控概览（甲 ${mrSummary.grade_distribution["甲"]} / 乙 ${mrSummary.grade_distribution["乙"]} / 丙 ${mrSummary.grade_distribution["丙"]}，均分 ${mrSummary.avg_score}）</h3>
      ${table(["机构", "病历数", "均分", "甲", "乙", "丙", "甲级率"], mrSummary.by_org, (o) =>
        `<tr><td>${esc(o.name)}</td><td>${o.total}</td><td>${o.avg_score}</td>
         <td><span class="tag green">${o.grade_a}</span></td><td><span class="tag orange">${o.grade_b}</span></td>
         <td>${o.grade_c ? `<span class="tag red">${o.grade_c}</span>` : 0}</td><td>${o.grade_a_pct}%</td></tr>`)}
      <h3 style="margin-top:12px">按医师</h3>
      ${table(["医师", "病历数", "均分", "甲", "乙", "丙"], mrSummary.by_doctor, (d) =>
        `<tr><td>${esc(d.name) || "（未署名）"}</td><td>${d.total}</td><td>${d.avg_score}</td>
         <td>${d.grade_a}</td><td>${d.grade_b}</td><td>${d.grade_c}</td></tr>`)}
      <h3 style="margin-top:12px">最近病历</h3>
      ${table(["ID", "就诊", "医师", "主诉", "得分", "等级", "操作"], mrRecords.slice(0, 20), (r) =>
        `<tr><td>${r.id}</td><td>${r.encounter_id}</td><td>${esc(r.doctor_name)}</td>
         <td>${esc(r.chief_complaint) || "（未填）"}</td><td>${r.qc_score}</td>
         <td><span class="tag ${MR_GRADE_COLOR[r.qc_grade] || ""}">${r.qc_grade}级</span></td>
         <td><button class="btn secondary" data-mrqc="${r.id}">复评并看缺陷</button></td></tr>`)}</div>
    <div class="panel"><h3>病历质控抽检（人工评分）</h3>
      <form class="inline" id="qc-rec-form">
        <select name="target_type"><option value="encounter">门急诊病历</option><option value="case_summary">病案首页</option></select>
        <input name="target_id" type="number" placeholder="对象ID" required>
        <input name="score" type="number" min="0" max="100" placeholder="评分0-100" required>
        <input name="defects" placeholder="缺陷项（分号分隔）" style="min-width:200px"><button>评分</button></form></div>
    <div class="panel"><h3>院感上报</h3>
      <form class="inline" id="inf-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="infection_site">${Object.entries(SITE).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="pathogen" placeholder="病原体"><input name="report_date" placeholder="日期 YYYY-MM-DD"><button>上报</button></form>
      ${table(["ID", "机构", "患者", "部位", "病原体", "状态", "操作"], infections, (r) => {
        const [text, color] = IST[r.status] || [r.status, ""];
        const actions = r.status === "reported"
          ? `<button class="btn secondary" data-verify="${r.id}" data-ok="true">确认</button>
             <button class="btn" data-verify="${r.id}" data-ok="false">排除</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${r.patient_id}</td><td>${SITE[r.infection_site]}</td>
          <td>${esc(r.pathogen)}</td><td><span class="tag ${color}">${text}</span></td><td>${actions}</td></tr>`;
      })}</div>`;
  $("#ae-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["org_id"]);
    body.anonymous = e.target.anonymous.checked;
    postAction("/api/quality/adverse-events", body, "#qa-msg");
  };
  $("#ae-att-form").onsubmit = async (e) => {
    e.preventDefault();
    const eventId = new FormData(e.target).get("event_id");
    try {
      await uploadAttachment("adverse_event", eventId, e.target.querySelector("input[type=file]"));
      setMsg("#ae-att-msg", "附件已上传");
      await drawAttachments("adverse_event", eventId, "#ae-att-list", "#ae-att-msg");
    } catch (err) { setMsg("#ae-att-msg", err.message, false); }
  };
  $("#ae-att-query").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawAttachments("adverse_event", new FormData(e.target).get("event_id"), "#ae-att-list", "#ae-att-msg"); }
    catch (err) { setMsg("#ae-att-msg", err.message, false); }
  };
  const drawQcResult = (qc, title) => {
    $("#mr-result").innerHTML = `
      <p style="font-size:13px">${esc(title)}：得分 <b>${qc.score}</b> 分，
        <span class="tag ${MR_GRADE_COLOR[qc.grade] || ""}">${qc.grade}级</span>
        （参与规则 ${qc.rules_checked} 条，扣 ${qc.deducted} 分）</p>
      ${qc.defects.length
        ? table(["规则", "环节", "缺陷描述", "扣分"], qc.defects, (d) =>
            `<tr style="color:#b23c3c"><td>${esc(d.rule_code)} ${esc(d.rule_name)}</td><td>${esc(d.field_name)}</td>
             <td>${esc(d.message)}</td><td>-${d.deduct_points}</td></tr>`)
        : '<p class="msg ok">无缺陷项，病历书写合规</p>'}`;
  };
  $("#mr-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { encounter_id: Number(f.get("encounter_id")) };
    MR_FIELDS.forEach(([key]) => { body[key] = f.get(key) || ""; });
    try {
      const res = await api("/api/quality/records", { method: "POST", body: JSON.stringify(body) });
      setMsg("#mr-msg", `${res.created ? "病历已提交" : "病历已修正"}（记录 #${res.record.id}）`);
      drawQcResult(res.qc, `就诊 ${body.encounter_id} 环节质控`);
    } catch (err) { setMsg("#mr-msg", err.message, false); }
  };
  $("#qc-rec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/quality/record-qc", formJson(e.target, ["target_id", "score"]), "#qa-msg"); };
  $("#inf-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/quality/infection-reports", formJson(e.target, ["org_id", "patient_id"]), "#qa-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.review) {
        const note = prompt("审核意见");
        if (!note) return;
        await api(`/api/quality/adverse-events/${d.review}/review`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (d.rectify) {
        const note = prompt("整改措施");
        if (!note) return;
        await api(`/api/quality/adverse-events/${d.rectify}/rectify`, { method: "POST", body: JSON.stringify({ note }) });
        route();
      }
      if (d.verify) {
        await api(`/api/quality/infection-reports/${d.verify}/verify?confirmed=${d.ok}`, { method: "POST" });
        route();
      }
      if (d.mrqc) {
        const qc = await api(`/api/quality/records/${d.mrqc}/qc`);
        drawQcResult(qc, `病历 #${d.mrqc} 复评`);
        $("#mr-result").scrollIntoView({ behavior: "smooth", block: "center" });
      }
    } catch (err) { setMsg("#qa-msg", err.message, false); }
  };
}

async function renderPerfIndicators() {
  $("#page-desc").textContent = "绩效指标目录：权重调节与启停（调整后按比例归一化计分）";
  const indicators = await api("/api/performance/indicators");
  $("#page-body").innerHTML = `
    <div class="panel"><p class="msg" id="pi-msg"></p>${
      table(["指标", "键", "权重", "状态", "操作"], indicators, (i) =>
        `<tr><td>${esc(i.name)}</td><td>${esc(i.key)}</td><td>${i.weight}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-weight="${esc(i.key)}">调权重</button>
             <button class="btn" data-toggle-ind="${esc(i.key)}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button></td></tr>`)}</div>`;
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.weight) {
        const w = prompt("新权重（≥0，自动按比例归一化）");
        if (w === null || w === "") return;
        await api(`/api/performance/indicators/${d.weight}`, { method: "PATCH", body: JSON.stringify({ weight: Number(w) }) });
        route();
      }
      if (d.toggleInd) {
        await api(`/api/performance/indicators/${d.toggleInd}`, { method: "PATCH",
          body: JSON.stringify({ active: d.active !== "true" }) });
        route();
      }
    } catch (err) { setMsg("#pi-msg", err.message, false); }
  };
}

