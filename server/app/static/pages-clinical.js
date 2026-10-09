/* 管理端 · 页面（一）：驾驶舱、共享诊断、会诊转诊、预约、处方药事等。 */

// 及时性后端是三态：目录外病种或发病日期非法时给 null，不能当成"及时"；null 再按有没有法定时限分两种说法（P2-686）
const CASE_TIMELY = {
  late: ["迟报", "red"], ontime: ["及时", "green"], unknown: ["无法定时限", ""], undetermined: ["无法判定", ""],
};

async function renderInfectious() {
  $("#page-desc").textContent =
    "病例报告 + 滑动窗口多点触发预警（多机构同报升级为高风险）；法定报告卡与批量导出";
  const [cases, alerts] = await Promise.all([api("/api/infectious/cases"), api("/api/infectious/alerts")]);
  // 报告卡与导出后端都是 require_roles("director")（admin 全通）——不是管理层就别摆
  const canReport = ["director", "admin"].includes(currentRole());
  // ADR-0009 第四批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = `
    ${panel("病例报告", `
      <form class="inline" id="case-form">
        <input name="org_id" type="number" placeholder="报告机构ID" required>
        <input name="disease_code" placeholder="病种编码" required>
        <input name="disease_name" placeholder="病种名称" required>
        <input name="onset_date" placeholder="发病日期 YYYY-MM-DD" required pattern="\\d{4}-\\d{2}-\\d{2}">
        <button>报告</button>
      </form><p class="msg" id="case-msg"></p>`)}
    ${alerts.length ? panel("⚠ 当前预警",
      table(["病种", "7日病例数", "报告机构数", "风险等级"], alerts, (a) =>
        `<tr><td>${esc(a.disease_name)}</td><td>${a.case_count}</td><td>${a.org_count}</td>
         <td><span class="tag ${a.severity === "high" ? "red" : "orange"}">${a.severity === "high" ? "高" : "中"}</span></td></tr>`)) : ""}
    ${panel("病例列表", table(["ID", "机构", "病种", "发病日期", "操作"], cases, (c) =>
      `<tr><td>${c.id}</td><td>${c.org_id}</td><td>${esc(c.disease_name)}</td><td>${esc(c.onset_date)}</td>
       <td>${canReport ? `<button class="btn" data-card="${c.id}">报告卡</button>` : "—"}</td></tr>`))}
    <div class="panel hidden" id="card-panel"><h3>法定传染病报告卡</h3><div id="card-body"></div></div>
    ${canReport ? panel("法定报告卡批量导出（CSV）", `
      <form class="inline" id="case-export">
        <input name="disease_code" placeholder="病种编码（留空导全部）">
        <label style="font-size:13px"><input type="checkbox" name="late_only" value="1"> 只导迟报清单</label>
        <button>导出</button>
      </form>
      <p class="desc"><b>平台不直连国家传染病网络直报系统</b>：本导出供手工网报
        （录入大疫情网）或县疾控前置机对接使用，及时性列与「未及时上报清单」同一口径。
        按筛选条件全量导出，不设条数上限。</p>
      <p class="msg" id="exp-msg"></p>`) : ""}`;
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
  const expForm = $("#case-export");
  if (expForm) expForm.onsubmit = (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const code = (f.get("disease_code") || "").trim();
    const qs = [code ? `disease_code=${encodeURIComponent(code)}` : "", f.get("late_only") ? "late_only=true" : ""]
      .filter(Boolean).join("&");
    downloadCsv(`/api/infectious/cases/export.csv${qs ? `?${qs}` : ""}`,
      `infectious_cases${f.get("late_only") ? "_late" : ""}.csv`, "#exp-msg");
  };
  $("#page-body").onclick = async (e) => {
    const { card } = e.target.dataset;
    if (!card) return;
    try {
      const c = await api(`/api/infectious/cases/${card}/report-card`);
      // late 是 bool | null：null 表示目录外病种或发病日期非法，**不是"及时"**。两种要分开说（P2-686）：目录外病种没有
      // 法定时限；发病日期非法时时限照给（目录里有）、只是算不出迟没迟——原先一律写「无法定时限」，卡上同时印着
      // 「法定时限 24 小时」，自相矛盾
      const timely = c.late === null ? (c.report_hours === null ? "unknown" : "undetermined") : c.late ? "late" : "ontime";
      $("#card-panel").classList.remove("hidden");
      $("#card-body").innerHTML = `<div class="cards">
        <div class="card"><span class="k">病例ID</span><b>${c.case_id}</b></div>
        <div class="card"><span class="k">报告机构</span><b>${esc(c.org_name) || c.org_id}</b></div>
        <div class="card"><span class="k">病种</span><b>${esc(c.disease_name)}（${esc(c.disease_code)}）</b></div>
        <div class="card"><span class="k">分类</span><b>${esc(c.category_name)}</b></div>
        <div class="card"><span class="k">发病日期</span><b>${esc(c.onset_date)}</b></div>
        <div class="card"><span class="k">报告时间</span><b>${esc(c.reported_at.slice(0, 16).replace("T", " "))}</b></div>
        <div class="card"><span class="k">法定时限</span><b>${
          c.report_hours === null ? "—" : `${c.report_hours} 小时`}</b></div>
        <div class="card"><span class="k">及时性</span><b>${statusTag(CASE_TIMELY, timely)}${
          // days_late 是「发病到报告隔了几天」（与迟报清单同口径），不是超出时限几天（P2-470）：原先写「迟 N 天」，
          // 限 24 小时的肺结核昨天发病、今天报告，卡上是「及时（迟 1 天）」
          c.days_late ? `（发病后 ${c.days_late} 天报告）` : ""}</b></div></div>
      <p class="desc">这是<b>平台留存的法定字段集</b>——病例登记本身不含患者个体标识
        （只记报告机构 / 病种 / 发病日期），卡片按此字段集导出，不虚构未存储的字段。</p>`;
    } catch (err) { setMsg("#exp-msg", err.message, false); }
  };
}

async function renderArchive() {
  $("#page-desc").textContent = "门诊接诊登记；按电子健康卡号汇聚档案、就诊、报告、慢病、处方";
  const encounters = await api("/api/encounters?limit=50");
  $("#page-body").innerHTML = `
    ${panel("门诊接诊登记", `
      <form class="inline" id="enc-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="doctor_name" placeholder="接诊医师">
        <input name="diagnosis_code" placeholder="诊断编码"><input name="diagnosis_name" placeholder="诊断名称">
        <input name="summary" placeholder="诊疗摘要" style="min-width:220px"><button>登记</button></form>
      <p class="msg" id="enc-msg"></p>
      <div id="enc-reminders"></div>
      <p class="desc">就诊记录是县域就诊率、诊次成本与绩效变量的共同数据源。</p>
      ${table(["ID", "患者", "机构", "类型", "诊断", "医师"], encounters, (e) =>
        `<tr><td>${e.id}</td><td>${e.patient_id}</td><td>${e.org_id}</td>
         <td>${e.encounter_type === "inpatient" ? "住院" : "门诊"}</td>
         <td>${esc(e.diagnosis_name || "—")}</td><td>${esc(e.doctor_name || "—")}</td></tr>`)}`)}
    ${panel("患者 360 视图", `
      <form class="inline" id="archive-form">
        <input name="ehc_no" placeholder="电子健康卡号" required>
        <button>查询</button>
      </form>
      <div id="archive-result"></div>`)}`;
  // 登记成功就地拉出这位患者的诊间公卫提醒（P2-1434）：提醒接口写明「接诊时汇聚该患者的公卫待办与风险提示」，原先只有
  // 「公卫协同」页手输患者 ID 才查得到——接诊时看不到随访超期、疫苗禁忌、处置中的公卫事件。先登记、后取：本机构刚登记了
  // 这位患者的就诊即有调阅依据，提醒接口照旧校验并留痕。先重画再写回执（P2-1013）；提醒取不到写原因，「登记成功」照写
  $("#enc-form").onsubmit = async (e) => {
    e.preventDefault();
    let enc;
    try {
      enc = await api("/api/encounters", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["patient_id", "org_id"])) });
    } catch (err) { setMsg("#enc-msg", err.message, false); return; }
    await route();
    setMsg("#enc-msg", `登记成功（就诊ID ${enc.id}）`);
    let r;
    try {
      r = await api(`/api/publichealth/reminders/${enc.patient_id}`);
    } catch (err) {
      $("#enc-reminders").innerHTML = `<p class="msg err">公卫提醒取不到：${esc(err.message)}</p>`;
      return;
    }
    $("#enc-reminders").innerHTML = r.reminders.length
      ? `<p class="desc">患者 ${esc(enc.patient_id)} 的诊间公卫提醒：</p>`
        + `<ul style="margin:4px 0 0 18px;font-size:13px">${r.reminders.map((x) => `<li>${esc(x.detail)}</li>`).join("")}</ul>`
      : '<p class="msg ok">暂无公卫提醒</p>';
  };
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
  $("#page-desc").textContent = "账号开通、停用与解困（重置口令 / 动态口令）、角色分配与变更留痕、系统参数配置（仅管理员）";
  const [usersList, orgs, roleChanges, params, roleMap] = await Promise.all([
    api("/api/users"), api("/api/organizations"), api("/api/users/role-changes"), api("/api/mgmt/params"),
    api("/api/users/roles")]);
  // 角色字典以后端为准（自定义角色只在后端有名字），六个内置角色仍有本地兜底
  const roles = { ...ROLE_NAMES, ...(roleMap || {}) };
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  $("#page-body").innerHTML = `
    ${panel("开通账号", `
      <form class="inline" id="user-form">
        <input name="username" placeholder="用户名（≥3位）" required minlength="3">
        <input name="password" type="password" placeholder="初始密码（≥6位）" required minlength="6">
        <input name="full_name" placeholder="姓名">
        <select name="role" required><option value="">选择角色</option>${Object.entries(roles).map(([v, t]) => `<option value="${esc(v)}">${esc(t)}</option>`).join("")}</select>
        <select name="org_id"><option value="">不挂机构</option>${orgs.map((o) => `<option value="${o.id}">${esc(o.name)}</option>`).join("")}</select>
        <button>开通</button>
      </form><p class="msg" id="user-msg"></p>`)}
    ${panel("修改本人密码", `
      <form class="inline" id="pwd-form">
        <input name="current_password" type="password" placeholder="当前密码" required>
        <input name="new_password" type="password" placeholder="新密码（≥6位）" required minlength="6">
        <button>修改</button>
      </form><p class="msg" id="pwd-msg"></p>`)}
    ${panel("", table(["ID", "用户名", "姓名", "角色", "所属机构", "状态", "操作"], usersList, (u) =>
      `<tr><td>${u.id}</td><td>${esc(u.username)}</td><td>${esc(u.full_name) || "—"}</td>
       <td><span class="tag">${esc(roles[u.role] || u.role)}</span></td>
       <td>${u.org_id ? esc(orgNames[u.org_id] || u.org_id) : "—"}</td>
       <td>${u.status === "disabled" ? '<span class="tag red">已停用</span>' : '<span class="tag green">在用</span>'}</td>
       <td><button class="btn secondary" data-chrole="${u.id}" data-role="${esc(u.role)}">调角色</button>
           <button class="btn secondary" data-ustatus="${u.id}" data-to="${u.status === "disabled" ? "active" : "disabled"}">${u.status === "disabled" ? "启用" : "停用"}</button>
           <button class="btn secondary" data-resetpw="${u.id}" data-name="${esc(u.username)}">重置口令</button>
           <button class="btn secondary" data-resettotp="${u.id}" data-name="${esc(u.username)}">重置动态口令</button></td></tr>`))}
    ${panel("角色变更记录（留痕，变更即吊销旧令牌）",
      table(["用户ID", "原角色", "新角色", "操作人", "时间"], roleChanges, (r) =>
        `<tr><td>${r.user_id}</td><td><span class="tag">${esc(roles[r.old_role] || r.old_role)}</span></td>
         <td><span class="tag green">${esc(roles[r.new_role] || r.new_role)}</span></td>
         <td>${r.changed_by}</td><td>${esc(r.at.slice(0, 16).replace("T", " "))}</td></tr>`))}
    ${panel("系统参数配置（键值集中管理）", `
      <form class="inline" id="param-form">
        <input name="key" placeholder="参数键（如 wechat_template_exam_report：检查报告的公众号模板 id）" required style="min-width:240px">
        <input name="value" placeholder="参数值" required>
        <input name="description" placeholder="说明" style="min-width:180px">
        <button>保存</button></form>
      <p class="msg" id="param-msg"></p>
      ${table(["键", "值", "说明", "更新时间"], params, (p) =>
        `<tr><td><span class="tag">${esc(p.key)}</span></td><td>${esc(p.value)}</td><td>${esc(p.description) || "—"}</td>
         <td>${esc(p.updated_at.slice(0, 16).replace("T", " "))}</td></tr>`)}`)}`;
  // 角色不给默认（P1-174）：原先下拉默认第一项「平台管理员」，不碰它直接开通，建出来的就是不挂机构的管理员（后端缺省是经办）
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
    const el = (attr) => e.target.closest(`[${attr}]`);
    const chrole = el("data-chrole"), ustatus = el("data-ustatus"), resetPw = el("data-resetpw"), resetTotp = el("data-resettotp");
    if (chrole) {
      // 原先是系统输入框输序号（P2-38 存量弹窗录入），改成下拉；角色字典同表格一致。
      // 预选现有角色（P1-174）：原先不给 value，下拉落在第一项「平台管理员」——点开看一眼再点确定，经办就成了管理员。
      // 字典里没有的角色（自定义角色）也摆进去，否则照样落回第一项
      const current = chrole.dataset.role;
      const options = Object.entries(roles).map(([k, v]) => ({ value: k, label: v }));
      if (!Object.keys(roles).includes(current)) options.unshift({ value: current, label: current });
      const form = await spdModal("调整角色（变更即吊销旧令牌）", [
        { name: "role", label: "新角色", type: "select", value: current, options },
      ]);
      if (!form || !form.role) return;
      try {
        await api(`/api/users/${chrole.dataset.chrole}/role`, { method: "PATCH", body: JSON.stringify({ role: form.role }) });
        route();
      } catch (err) { setMsg("#user-msg", err.message, false); }
      return;
    }
    if (ustatus) {
      const to = ustatus.dataset.to;
      if (to === "disabled" && !confirm("停用后该账号的既有登录立即失效，重新启用也须重新登录。确认停用？")) return;
      return postAction(`/api/users/${ustatus.dataset.ustatus}/status`, { status: to }, "#user-msg", "PATCH");
    }
    if (resetPw) {
      const form = await spdModal(`重置口令 · ${resetPw.dataset.name}`, [
        { name: "new_password", label: "临时口令（≥6 位；只能用一次，本人首次登录须改密，既有登录同时吊销）",
          type: "password", required: true },
      ]);
      if (!form || !form.new_password) return;
      try {
        await api(`/api/users/${resetPw.dataset.resetpw}/reset-password`,
          { method: "POST", body: JSON.stringify({ new_password: form.new_password }) });
        setMsg("#user-msg", `已重置 ${resetPw.dataset.name} 的口令：既有登录已吊销，本人首次登录须改密`);
      } catch (err) { setMsg("#user-msg", err.message, false); }
      return;
    }
    if (resetTotp) {
      if (!confirm(`确认重置 ${resetTotp.dataset.name} 的动态口令？其下次登录按未开通处理（换手机 / 令牌丢失时用）。`)) return;
      try {
        await api(`/api/users/${resetTotp.dataset.resettotp}/totp/reset`, { method: "POST" });
        setMsg("#user-msg", `已重置 ${resetTotp.dataset.name} 的动态口令`);
      } catch (err) { setMsg("#user-msg", err.message, false); }
    }
  };
}

const LOGIN_CHANNEL_NAMES = { password: "员工口令", sms: "居民端短信", wechat: "居民端微信" };

async function renderAudit() {
  $("#page-desc").textContent = "全部写操作留痕（等保三级安全审计）、登录留痕、哈希链校验与归档导出，仅管理员可查";
  const draw = async (username = "") => {
    const logs = await api(`/api/audit?limit=200${username ? `&username=${encodeURIComponent(username)}` : ""}`);
    $("#audit-table").innerHTML = table(["时间", "用户", "操作", "接口", "结果"], logs, (l) =>
      `<tr><td>${esc(l.at.replace("T", " ").slice(0, 19))}</td><td>${esc(l.username)}</td>
       <td><span class="tag">${esc(l.method)}</span></td><td>${esc(l.path)}</td>
       <td><span class="tag ${l.status_code < 400 ? "green" : "red"}">${l.status_code}</span></td></tr>`);
  };
  const drawLogins = async (params = {}) => {
    const q = Object.entries(params).filter(([, v]) => v !== "" && v != null)
      .map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
    const rows = await api(`/api/audit/logins?limit=100${q ? "&" + q : ""}`);
    $("#login-table").innerHTML = table(["时间", "登录名", "用户ID", "IP", "通道", "结果", "失败原因"], rows, (l) =>
      `<tr><td>${esc((l.created_at || "").replace("T", " ").slice(0, 19))}</td><td>${esc(l.username)}</td>
       <td>${l.user_id ?? "—"}</td><td>${esc(l.ip)}</td><td>${esc(LOGIN_CHANNEL_NAMES[l.channel] || l.channel)}</td>
       <td><span class="tag ${l.success ? "green" : "red"}">${l.success ? "成功" : "失败"}</span></td>
       <td>${esc(l.fail_reason_name || "—")}</td></tr>`);   // 文案取自后端（P2-427）：原先原样显示 bad_credentials / code_401
  };
  $("#page-body").innerHTML = `
    ${panel("", `
      <form class="inline" id="audit-search"><input name="username" placeholder="按用户名过滤"><button>查询</button>
        <button type="button" class="secondary" id="audit-verify">校验哈希链</button>
        <button type="button" class="secondary" id="audit-export">导出归档（NDJSON）</button></form>
      <p class="msg" id="audit-msg"></p>
      <div id="audit-verify-result"></div>
      <div id="audit-table"></div>`)}
    ${panel("登录留痕（成功与失败都记，等保 E1）", `
      <form class="inline" id="login-search">
        <input name="username" placeholder="登录名">
        <select name="success"><option value="">全部结果</option><option value="true">成功</option><option value="false">失败</option></select>
        <select name="channel"><option value="">全部通道</option>${Object.entries(LOGIN_CHANNEL_NAMES).map(([k, v]) =>
          `<option value="${k}">${esc(v)}</option>`).join("")}</select>
        <button>查询</button>
      </form>
      <div id="login-table"></div>`)}`;
  // 查询失败要说出来（P2-378）：原先 draw 抛错没人接，列表还是上一次的结果
  $("#audit-search").onsubmit = async (e) => {
    e.preventDefault();
    try { await draw(new FormData(e.target).get("username")); } catch (err) { setMsg("#audit-msg", err.message, false); }
  };
  $("#login-search").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawLogins(formJson(e.target)); } catch (err) { setMsg("#audit-msg", err.message, false); }
  };
  $("#audit-verify").onclick = async () => {
    try {
      const v = await api("/api/audit/verify?limit=5000");
      // 能力边界后端写在 caliber 里，原样给人看：哈希链能发现改动，拦不住重算整条链的人
      $("#audit-verify-result").innerHTML = `<p class="msg ${v.valid ? "ok" : "err"}">${v.valid ? "链完好"
          : `链在 #${v.broken_at} 断开：${esc(v.reason || "")}`}；校验 ${v.checked} 条${v.legacy_unchained
          ? `，未入链历史 ${v.legacy_unchained} 条` : ""}${v.partial_segment ? "（起点不是链首，只证明这一段自洽）" : ""}${v.note
          ? `；${esc(v.note)}` : ""}</p>${v.caliber ? `<p class="desc">${esc(v.caliber)}</p>` : ""}`;
    } catch (err) { setMsg("#audit-msg", err.message, false); }
  };
  $("#audit-export").onclick = () =>
    downloadCsv("/api/audit/export", `audit-${localToday()}.ndjson`, "#audit-msg");
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
  await drawLogins();
}

async function renderAccessLogs() {
  // 敏感读留痕查询（第十轮）：写审计回答"谁改了什么"，这里回答"谁凭什么看了谁"。
  $("#page-desc").textContent = "档案调阅留痕：谁、什么时候、凭什么依据、看了谁（院长/管理员可查）";
  const drawStats = async (patientId) => {
    // 这条只认 patient_id：起止日期与调阅人两个筛选它**不吃**（后端没有这两个形参），
    // 所以标题与说明都按"全量/该患者全量"写，不能顺着上面的筛选条件说成"本次筛选的构成"。
    const q = patientId ? `?patient_id=${encodeURIComponent(patientId)}` : "";
    const st = await api(`/api/access-logs/stats${q}`);
    if (!patientId) fillBases(st.by_basis);
    $("#al-stats").innerHTML = `
      <p class="desc">口径：按<b>依据</b>汇总的调阅构成，${patientId
        ? `只统计患者 ${esc(patientId)} 的记录——<b>聚焦到某个人本身也会留痕</b>（后端会把这次查询记进调阅日志）`
        : "统计的是<b>全量</b>调阅记录"}。
        起止日期与调阅人两个筛选对这一段<b>不生效</b>：这条接口只收 patient_id。
        跨机构调阅（转诊/授权）占比异常，是这张表最该看的东西。</p>
      <div class="cards"><div class="card"><div class="label">调阅总次数</div>
        <div class="value">${st.total}</div></div>
        ${st.by_basis.slice(0, 4).map((b) =>
          `<div class="card"><div class="label">${esc(b.basis_name)}</div>
           <div class="value">${b.count}</div></div>`).join("")}</div>
      ${st.by_basis.length
        ? barChart(st.by_basis.map((b) => [b.basis_name, b.count]))
        : '<p class="empty">暂无调阅记录</p>'}`;
  };
  // 依据下拉（P2-1023，照 P2-826）：接口按依据编码等值比，表格显示的是中文名——原先是自由文本框、占位只提示了 11 个码里的两个，
  // 照表格填「本机构就诊」「患者授权」查回空表，看着像没有这类调阅。选项取自全量调阅构成里出现过的依据，编码作值、后端给的
  // 中文名（basis_name）作显示；没出现过的依据选了也是空表，不列。按患者聚焦的构成只是一个人的，不拿它收窄下拉
  const fillBases = (bases) => {
    const select = $('#al-search select[name="basis"]');
    const picked = select.value;
    select.innerHTML = '<option value="">全部依据</option>'
      + bases.map((b) => `<option value="${esc(b.basis)}">${esc(b.basis_name)}</option>`).join("");
    select.value = picked;
  };
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
    await drawStats(params.patient_id);
  };
  $("#page-body").innerHTML = `
    ${panel("", `
      <form class="inline" id="al-search">
        <input name="patient_id" placeholder="患者ID">
        <input name="username" placeholder="调阅人账号">
        <input name="org_id" type="number" placeholder="调阅人所属机构ID">
        <select name="basis"><option value="">全部依据</option></select>
        <input name="start" placeholder="起 YYYY-MM-DD"><input name="end" placeholder="止 YYYY-MM-DD">
        <button>查询</button></form>
      <p class="desc">按患者查询会一并留痕——查"谁看过某人"本身也是在看这个人的隐私。</p>
      <div id="al-table"></div>`)}
    ${panel("调阅构成", `<div id="al-stats"></div>`)}`;
  $("#al-search").onsubmit = async (e) => {
    e.preventDefault();
    // 查询失败要说出来（P2-378）：原先 draw 抛错没人接，列表还是上一次的结果
    try { await draw(formJson(e.target)); }
    catch (err) { $("#al-table").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await draw();
}

// active 是 bool，没有后端文案可取；映成状态码再走 statusTag，与本文件其余状态列同写法
const TEXT_STATUS = { on: ["生效", "green"], off: ["已停用", "red"] };

async function renderConsents() {
  // 个保法落地（阶段十四 E2）：知情同意台账 + 更正/注销申请审核。
  $("#page-desc").textContent = "知情同意登记与查询；居民更正/注销申请的受理与审核（审核限管理层）";
  // 按接口的角色守卫给（P2-786，P1-175 / P1-220 同形）：待审核的更正 / 注销申请只给管理层（后端 require_roles("director")），
  // 原先渲染末尾不分角色地取——经办一进页整页只剩「需要以下角色之一：管理层」，台账查询、撤回、文本版本库一样也用不了；
  // 「撤回」只收经办 / 医师 / 公卫（管理员全给），管理层点了必 403，原先照样摆着
  const role = currentRole();
  const canReview = role === "admin" || role === "director";
  const canRevoke = role === "admin" || ["operator", "doctor", "public_health"].includes(role);
  const drawConsents = async (patientId) => {
    if (!patientId) { $("#ct-table").innerHTML = '<p class="desc">输入患者ID查询其同意记录（查询会落调阅留痕）。</p>'; return; }
    const rows = await api(`/api/consents?patient_id=${encodeURIComponent(patientId)}`);
    $("#ct-table").innerHTML = table(
      ["时间", "场景", "文本版本", "方式", "凭证", "状态", "操作"], rows, (r) =>
      `<tr><td>${esc((r.created_at || "").replace("T", " ").slice(0, 19))}</td>
       <td>${esc(r.scene_name)}</td><td>${esc(r.text_version)}</td><td>${esc(r.method_name)}</td>
       <td>${esc(r.evidence || "—")}</td>
       <td>${r.revoked_at
         ? `<span class="tag">已撤回</span> <span class="desc">${
             esc(r.revoked_at.replace("T", " ").slice(0, 19))}</span>`
         : '<span class="tag ok">有效</span>'}</td>
       <td><button class="btn secondary" data-print-consent="${r.id}">打印</button>
           ${r.revoked_at || !canRevoke ? "" : `<button class="btn danger" data-revoke-consent="${r.id}">撤回</button>`}</td></tr>`);
  };
  const drawTexts = async (scene, includeInactive) => {
    const qs = [scene ? `scene=${encodeURIComponent(scene)}` : "",
                includeInactive ? "active_only=false" : ""].filter(Boolean).join("&");
    const rows = await api(`/api/consents/texts${qs ? `?${qs}` : ""}`);
    $("#tx-table").innerHTML = table(["ID", "场景", "版本", "状态", "正文"], rows, (t) =>
      `<tr><td>${t.id}</td><td>${esc(t.scene_name)}</td><td><span class="tag">${esc(t.version)}</span></td>
       <td>${statusTag(TEXT_STATUS, t.active ? "on" : "off")}</td>
       <td style="white-space:pre-wrap">${esc(t.content) || "—"}</td></tr>`);
  };
  // 场景下拉（P2-826）：接口按场景编码等值比，表格显示的是中文名——原先是自由文本框，照表格填「建档」「随访」查回空表，
  // 只有填 archive 这种编码才查得到，看着像这个场景还没有文本。选项取自版本库里有的场景（含已停用的版本），编码作值、
  // 中文名（后端给的 scene_name）作显示；没有文本的场景选了也是空表，不列
  const fillTextScenes = async () => {
    const scenes = new Map((await api("/api/consents/texts?active_only=false")).map((t) => [t.scene, t.scene_name]));
    $('#tx-filter select[name="scene"]').innerHTML = '<option value="">全部场景</option>'
      + [...scenes].map(([code, name]) => `<option value="${esc(code)}">${esc(name)}</option>`).join("");
  };
  let correctionRows = {};
  const drawCorrections = async () => {
    if (!canReview) {
      $("#cr-table").innerHTML = '<p class="desc">待审核的更正 / 注销申请由管理层审核，清单只给管理层看。</p>';
      return;
    }
    const rows = await api("/api/consents/corrections?status=pending");
    correctionRows = Object.fromEntries(rows.map((r) => [String(r.id), r]));
    $("#cr-table").innerHTML = table(
      ["ID", "患者", "类型", "内容", "理由", "操作"], rows, (r) =>
      `<tr><td>${esc(String(r.id))}</td><td>${esc(String(r.patient_id))}</td>
       <td>${esc(r.request_type_name)}</td><td>${esc(r.changes || "—")}</td><td>${esc(r.reason || "")}</td>
       <td><button data-review="${esc(String(r.id))}" data-verdict="approved">通过</button>
           <button data-review="${esc(String(r.id))}" data-verdict="rejected" class="danger">拒绝</button></td></tr>`);
  };
  $("#page-body").innerHTML = `
    ${panel("知情同意台账", `
      <form class="inline" id="ct-search"><input name="patient_id" placeholder="患者ID"><button>查询</button></form>
      <div id="ct-table"></div>`)}
    ${panel("更正 / 注销申请（待审核）", `
      <p class="desc">通过即按白名单字段执行变更并落审计；拒绝必须填写意见。</p>
      <div id="cr-table"></div><p id="cr-msg"></p>`)}
    ${panel("同意文本版本库（窗口/居民端展示的告知文本）", `
      <form class="inline" id="tx-filter">
        <select name="scene"><option value="">全部场景</option></select>
        <label style="font-size:13px"><input type="checkbox" name="inactive" value="1"> 含已停用版本</label>
        <button>查询</button>
      </form>
      <p class="desc">默认只列<b>生效版本</b>。同意记录里的「文本版本」指向的就是这里的某一版——
        停用旧版不会改动已登记的同意（那条记录仍指着它签署当时的版本），所以历史举证不受影响。</p>
      <div id="tx-table"></div>`)}`;
  // 查询失败要说出来（P2-378）：原先 draw 抛错没人接，列表还是上一次的结果
  $("#ct-search").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawConsents(new FormData(e.target).get("patient_id")); }
    catch (err) { $("#ct-table").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#tx-filter").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    try { await drawTexts((f.get("scene") || "").trim(), !!f.get("inactive")); }
    catch (err) { $("#tx-table").innerHTML = `<p class="msg err">${esc(err.message)}</p>`; }
  };
  $("#ct-table").onclick = async (e) => {
    const { printConsent, revokeConsent } = e.target.dataset;
    if (revokeConsent) {
      // 撤回不删行（后端置 revoked_at，"撤回本身也要可举证"），但**没有反向端点**：
      // 再撤一次 409。所以在点之前说清楚，而不是点完才发现回不去
      if (!confirm(`撤回同意记录 ${revokeConsent}？记录会保留并标记撤回时刻，但无法再恢复为有效。`)) return;
      try {
        await api(`/api/consents/${revokeConsent}/revoke`, { method: "POST" });
        await drawConsents($("#ct-search").patient_id.value.trim());
        setMsg("#cr-msg", "已撤回", true);
      } catch (err) { setMsg("#cr-msg", err.message, false); }
      return;
    }
    if (!printConsent) return;
    try { await openPrintPage(`/api/print/consents/${printConsent}`); }
    catch (err) { setMsg("#cr-msg", err.message, false); }
  };
  $("#cr-table").onclick = async (e) => {
    const id = e.target.dataset.review; if (!id) return;
    const approve = e.target.dataset.verdict === "approved";
    // P2-38：原先"通过"弹审核意见框，点取消照样通过——**通过即改写患者主索引，注销申请则直接
    // 注销档案**。表单里取消就是不审；通过前把这一下会改什么写在表单上方。拒绝不填意见由后端报人话。
    const r = correctionRows[id] || {};
    // 框自己提交（P2-607）：拒绝没写意见、意见写超了（后端 256 字），报错写在框里、框不关，写好的意见不用重填
    const done = await spdModal(approve ? "通过申请" : "拒绝申请", [
      { name: "comment", label: approve ? "审核意见（可空）" : "拒绝意见（必填）", type: "textarea" },
    ], { intro: approve
      ? (r.request_type === "deactivate"
        ? "通过后该档案即注销：患者检索与居民端绑定入口不再出现（医疗记录照常保留）。"
        : `通过后按申请改写患者档案：${r.changes || "—"}`)
      : "",
    submit: (form) => api(`/api/consents/corrections/${id}/review`, { method: "POST",
      body: JSON.stringify({ approve, comment: form.comment }) }) });
    if (!done) return;
    try { await drawCorrections(); setMsg("#cr-msg", "已处理", true); }
    catch (err) { setMsg("#cr-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawConsents(); await drawCorrections(); await fillTextScenes(); await drawTexts("", false);
}

/* ---------- 通用小工具：表单序列化 + 动作分派 ---------- */
function formJson(form, numFields = []) {
  const f = new FormData(form), out = {};
  for (const [k, v] of f.entries()) {
    if (v === "") continue;
    out[k] = numFields.includes(k) ? Number(v) : v;
  }
  // 日期时间控件送的是 `T` 分隔（2026-10-01T08:00）；平台里的时间戳是空格写法（服务端默认值同此），
  // 换成空格再送，界面录的与服务端补的两种写法不混在同一列里（P1-100）
  form.querySelectorAll('input[type="datetime-local"]').forEach((el) => {
    if (out[el.name]) out[el.name] = out[el.name].replace("T", " ");
  });
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
  // multipart 上传绕过 api()（不能带 JSON Content-Type）：Cookie 模式写请求补双提交 CSRF
  const resp = await fetch("/api/attachments", {
    method: "POST", credentials: "same-origin",
    headers: token ? { Authorization: `Bearer ${token}` } : { "X-CSRF-Token": csrfToken() },
    body: fd });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(errorText(data.detail, `上传失败(${resp.status})`));
  return data;
}

/* 块1：报告打印——服务端渲染的打印页需带令牌拉取，取回后写入新窗口并唤起打印。
 * 开窗在点击手势里同步做、取回再写（P2-682，与 spdOpenSvg 同一口径）：原先 fetch 回来之后才 window.open——await 之后
 * 已不在用户手势里，Safari 一律拦、Chrome 在打印页生成慢（临时激活过期）时拦，只剩一句「浏览器拦截了新窗口」。
 * 取数失败就把先开的空窗关掉、照旧报后端的原话 */
async function openPrintPage(path) {
  const win = window.open("", "_blank");
  if (!win) throw new Error("浏览器拦截了新窗口，请允许弹出后重试");
  try {
    const resp = await fetch(path, {
      credentials: "same-origin",
      headers: token ? { Authorization: `Bearer ${token}` } : {},
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(errorText(data.detail, `打印页加载失败(${resp.status})`));
    }
    const html = await resp.text();
    win.document.open();
    win.document.write(html);
    win.document.close();
    win.focus();
    setTimeout(() => win.print(), 300);
  } catch (err) { win.close(); throw err; }
}

/* 响应头 Content-Disposition 里的文件名（RFC 6266：带编码的 `filename*=` 优先，其次 `filename=`）；没有给空串 */
function dispositionFilename(resp) {
  const cd = resp.headers.get("Content-Disposition") || "";
  const star = /filename\*=utf-8''([^;]+)/i.exec(cd);
  if (star) {
    try { return decodeURIComponent(star[1].trim()); } catch (err) { /* 编码坏了，退回下一种写法 */ }
  }
  const plain = /filename="?([^";]+)"?/i.exec(cd);
  return plain ? plain[1].trim() : "";
}

async function downloadAttachment(id, filename) {
  const resp = await fetch(`/api/attachments/${id}`, {
    credentials: "same-origin",
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  });
  if (!resp.ok) {
    // 失败时后端回的是 JSON detail（被病毒扫描隔离的 410、文件缺失的 404）：照 api() 的口径报人话，别只给状态码（P2-420）
    const data = await resp.json().catch(() => ({}));
    throw new Error(errorText(data.detail, `下载失败(${resp.status})`));
  }
  const url = URL.createObjectURL(await resp.blob());
  const a = document.createElement("a");
  a.href = url;
  // 调用方没给名字就用后端回的上传原名（P2-683）：原先落到 attachment-{id}，没有扩展名，照片 / PDF 下下来打不开
  a.download = filename || dispositionFilename(resp) || `attachment-${id}`;
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

/** 业务单据的佐证材料面板：按单号上传 / 查附件（P2-432；与检查报告、不良事件两处附件面板同形）。
    上传只给接口收的角色（OwnerSpec 的 roles），查看由后端按患者档口径判（校验 + 留痕）。 */
function attachmentPanelHtml(prefix, title, idLabel, canUpload) {
  return panel(title, `
    ${canUpload ? `<form class="inline" id="${prefix}-att-form">
      <input name="owner_id" type="number" placeholder="${esc(idLabel)}" required>
      <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
      <button>上传</button></form>` : ""}
    <form class="inline" id="${prefix}-att-query">
      <input name="owner_id" type="number" placeholder="${esc(idLabel)}" required>
      <button>查附件</button></form>
    <p class="msg" id="${prefix}-att-msg"></p><div id="${prefix}-att-list"></div>`);
}

function bindAttachmentPanel(ownerType, prefix) {
  const list = `#${prefix}-att-list`;
  const msg = `#${prefix}-att-msg`;
  const form = $(`#${prefix}-att-form`);
  if (form) form.onsubmit = async (e) => {
    e.preventDefault();
    const ownerId = new FormData(e.target).get("owner_id");
    try {
      await uploadAttachment(ownerType, ownerId, e.target.querySelector("input[type=file]"));
      setMsg(msg, "附件已上传");
      await drawAttachments(ownerType, ownerId, list, msg);
    } catch (err) { setMsg(msg, err.message, false); }
  };
  $(`#${prefix}-att-query`).onsubmit = async (e) => {
    e.preventDefault();
    try { await drawAttachments(ownerType, new FormData(e.target).get("owner_id"), list, msg); }
    catch (err) { setMsg(msg, err.message, false); }
  };
}

async function renderEmergency() {
  $("#page-desc").textContent = "呼救调度→转运（生命体征回传）→到院→收治，上车即入院；到院后判定抢救转归";
  // 待流转、待判转归的单独取一遍、排在最前（P2-1369，同 P2-408 / P2-456）：清单只回全县最新 200 起，调度量大的县一两周
  // 就过——被后来的挤出窗口的已调度 / 转运中事件没了「流转」，已到院 / 已收治而转归未判定的没了「判定转归」。
  // 待判转归按 `rescue_outcome=pending` 取（后端只算已到院 / 已收治的，未判定落库是空串）
  const [recent, dispatched, enRoute, unjudged] = await Promise.all([api("/api/emergency/cases"),
    api("/api/emergency/cases?status=dispatched"), api("/api/emergency/cases?status=en_route"),
    api("/api/emergency/cases?rescue_outcome=pending")]);
  const cases = actionableFirst(recent, dispatched, enRoute, unjudged);
  // 状态文案取自后端 status_name（P2-72，公卫侧绿道页同一份）；这里只管配色
  const ES_COLOR = { dispatched: "orange", en_route: "orange", admitted: "green" };
  // 空串是**未判定**，与 failed 是两回事：写成 failed 会把抢救成功率算低（后端注释的原话）
  const RESCUE = { success: ["抢救成功", "green"], failed: ["抢救无效", "red"], "": ["未判定", "orange"] };
  // 判定转归后端是 require_roles("doctor")（admin 全通）——这是临床结论，不是调度动作
  const canOutcome = ["doctor", "admin"].includes(currentRole());
  $("#page-body").innerHTML = `
    ${panel("呼救登记", `
      <form class="inline" id="em-form">
        <input name="location" placeholder="事发地点" required><input name="symptom" placeholder="主诉">
        <input name="ambulance_no" placeholder="车牌"><input name="dest_org_id" type="number" placeholder="目标医院ID">
        <input name="patient_id" type="number" placeholder="患者ID(可空)"><button>调度</button>
      </form><p class="msg" id="em-msg"></p>`)}
    ${panel("", table(["ID", "地点", "主诉", "车辆", "状态", "抢救转归", "操作"], cases, (c) => {
      const arrived = ["arrived", "admitted"].includes(c.status);
      // 拼成一个数组再 join，比三段三元套着读得清楚（admitted 且无判定权时才是真的没动作可做）
      const acts = [
        c.status !== "admitted" ? `<button class="btn secondary" data-adv="${c.id}">流转</button>
          <button class="btn secondary" data-vital="${c.id}">回传体征</button>` : "",
        // 途中体征收治之后照样看得见（P2-491）：接口注释写着「院内可实时调阅，实现院前院内无缝对接」
        `<button class="btn secondary" data-vitals="${c.id}">途中体征</button>`,
        canOutcome && arrived ? `<button class="btn" data-outcome="${c.id}">判定转归</button>` : "",
      ].filter(Boolean);
      return `<tr><td>${c.id}</td><td>${esc(c.location)}</td><td>${esc(c.symptom)}</td><td>${esc(c.ambulance_no)}</td>
        <td><span class="tag ${ES_COLOR[c.status] || ""}">${esc(c.status_name)}</span></td>
        <td>${arrived ? statusTag(RESCUE, c.rescue_outcome || "") : "—"}</td>
        <td>${acts.length ? acts.join(" ") : "—"}</td></tr>`;
    }) + `<p class="desc">抢救转归<b>只对已到院/已收治的病例开放</b>——车还在路上就写"抢救成功"，
      这个指标就没有可信度了（后端对未到院的直接 409）。<b>未判定与抢救无效是两回事</b>：
      留空表示还没下结论，误填「无效」会把抢救成功率算低。判定后可更正。</p><div id="em-vitals"></div>`)}`;
  $("#em-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/emergency/cases", formJson(e.target, ["dest_org_id", "patient_id"]), "#em-msg"); };
  const drawVitals = async (caseId) => {
    try {
      const rows = await api(`/api/emergency/cases/${caseId}/vitals`);
      $("#em-vitals").innerHTML = `<h3 style="margin-top:12px">事件 ${esc(caseId)} 的途中体征（${rows.length} 次）</h3>
        ${table(["回传时刻", "心率", "血压", "血氧 %", "备注"], rows, (v) =>
          `<tr><td>${esc((v.created_at || "").replace("T", " ").slice(0, 16))}</td><td>${esc(v.heart_rate ?? "—")}</td>
           <td>${esc(v.sbp ?? "—")}/${esc(v.dbp ?? "—")}</td><td>${esc(v.spo2 ?? "—")}</td><td>${esc(v.note) || "—"}</td></tr>`)}`;
    } catch (err) { setMsg("#em-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { adv, vital, vitals, outcome } = e.target.dataset;
    if (adv) return postAction(`/api/emergency/cases/${adv}/advance`, null, "#em-msg");
    if (vitals) return drawVitals(vitals);
    if (vital) {
      // 各项用文本框、自己解析（P2-249，与逐题作答的数值题同一个理由）：spdModal 的数字框把空值读成 0，原先再
      // `|| null`——心跳骤停记的 0 与「未测」混成同一个 null。后端写着「0 照收——抢救现场心跳骤停，记 0 是真实的」。
      // 血压、血氧（P2-491）：接口一直收，弹窗原先只有心率
      const FIELDS = { heart_rate: "心率", sbp: "收缩压", dbp: "舒张压", spo2: "血氧" };
      const picked = await spdModal("回传生命体征", [
        { name: "heart_rate", label: "心率（次/分，留空表示未测；心跳骤停填 0）" },
        { name: "sbp", label: "收缩压（mmHg，留空表示未测；测不出填 0）" },
        { name: "dbp", label: "舒张压（mmHg，留空表示未测）" },
        { name: "spo2", label: "血氧饱和度（%，留空表示未测）" },
        { name: "note", label: "备注", type: "text" },
      ]);
      if (!picked) return;
      const body = { note: picked.note };
      for (const [key, label] of Object.entries(FIELDS)) {
        body[key] = picked[key] === "" ? null : Number(picked[key]);
        if (Number.isNaN(body[key])) return setMsg("#em-msg", `${label}须填数字（未测留空）`, false);
      }
      try {
        await api(`/api/emergency/cases/${vital}/vitals`, { method: "POST", body: JSON.stringify(body) });
      } catch (err) { return setMsg("#em-msg", err.message, false); }
      await route();
      return drawVitals(vital);   // 刚回传的这一次就在眼前
    }
    if (outcome) {
      const picked = await spdModal("判定抢救转归", [
        { name: "rescue_outcome", label: "转归结论（后端只收这两种；还没下结论就直接取消，别填「无效」）",
          type: "select", options: [
            { value: "success", label: "抢救成功" }, { value: "failed", label: "抢救无效" }] },
      ]);
      if (!picked) return;
      return postAction(`/api/emergency/cases/${outcome}/rescue-outcome`,
        { rescue_outcome: picked.rescue_outcome }, "#em-msg");
    }
  };
}

async function renderTelemedicine() {
  $("#page-desc").textContent = "在线咨询、复诊续方（续方须关联已过审处方）";
  // 待回复、待结束的单独取一遍、排在最前（P2-408，同审方 P1-148）：清单只回最新 200 条，压着没回的咨询一被后来的
  // 挤出这个窗口，页面上就再没有一行给「回复 / 结束」
  const [recent, open, replied] = await Promise.all([api("/api/telemedicine/consults"),
    api("/api/telemedicine/consults?status=open"), api("/api/telemedicine/consults?status=replied")]);
  const actionableIds = new Set([...open, ...replied].map((c) => c.id));
  const consults = [...open, ...replied, ...recent.filter((c) => !actionableIds.has(c.id))];
  const TS = { open: ["待回复", "orange"], replied: ["已回复", "green"], closed: ["已结束", ""] };
  // 按钮只给接口收的角色（P2-429）：回复限医师（require_roles("doctor")），结束限经办 / 医师；管理员两样都放行。
  // 原先谁打开都摆着，点下去一次 403
  const role = currentRole();
  const canReply = ["doctor", "admin"].includes(role);
  const canClose = ["operator", "doctor", "admin"].includes(role);
  // 「关联处方」一列带出处方状态（P2-1476）：原先只印编号——回复之后才退药冲销的那张方再也发不出去，这里看不出来。
  // 审方状态取后端的中文名，已退药冲销另标（与医生 360 处方段同一对键）
  const rxCell = (c) => c.prescription_id == null ? "—"
    : `${c.prescription_id}（${esc(c.prescription_status_name)}）${c.prescription_dispense_reversed
      ? ' <span class="tag red">已退药冲销</span>' : ""}`;
  $("#page-body").innerHTML = `
    ${panel("发起咨询", `
      <form class="inline" id="tm-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="consult_type"><option value="consult">在线咨询</option><option value="repeat_rx">复诊续方</option></select>
        <input name="question" placeholder="咨询内容" required style="min-width:220px"><button>提交</button>
      </form><p class="msg" id="tm-msg"></p>`)}
    ${panel("", table(["ID", "患者", "类型", "内容", "回复", "关联处方", "状态", "操作"], consults, (c) => {
      return `<tr><td>${c.id}</td><td>${c.patient_id}</td><td>${c.consult_type === "repeat_rx" ? "续方" : "咨询"}</td>
        <td>${esc(c.question)}</td><td>${esc(c.reply) || "—"}</td><td>${rxCell(c)}</td>
        <td>${statusTag(TS, c.status)}</td>
        <td>${c.status === "open" ? (canReply ? `<button class="btn secondary" data-reply="${c.id}">回复</button>` : "待医师回复")
          : c.status === "replied" && canClose ? `<button class="btn secondary" data-close="${c.id}">结束</button>` : "—"}</td></tr>`;
    }))}`;
  $("#tm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/telemedicine/consults", formJson(e.target, ["patient_id", "org_id"]), "#tm-msg"); };
  // P2-38：回复三连问换成页内表单。医师姓名原先留空就记成"医师"——这条回复是谁答的，
  // 事后查不出来；现在必填。处方号写成认不出的样子原样交给后端报人话，不再悄悄变成"不关联"。
  $("#page-body").onclick = async (e) => {
    const { reply, close } = e.target.dataset;
    if (reply) {
      // 框自己提交（P2-607）：处方号不对、回复写超了（后端 2048 字）报错写在框里、框不关，写好的回复不用重填
      const done = await spdModal(`回复咨询 ${reply}`, [
        { name: "reply", label: "回复内容（必填）", type: "textarea" },
        { name: "doctor_name", label: "回复医师姓名", required: true },
        { name: "prescription_id", label: "关联处方ID（续方时填写，须是该患者已通过审方、未退药冲销的处方；可空）" },
      ], { submit: (v) => {
        const rx = v.prescription_id;
        return api(`/api/telemedicine/consults/${reply}/reply`, { method: "POST", body: JSON.stringify({
          reply: v.reply, doctor_name: v.doctor_name,
          prescription_id: rx === "" ? null : (Number.isNaN(Number(rx)) ? rx : Number(rx)) }) });
      } });
      if (done) route();
      return;
    }
    if (close) {
      // P2-43：原先点一下就结束；「已回复 → 已结束」页面上没有撤回入口
      if (!await spdModal("结束问诊", [], { intro: "结束后这次问诊标记为「已结束」，不能撤回。" })) return;
      return postAction(`/api/telemedicine/consults/${close}/close`, null, "#tm-msg");
    }
  };
}

async function renderTcm() {
  $("#page-desc").textContent = "智能辅诊（辨证推荐）、共享中药房追溯、适宜技术库";
  // 还没送达的代煎单单独取一遍、排在最前（P2-457，同 P2-456）：清单只回最新 200 张，挤出窗口的就没有一行能「流转」
  const [recent, techniques, spec, ...open] = await Promise.all([
    api("/api/tcm/dispense-orders"), api("/api/tcm/techniques"), api("/api/tcm/constitution/spec"),
    ...["ordered", "dispensed", "decocted", "delivering"].map((st) => api(`/api/tcm/dispense-orders?status=${st}`))]);
  const orders = actionableFirst(recent, ...open);
  const DS = { ordered: "已下单", dispensed: "已调配", decocted: "已煎煮", delivering: "配送中", delivered: "已送达" };
  // 代煎单的「煎法」列与按钮上的下一步（P2-1407）：原先清单不显示代煎 / 自煎、按钮一律写「流转」——同在「已调配」的两张单
  // 看着一模一样，点下去代煎单记成「已煎煮」（没煎也这么记）、自煎单直接「配送中」。下一步取后端的 next_status_name（终态为
  // null 即不摆按钮），页面不另抄一份流转表。下单表单原先把不代煎写成「免煎」——免煎通常指配方颗粒，后端没有颗粒这一说，
  // 发出去的是要患者自己煎的饮片
  // 平和质不收分：后端判定时 `k != "balanced"`——它是"八种偏颇都不够格"的结论，不是一个维度
  const BIASED = spec.constitutions.filter((c) => c.key !== "balanced");
  // 适宜技术表加「操作要点」列（P2-1408）：description 入库表单录得进、出参也带，原先表里只列名称、分类、适应症
  $("#page-body").innerHTML = `
    ${panel("智能辨证", `
      <form class="inline" id="tcm-diag"><input name="symptoms" placeholder="症状（逗号、顿号或空格分隔，如：乏力、气短）" required style="min-width:280px"><button>辨证</button></form>
      <div id="tcm-diag-result"></div>`)}
    ${panel("体质辨识（标准化简表）", `
      <p class="desc">${esc(spec.method)}。${esc(spec.item_scoring)}。<br>
        ${esc(spec.raw_score)}；${esc(spec.transformed_score)}。<br>
        判定：${esc(spec.judge.positive)}；${esc(spec.judge.tendency)}；${esc(spec.judge.balanced)}。</p>
      <form id="tcm-const">
        <div class="inline" style="flex-wrap:wrap">${BIASED.map((c) =>
          `<label style="font-size:13px;margin-right:10px">${esc(c.name)}
            <input name="${esc(c.key)}" type="number" min="0" max="100" placeholder="转化分"
                   style="width:78px"></label>`).join("")}</div>
        <div class="inline" style="margin-top:8px"><button>辨识</button></div></form>
      <p class="desc">留空的维度不参与判定——只填了两三项就下结论，结论本身就不可靠，
        但平台不替你拦：把哪几项当依据是辨识者的判断，界面只保证不替你编。</p>
      <p class="msg" id="tcm-const-msg"></p>
      <div id="tcm-const-result"></div>`)}
    ${panel("共享中药房下单", `
      <form class="inline" id="tcm-order">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="from_org_id" type="number" placeholder="机构ID" required>
        <input name="herbs" placeholder="处方饮片" required style="min-width:220px"><input name="doses" type="number" value="7" min="1" style="min-width:60px">
        <select name="decoct"><option value="true">代煎</option><option value="false">自煎（不代煎）</option></select><button>下单</button>
      </form><p class="msg" id="tcm-msg"></p>
      ${table(["ID", "患者", "饮片", "剂数", "煎法", "状态", "操作"], orders, (o) =>
        `<tr><td>${o.id}</td><td>${o.patient_id}</td><td>${esc(o.herbs)}</td><td>${o.doses}</td>
         <td>${o.decoct ? "代煎" : "自煎"}</td>
         <td><span class="tag ${o.status === "delivered" ? "green" : "orange"}">${esc(DS[o.status] || o.status)}</span></td>
         <td>${o.next_status_name
           ? `<button class="btn secondary" data-adv="${o.id}">标为${esc(o.next_status_name)}</button>` : "—"}</td></tr>`)}`)}
    ${panel("适宜技术库", `
      ${currentRole() === "admin" ? `<form class="inline" id="tcm-tech-form" style="margin-bottom:8px">
        <input name="name" placeholder="技术名称" required>
        <input name="category" placeholder="分类（如 针刺类）" style="width:130px">
        <input name="indication" placeholder="适应症" style="min-width:200px">
        <input name="description" placeholder="操作要点" style="min-width:200px">
        <button>入库</button>
      </form><p class="msg" id="tcm-tech-msg"></p>` : ""}
      ${table(["名称", "分类", "适应症", "操作要点"], techniques, (t) =>
        `<tr><td>${esc(t.name)}</td><td>${esc(t.category)}</td><td>${esc(t.indication)}</td>
         <td style="white-space:pre-wrap">${esc(t.description) || "—"}</td></tr>`)}`)}`;
  $("#tcm-diag").onsubmit = async (e) => {
    e.preventDefault();
    // 切词认顿号与空白（P2-1409）：原先只按 /[,，]/ 切，「乏力、气短、自汗」「乏力 气短 自汗」整串当成一个症状送出去，推荐为空表。
    // 分隔符照抄导诊台（core.js renderAppointments 的 #triage-form）：那是处理函数里的字面量，没有可复用的常量；shared.js 只收
    // 三套前端都在用的（它自己写明的判据），居民端、医生端都不切症状，故照抄、不往那里抽
    const symptoms = new FormData(e.target).get("symptoms").split(/[，,、\s]+/).map((s) => s.trim()).filter(Boolean);
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着
    $("#tcm-diag-result").innerHTML = "";
    let result;
    try {
      result = await api("/api/tcm/assist-diagnosis", { method: "POST", body: JSON.stringify({ symptoms }) });
    } catch (err) {
      $("#tcm-diag-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    // 原先整块 JSON 甩在页面上：匹配了哪几个症状、推荐什么方，得让人自己从括号里读
    $("#tcm-diag-result").innerHTML =
      table(["证型", "命中症状", "命中数", "推荐方剂", "适宜技术"], result.recommendations, (r) =>
        `<tr><td>${esc(r.syndrome)}</td><td>${esc(r.matched.join("、")) || "—"}</td>
         <td>${r.match_count}</td><td>${esc(r.formula) || "—"}</td>
         <td>${esc(r.techniques.join("、")) || "—"}</td></tr>`)
      + `<p class="desc">${esc(result.note)}</p>`;
  };
  $("#tcm-const").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const scores = {};
    BIASED.forEach((c) => { const v = f.get(c.key); if (v !== "" && v !== null) scores[c.key] = Number(v); });
    if (!Object.keys(scores).length) return setMsg("#tcm-const-msg", "至少填一个维度的转化分", false);
    try {
      const r = await api("/api/tcm/constitution", { method: "POST", body: JSON.stringify({ scores }) });
      // 判定一律取后端结果：是/倾向两条线由 CONSTITUTION_*_THRESHOLD 定，前端不再算一遍
      const isTendency = (name) => r.tendencies.includes(name);
      // 兼夹体质（P2-117）：判定体质之外同样 ≥ 40 的偏颇体质，原先在下表里只显示「—」
      const isPositive = (name) => name === r.constitution || r.also.includes(name);
      const nameOf = (key) => (BIASED.find((c) => c.key === key) || { name: key }).name;
      $("#tcm-const-result").innerHTML = `
        <div class="cards">
          <div class="card"><div class="label">判定体质</div><div class="value">${esc(r.constitution)}</div></div>
          <div class="card"><div class="label">兼夹体质</div>
            <div class="value">${esc(r.also.join("、")) || "无"}</div></div>
          <div class="card"><div class="label">最高转化分</div><div class="value">${r.score}</div></div>
          <div class="card"><div class="label">倾向体质</div>
            <div class="value">${esc(r.tendencies.join("、")) || "无"}</div></div></div>
        <p class="desc">调养建议：${esc(r.advice)}${r.formula ? `；参考方剂：${esc(r.formula)}` : ""}</p>
        ${table(["体质", "转化分", "判定"],
          Object.entries(r.transformed_scores).sort((a, b) => b[1] - a[1]), ([key, v]) =>
          `<tr><td>${esc(nameOf(key))}</td><td>${v}</td>
           <td>${isPositive(nameOf(key)) ? '<span class="tag red">是</span>'
             : isTendency(nameOf(key)) ? '<span class="tag orange">倾向是</span>' : "—"}</td></tr>`)}`;
      setMsg("#tcm-const-msg", "", true);
    } catch (err) { setMsg("#tcm-const-msg", err.message, false); }
  };
  $("#tcm-order").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "from_org_id", "doses"]);
    body.decoct = body.decoct === "true";
    postAction("/api/tcm/dispense-orders", body, "#tcm-msg");
  };
  // 适宜技术库原先只能看不能建（P2-93 动词级孤儿）：入库接口仅管理员，表单也只给管理员
  const techForm = $("#tcm-tech-form");
  if (techForm) techForm.onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/tcm/techniques", formJson(e.target), "#tcm-tech-msg");
  };
  $("#page-body").onclick = (e) => { if (e.target.dataset.adv) postAction(`/api/tcm/dispense-orders/${e.target.dataset.adv}/advance`, null, "#tcm-msg"); };
  await drawTcmPreparations();  // 块4⑭ 中药制剂管理
}

async function renderMedication() {
  $("#page-desc").textContent = "缺药登记流转、供应风险研判、全县用药地图、居民用药画像";
  // 还没结案的三种单独取一遍、排在最前（P2-1310，同 P2-456）：清单只回最新 200 条，「在途」卡片数的是已登记 + 采购中的全量，
  // 已配送的正等着结案（已取药 / 未取药只能在已配送之后判）——这三种一被后来结案的挤出窗口，页面上就没有一行能流转 / 结案
  const [recent, registered, purchasing, delivered, stats, risk, sstats] = await Promise.all([
    api("/api/medication/shortages"), api("/api/medication/shortages?status=registered"),
    api("/api/medication/shortages?status=purchasing"), api("/api/medication/shortages?status=delivered"),
    api("/api/medication/usage-stats"), api("/api/medication/supply-risk"), api("/api/medication/shortages/stats")]);
  const shortages = actionableFirst(recent, registered, purchasing, delivered);
  // 补齐三个**结案**状态：后端 `_SHORTAGE_CLOSED` 就是这三个，本批把结案接上之后
  // 它们会真的出现在列表里。原先只有流转中的三个，结案行会把英文键直接印给人看。
  const SS = {
    registered: ["已登记", "orange"], purchasing: ["采购中", "orange"], delivered: ["已配送", "green"],
    collected: ["已取药", "green"], no_show: ["未取药", "red"], cancelled: ["已取消", ""],
  };
  const CLOSED = ["collected", "no_show", "cancelled"];
  // ADR-0009 第三批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 供应风险面板的红色左边框走 `accent`；"有风险才渲染"这个条件仍留在调用点。
  $("#page-body").innerHTML = `
    ${risk.total ? panel(`⚠ 药品供应风险评估（${risk.total}）`,
      table(["药品编码", "药品", "库存告警机构数", "待到货的缺药登记", "风险等级"], risk.risks, (r) =>
        `<tr><td>${esc(r.drug_code)}</td><td>${esc(r.drug_name) || "—"}</td><td>${r.low_stock_orgs}</td><td>${r.open_shortages}</td>
         <td><span class="tag ${r.risk_level === "high" ? "red" : "orange"}">${r.risk_level === "high" ? "高" : "中"}</span></td></tr>`),
      { accent: "#c62828" }) : ""}`;
  $("#page-body").innerHTML += `
    ${panel("缺药登记", `
      <form class="inline" id="short-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID（按患者登记；按机构补货留空）" style="min-width:230px">
        <input name="drug_code" placeholder="药品编码" required>
        <input name="drug_name" placeholder="药品名称" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>登记</button>
      </form><p class="msg" id="short-msg"></p>
      ${table(["ID", "机构", "药品", "数量", "状态", "操作"], shortages, (s) => {
        // `SS` 只有流转中的三个状态，而后端还会写 collected / no_show / cancelled
        // （medication.py `shortage.status = body.result`）。没有兜底时
        // `SS[s.status]` 是 undefined，解构直接 TypeError——**整页白屏**，
        // 不是这一行降级。只要有一条缺药登记结了案，这一页就打不开了。
        // 「流转」只在**还能往下走**的状态出现。原判据是 `!== "delivered"`，
        // 那是把"终态"等同于"已配送"——而后端的终态还有 collected / no_show /
        // cancelled，这些行会显示一个点下去必定 409（"状态 collected 已是终态"）
        // 的按钮。此前看不出来是因为这一页在有结案登记时根本打不开。
        // 判据与后端 `_SHORTAGE_FLOW` 的键一一对应：能流转的只有这两个状态。
        // 流转 / 结案以登记机构的名义写（P2-793）：清单是全县的，别家的登记原先照样摆这两个按钮、点了必 403
        const canAdvance = s.can_handle && (s.status === "registered" || s.status === "purchasing");
        return `<tr><td>${s.id}</td><td>${s.org_id}</td><td>${esc(s.drug_name)}</td><td>${s.quantity}</td>
          <td>${statusTag(SS, s.status)}</td>
          <td>${[
            canAdvance ? `<button class="btn secondary" data-adv="${s.id}">流转</button>` : "",
            CLOSED.includes(s.status) || !s.can_handle ? "" : `<button class="btn" data-close="${s.id}">结案</button>`,
          ].filter(Boolean).join(" ") || "—"}</td></tr>`;
      })}
      <p class="desc">结案分三种：<b>已取药 / 未取药只能在「已配送」之后判定</b>——药还没到就说
        "未取药"是冤枉人（后端对未配送的直接 409）；<b>已取消</b>任何阶段都可以
        （患者转院、药源已解决）。结案不可撤销，后端没有反向端点。</p>`)}
    ${panel("缺药登记统计（履约率）", `
      <div class="cards">
        <div class="card"><div class="label">在途</div><div class="value">${sstats.in_transit}</div></div>
        <div class="card"><div class="label">已取药</div><div class="value">${sstats.collected}</div></div>
        <div class="card"><div class="label">未取药</div><div class="value${
          sstats.no_show ? " warn" : ""}">${sstats.no_show}</div></div>
        <div class="card"><div class="label">履约率</div><div class="value">${
          sstats.fulfillment_rate_pct === null ? "—" : `${sstats.fulfillment_rate_pct}%`}</div></div></div>
      ${table(["状态", "件数"], Object.entries(sstats.by_status), ([code, n]) =>
        `<tr><td>${statusTag(SS, code)}</td><td>${n}</td></tr>`)}
      <p class="desc">${esc(sstats.caliber)}</p>`)}
    ${panel("用药画像查询", `
      <form class="inline" id="prof-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="prof-result"></div>`)}
    ${panel("全县用药地图（品种排名）",
      stats.length ? barChart(stats.slice(0, 8).map((s) => [s.drug_name, s.rx_count]), { unit: " 方" }) : "暂无数据")}`;
  // 患者可选（P2-859）：只有按患者登记才谈得上「登记后不来取药」、才过得了黑名单拦截；原先表单没有这一项，登的全是按机构报缺
  $("#short-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/medication/shortages", formJson(e.target, ["org_id", "patient_id", "quantity"]), "#short-msg"); };
  $("#prof-form").onsubmit = async (e) => {
    e.preventDefault();
    // 先清掉上一位的结果、查不到就把原因写出来（P2-358）：原先 api() 抛错没人接，患者号输错（404）或无权查看（403）时
    // 页面一声不吭，上一位患者的画像与「多重用药风险」照旧挂着——那一行又不写是谁
    $("#prof-result").innerHTML = "";
    let profile;
    try {
      profile = await api(`/api/medication/profile/${new FormData(e.target).get("patient_id")}`);
    } catch (err) {
      $("#prof-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    // 预警按同时在用的品种数判（P2-144），把那个数写出来——只写「多重用药风险」，看的人得自己去 JSON 里数
    $("#prof-result").innerHTML = `${profile.polypharmacy_warning
      ? `<p class="msg err">⚠ 患者 ${esc(String(profile.patient_id))} 多重用药风险：同时在用 ${profile.in_use_drugs} 种</p>` : ""}<pre class="json">${esc(JSON.stringify(profile, null, 2))}</pre>`;
  };
  $("#page-body").onclick = async (e) => {
    const { adv, close } = e.target.dataset;
    if (adv) return postAction(`/api/medication/shortages/${adv}/advance`, null, "#short-msg");
    if (close) {
      const row = shortages.find((x) => x.id === Number(close));
      // 未配送的只给"已取消"：另外两个后端会 409，摆出来只会让人点一次看一句错
      const options = row && row.status === "delivered"
        ? [{ value: "collected", label: "已取药" }, { value: "no_show", label: "未取药" },
           { value: "cancelled", label: "已取消" }]
        : [{ value: "cancelled", label: "已取消（药未配送到位，只能取消）" }];
      const picked = await spdModal(`结案缺药登记 ${close}`, [
        { name: "result", label: "结案结论", type: "select", options },
        { name: "reason", label: "结案说明", type: "text" },
      ]);
      if (!picked) return;
      return postAction(`/api/medication/shortages/${close}/close`,
        { result: picked.result, reason: picked.reason }, "#short-msg");
    }
  };
}

async function renderInsurance() {
  $("#page-desc").textContent = "结算记录、转诊证明、特殊病种申报、双通道药品申报、基金监测";
  // 表单与按钮按接口的角色守卫给（P1-175）：原先一个 Promise.all 连管理层才看得到的基金监测一起取，经办、医师一进来
  // 整页只剩一句「需要以下角色之一：管理层」——结算录入、转诊证明、特病 / 双通道申报这些给他们用的表单一张也看不到；
  // 管理层进得来，点哪张表单都是 403
  const role = currentRole();
  const can = (...roles) => role === "admin" || roles.includes(role);
  const canSettle = can("operator"), canApply = can("operator", "doctor"), canReview = can("director");
  // 待审的单独取一遍、排在最前（P2-1478，同 P2-1310 / P2-1441 的 actionableFirst）：两张队列原先只取不带 status 的清单，
  // 接口按 id 倒序只回全部状态里最新的 200 条——已审的一多，压着没审的那条就被挤出窗口，管理层看不到也审不了；同病种 /
  // 同药品只许挂一条待审（部分唯一索引），申报人重报又是 409，这条申报就永远卡住。接口早就收 status：特病待审是 applied、
  // 双通道待审是 pending
  const [fund, settlements, recentApps, appliedApps, recentDual, pendingDual] = await Promise.all([
    canReview ? api("/api/insurance/fund-stats") : Promise.resolve(null), api("/api/insurance/settlements"),
    api("/api/insurance/special-diseases"), api("/api/insurance/special-diseases?status=applied"),
    api("/api/insurance/dual-channel"), api("/api/insurance/dual-channel?status=pending")]);
  const apps = actionableFirst(recentApps, appliedApps);
  const dualApps = actionableFirst(recentDual, pendingDual);
  // 列不全时标题写明（P2-1478，同 P2-1441）：两个清单一页最多 200 条、不给总数——最新一页取满了，更早的已审申报就没列出来；
  // 待审那一页也取满了，最早的待审同样列不出
  const queueNote = (recent, pending, rows) => recent.length < 200 ? ""
    : `（待审${pending.length >= 200 ? "只列最新 " : " "}${pending.length} 条排在最前、其余只列最新 ${rows.length - pending.length} 条）`;
  // 特病申报表单收「申报理由」、队列加理由列（P2-1481，照同页双通道的表单与队列）：接口早就收 reason、出参也带，页面原先
  // 表单只有患者号和病种、队列不显示理由——申报的人写不了，审核的人只看得见一个病种名。
  // 双通道的状态文案取后端的 status_name（P2-1482，同页特病同一做法），这里只管配色：原先三元写死三种文案，表外的值
  // 一律显示成「待审核」
  $("#page-body").innerHTML = `
    ${fund ? `<div class="cards">
      <div class="card"><div class="label">医保基金支出总额</div><div class="value">${fund.insurance_pay_total}</div></div>
      <div class="card"><div class="label">县域内结算占比</div><div class="value">${fund.local_ratio_pct}%</div></div>
      <div class="card"><div class="label">基层支出占比</div><div class="value">${fund.grassroots_ratio_pct}%</div></div></div>` : ""}
    ${panel("结算登记", `
      ${canSettle ? `<form class="inline" id="ins-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="settle_type"><option value="local">本地</option><option value="remote">异地</option></select>
        <input name="total_amount" type="number" step="any" placeholder="总额" required><input name="insurance_pay" type="number" step="any" placeholder="医保支付" required>
        <input name="self_pay" type="number" step="any" placeholder="自付" required><button>登记</button>
      </form>` : ""}
      <h3 style="margin-top:12px">转诊证明 / 特病申报</h3>
      ${canSettle ? `<form class="inline" id="cert-form"><input name="referral_id" type="number" placeholder="平台转诊记录ID" required><input name="patient_id" type="number" placeholder="患者ID" required><button>签发证明</button></form>` : ""}
      ${canApply ? `<form class="inline" id="spec-form"><input name="patient_id" type="number" placeholder="患者ID" required><input name="disease_name" placeholder="病种" required><input name="reason" placeholder="申报理由" style="min-width:180px"><button>特病申报</button></form>` : ""}
      ${canSettle || canApply ? "" : `<p class="muted">结算登记与转诊证明由经办办理，特病申报由经办或医师提出；管理层在下面的队列里审核。</p>`}
      <p class="msg" id="ins-msg"></p>`)}
    ${panel("特病申报队列" + queueNote(recentApps, appliedApps, apps), table(["ID", "患者", "病种", "理由", "状态", "操作"], apps, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.disease_name)}</td><td>${esc(a.reason) || "—"}</td>
       <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${esc(a.status_name)}</span></td>
       <td>${a.status === "applied" && canReview ? `<button class="btn secondary" data-ok="${a.id}">批准</button><button class="btn danger" data-no="${a.id}">驳回</button>` : "—"}</td></tr>`))}
    ${panel("双通道药品申报（医师/经办申报 → 管理层审核）" + queueNote(recentDual, pendingDual, dualApps), `
      ${canApply ? `<form class="inline" id="dual-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="drug_name" placeholder="药品名称" required>
        <input name="reason" placeholder="申报理由" style="min-width:180px">
        <button>申报</button></form>` : ""}
      ${table(["ID", "患者", "药品", "理由", "状态", "审核意见", "操作"], dualApps, (a) =>
        `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${esc(a.drug_name)}</td><td>${esc(a.reason) || "—"}</td>
         <td><span class="tag ${a.status === "approved" ? "green" : a.status === "rejected" ? "red" : "orange"}">${esc(a.status_name)}</span></td>
         <td>${esc(a.review_comment) || "—"}</td>
         <td>${a.status === "pending" && canReview
           ? `<button class="btn secondary" data-dualok="${a.id}">批准</button><button class="btn danger" data-dualno="${a.id}">驳回</button>` : "—"}</td></tr>`)}`)}
    ${panel("结算记录", table(["ID", "患者", "机构", "类型", "总额", "医保付", "自付"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${s.org_id}</td><td>${s.settle_type === "local" ? "本地" : "异地"}</td>
       <td>${s.total_amount}</td><td>${s.insurance_pay}</td><td>${s.self_pay}</td></tr>`))}`;
  if (canSettle) {
    $("#ins-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/settlements", formJson(e.target, ["patient_id", "org_id", "total_amount", "insurance_pay", "self_pay"]), "#ins-msg"); };
    $("#cert-form").onsubmit = async (e) => {
      e.preventDefault();
      // 带上患者号、后端核对转诊单是不是这位患者的（P2-997）：平台转诊与慢专病转诊各自编号，照慢专病转诊单号来签会签到同号
      // 的另一位患者的平台转诊上
      const f = new FormData(e.target);
      try {
        const c = await api(`/api/insurance/referral-certs/${f.get("referral_id")}?patient_id=${encodeURIComponent(f.get("patient_id"))}`,
          { method: "POST" });
        setMsg("#ins-msg", `证明号：${c.cert_no}（转诊记录 ${c.referral_id}，患者 ${f.get("patient_id")}）`);
      }
      catch (err) { setMsg("#ins-msg", err.message, false); }
    };
  }
  if (canApply) {
    $("#spec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/special-diseases", formJson(e.target, ["patient_id"]), "#ins-msg"); };
    $("#dual-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/insurance/dual-channel", formJson(e.target, ["patient_id"]), "#ins-msg"); };
  }
  $("#page-body").onclick = async (e) => {
    const { ok, no, dualok, dualno } = e.target.dataset;
    if (ok) postAction(`/api/insurance/special-diseases/${ok}/review?approve=true`, null, "#ins-msg");
    if (no) postAction(`/api/insurance/special-diseases/${no}/review?approve=false`, null, "#ins-msg");
    if (dualok || dualno) {
      // P2-38：原先意见框点"取消"照样批准 / 驳回（意见记空）。表单里取消就是不审。
      // 框自己提交（P2-607）：意见写超了（后端 512 字）报错写在框里、框不关，写好的意见不用重填
      const approve = Boolean(dualok);
      const done = await spdModal(approve ? "批准双通道申报" : "驳回双通道申报",
        [{ name: "comment", label: approve ? "审核意见" : "驳回理由", type: "textarea" }],
        { submit: (form) => api(`/api/insurance/dual-channel/${dualok || dualno}/review?approve=${approve}`
          + `&comment=${encodeURIComponent(form.comment)}`, { method: "POST" }) });
      if (done) route();
    }
  };
}

async function renderEducation() {
  // 「个人考核记录」原先写「个人学分」（P2-1431）：平台没有学分 / 学时字段，「我的学习记录」只列课程考试
  $("#page-desc").textContent = "课程管理、培训考核（60分合格）、个人考核记录；直播申请与排期审核";
  const [courses, mine, lives] = await Promise.all([
    api("/api/education/courses"), api("/api/education/my-records"), api("/api/education/live-sessions")]);
  const role = currentRole();
  const LS = { pending: ["待审核", "orange"], approved: ["已排期", ""], rejected: ["已驳回", "red"], finished: ["已结束", "green"] };
  $("#page-body").innerHTML = `
    ${panel("新建课程（管理员）", `
      <form class="inline" id="course-form">
        <input name="title" placeholder="课程名" required style="min-width:220px">
        <select name="course_type"><option value="vod">点播</option><option value="live">直播</option></select>
        <select name="category"><option value="clinical">临床医学</option><option value="tcm">中医适宜技术</option><option value="public_health">公共卫生</option></select>
        <input name="speaker" placeholder="讲者"><button>创建</button>
      </form><p class="msg" id="edu-msg"></p>`)}
    ${panel("课程列表", table(["ID", "课程", "形式", "类别", "讲者", "操作"], courses, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.title)}</td><td>${c.course_type === "live" ? "直播" : "点播"}</td><td>${esc(c.category_name)}</td><td>${esc(c.speaker)}</td>
       <td><button class="btn secondary" data-exam="${c.id}">提交考核</button>
           <button class="btn secondary" data-cstats="${c.id}">培训统计</button></td></tr>`))}
      <div id="edu-detail"></div>
    ${panel("我的学习记录", table(["课程", "成绩", "结果"], mine, (r) =>
      `<tr><td>${esc(r.title)}</td><td>${r.score}</td><td><span class="tag ${r.passed ? "green" : "red"}">${r.passed ? "合格" : "未合格"}</span></td></tr>`))}
    ${panel("直播管理（申请 → 管理层排期审核 → 结束；音视频通道为对接项）", `
      <form class="inline" id="live-form">
        <input name="title" placeholder="直播主题" required style="min-width:220px">
        <input name="speaker" placeholder="主讲人">
        <label style="font-size:13px">计划时间 <input name="planned_at" type="datetime-local"></label>
        <button>申请直播</button></form>
      ${table(["ID", "主题", "主讲", "计划时间", "状态", "审核意见", "回放", "操作"], lives, (s) => {
        const actions = s.status === "pending" && ["director", "admin"].includes(role)
          ? `<button class="btn secondary" data-liveok="${s.id}">排期</button>
             <button class="btn danger" data-liveno="${s.id}">驳回</button>`
          : s.status === "approved" && ["director", "operator", "admin"].includes(role)
          ? `<button class="btn secondary" data-livefin="${s.id}">结束</button>` : "";
        // 已结束的直播才谈得上回放与反馈（后端两处都是 409），所以按状态摆
        const after = s.status === "finished"
          ? `<button class="btn secondary" data-liverec="${s.id}">${s.recording_url ? "换回放" : "上传回放"}</button>
             <button class="btn secondary" data-livefb="${s.id}">评价</button>
             <button class="btn secondary" data-livefbs="${s.id}">看评价</button>` : "";
        // 回放只给 http(s) 地址画链接（P2-1429）：原先非空即放进 href——CSP 放行了 'unsafe-inline'，javascript: 链接点了会在
        // 本站执行，只做 esc() 挡不住。存量的非 http(s) 地址照原样转义成文字、不做 href
        const replay = isHttpUrl(s.recording_url)
          ? `<a href="${esc(s.recording_url)}" target="_blank" rel="noopener">回放</a>` : esc(s.recording_url) || "—";
        return `<tr><td>${s.id}</td><td>${esc(s.title)}</td><td>${esc(s.speaker) || "—"}</td><td>${esc(s.planned_at) || "—"}</td>
          <td>${statusTag(LS, s.status)}</td><td>${esc(s.review_comment) || "—"}</td>
          <td>${replay}</td>
          <td>${actions + after || "—"}</td></tr>`;
      })}
      <div id="live-detail"></div>`)}
    ${panel("健康宣教文章（公卫 / 经办编制，发布后进居民端）", `
      <p class="desc">发布即对居民端可见（居民端只读已发布的）。管理端目前没有文章列表接口，
        新建后这里给出文章 ID，发布按 ID 走——后端补上列表接口之前，先这样用</p>
      <form class="inline" id="article-form">
        <input name="title" placeholder="标题" required style="min-width:220px">
        <select name="category"><option value="general">综合</option><option value="chronic">慢病</option>
          <option value="maternal">妇幼</option><option value="infectious">传染病</option></select>
        <input name="content" placeholder="正文" style="min-width:260px">
        <button>新建文章</button>
      </form>
      <form class="inline" id="article-pub-form" style="margin-top:8px">
        <input name="article_id" type="number" placeholder="文章ID" required>
        <button class="secondary">发布</button>
      </form>
      <p class="msg" id="article-msg"></p>`)}`;
  $("#course-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/courses", formJson(e.target), "#edu-msg"); };
  $("#live-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/education/live-sessions", formJson(e.target), "#edu-msg"); };
  $("#article-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      // 新建回执只有 {id, status}，把 id 显出来——没有列表接口时它是发布的唯一线索
      const a = await api("/api/education/articles", { method: "POST", body: JSON.stringify(formJson(e.target)) });
      setMsg("#article-msg", `文章已新建：ID ${a.id}（${a.status === "published" ? "已发布" : "草稿"}），可在下方按 ID 发布`);
      e.target.reset();
    } catch (err) { setMsg("#article-msg", err.message, false); }
  };
  $("#article-pub-form").onsubmit = async (e) => {
    e.preventDefault();
    const id = new FormData(e.target).get("article_id");
    try {
      const a = await api(`/api/education/articles/${id}/publish`, { method: "POST" });
      setMsg("#article-msg", `文章 ${a.id} 已发布，居民端可见`);
    } catch (err) { setMsg("#article-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.liveok || d.liveno) {
      // 框自己提交（P2-607）：意见写超了（后端 256 字）报错写在框里、框不关，写好的意见不用重填
      const approve = Boolean(d.liveok);
      const done = await spdModal(approve ? "排期审核" : "驳回直播申请", [
        { name: "comment", label: approve ? "审核意见" : "驳回理由", type: "textarea" },
      ], { submit: (form) => {
        const comment = form.comment || (approve ? "同意排期" : "");
        return api(
          `/api/education/live-sessions/${d.liveok || d.liveno}/review?approve=${approve}&comment=${encodeURIComponent(comment)}`,
          { method: "POST" });
      } });
      if (done) route();
      return;
    }
    if (d.livefin) return postAction(`/api/education/live-sessions/${d.livefin}/finish`, null, "#edu-msg");
    if (d.cstats) {
      try {
        const s = await api(`/api/education/courses/${d.cstats}/stats`);
        // 「参训人数」原先写「参训人次」（P2-1431）：trainees 数的是考试记录行，每人每课一行，同一个人考几次都只算一个
        $("#edu-detail").innerHTML = panel(`培训统计 · 课程 #${s.course_id}`,
          spdCards([["参训人数", s.trainees], ["合格", s.passed], ["合格率", s.pass_rate_pct + "%"]]));
      } catch (err) { setMsg("#edu-msg", err.message, false); }
      return;
    }
    if (d.liverec) {
      const form = await spdModal("上传课程回放（仅已结束的直播）", [
        { name: "recording_url", label: "回放地址", required: true },
      ]);
      if (!form || !form.recording_url) return;
      return postAction(`/api/education/live-sessions/${d.liverec}/recording`,
        { recording_url: form.recording_url }, "#edu-msg");
    }
    if (d.livefb) {
      // 框自己提交（P2-607）：评价写超了（后端 512 字）报错写在框里、框不关，写好的评价不用重填
      const r = await spdModal("直播评价（一人一场一条，再评即覆盖）", [
        { name: "rating", label: "评分", type: "select", value: "5",
          options: [5, 4, 3, 2, 1].map((n) => ({ value: String(n), label: `${n} 分` })) },
        { name: "comment", label: "评价", type: "textarea" },
      ], { submit: (form) => api(`/api/education/live-sessions/${d.livefb}/feedback`, { method: "POST",
        body: JSON.stringify({ rating: Number(form.rating), comment: form.comment || "" }) }) });
      if (r) setMsg("#edu-msg", r.updated ? "已更新你的评价" : "评价已提交");
      return;
    }
    if (d.livefbs) {
      try {
        const f = await api(`/api/education/live-sessions/${d.livefbs}/feedback`);
        $("#live-detail").innerHTML = panel(
          `直播评价 · 第 ${f.session_id} 场（${f.count} 条${f.avg_rating === null ? "" : `，均分 ${f.avg_rating}`}）`,
          table(["用户", "评分", "评价"], f.feedbacks, (x) =>
            `<tr><td>${x.user_id}</td><td>${x.rating}</td><td>${esc(x.comment) || "—"}</td></tr>`));
      } catch (err) { setMsg("#edu-msg", err.message, false); }
      return;
    }
    if (!d.exam) return;
    const form = await spdModal("提交课程考核", [
      { name: "score", label: "考核得分（0-100，≥60 合格）", type: "number", required: true },
    ]);
    if (!form) return;
    return postAction(`/api/education/courses/${d.exam}/exam`, { score: form.score }, "#edu-msg");
  };
  await drawEduGaps();  // 块4⑳㉑ 课件资源与适宜技术实训
}

async function renderEldercare() {
  $("#page-desc").textContent = "自理能力评估（Barthel自动分级）、失能老人清单、健康预警（重度失能专案+年度复评到期）";
  const [assessments, disabled, alerts, stats] = await Promise.all([
    api("/api/eldercare/assessments"), api("/api/eldercare/disabled"),
    api("/api/eldercare/alerts"), api("/api/eldercare/stats")]);
  $("#page-body").innerHTML = `
    ${alerts.total ? panel(`⚠ 老年健康预警（${alerts.total}）`,
      table(["患者", "预警类型", "提示", "末次评估"], alerts.alerts, (a) =>
        `<tr><td>${a.patient_id}</td>
         <td><span class="tag ${a.alert_type === "severe_disability" ? "red" : "orange"}">${a.alert_type === "severe_disability" ? "重度失能专案" : "复评到期"}</span></td>
         <td>${esc(a.message)}</td><td>${esc(a.assessed_date) || "—"}</td></tr>`),
      { accent: "#c62828" }) : ""}`;
  $("#page-body").innerHTML += `
    ${panel("新评估", `
      <form class="inline" id="eld-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="adl_score" type="number" min="0" max="100" placeholder="ADL(0-100)" required>
        <input name="cognitive_score" type="number" min="0" max="30" placeholder="认知(0-30)"><input name="tcm_constitution" placeholder="体质">
        <input name="assessed_date" placeholder="评估日期 YYYY-MM-DD"><button>评估</button>
      </form><p class="msg" id="eld-msg"></p>`)}
    ${disabled.length ? panel(`⚠ 失能老人清单（${disabled.length}）`, table(["患者", "分级", "ADL"], disabled, (d) =>
      `<tr><td>${d.patient_id}</td><td><span class="tag red">${esc(d.care_level)}</span></td><td>${d.adl_score}</td></tr>`)) : ""}
    ${panel("老年健康统计", `
      <div class="cards">
        <div class="card"><div class="label">已评估老人</div><div class="value">${stats.assessed_people}</div></div>
        <div class="card"><div class="label">评估条数</div><div class="value">${stats.assessment_records}</div></div>
        <div class="card"><div class="label">失能人数</div>
          <div class="value${stats.disabled_count ? " warn" : ""}">${stats.disabled_count}</div></div>
        <div class="card"><div class="label">失能率</div><div class="value">${
          stats.disabled_rate_pct === null ? "无评估" : stats.disabled_rate_pct + "%"}</div></div>
        <div class="card"><div class="label">认知已筛 / 未筛</div>
          <div class="value">${stats.cognitive.screened} / ${stats.cognitive.unscreened}</div></div>
        <div class="card"><div class="label">认知平均分</div><div class="value">${
          stats.cognitive.avg_score === null ? "无筛查" : stats.cognitive.avg_score}</div></div>
        <div class="card"><div class="label">体质已辨 / 未辨</div>
          <div class="value">${stats.tcm_constitution.done} / ${stats.tcm_constitution.not_done}</div></div>
      </div>
      <p class="desc">${esc(stats.caliber)}</p>
      ${Object.keys(stats.by_care_level).length
        ? barChart(Object.entries(stats.by_care_level), { unit: " 人" })
        : '<p class="empty">尚无评估</p>'}
      <p class="desc"><b>"未做"与"0 分"不是一回事</b>：认知筛查与体质辨识本就不是每次评估必做，
        所以它们各自单列了"未做"的人数，而不是按 0 分并进平均值——
        并进去会让筛查做得少的机构看起来认知水平特别差。</p>`)}
    ${panel("", table(["ID", "患者", "ADL", "认知", "分级", "日期"], assessments, (a) =>
      `<tr><td>${a.id}</td><td>${a.patient_id}</td><td>${a.adl_score}</td><td>${a.cognitive_score}</td>
       <td><span class="tag ${a.care_level === "能力完好" ? "green" : "red"}">${esc(a.care_level)}</span></td><td>${esc(a.assessed_date)}</td></tr>`))}`;
  $("#eld-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/eldercare/assessments", formJson(e.target, ["patient_id", "adl_score", "cognitive_score"]), "#eld-msg"); };
}

async function renderMaternal() {
  $("#page-desc").textContent = "孕产妇建册、产检、分娩登记、产后结案；儿童保健、新筛与高危儿；妇女保健四类记录";
  const [records, children, highRisk, womenHealth, orgs] = await Promise.all([
    api("/api/maternal/records"), api("/api/maternal/children"),
    api("/api/maternal/children/high-risk"), api("/api/maternal/women-health"),
    api("/api/organizations")]);
  const MS = { registered: "孕期管理", delivered: "已分娩", closed: "已结案" };
  const WH_TYPES = { premarital: "婚前保健", preconception: "孕前保健", gynecology: "妇女病检查", contraception: "避孕节育" };
  const SCREEN_ITEMS = { metabolic: "遗传代谢病", hearing: "听力", chd: "先心病" };
  const hrIds = new Set(highRisk.map((c) => c.id));
  $("#page-body").innerHTML = `
    ${panel("孕产妇建册 / 儿童建档", `
      <form class="inline" id="mat-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="lmp" placeholder="末次月经 YYYY-MM-DD">
        <input name="edc" placeholder="预产期 YYYY-MM-DD">
        <input name="gravidity" type="number" min="1" placeholder="孕次（空=1）"><input name="parity" type="number" min="0" placeholder="产次（空=0）">
        <label style="font-size:13px"><input type="checkbox" name="high_risk"> 高危</label>
        <input name="risk_factors" placeholder="高危因素（如 高龄、瘢痕子宫）" style="min-width:200px"><button>建册</button></form>
      <form class="inline" id="child-form">
        <input name="name" placeholder="儿童姓名" required><select name="gender"><option>未知</option><option>男</option><option>女</option></select>
        <input name="birth_date" placeholder="出生日期 YYYY-MM-DD" required><input name="guardian_patient_id" type="number" placeholder="监护人患者ID"><button>建档</button></form>
      <p class="msg" id="mat-msg"></p>`)}
    ${panel("孕产妇档案", table(["ID", "患者", "预产期", "孕/产次", "高危", "状态", "操作"], records, (r) =>
      `<tr><td>${r.id}</td><td>${r.patient_id}</td><td>${esc(r.edc)}</td><td>G${r.gravidity}P${r.parity}</td>
       <td>${r.high_risk ? `<span class="tag red">高危</span> ${esc(r.risk_factors)}` : '<span class="tag green">正常</span>'}</td>
       <td><span class="tag">${esc(MS[r.status] || r.status)}</span></td>
       <td>${r.status !== "closed" ? `<button class="btn secondary" data-visit="${r.id}">记录访视</button>
         ${!r.has_delivery   // 看有没有分娩记录、不看状态：产后访视先录也会把档案推到「已分娩」（P2-594）
           ? `<button class="btn secondary" data-delivery="${r.id}">分娩登记</button>` : ""}
         ${r.status === "delivered" && r.has_postpartum   // 没有产后访视的结案接口 409（P2-211）
           ? `<button class="btn secondary" data-close="${r.id}">结案</button>` : ""}` : "—"}</td></tr>`))}
    ${highRisk.length ? panel(`⚠ 高危儿专案清单（${highRisk.length}）`,
      table(["ID", "姓名", "出生日期", "高危原因"], highRisk, (c) =>
        `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.birth_date)}</td><td><span class="tag red">${esc(c.risk_note)}</span></td></tr>`),
      { accent: "#c62828" }) : ""}
    ${panel("儿童档案（新筛异常自动纳入高危儿）", table(["ID", "姓名", "性别", "出生日期", "高危", "操作"], children, (c) =>
      `<tr><td>${c.id}</td><td>${esc(c.name)}</td><td>${esc(c.gender)}</td><td>${esc(c.birth_date)}</td>
       <td>${hrIds.has(c.id) ? '<span class="tag red">高危</span>' : '<span class="tag green">—</span>'}</td>
       <td><button class="btn secondary" data-cvisit="${c.id}">记录访视</button>
           <button class="btn secondary" data-screen="${c.id}">新筛登记</button>
           <button class="btn" data-shist="${c.id}">筛查史</button>
           <button class="btn ${hrIds.has(c.id) ? "" : "danger"}" data-hrtoggle="${c.id}" data-cur="${hrIds.has(c.id)}">${hrIds.has(c.id) ? "解除高危" : "标记高危"}</button></td></tr>`))}
    <div class="panel hidden" id="screen-panel"><h3>新生儿筛查史</h3><div id="screen-list"></div></div>
    ${panel("妇女保健记录（婚前/孕前/妇女病/避孕节育）", `
      <form class="inline" id="wh-form">
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="record_type">${Object.entries(WH_TYPES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="exam_date" placeholder="检查日期 YYYY-MM-DD">
        <input name="result" placeholder="检查结果">
        <input name="advice" placeholder="指导意见">
        <button>登记</button></form>
      ${table(["ID", "患者", "类型", "日期", "结果", "指导"], womenHealth, (w) =>
        `<tr><td>${w.id}</td><td>${w.patient_id}</td><td><span class="tag">${WH_TYPES[w.record_type] || esc(w.record_type)}</span></td>
         <td>${esc(w.exam_date) || "—"}</td><td>${esc(w.result) || "—"}</td><td>${esc(w.advice) || "—"}</td></tr>`)}`)}`;
  // 一孕一册（P1-140）：上一胎结案后再孕建的是新册，孕次 / 产次要录得进去（原先表单没有这两格，每本都是 G1P0）
  // 建册时就能判出的高危（高龄、瘢痕子宫……）录得进去（P2-857）：原先表单没有这两项，只能等产检血压 ≥140 或产筛高风险自动标
  $("#mat-form").onsubmit = async (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["patient_id", "gravidity", "parity"]);
    body.high_risk = e.target.high_risk.checked;
    try {
      const r = await api("/api/maternal/records", { method: "POST", body: JSON.stringify(body) });
      // 这位妇女已有在册档案（P2-1305，与患者建档 P2-1244 同一口径）：一孕一册，后端原样返回那本、本次所填一概没写进去
      // （回执 created=false，状态码照旧 201）。原先照样整页重画、表单清空——按 B 超校正的预产期像是改好了，档案里还是旧值。
      // 什么都没改，不重画：填的留在表单里
      if (r.created === false) {
        setMsg("#mat-msg", `该孕产妇已有在册档案（档案ID：${r.id}），本次填写未写入：档案上仍是末次月经 ${r.lmp || "未填"}、`
          + `预产期 ${r.edc || "未填"}、G${r.gravidity}P${r.parity}`, false);
        return;
      }
      route();
    } catch (err) { setMsg("#mat-msg", err.message, false); }
  };
  $("#child-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/children", formJson(e.target, ["guardian_patient_id"]), "#mat-msg"); };
  $("#wh-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/maternal/women-health", formJson(e.target, ["patient_id"]), "#mat-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    // P2-38：原先是浏览器原生弹窗连问（访视三连、分娩四连、新筛输序号再加确认框），录不了多字段、
    // 输错也没有提示。换成页内表单，顺带把后端早就收、页面一直没入口的字段补上：
    // 产检孕周、新生儿数、儿童身高体重、各处备注。
    // P2-607：带填写内容的几张框由框自己提交（spdModal 的 submit）——422 的人话写在框里、框不关、填的都在，改了再交；
    // 原先点确定就关框、报错落到 #mat-msg，访视备注、分娩结局写了一大段也要从头再填。
    // 留空发 null；认不出的原样发给后端，让它 422 出人话——Number() 得到 NaN、
    // JSON.stringify 又会把 NaN 变成 null，值就被悄悄丢掉了
    const numOrNull = (v) => (v === "" || v == null ? null : Number.isNaN(Number(v)) ? v : Number(v));
    if (d.visit) {
      const v = await spdModal("记录访视", [
        { name: "visit_type", label: "访视类型", type: "select", options: [
          { value: "prenatal", label: "产检" }, { value: "postpartum", label: "产后访视" }] },
        { name: "gest_week", label: "孕周（产检填，4–45）", placeholder: "如 28" },
        { name: "bp", label: "血压（收缩压 ≥140 自动标记高危）", placeholder: "如 120/80" },
        { name: "visit_date", label: "访视日期（可空）", placeholder: "YYYY-MM-DD" },
        { name: "note", label: "备注", type: "textarea" },
      ], { submit: (v) => api(`/api/maternal/records/${d.visit}/visits`, { method: "POST",
        body: JSON.stringify({ ...v, gest_week: numOrNull(v.gest_week) }) }) });
      if (v) route();
      return;
    }
    if (d.delivery) {
      const v = await spdModal("分娩登记", [
        { name: "org_id", label: "分娩机构", type: "select",
          options: orgs.map((o) => ({ value: o.id, label: o.name })) },
        { name: "delivery_date", label: "分娩日期", placeholder: "YYYY-MM-DD", required: true },
        { name: "delivery_mode", label: "分娩方式", type: "select", options: [
          { value: "natural", label: "顺产" }, { value: "cesarean", label: "剖宫产" }] },
        { name: "newborn_count", label: "新生儿数", type: "number", value: 1 },
        { name: "outcome", label: "分娩结局", type: "textarea" },
      ], { submit: (v) => api(`/api/maternal/records/${d.delivery}/delivery`, { method: "POST",
        body: JSON.stringify({ ...v, org_id: Number(v.org_id), newborn_count: v.newborn_count || 1 }) }) });
      if (v) route();
      return;
    }
    if (d.close) {
      // P2-43：原先点一下就结案；页面上没有重开入口
      if (!await spdModal("孕产妇保健结案", [], { intro: "结案后档案标记为「已结案」，页面上不能重开。" })) return;
      return postAction(`/api/maternal/records/${d.close}/close`, null, "#mat-msg");
    }
    if (d.cvisit) {
      const v = await spdModal("儿童访视", [
        { name: "visit_type", label: "访视类型", type: "select", options: [
          { value: "checkup", label: "健康检查" }, { value: "newborn", label: "新生儿访视" }] },
        // 身长体重带小数：用文本框收（number 框默认步长 1，3.5 kg 会被浏览器拦下）
        { name: "height_cm", label: "身长/身高（cm）", placeholder: "如 50.5" },
        { name: "weight_kg", label: "体重（kg）", placeholder: "如 3.5" },
        { name: "visit_date", label: "访视日期（可空）", placeholder: "YYYY-MM-DD" },
        { name: "note", label: "备注", type: "textarea" },
      ], { submit: (v) => api(`/api/maternal/children/${d.cvisit}/visits`, { method: "POST",
        body: JSON.stringify({ ...v, height_cm: numOrNull(v.height_cm), weight_kg: numOrNull(v.weight_kg) }) }) });
      if (v) route();
      return;
    }
    if (d.screen) {
      const v = await spdModal("新生儿筛查登记", [
        { name: "item", label: "筛查项目", type: "select",
          options: Object.entries(SCREEN_ITEMS).map(([value, label]) => ({ value, label })) },
        { name: "result", label: "结果（异常/可疑将自动纳入高危儿）", type: "select", options: [
          { value: "normal", label: "正常" }, { value: "abnormal", label: "异常/可疑" }] },
        { name: "screen_date", label: "筛查日期（可空）", placeholder: "YYYY-MM-DD" },
        { name: "note", label: "备注", type: "textarea" },
      ], { submit: (v) => api(`/api/maternal/children/${d.screen}/screenings`, { method: "POST",
        body: JSON.stringify(v) }) });
      if (v) route();
      return;
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
      if (!toHigh) {
        return postAction(`/api/maternal/children/${d.hrtoggle}/high-risk`, { high_risk: false, risk_note: "" }, "#mat-msg");
      }
      const ok = await spdModal("标记高危儿", [{ name: "risk_note", label: "高危原因", type: "textarea" }], {
        submit: (v) => api(`/api/maternal/children/${d.hrtoggle}/high-risk`, { method: "POST",
          body: JSON.stringify({ high_risk: true, risk_note: v.risk_note || "人工标记" }) }) });
      if (ok) route();
      return;
    }
  };
  await drawPrenatalScreenings();  // 块4㉔ 产前筛查与诊断
}

/* 接种史行点「上报 AEFI」时带过去的这一剂（P2-1499）：{ patient_id, record_id }，「疫苗批次与冷链」页的 AEFI 表单取用一次即清
 * （同 P2-1316 的 spdEnrollJump）。只放内存、不进 localStorage——它是「这一次点击」的上下文，刷新页面就该回到空表单。 */
let vacAefiJump = null;

async function renderVaccination() {
  $("#page-desc").textContent = "接种前综合评估（禁忌硬拦截）、接种登记、禁忌管理";
  // ADR-0009 第五批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = `
    ${panel("接种前评估", `
      <form class="inline" id="vac-check"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="vaccine_code" placeholder="疫苗编码" required><button>评估</button></form>
      <div id="vac-check-result"></div>`)}
    ${panel("接种登记 / 禁忌登记", `
      <form class="inline" id="vac-form">
        <select name="batch_id" id="vac-batch"><option value="">疫苗批次（选了就查效期 / 封存 / 库存并扣减一支）</option></select>
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="vaccine_name" placeholder="疫苗名称" required><input name="dose_no" type="number" min="1" placeholder="剂次（留空按既往剂次+1）" style="min-width:60px">
        <input name="vaccinated_date" placeholder="接种日期"><input name="org_id" type="number" placeholder="接种机构ID" required>
        <input name="site" placeholder="接种部位"><input name="vaccinator" placeholder="接种人"><button>登记接种</button></form>
      <p class="desc">新接种一律建议选批次：出了问题按批号召回、查受种者时，没挂批次的这一针查不出来。
        下拉只列可用的批次（未过期、未封存、尚有余量），同一疫苗先到期的在前；选了自动带出疫苗编码、名称与接种机构。</p>
      <form class="inline" id="contra-form">
        <input name="patient_id" type="number" placeholder="患者ID" required><input name="vaccine_code" placeholder="疫苗编码" required>
        <input name="reason" placeholder="禁忌原因" required>
        <select name="contra_type"><option value="permanent">长期禁忌</option><option value="temporary">暂时禁忌</option></select>
        <input name="valid_until" placeholder="有效期末日（暂时禁忌必填）"><button class="btn danger">登记禁忌</button></form>
      <p class="msg" id="vac-msg"></p>`)}
    ${panel("禁忌清单（可解除）", `
      <form class="inline" id="contra-list"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="contra-result"></div>`)}
    ${panel("接种史查询", `
      <form class="inline" id="vac-hist"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询</button></form>
      <div id="vac-hist-result"></div>`)}`;
  $("#vac-check").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着——
    // 这里挂着的是上一位的「可以接种，本次为第 N 剂」，换了患者号查失败，看的人会当成这一位可以打
    $("#vac-check-result").innerHTML = "";
    let r;
    try {
      r = await api(`/api/vaccination/pre-check?patient_id=${f.get("patient_id")}&vaccine_code=${encodeURIComponent(f.get("vaccine_code"))}`);
    } catch (err) {
      $("#vac-check-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    $("#vac-check-result").innerHTML = r.allowed
      ? `<p class="msg ok">可以接种，本次为第 ${r.next_dose_no} 剂</p>`
      : `<p class="msg err">禁止接种：${esc(r.contraindications.join("；"))}</p>`;
  };
  // 批次要送（P1-154）：原先表单没有批次，接种登记的「批次三查」（过期 / 封存 / 库存）从界面上一次都不执行——
  // 封存的批次照样打、库存不扣，这一针也挂不到批号上，按批号召回与 AEFI 追踪都查不到这个孩子
  $("#vac-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/records", formJson(e.target, ["patient_id", "dose_no", "org_id", "batch_id"]), "#vac-msg"); };
  let usableBatches = [];
  $("#vac-batch").onchange = (e) => {
    const b = usableBatches.find((x) => String(x.id) === e.target.value);
    if (!b) return;
    const form = $("#vac-form");
    form.vaccine_code.value = b.vaccine_code;
    form.vaccine_name.value = b.vaccine_name;
    form.org_id.value = b.org_id;
  };
  $("#contra-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccination/contraindications", formJson(e.target, ["patient_id"]), "#vac-msg"); };
  // 先清空、查不到把原因写出来（P2-1009，与 P2-378 同一写法）：原先抛错没人接，换了患者号查失败（无权 403、打错号），
  // 清单照旧挂着上一位的禁忌、「解除」按钮挂着上一位的患者号——点解除解的是上一位的长期禁忌，他的接种前评估随即放行
  const drawContras = async (pid) => {
    $("#contra-result").innerHTML = "";
    let rows;
    try {
      rows = await api(`/api/vaccination/contraindications?patient_id=${pid}`);
    } catch (err) {
      $("#contra-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    $("#contra-result").innerHTML = table(["疫苗", "原因", "类型", "有效期至", "当前", "操作"], rows, (r) =>
      `<tr><td>${esc(r.vaccine_code)}</td><td>${esc(r.reason)}</td>` +
      `<td>${r.contra_type === "temporary" ? "暂时" : "长期"}</td><td>${esc(r.valid_until || "—")}</td>` +
      `<td>${r.blocking ? '<span class="tag danger">拦截中</span>' : (r.status === "lifted" ? "已解除" : "已过期")}</td>` +
      `<td>${r.blocking ? `<button class="btn sm" data-lift="${r.id}" data-pid="${pid}">解除</button>` : esc(r.lift_reason || "—")}</td></tr>`);
  };
  $("#contra-list").onsubmit = (e) => { e.preventDefault(); drawContras(new FormData(e.target).get("patient_id")); };
  $("#contra-result").onclick = async (e) => {
    const id = e.target.dataset.lift; if (!id) return;
    // P2-38：弹窗换成页内表单（与本页其余录入一致）；解除后接种前评估不再拦截这一条
    const form = await spdModal("解除接种禁忌", [
      { name: "lift_reason", label: "解除原因", required: true, placeholder: "如：体温已恢复正常" },
    ], { intro: "解除后，接种前评估不再因这一条拦截。" });
    if (!form) return;
    await postAction(`/api/vaccination/contraindications/${id}/lift`, { lift_reason: form.lift_reason }, "#vac-msg");
    drawContras(e.target.dataset.pid);
  };
  $("#vac-hist").onsubmit = async (e) => {
    e.preventDefault();
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着
    $("#vac-hist-result").innerHTML = "";
    let records;
    try {
      records = await api(`/api/vaccination/records?patient_id=${new FormData(e.target).get("patient_id")}`);
    } catch (err) {
      $("#vac-hist-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    // 记录号、批号印出来，每一剂可直接上报 AEFI（P2-1499）：原先记录号只藏在打印按钮的 data 属性里，AEFI 表单却要人手填
    // 「接种记录ID」——全站没有一处页面显示它，从界面上报的 AEFI 关联不到剂次、批号恒空（按批号查不到这一例，「发病不得早于
    // 接种」不拦，统计按上报机构归）
    $("#vac-hist-result").innerHTML = table(["记录号", "疫苗", "剂次", "日期", "批号", "机构", "操作"], records, (r) =>
      `<tr><td>${r.id}</td><td>${esc(r.vaccine_name)}</td><td>第${r.dose_no}剂</td><td>${esc(r.vaccinated_date)}</td>
       <td>${esc(r.batch_no || "—")}</td><td>${r.org_id}</td>
       <td><button class="btn secondary" data-print-vac="${r.id}">打印接种证明</button>
           <button class="btn secondary" data-aefi-dose="${r.id}" data-aefi-pid="${r.patient_id}">上报 AEFI</button></td></tr>`);
  };
  $("#vac-hist-result").onclick = async (e) => {
    const { aefiDose, aefiPid } = e.target.dataset;
    if (aefiDose) {
      // AEFI 表单在「疫苗批次与冷链」页：带着这一剂过去，患者号与剂次替人填好，症状、发病日期仍由人填（P2-1499）
      vacAefiJump = { patient_id: Number(aefiPid), record_id: Number(aefiDose) };
      nav("vaccinesupply");
      return;
    }
    const id = e.target.dataset.printVac; if (!id) return;
    try { await openPrintPage(`/api/print/vaccinations/${id}`); }
    catch (err) { setMsg("#vac-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好（P2-31 根修的写法）
  // 下拉按 疫苗、效期升序、批号 排，同一疫苗先到期的在前（P2-1359）：接口按登记倒序（最新在前），原样列出来，25 天后到期
  // 的旧批次排在 700 天后到期的新批次后面——新批次先打掉、旧批次放到过期。药品侧发药按 FEFO（dispense 模块说明第 2 条）。
  // 只在这里排，接口顺序不动（疫苗批次台账照旧按登记倒序）
  const cmp = (x, y) => (x < y ? -1 : x > y ? 1 : 0);
  usableBatches = (await api("/api/vaccine-supply/batches?usable_only=true").catch(() => []))
    .sort((a, b) => cmp(a.vaccine_code, b.vaccine_code) || cmp(a.expire_date, b.expire_date) || cmp(a.batch_no, b.batch_no));
  $("#vac-batch").innerHTML = `<option value="">疫苗批次（选了就查效期 / 封存 / 库存并扣减一支）</option>` +
    usableBatches.map((b) => `<option value="${b.id}">${esc(b.vaccine_name)} · 批号 ${esc(b.batch_no)} · 机构 ${b.org_id}` +
      ` · 余 ${b.remaining} 支 · 效期 ${esc(b.expire_date)}</option>`).join("");
}


// 转归五个取值来自后端 `AefiOutcome` 的 pattern 与 `AEFI_OUTCOMES` 映射，键序照抄
const AEFI_OUTCOMES = {
  unknown: "未知", improving: "好转中", recovered: "痊愈",
  sequelae: "留有后遗症", death: "死亡",
};

async function renderVaccineSupply() {
  $("#page-desc").textContent = "疫苗批次（批号/厂家/效期）、冷链温度监测、AEFI 上报与统计";
  // 从接种史行点「上报 AEFI」带来的这一剂（P2-1499），进门先取走：下面取数失败也不留到下次从导航进来
  const aefiJump = vacAefiJump;
  vacAefiJump = null;
  // 机构清单只用来把临期面板的机构编号映射成名称（P2-1504）：取不到就回显编号，不为它把整页掀掉
  const [batches, cold, aefi, stats, orgs] = await Promise.all([
    api("/api/vaccine-supply/batches"), api("/api/vaccine-supply/cold-chain"),
    api("/api/vaccine-supply/aefi"), api("/api/vaccine-supply/stats"), api("/api/organizations").catch(() => []),
  ]);
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const a = stats.aefi, b = stats.batches;
  // 状态列带上封存原因（P2-1502）：封存框写着「封存原因会印在批次状态列上」，原先只印后端的「已封存」——召回与超温待评估
  // 分不清，旁边就是一点即发的「解除封存」。过期优先照旧（后端 unusable_reason 先判过期，过期的不带原因），原因为空照旧只写
  // 「已封存」。批次台账与临期面板共用这一句
  const batchStatus = (x) => (x.usable ? '<span class="tag ok">可用</span>'
    : esc(!x.expired && x.status === "frozen" && x.frozen_reason ? `${x.unusable_reason}：${x.frozen_reason}` : x.unusable_reason));
  // 冷链处置格：处置说明后面写处置人与处置时刻（P2-1503，截到分钟、显示时区随 P1-105，同质控失控处理 P2-1472）；
  // 加列之前处置的存量没记人和时刻，只写说明
  const coldHandled = (r) => esc(r.handle_note) + (r.handled_by || r.handled_at
    ? `<div style="font-size:12px">${esc(r.handled_by || "—")}${
      r.handled_at ? ` ${esc(r.handled_at.slice(0, 16).replace("T", " "))}` : ""} 处置</div>` : "");
  const drawExpiring = async (days) => {
    const r = await api(`/api/vaccine-supply/expiring?days=${encodeURIComponent(days)}`);
    // 机构列（P2-1504）：临期清单列的是全县的批次（端点收口属待裁定的 P1-49），原先不印机构——西镇的临期批次出现在东镇的
    // 页面上，看着像自家的。照 P2-1467 印机构名，映射不到（页面打开之后才建的机构）回显编号
    $("#vx-list").innerHTML =
      table(["机构", "疫苗", "批号", "厂家", "效期", "余量", "状态"], r.batches, (x) =>
        `<tr><td>${esc(orgNames[x.org_id] || x.org_id)}</td><td>${esc(x.vaccine_name)}</td><td>${esc(x.batch_no)}</td>
         <td>${esc(x.manufacturer) || "—"}</td>
         <td>${esc(x.expire_date)}${x.expired ? ' <span class="tag danger">已过期</span>' : ""}</td>
         <td>${x.remaining}/${x.quantity}</td>
         <td>${batchStatus(x)}</td></tr>`)
      // today 与 generated_at 都印出来：这张表的"临期"是相对**业务日期**算的，不是浏览器当下
      + `<p class="desc">共 ${r.batches.length} 个批次，口径日期 ${esc(r.today)}、
        未来 ${r.within_days} 天，生成于 ${esc((r.generated_at || "").slice(0, 19).replace("T", " "))}。
        ${r.batches.length >= 500 ? "<b>已截到 500 条</b>，请缩小天数再看。" : ""}</p>`;
    setMsg("#vx-msg", "", true);
  };
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
    ${panel("登记批次", `
      <form class="inline" id="vb-form">
        <input name="vaccine_code" placeholder="疫苗编码" required><input name="vaccine_name" placeholder="疫苗名称" required>
        <input name="batch_no" placeholder="批号" required><input name="manufacturer" placeholder="厂家">
        <input name="expire_date" placeholder="效期 YYYY-MM-DD" required><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="quantity" type="number" placeholder="数量" value="0"><button>登记</button></form>
      <p class="msg" id="vb-msg"></p>
      <div id="vb-list"></div>
      <div id="vb-recipients"></div>`)}
    ${panel("临期与过期批次", `
      <form class="inline" id="vx-form">
        <label style="font-size:13px">未来
          <input name="days" type="number" min="1" max="365" value="30" style="width:80px"> 天内到期</label>
        <button>查询</button></form>
      <p class="desc">上面那张卡片只给得出「30天内到期 N 个批次」（数的是批次、不是支数），具体是哪几个批号在这里看。
        <b>已过期的也一并列出并标注</b>——不是催人用掉，是提示尽快按报废流程处理，
        别让它躺在冰箱里被误用（后端 docstring 的原话）。
        <b>只列还有余量的批次</b>：发完的批次不删行，只累加已用量，列出来没有意义。</p>
      <p class="msg" id="vx-msg"></p>
      <div id="vx-list"></div>`)}
    ${panel("冷链录温", `
      <form class="inline" id="cc-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="device_name" placeholder="设备名称" required>
        <input name="temperature" type="number" step="0.1" placeholder="温度℃" required>
        <input name="min_allowed" type="number" step="0.1" value="2" style="min-width:70px"><input name="max_allowed" type="number" step="0.1" value="8" style="min-width:70px">
        <label style="font-size:13px">记录时间 <input name="recorded_at" type="datetime-local" required></label><button>录入</button></form>
      <p class="msg" id="cc-msg"></p>
      ${table(["记录时刻", "机构", "设备", "温度", "区间", "状态", "处置"], cold, (r) =>
        `<tr><td>${esc(r.recorded_at)}</td><td>${r.org_id}</td><td>${esc(r.device_name)}</td><td>${r.temperature}</td><td>${esc(r.range)}</td>` +
        `<td>${r.exceeded ? '<span class="tag danger">超温</span>' : "正常"}</td>` +
        `<td>${r.exceeded ? (r.handled ? coldHandled(r) : `<button class="btn sm" data-handle="${r.id}">处置</button>`) : "—"}</td></tr>`)}
    `)}
    ${panel("AEFI 报告", `
      <form class="inline" id="aefi-form">
        <input name="patient_id" id="aefi-pid" type="number" placeholder="患者ID" required>
        <select name="record_id" id="aefi-dose"><option value="">不关联</option></select>
        <input name="vaccine_code" placeholder="疫苗编码（未关联记录时必填）">
        <select name="reaction_type"><option value="general">一般反应</option><option value="severe">严重反应</option>
          <option value="psychogenic">心因性</option><option value="coincidental">偶合症</option></select>
        <input name="symptom" placeholder="症状" required><input name="onset_date" placeholder="发生日期" required>
        <input name="org_id" type="number" placeholder="机构ID" required><button class="btn danger">上报</button></form>
      <p class="desc">剂次从这位患者的接种记录里选（填好患者号即列出，也可从「疫苗接种」页接种史的「上报 AEFI」带过来）：
        关联了剂次，疫苗与批号按接种记录带出，按批号才查得到这一例。</p>
      <p class="msg" id="aefi-msg"></p>
      ${table(["患者", "疫苗", "批号", "类型", "症状", "发生日期", "转归"], aefi, (r) =>
        `<tr><td>${r.patient_id}</td><td>${esc(r.vaccine_code)}</td><td>${esc(r.batch_no || "—")}</td>` +
        `<td>${r.reaction_type === "severe" ? '<span class="tag danger">' + esc(r.reaction_type_name) + "</span>" : esc(r.reaction_type_name)}</td>` +
        `<td>${esc(r.symptom)}</td><td>${esc(r.onset_date)}</td>
         <td>${esc(r.outcome_name)}
           <button class="btn sm" data-aefioutcome="${r.id}">转归</button></td></tr>`)}
      <p class="desc">转归随访随时可改：上报当时多半只能填「未知」，
        好转、痊愈还是留有后遗症，是后续随访才知道的（后端 docstring 的原话）。
        <b>没有"改完就锁"这一说</b>——随访结论变了就再改一次。</p>
    `)}`;
  $("#vb-list").innerHTML = table(["疫苗", "批号", "厂家", "效期", "在库", "状态", "操作"], batches, (r) =>
    `<tr><td>${esc(r.vaccine_name)}</td><td>${esc(r.batch_no)}</td><td>${esc(r.manufacturer || "—")}</td>` +
    `<td>${esc(r.expire_date)}${r.expired ? ' <span class="tag danger">已过期</span>' : ""}</td>` +
    `<td>${r.remaining}/${r.quantity}</td>` +
    `<td>${batchStatus(r)}</td>` +
    `<td><button class="btn sm" data-recipients="${r.id}">受种者</button>` +
    (r.status === "frozen" ? `<button class="btn sm" data-unfreeze="${r.id}">解除封存</button>`
                           : `<button class="btn sm danger" data-freeze="${r.id}">封存</button>`) + "</td></tr>");
  $("#vb-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccine-supply/batches", formJson(e.target, ["org_id", "quantity"]), "#vb-msg"); };
  // 超温的提示要说出来（P2-1500）：录温回执里的 hint（「请核查该设备内疫苗批次并决定是否封存」）是超温时平台给的那一句
  // （模块口径 1：不自动封存，由人决定）；原先走 postAction，成功即整页重画、回执整个丢掉，录了 12℃ 页面上什么都没说。
  // 先重画再写（同 P2-1480 / P2-1432）；没超温的回执不带 hint，不写
  $("#cc-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/vaccine-supply/cold-chain", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["org_id", "temperature", "min_allowed", "max_allowed"])) });
      await route();
      if (r.hint) setMsg("#cc-msg", r.hint, false);
    } catch (err) { setMsg("#cc-msg", err.message, false); }
  };
  $("#aefi-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/vaccine-supply/aefi", formJson(e.target, ["patient_id", "record_id", "org_id"]), "#aefi-msg"); };
  // 剂次从这位患者的接种记录里选（P2-1499）：原先手填「接种记录ID」，而全站没有一处页面显示记录号。填好患者号（change 时取，
  // 不逐键取——每敲一位就调阅一位别人的接种史）按 /api/vaccination/records 取他的各剂次，首项「不关联」；取不到写进消息行、
  // 只剩「不关联」。连着改患者号只画最后一次的（P2-1012 同一种修法）
  let aefiDoseSeq = 0;
  const drawAefiDoses = async (patientId, recordId = "") => {
    const seq = ++aefiDoseSeq;
    const none = '<option value="">不关联</option>';
    $("#aefi-dose").innerHTML = none;
    setMsg("#aefi-msg", "");
    if (!patientId) return;
    let records;
    try { records = await api(`/api/vaccination/records?patient_id=${encodeURIComponent(patientId)}`); }
    catch (err) {
      if (seq === aefiDoseSeq) setMsg("#aefi-msg", err.message, false);
      return;
    }
    if (seq !== aefiDoseSeq) return;
    $("#aefi-dose").innerHTML = none + records.map((r) =>
      `<option value="${r.id}">${esc(r.vaccine_name)} 第${r.dose_no}剂 ${esc(r.vaccinated_date)} ` +
      `${r.batch_no ? `批号 ${esc(r.batch_no)}` : "无批号"}</option>`).join("");
    if (records.some((r) => String(r.id) === String(recordId))) $("#aefi-dose").value = String(recordId);
  };
  $("#aefi-pid").onchange = (e) => drawAefiDoses(e.target.value.trim());
  $("#vx-form").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawExpiring(Number(new FormData(e.target).get("days")) || 30); }
    catch (err) { setMsg("#vx-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.aefioutcome) {
      const row = aefi.find((x) => x.id === Number(d.aefioutcome));
      const picked = await spdModal(`转归随访（报告 ${d.aefioutcome}）`, [
        { name: "outcome", label: "转归", type: "select", value: row ? row.outcome : "unknown",
          options: Object.entries(AEFI_OUTCOMES).map(([k, v]) => ({ value: k, label: v })) },
      ]);
      if (!picked) return;
      return postAction(`/api/vaccine-supply/aefi/${d.aefioutcome}/outcome`,
        { outcome: picked.outcome }, "#aefi-msg", "PATCH");
    }
    if (d.freeze) {
      const picked = await spdModal("封存批次", [
        { name: "frozen_reason", label: "封存原因（会印在批次状态列上）", type: "text", value: "" },
      ]);
      if (!picked || !picked.frozen_reason) return;
      return postAction(`/api/vaccine-supply/batches/${d.freeze}/freeze`,
        { frozen_reason: picked.frozen_reason }, "#vb-msg");
    }
    if (d.unfreeze) return postAction(`/api/vaccine-supply/batches/${d.unfreeze}/unfreeze`, {}, "#vb-msg");
    if (d.handle) {
      // 框自己提交（P2-607）：处置说明写超了（后端 512 字）报错写在框里、框不关；原先留空就关框、什么也不发生，
      // 现在由后端说不能留空（后端本就必填）
      const done = await spdModal("超温处置", [
        { name: "handle_note", label: "处置说明（处置后这一格印的就是它）", type: "textarea", value: "" },
      ], { submit: (picked) => api(`/api/vaccine-supply/cold-chain/${d.handle}/handle`,
        { method: "POST", body: JSON.stringify({ handle_note: picked.handle_note }) }) });
      if (done) route();
      return;
    }
    if (d.recipients) {
      // 受种者整表列在页内（P2-1501）：原先 alert 只列最近 20 位——25 人次的批次，最早接种的 5 位只体现在「共接种 25 人次」
      // 这个数里，召回时界面上无从知道是谁，也不给记录号；药品「发给了谁」早就整表列出。接口最多回 1000 行（放开它要先解决
      // 这个端点的收口，P1-49），截断时标题写明「已列 N / 共 total」
      $("#vb-recipients").innerHTML = "";
      let r;   // 查失败要说出来（P2-378）：原先 api() 抛错没人接，点了没反应
      try { r = await api(`/api/vaccine-supply/batches/${d.recipients}/recipients`); }
      catch (err) { return setMsg("#vb-msg", err.message, false); }
      // total 是这一批的接种人次（同一人打两剂记两次，后端 docstring 原话），原先写成「名受种者」（P2-780）
      const listed = r.recipients.length;
      const title = `批号 ${r.batch_no}（${r.vaccine_name}）共接种 ${r.total} 人次`
        + (listed < r.total ? `，已列 ${listed} / 共 ${r.total}` : "");
      $("#vb-recipients").innerHTML = panel(title, table(["记录号", "受种者", "剂次", "接种日期"], r.recipients, (x) =>
        `<tr><td>${esc(x.record_id)}</td><td>${esc(x.patient_name) || "—"}（${esc(x.patient_id)}）</td>` +
        `<td>第${esc(x.dose_no)}剂</td><td>${esc(x.vaccinated_date)}</td></tr>`));
    }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawExpiring(30);
  // 从接种史行点「上报 AEFI」进来的（P2-1499）：患者号与这一剂预填好，表单滚到眼前
  if (aefiJump) {
    $("#aefi-pid").value = aefiJump.patient_id;
    await drawAefiDoses(aefiJump.patient_id, aefiJump.record_id);
    $("#aefi-form").scrollIntoView();
  }
}

async function renderSurveillance() {
  $("#page-desc").textContent = "症候群监测、病原监测、多点触发预警与应急资源保障";
  const [syndromes, pathogens, alerts, ready, orgs] = await Promise.all([
    api("/api/surveillance/syndromes"), api("/api/surveillance/pathogens"),
    api("/api/surveillance/alerts"), api("/api/surveillance/resources/readiness"), api("/api/organizations"),
  ]);
  const SYN = { fever: "发热", respiratory: "呼吸道", diarrhea: "腹泻", rash: "皮疹", jaundice: "黄疸", neuro: "脑炎脑膜炎" };
  // 机构名：症候群日报覆盖提示说盖掉的是哪家的（P2-1432）；多点预警、症候群日报、病原日报三张表的机构列印名称、不印编号
  // （P2-1437，同页保障情况表的做法），资源台账同样（P2-1467）。映射不到（页面打开之后才建的机构）回显编号
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  // ADR-0009 第六批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = `
    ${panel(`多点触发预警（近 ${alerts.window.days} 天）`, `
      <p class="hint">两路信号分列，不做综合评分：症候群异常查接诊，病原阳性抬头查实验室。</p>
      <h4>症候群达到阈值</h4>
      <p class="hint">口径：${esc(alerts.caliber.syndrome)}</p>
      ${table(["机构", "症候群", "例数", "阈值", "日期"], alerts.syndrome_alerts, (r) =>
        `<tr><td>${esc(orgNames[r.org_id] || r.org_id)}</td><td>${esc(r.syndrome_name)}</td><td><b>${r.case_count}</b></td><td>${r.threshold}</td><td>${esc(r.record_date)}</td></tr>`)}
      <h4>病原阳性率抬头</h4>
      ${table(["机构", "病原", "标本", "阳性/送检", "阳性率"], alerts.pathogen_alerts, (r) =>
        `<tr><td>${esc(orgNames[r.org_id] || r.org_id)}</td><td>${esc(r.pathogen_name)}</td><td>${esc(r.specimen_type || "—")}</td>` +
        `<td>${r.positive_count}/${r.tested_count}</td><td><b>${r.positive_rate_pct}%</b></td></tr>`)}
      <p class="hint">口径：${esc(alerts.caliber.pathogen)}</p>`)}
    ${panel("症候群日报", `
      <form class="inline" id="syn-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <select name="syndrome">${Object.entries(SYN).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <input name="case_count" type="number" placeholder="例数" required>
        <input name="threshold" type="number" min="0" placeholder="阈值（留空沿用上次，0=不预警）" title="阈值是本机构这个症候群的设定：留空沿用上次上报的，填 0 表示不预警（P1-182）">
        <input name="record_date" placeholder="日期 YYYY-MM-DD" required><button>上报</button></form>
      <p class="msg" id="syn-msg"></p>
      ${table(["机构", "症候群", "例数", "阈值", "日期", "预警"], syndromes.slice(0, 50), (r) =>
        `<tr><td>${esc(orgNames[r.org_id] || r.org_id)}</td><td>${esc(r.syndrome_name)}</td><td>${r.case_count}</td><td>${r.threshold || "不设"}</td>` +
        // 例数等于阈值就报，与接口口径「达到阈值即列出」同一个说法（P2-695）：原先写「超阈值」，8 例对阈值 8 也标超
        `<td>${esc(r.record_date)}</td><td>${r.alert ? '<span class="tag danger">达到阈值</span>' : "—"}</td></tr>`)}
    `)}
    ${panel("病原监测", `
      <form class="inline" id="pat-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="pathogen_name" placeholder="病原名称" required>
        <input name="specimen_type" placeholder="标本类型"><input name="tested_count" type="number" placeholder="送检数" required>
        <input name="positive_count" type="number" placeholder="阳性数" required>
        <input name="record_date" placeholder="日期 YYYY-MM-DD" required><button>上报</button></form>
      <p class="msg" id="pat-msg"></p>
      ${table(["机构", "病原", "标本", "阳性/送检", "阳性率", "日期"], pathogens.slice(0, 50), (r) =>
        `<tr><td>${esc(orgNames[r.org_id] || r.org_id)}</td><td>${esc(r.pathogen_name)}</td><td>${esc(r.specimen_type || "—")}</td>` +
        `<td>${r.positive_count}/${r.tested_count}</td><td>${r.positive_rate_pct === null ? "未送检" : r.positive_rate_pct + "%"}</td>` +
        `<td>${esc(r.record_date)}</td></tr>`)}
    `)}
    ${panel("应急资源保障", `
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
      <h3 style="margin-top:14px">资源台账</h3>
      <form class="inline" id="res-filter">
        <select name="resource_type"><option value="">全部类型</option><option value="material">应急物资</option>
          <option value="team">应急队伍</option><option value="equipment">应急装备</option></select>
        <label style="font-size:13px"><input type="checkbox" name="shortage_only" value="1"> 只看缺口与过期</label>
        <button>查询</button></form>
      <p class="desc">上面那张表按机构汇总，只说"哪家缺、缺什么"；要<b>补货、调下限、换效期</b>
        得在这张台账上逐条改。应急队伍没有效期（后端允许留空），所以效期一列的「—」
        对队伍是正常的，对物资才是没填。</p>
      <div id="res-list"></div>
    `)}`;
  const drawResources = async () => {
    const f = new FormData($("#res-filter"));
    const params = new URLSearchParams();
    if (f.get("resource_type")) params.set("resource_type", f.get("resource_type"));
    if (f.get("shortage_only")) params.set("shortage_only", "true");
    const rows = await api(`/api/surveillance/resources?${params}`);
    $("#res-list").innerHTML = table(
      ["机构", "类型", "名称", "数量/下限", "效期", "联系方式", "位置", "操作"], rows, (r) =>
      `<tr><td>${esc(orgNames[r.org_id] || r.org_id)}</td><td>${esc(r.resource_type_name)}</td><td>${esc(r.name)}</td>
       <td>${r.below_min ? `<span class="tag danger">${r.quantity}${esc(r.unit)}/${r.min_quantity}</span>`
         : `${r.quantity}${esc(r.unit)}/${r.min_quantity}`}</td>
       <td>${r.expire_date
         ? (r.expired ? `<span class="tag warn">${esc(r.expire_date)} 已过期</span>` : esc(r.expire_date))
         : "—"}</td>
       <td>${esc(r.contact) || "—"}</td><td>${esc(r.location) || "—"}</td>
       <td><button class="btn sm" data-resedit="${r.id}">编辑</button></td></tr>`)
      + `<p class="desc">共 ${rows.length} 条${rows.length >= 500 ? "——<b>已截到 500 条</b>，请按类型收窄" : ""}。</p>`;
  };
  // 盖掉了当日原上报要说出来（P2-1432）：同机构同症候群同日按覆盖（模块口径 2），回执的 overwritten 说覆盖没覆盖、
  // previous_case_count 说原来报的是几例。原先走 postAction，成功即重画、回执整个丢掉——发热门诊报的 8 例被儿科报的 5 例
  // 盖掉、预警跟着消失，页面上只是那一行悄悄变成 5。先重画再写回执（P2-1013）
  $("#syn-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/surveillance/syndromes", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["org_id", "case_count", "threshold"])) });
      await route();
      if (r.overwritten) {
        setMsg("#syn-msg", `已覆盖 ${r.record_date} ${orgNames[r.org_id] || `机构 ${r.org_id}`} 的「${r.syndrome_name}」原上报`
          + `（原 ${r.previous_case_count ?? "—"} 例）`, false);
      }
    } catch (err) { setMsg("#syn-msg", err.message, false); }
  };
  $("#pat-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/surveillance/pathogens", formJson(e.target, ["org_id", "tested_count", "positive_count"]), "#pat-msg"); };
  $("#res-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/surveillance/resources", formJson(e.target, ["org_id", "quantity", "min_quantity"]), "#res-msg"); };
  $("#res-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawResources(); } catch (err) { setMsg("#res-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { resedit } = e.target.dataset;
    if (!resedit) return;
    // 数量与下限**用 text 而不是 number**：spdModal 的 number 字段把空串折成 0，
    // 于是"留空不改"与"改成 0"变成同一个值——而下限 0 是合法且有意义的取值
    // （= 不设下限，后端 `bool(r.min_quantity)` 据此判 below_min）。用 text 才分得开。
    const picked = await spdModal(`编辑应急资源 ${resedit}`, [
      { name: "quantity", label: "数量（留空不改）", type: "text", value: "" },
      { name: "min_quantity", label: "储备下限（留空不改；填 0 表示不设下限）", type: "text", value: "" },
      { name: "expire_date", label: "效期 YYYY-MM-DD（留空不改；应急队伍本就没有效期）", type: "text", value: "" },
      { name: "contact", label: "联系方式（留空不改）", type: "text", value: "" },
      { name: "location", label: "存放位置（留空不改）", type: "text", value: "" },
    ]);
    if (!picked) return;
    // 后端 exclude_unset + `if value is not None`：留空的键不送
    const body = {};
    for (const k of ["quantity", "min_quantity"]) {
      if (picked[k] === "") continue;
      if (!/^\d+$/.test(picked[k])) return setMsg("#res-msg", `${k} 要填非负整数`, false);
      body[k] = Number(picked[k]);
    }
    if (picked.expire_date) body.expire_date = picked.expire_date;
    if (picked.contact) body.contact = picked.contact;
    if (picked.location) body.location = picked.location;
    if (!Object.keys(body).length) return setMsg("#res-msg", "五项都留空了，没有要改的", false);
    try {
      await api(`/api/surveillance/resources/${resedit}`, { method: "PATCH", body: JSON.stringify(body) });
      setMsg("#res-msg", "已更新", true);
      await drawResources();
    } catch (err) { setMsg("#res-msg", err.message, false); }
  };
  // 取数放最后：监听已与 innerHTML 同一同步块挂好，窗口为零（P2-31 根修，样板见 pages-spd.js renderSpdPath）
  await drawResources();
}


async function renderPathology() {
  $("#page-desc").textContent = "病理标本核收（含拒收）、取材制片阅片流转、冷缺血时间质控";
  // 还要流转的（待核收 / 已核收 / 已取材 / 已制片）单独取一遍、排在最前（P2-456，同 P2-408）：清单只回最新 500 个标本，
  // 挤出窗口的就没有一行能核收 / 拒收 / 推进
  // 标本表加申请单号、项目两列（P2-1367）：核收时要比对申请单（拒收原因里就有「申请单信息不符」），表上原先连申请单号都没有。
  // 病理申请单同一趟取一页：标本出参只带申请单号，项目按申请单号对上，不逐行请求；最新 500 张之外的对不上，印「—」
  const [recent, stats, requests, ...open] = await Promise.all([
    api("/api/pathology/specimens"), api("/api/pathology/specimen-stats"),
    api("/api/exams?center_type=pathology&limit=500"),
    ...["pending", "received", "embedded", "slided"].map((st) => api(`/api/pathology/specimens?status=${st}`)),
  ]);
  const specimens = actionableFirst(recent, ...open);
  const itemOf = Object.fromEntries(requests.map((r) => [r.id, r.item_name]));
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
    ${stats.rejected ? `<p class="desc">拒收原因：${Object.entries(stats.rejected_by_reason || {})
      .map(([reason, n]) => `${esc(reason)} ${n}`).join(" · ")}</p>` : ""}
    ${panel("标本送检登记", `
      <form class="inline" id="sp-form">
        <input name="request_id" type="number" placeholder="病理申请单ID" required>
        <input name="site" placeholder="送检部位">
        <label style="font-size:13px">离体时间 <input name="excised_at" type="datetime-local"></label>
        <label style="font-size:13px">固定时间 <input name="fixed_at" type="datetime-local"></label>
        <input name="fixative" placeholder="固定液"><button>登记</button></form>
      <p class="msg" id="sp-msg"></p>
      <p class="hint">${esc(stats.caliber)}</p>`)}
    ${panel("标本流转", `
      ${table(["标本号", "申请单号", "项目", "部位", "状态", "冷缺血", "蜡块/切片", "核收人", "操作"], specimens, (s) =>
        `<tr><td>${esc(s.specimen_no)}</td><td>${esc(s.request_id)}</td><td>${esc(itemOf[s.request_id] || "—")}</td>` +
        `<td>${esc(s.site || "—")}</td>` +
        `<td>${s.status === "rejected" ? '<span class="tag danger">' + esc(s.status_name) + "</span>" : esc(s.status_name)}` +
        `${s.reject_reason ? "<br><small>" + esc(s.reject_reason) + "</small>" : ""}</td>` +
        `<td>${s.cold_ischemia_minutes === null ? "未记录" : s.cold_ischemia_minutes + " 分"}</td>` +
        `<td>${s.block_count}/${s.slide_count}</td><td>${esc(s.received_by || "—")}</td>` +
        `<td>${s.status === "pending"
          ? `<button class="btn sm" data-receive="${s.id}">核收</button><button class="btn sm danger" data-reject="${s.id}">拒收</button>`
          : (s.status === "rejected" || s.status === "read" ? "—" : `<button class="btn sm" data-advance="${s.id}">推进</button>`)}</td></tr>`)}
    `)}`;
  // 登记成功回显申请单号与项目（P2-1367）：申请单号是手输的，原先走 postAction、成功即重画、什么都不说——敲错一位，标本就挂到
  // 别人的病理申请上（并按 P2-1239 印进对方的报告单）。申请单号取回执里的，项目按页面已取到的申请单对上，对不上的照实说
  $("#sp-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await api("/api/pathology/specimens", { method: "POST",
        body: JSON.stringify(formJson(e.target, ["request_id"])) });
      await route();   // 先重画再写回执（P2-1013）
      const item = itemOf[r.request_id];
      setMsg("#sp-msg", `已登记标本 ${r.specimen_no}：申请单 ${r.request_id} · `
        + `${item ? `项目 ${item}` : "项目未对上（本页取到的病理申请里没有这一张）"}，请与送检单核对`);
    } catch (err) { setMsg("#sp-msg", err.message, false); }
  };
  // P2-38：核收 / 拒收 / 推进三处原生弹窗换成页内表单。拒收原因后端只收五个标准项之一（否则 422），
  // 弹窗却让人手打——差一个字就被拒；现在从后端给的标准项里选。推进原先一律问"蜡块数或切片数"、
  // 点取消照样推进；现在按当前环节只问该环节的数（取材问蜡块、制片问切片、阅片不问），取消就是放弃。
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.receive) {
      const v = await spdModal("标本核收", [{ name: "received_by", label: "核收人姓名", required: true }]);
      if (!v) return;
      return postAction(`/api/pathology/specimens/${d.receive}/receive`, v, "#sp-msg");
    }
    if (d.reject) {
      const v = await spdModal("标本拒收", [{ name: "reject_reason", label: "拒收原因", type: "select",
        options: stats.reject_reason_options.map((r) => ({ value: r, label: r })) }]);
      if (!v) return;
      return postAction(`/api/pathology/specimens/${d.reject}/reject`, v, "#sp-msg");
    }
    if (d.advance) {
      const s = specimens.find((x) => x.id === Number(d.advance));
      const field = s && s.status === "received" ? { name: "block_count", label: "蜡块数（取材）" }
        : s && s.status === "embedded" ? { name: "slide_count", label: "切片数（制片）" } : null;
      const v = await spdModal(`推进标本 ${s ? s.specimen_no : d.advance}（当前：${s ? s.status_name : "—"}）`,
        field ? [{ ...field, type: "number", value: 0 }] : []);
      if (!v) return;
      return postAction(`/api/pathology/specimens/${d.advance}/advance`, v, "#sp-msg");
    }
  };
}

// 取值真源是 projects.py 的 PROJECT_STATUS（项目行的 status_name 由后端给，这份只给下拉用）
const PROJECT_STATUS_OPTS = [
  { value: "planning", label: "筹备" }, { value: "ongoing", label: "进行中" },
  { value: "done", label: "已完成" }, { value: "suspended", label: "已中止" },
];
const MS_STATUS = { done: ["已完成", "green"], overdue: ["逾期未完成", "red"], open: ["进行中", "orange"] };

async function renderProjects() {
  $("#page-desc").textContent = "行政协同项目管理：立项、里程碑（完成与撤销完成）、进度与逾期";
  // 逾期未结的单独取一遍、排在最前（P2-1441，同 P2-1310 的 actionableFirst）：清单接口只回最新 200 个项目，卡片「逾期未结 N」
  // 数的却是全量——逾期最久的老项目恰恰被后来立项的挤出窗口，卡片上算着、清单里找不到，「报进度」也够不着。接口的「只看逾期」
  // 早就在库里筛（P2-414），页面原先一处都没用
  const [recent, overdue, stats, orgs] = await Promise.all([
    api("/api/projects"), api("/api/projects?overdue_only=true"), api("/api/projects/stats/overview"),
    api("/api/organizations"),
  ]);
  const projects = actionableFirst(recent, overdue);
  // 清单列不全时标题写明（P2-1441）：卡片「项目总数」与清单是同一批项目的全量，列出的比它少就说清逾期的在前、其余只是最新的
  const listNote = projects.length < stats.total
    ? `（共 ${stats.total} 个：逾期未结 ${overdue.length} 个排在最前、其余只列最新 ${projects.length - overdue.length} 个）` : "";
  // 项目清单写明是哪家（P2-1438，同会计、成本两页）：清单是全县各家的项目，原先只有名称——同名的项目分不清是哪家的。
  // 项目行只给机构编号，机构名从机构清单取，映射不到回显编号
  const orgName = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
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
    ${panel("立项", `
      <form class="inline" id="pj-form">
        <input name="org_id" type="number" placeholder="机构ID" required><input name="name" placeholder="项目名称" required>
        <input name="category" placeholder="类别" value="general"><input name="owner_name" placeholder="负责人">
        <input name="start_date" placeholder="开始 YYYY-MM-DD"><input name="due_date" placeholder="计划完成 YYYY-MM-DD">
        <input name="budget_amount" type="number" step="0.01" placeholder="预算"><button>立项</button></form>
      <p class="msg" id="pj-msg"></p>
      <p class="hint">${esc(stats.caliber)}</p>`)}
    ${panel("项目清单" + listNote, `
      ${table(["名称", "机构", "负责人", "状态", "进度", "计划完成", "里程碑", "操作"], projects, (p) =>
        `<tr><td>${esc(p.name)}${p.overdue ? ' <span class="tag danger">逾期</span>' : ""}</td>` +
        `<td>${esc(orgName[p.org_id] || p.org_id)}</td>` +
        `<td>${esc(p.owner_name || "—")}</td><td>${esc(p.status_name)}</td>` +
        `<td>${p.progress_pct}%</td><td>${esc(p.due_date || "—")}</td>` +
        `<td>${p.milestone_done}/${p.milestone_total}` +
        `${p.milestone_overdue ? ' <span class="tag warn">' + p.milestone_overdue + " 逾期</span>" : ""}</td>` +
        `<td><button class="btn sm" data-progress="${p.id}">报进度</button>` +
        // 已完成 / 已中止的项目接口不收新里程碑（409），不给按钮（P2-597）；要加先把状态改回来
        (p.status === "done" || p.status === "suspended" ? "" : `<button class="btn sm" data-ms="${p.id}">加里程碑</button>`) +
        `</td></tr>`)}
    `)}
    ${panel("里程碑（全部项目）", `
      ${table(["项目", "里程碑", "到期日", "状态", "完成日", "操作"],
        projects.flatMap((p) => p.milestones.map((m) => ({ project: p.name, closed: p.status === "done" || p.status === "suspended", m }))),
        ({ project, closed, m }) =>
        `<tr><td>${esc(project)}</td><td>${esc(m.name)}</td><td>${esc(m.due_date) || "—"}</td>
         <td>${statusTag(MS_STATUS, m.done ? "done" : m.overdue ? "overdue" : "open")}</td>
         <td>${esc(m.done_date) || "—"}</td>
         <td>${closed ? "—"   // 已完成 / 已中止的项目接口不收完成 / 撤销完成（P2-1223，与加里程碑同一句 409），不给按钮
           : m.done
           ? `<button class="btn secondary" data-msreopen="${m.id}">撤销完成</button>`
           : `<button class="btn" data-msdone="${m.id}">完成</button>`}</td></tr>`)}
      <p class="desc">逾期是<b>现算</b>的：已完成的不算逾期，没填到期日的也不算。
        完成日留空按业务日期记。<b>撤销完成会把完成日一并清掉</b>——
        误点了要能改回来，凡是拦得住的都要放得开（后端 reopen 那条 docstring 的原话）。
        已完成 / 已中止的项目不给「完成」「撤销完成」：要改先用「报进度」把状态改回「进行中」。</p>`)}`;
  $("#pj-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/projects", formJson(e.target, ["org_id", "budget_amount"]), "#pj-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.progress) {
      const p = projects.find((x) => x.id === Number(d.progress));
      const picked = await spdModal(`报进度：${p ? p.name : d.progress}`, [
        { name: "progress_pct", label: "当前进度（0-100）", type: "number", value: p ? p.progress_pct : 0 },
        { name: "status", label: "状态", type: "select", value: p ? p.status : "ongoing",
          options: PROJECT_STATUS_OPTS },
      ]);
      if (!picked) return;
      // 只送改过的列（P2-969）：原先两列都按载入值预填、恒送——两列都送时后端不设条件，别人刚结项（完成、100%），旧页面上报个
      // 进度 85 就把项目改回「进行中 85%」，P2-416 那道「只改一列时按另一列判」从页面上永远不生效
      const body = {};
      if (!p || String(picked.progress_pct) !== String(p.progress_pct)) body.progress_pct = picked.progress_pct;
      if (!p || picked.status !== p.status) body.status = picked.status;
      return postAction(`/api/projects/${d.progress}`, body, "#pj-msg", "PATCH");
    }
    if (d.ms) {
      const picked = await spdModal("新增里程碑", [
        { name: "name", label: "里程碑名称", type: "text" },
        { name: "due_date", label: "到期日 YYYY-MM-DD（留空=不设到期日，也就永远不算逾期）", type: "text" },
      ]);
      if (!picked || !picked.name) return;
      return postAction(`/api/projects/${d.ms}/milestones`,
        { name: picked.name, due_date: picked.due_date }, "#pj-msg");
    }
    if (d.msdone) {
      const picked = await spdModal(`完成里程碑 ${d.msdone}`, [
        { name: "done_date", label: "完成日 YYYY-MM-DD（留空按业务日期记）", type: "text" },
      ]);
      if (!picked) return;
      // 两条路径分开写而不是拼查询串：地址本身要让孤儿闸门按字面看得见
      const date = (picked.done_date || "").trim();
      try {   // 登记失败要说出来（P2-378）：原先 api() 抛错没人接（日期写错 422、已完成 409），点了没反应
        if (date) {
          await api(`/api/projects/milestones/${d.msdone}/done?done_date=${encodeURIComponent(date)}`, { method: "POST" });
        } else {
          await api(`/api/projects/milestones/${d.msdone}/done`, { method: "POST" });
        }
      } catch (err) { return setMsg("#pj-msg", err.message, false); }
      return route();
    }
    if (d.msreopen) {
      if (!confirm("撤销完成会把完成日一并清掉，确认？")) return;
      try {   // 撤销失败同样要说出来（P2-423）：原先 api() 抛错没人接（别人刚撤销过 409、无权 403），点了没反应
        await api(`/api/projects/milestones/${d.msreopen}/reopen`, { method: "POST" });
      } catch (err) { return setMsg("#pj-msg", err.message, false); }
      return route();
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
  const canSim = ["doctor", "director", "admin"].includes(currentRole());   // 与建病例接口同一权限
  // ADR-0009 第五批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 医案库（P2-1408）：四诊摘要、按语改成多行文本框；医案行可「展开」看就诊日期、传承人、四诊摘要、按语（renderCaseTable）——
  // 原先录得进、页面上哪儿都看不见；检索加老师、病名、证型三个筛选——接口一直收 master_name / disease / syndrome，原先只送
  // keyword（只搜处方、按语、标题），搜「痹证」「陈老」都是空表
  $("#page-body").innerHTML = `
    ${panel("传承概览", `
      ${table(["名老中医", "医案数", "已发布", "涉及病种", "传承人"], stats.masters, (m) =>
        `<tr><td>${esc(m.master_name)}</td><td>${m.total}</td><td>${m.published}</td>` +
        `<td>${esc(m.diseases.join("、") || "—")}</td><td>${esc(m.successors.join("、") || "—")}</td></tr>`)}
    `)}
    ${panel("医案录入", `
      <form id="mc-form">
        <div class="inline">
          <input name="master_name" placeholder="名老中医" required><input name="successor_name" placeholder="传承人">
          <input name="title" placeholder="医案标题" required><input name="disease" placeholder="病名">
          <input name="syndrome" placeholder="证型"><input name="visit_date" placeholder="就诊日期 YYYY-MM-DD"></div>
        <div class="inline">
          <textarea name="four_exams" rows="3" placeholder="四诊摘要" style="min-width:280px;vertical-align:top"></textarea>
          <input name="treatment_method" placeholder="治法"><input name="prescription" placeholder="处方" style="min-width:220px">
          <textarea name="commentary" rows="3" placeholder="按语" style="min-width:280px;vertical-align:top"></textarea>
          <button>保存草稿</button></div></form>
      <p class="msg" id="mc-msg"></p>`)}
    ${panel("医案库", `
      <form class="inline" id="mc-search">
        <input name="master_name" placeholder="老师"><input name="disease" placeholder="病名"><input name="syndrome" placeholder="证型">
        <input name="keyword" placeholder="搜方药/按语/标题"><button>检索</button></form>
      <div id="mc-list">${renderCaseTable(cases)}</div>`)}
    ${panel("模拟诊疗病例", `
      ${canSim ? `<details id="sim-new"><summary>新建模拟病例</summary>
        <form id="sim-new-form" style="margin-top:8px">
          <div class="inline">
            <input name="title" placeholder="病例标题" required style="min-width:240px">
            <select name="category"><option value="clinical">临床</option>
              <option value="tcm">中医药适宜技术</option><option value="emergency">急救</option></select>
            <input name="pass_score" type="number" min="1" max="100" value="60" style="width:90px"
              title="及格分按百分制：得分 = 答对的分值 ÷ 满分 × 100"> <span class="desc">及格分（百分制）</span></div>
          <div class="inline"><input name="scenario" placeholder="情境：主诉、现病史、查体……" style="min-width:520px"></div>
          <div id="sim-points"></div>
          <div class="inline"><button type="button" class="btn secondary" id="sim-add-point">加一个决策点</button>
            <button>保存病例</button></div>
        </form>
        <p class="desc">每个决策点：问题、选项（用 / 分隔，至少两个）、正确答案（从选项里选）、分值、解析（只给答错的人看）；
          没填问题的空行不提交。</p>
        <p class="msg" id="sim-new-msg"></p></details>` : ""}
      ${table(["标题", "类别", "决策点", "满分", "及格分（百分制）", "状态", "操作"], sims, (s) =>
        `<tr><td>${esc(s.title)}</td><td>${esc(s.category_name)}</td><td>${s.decision_points.length}</td>` +
        `<td>${s.total_score}</td><td>${s.pass_score}</td>` +
        `<td>${s.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>` +
        `<td>${s.active && s.decision_points.length
          ? `<button class="btn sm" data-simdo="${s.id}">作答</button>` : "—"}</td></tr>`)}
      <p class="hint">列表<b>刻意不含正确答案</b>——答案与解析只在交卷回执里给，
        所以这张表不能拿来对答案。停用的病例不摆「作答」：后端直接回 409。</p>
      <div id="sim-box"></div>`)}`;
  $("#mc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/tcm-heritage/master-cases", formJson(e.target, []), "#mc-msg"); };
  // 模拟病例原先只能作答、不能新建（P2-93 动词级孤儿）：建病例的接口一直在，新装的平台上这张表一条都没有
  const simForm = $("#sim-new-form");
  if (simForm) {
    const splitOptions = (text) => text.split("/").map((x) => x.trim()).filter(Boolean);
    const addPointRow = () => {
      const row = document.createElement("div");
      row.className = "inline sim-point";
      row.innerHTML = `<input class="p-question" placeholder="决策点：问题" style="min-width:220px">
        <input class="p-options" placeholder="选项，用 / 分隔" style="min-width:200px">
        <select class="p-answer"><option value="">正确答案（先填选项）</option></select>
        <input class="p-score" type="number" min="1" max="100" value="10" style="width:70px" title="分值">
        <input class="p-explain" placeholder="解析（答错时给）" style="min-width:200px">`;
      const options = row.querySelector(".p-options"), answer = row.querySelector(".p-answer");
      // 正确答案只能从选项里挑：手填的答案差一个字就是「正确答案不在选项里」（后端 422）
      options.oninput = () => {
        const keep = answer.value, list = splitOptions(options.value);
        answer.innerHTML = `<option value="">正确答案</option>` +
          list.map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("");
        if (list.includes(keep)) answer.value = keep;
      };
      $("#sim-points").appendChild(row);
    };
    addPointRow(); addPointRow();
    $("#sim-add-point").onclick = addPointRow;
    simForm.onsubmit = (e) => {
      e.preventDefault();
      const body = formJson(e.target, ["pass_score"]);
      const rows = [...document.querySelectorAll(".sim-point")].filter((r) => r.querySelector(".p-question").value.trim());
      body.decision_points = rows.map((r, i) => ({
        key: `p${i + 1}`,
        question: r.querySelector(".p-question").value.trim(),
        options: splitOptions(r.querySelector(".p-options").value),
        answer: r.querySelector(".p-answer").value,
        score: Number(r.querySelector(".p-score").value || 10),
        explain: r.querySelector(".p-explain").value.trim(),
      }));
      return postAction("/api/tcm-heritage/simulations", body, "#sim-new-msg");
    };
  }
  $("#mc-search").onsubmit = async (e) => {
    e.preventDefault();
    // 老师 / 病名 / 证型 / 关键词照接口的参数名送，留空的不送（P2-1408）
    const query = new URLSearchParams([["include_draft", "true"],
      ...[...new FormData(e.target).entries()].filter(([, v]) => v !== "")]);
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着
    $("#mc-list").innerHTML = "";
    try {
      const rows = await api(`/api/tcm-heritage/master-cases?${query}`);
      $("#mc-list").innerHTML = renderCaseTable(rows);
    } catch (err) {
      $("#mc-list").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
    }
  };
  $("#mc-list").onclick = (e) => {
    const d = e.target.dataset;
    if (d.mcopen) {
      // 展开 / 收起这一条的就诊日期、传承人、四诊摘要、按语（P2-1408）：展开行紧跟在这一行后面（renderCaseTable）
      const detail = e.target.closest("tr").nextElementSibling;
      if (!detail || detail.dataset.mcdetail !== d.mcopen) return;
      detail.classList.toggle("hidden");
      e.target.textContent = detail.classList.contains("hidden") ? "展开" : "收起";
      return;
    }
    if (d.publish) return postAction(`/api/tcm-heritage/master-cases/${d.publish}/publish`, {}, "#mc-msg");
    if (d.unpublish) return postAction(`/api/tcm-heritage/master-cases/${d.unpublish}/unpublish`, {}, "#mc-msg");
  };
  // 本人在这个病例上的历次作答（P2-479）：交卷后写着「练几次、进步多少都查得到」，原先页面上哪儿都查不到
  const drawSimHistory = async (sim) => {
    const box = $("#sim-history");
    if (!box) return;
    try {
      const r = await api(`/api/tcm-heritage/simulations/${sim.id}/attempts?mine=true`);
      const best = r.attempts.length ? Math.max(...r.attempts.map((a) => a.score)) : null;
      box.innerHTML = `<h3 style="margin-top:14px">我的作答记录（${r.attempts.length} 次${
        best === null ? "" : `，最高 ${best} 分`}）</h3>
        ${table(["第几次", "交卷时间", "得分", "结果"], r.attempts, (a) =>
          `<tr><td>${esc(a.attempt_no)}</td><td>${esc((a.created_at || "").replace("T", " ").slice(0, 16))}</td>
           <td>${esc(a.score)}</td><td>${a.passed ? '<span class="tag green">通过</span>' : '<span class="tag red">未通过</span>'}</td></tr>`)}
        <p class="desc">${esc(r.caliber)}</p>`;
    } catch (err) {
      box.innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
    }
  };
  const drawSim = (sim) => {
    $("#sim-box").innerHTML = `<h3 style="margin-top:14px">${esc(sim.title)}</h3>
      <p class="desc">${esc(sim.scenario)}</p>
      <form id="sim-form">
        ${sim.decision_points.map((p, i) => `<div style="margin:10px 0">
          <div style="font-size:13.5px"><b>${i + 1}. ${esc(p.question)}</b>（${p.score} 分）</div>
          ${p.options.map((o) => `<label style="display:block;font-size:13px;margin:2px 0 2px 12px">
            <input type="radio" name="${esc(p.key)}" value="${esc(o)}"> ${esc(o)}</label>`).join("")}
        </div>`).join("")}
        <div class="inline"><button>交卷</button>
          <button type="button" class="btn secondary" data-simclose="1">关闭</button></div></form>
      <p class="msg" id="sim-msg"></p><div id="sim-result"></div><div id="sim-history"></div>`;
    drawSimHistory(sim);
    $("#sim-form").onsubmit = async (e) => {
      e.preventDefault();
      const f = new FormData(e.target);
      // 没选的决策点**不送这个键**：后端 `body.answers.get(p["key"], "")` 会折成"未作答"，
      // 送一个空串是同样的结果，但少送更贴近"这题跳过了"的本意
      const answers = {};
      sim.decision_points.forEach((p) => { const v = f.get(p.key); if (v) answers[p.key] = v; });
      try {
        const r = await api(`/api/tcm-heritage/simulations/${sim.id}/attempts`,
          { method: "POST", body: JSON.stringify({ answers }) });
        $("#sim-result").innerHTML = `
          <div class="cards">
            <div class="card"><div class="label">得分</div><div class="value">${r.score}</div></div>
            <div class="card"><div class="label">及格线</div><div class="value">${r.pass_score}</div></div>
            <div class="card"><div class="label">结果</div>
              <div class="value${r.passed ? "" : " warn"}">${r.passed ? "通过" : "未通过"}</div></div>
          </div>
          ${table(["决策点", "你的选择", "对错", "正确答案", "解析"], r.detail, (d) =>
            `<tr><td>${esc(d.question)}</td><td>${esc(d.chosen)}</td>
             <td>${d.correct ? '<span class="tag green">对</span>' : '<span class="tag red">错</span>'}</td>
             <td>${esc(d.answer)}</td><td style="font-size:12px">${esc(d.explain) || "—"}</td></tr>`)}
          <p class="desc"><b>只有答错的才给解析</b>（后端的原话：答对的人不需要，堆一屏解析反而没人看），
            所以解析列的「—」意味着这题答对了，不是"没写解析"。
            每次交卷都会留一条尝试记录，分数不覆盖——练几次、进步多少都查得到（见下方「我的作答记录」）。</p>`;
        setMsg("#sim-msg", "", true);
        drawSimHistory(sim);
      } catch (err) { setMsg("#sim-msg", err.message, false); }
    };
  };
  $("#page-body").onclick = (e) => {
    const { simdo, simclose } = e.target.dataset;
    if (simclose) return ($("#sim-box").innerHTML = "");
    if (!simdo) return;
    const sim = sims.find((x) => x.id === Number(simdo));
    if (sim) drawSim(sim);
  };
}

function renderCaseTable(rows) {
  return table(["名老中医", "标题", "病/证", "治法", "处方", "状态", "操作"], rows, (c) =>
    `<tr><td>${esc(c.master_name)}</td><td>${esc(c.title)}</td>` +
    `<td>${esc(c.disease || "—")} / ${esc(c.syndrome || "—")}</td><td>${esc(c.treatment_method || "—")}</td>` +
    `<td>${esc(c.prescription || "—")}</td>` +
    `<td>${c.published ? '<span class="tag ok">已发布</span>' : "草稿"}</td>` +
    `<td><button class="btn secondary" data-mcopen="${c.id}">展开</button> ` +
    `${c.published ? `<button class="btn sm" data-unpublish="${c.id}">撤回</button>`
                   : `<button class="btn sm" data-publish="${c.id}">发布</button>`}</td></tr>` +
    `<tr class="hidden" data-mcdetail="${c.id}"><td colspan="7">${masterCaseDetail(c)}</td></tr>`);
}

/* 医案行展开后的内容（P2-1408）：就诊日期、传承人、四诊摘要、按语——出参一直带着，原先清单行只列名老中医 / 标题 / 病证 /
   治法 / 处方：按按语里的词检索命中了，处方列写的是另一回事，看不出为什么命中、也读不到那段按语。一律 esc()；多行照录入时的
   换行显示，没填的写 —。 */
function masterCaseDetail(c) {
  const text = (v) => `<span style="white-space:pre-wrap">${esc(v) || "—"}</span>`;
  return `<div style="font-size:13px;line-height:1.7">` +
    `<div>就诊日期：${text(c.visit_date)}　传承人：${text(c.successor_name)}</div>` +
    `<div><b>四诊摘要</b>：${text(c.four_exams)}</div>` +
    `<div><b>按语</b>：${text(c.commentary)}</div></div>`;
}


async function renderResources() {
  $("#page-desc").textContent = "通用资源登记（登记→发布→撤回）、五类资源统一视图、号源与手术间排程撮合";
  const [resources, catalog, slotMatch] = await Promise.all([
    api("/api/resources"), api("/api/resources/catalog"), api("/api/resources/match/slots"),
  ]);
  // 统一资源视图每类各列前 20 行（P2-1507）：原先 `catalog.items.slice(0, 100)`——接口按号源、检查资源、手术间、血制品、通用
  // 资源的次序拼、每类最多 500 行，号源排最前：一个门诊每天 8 个时段 × 14 天就是 112 个号源，表里 100 行全是号源，计数卡写着
  // 「手术间 2/2」「通用资源 1/1」，表里一行都没有、也不说截断。接口 docstring 定的是「五类资源一处看全」，每类都得露面；合计
  // 仍不超过原先的 100 行，没列全的类别写明共几个、列了几个（计数卡照旧取全量的 by_kind），某一类要看全到它自己的模块
  const CATALOG_PER_KIND = 20;
  const catalogRows = [];
  const catalogCut = [];
  for (const [kind, stat] of Object.entries(catalog.by_kind)) {
    const shown = catalog.items.filter((i) => i.kind === kind).slice(0, CATALOG_PER_KIND);
    catalogRows.push(...shown);
    if (stat.total > shown.length) catalogCut.push(`${stat.name}共 ${stat.total} 个，列前 ${shown.length} 个`);
  }
  $("#page-body").innerHTML = `
    <div class="cards">
      ${Object.values(catalog.by_kind).map((v) =>
        `<div class="card"><div class="label">${esc(v.name)}</div>` +
        `<div class="value">${v.usable}/${v.total}</div></div>`).join("")}
    </div>
    ${panel("通用资源登记", `
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
        `<td><button class="btn sm secondary" data-rsedit="${r.id}">编辑</button>` +
        (r.status === "published" ? `<button class="btn sm danger" data-withdraw="${r.id}">撤回</button>`
                                  : `<button class="btn sm" data-publish="${r.id}">发布</button>`) + "</td></tr>")}
      <p class="desc">编辑改的是名称 / 容量 / 单位 / 位置 / 联系方式 / 备注这几项，
        <b>编码与资源类型建后不可改</b>（后端那个入参模型里就没有这两个键）——
        它们是这条资源的身份，改了等于换了一个东西。
        留空的字段不进 PATCH body：只想改位置的那一次，不该把备注清掉。</p>
    `)}
    ${panel("统一资源视图", `
      <p class="hint">${esc(catalog.caliber)}</p>
      ${table(["类别", "名称", "详情", "可用量", "状态"], catalogRows, (i) =>
        `<tr><td>${esc(i.kind_name)}</td><td>${esc(i.name)}</td><td>${esc(i.detail || "—")}</td>` +
        `<td>${i.available === null ? "—" : i.available + esc(i.unit)}</td>` +
        `<td>${i.usable ? '<span class="tag ok">可用</span>' : "不可用"}</td></tr>`)}
      ${catalogCut.length ? `<p class="desc">每类最多列前 ${CATALOG_PER_KIND} 行，以下几类没列全：${esc(catalogCut.join("；"))}。
        完整清单请到各自的模块查询。</p>` : ""}
    `)}
    ${panel("号源撮合（未来 14 天）", `
      <p class="hint">${esc(slotMatch.caliber)}</p>
      ${table(["机构", "最早可约", "余量合计", "近期号源"], slotMatch.candidates, (c) =>
        `<tr><td>${esc(c.org_name || c.org_id)}</td><td>${esc(c.earliest)}</td><td>${c.remaining_total}</td>` +
        `<td>${c.slots.map((s) => esc(`${s.slot_date} ${s.slot_time} ${s.resource_name}(余${s.remaining})`)).join("；")}</td></tr>`)}
    `)}
    ${panel("手术间撮合", `
      <form class="inline" id="or-form">
        <input name="org_id" type="number" placeholder="机构ID" required>
        <input name="scheduled_date" placeholder="日期 YYYY-MM-DD">
        <input name="start_time" value="08:00" style="min-width:80px"><input name="end_time" value="18:00" style="min-width:80px">
        <button>查空档</button></form>
      <div id="or-result"></div>`)}`;
  $("#rs-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/resources", formJson(e.target, ["org_id", "capacity"]), "#rs-msg"); };
  $("#or-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const qs = new URLSearchParams([...f.entries()].filter(([, v]) => v)).toString();
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着
    $("#or-result").innerHTML = "";
    let r;
    try {
      r = await api(`/api/resources/match/or-rooms?${qs}`);
    } catch (err) {
      $("#or-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    $("#or-result").innerHTML = table(["手术间", "该窗口", "冲突时段", "空档"], r.rooms, (x) =>
      `<tr><td>${esc(x.room_name)}</td>` +
      `<td>${x.available ? '<span class="tag ok">可用</span>' : '<span class="tag danger">有冲突</span>'}</td>` +
      `<td>${x.conflicts.map((c) => esc(`${c.start_time}-${c.end_time}`)).join("、") || "—"}</td>` +
      `<td>${x.gaps.map((g) => esc(`${g.start_time}-${g.end_time}`)).join("、") || "无"}</td></tr>`);
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    if (d.publish) return postAction(`/api/resources/${d.publish}/publish`, {}, "#rs-msg");
    if (d.rsedit) {
      const r = resources.find((x) => x.id === Number(d.rsedit));
      // 框自己提交（P2-607）：名称只填了空格、备注写超了（后端 512 字）、六项都留空，报错写在框里、框不关，改好的几项不用重填。
      // 单位一项是 P2-1508 补的：后端改档入参原先没有 unit，建档时误填的单位改不了，框里也没有这一项
      const done = await spdModal(`编辑资源 ${r ? r.code : d.rsedit}`, [
        { name: "name", label: "名称（留空不改）", type: "text", value: r ? r.name : "" },
        { name: "capacity", label: "容量（留空不改，最小 1）", type: "number", value: r ? r.capacity : 1 },
        { name: "unit", label: "单位（留空不改）", type: "text", value: r ? r.unit || "" : "" },
        { name: "location", label: "位置（留空不改）", type: "text", value: r ? r.location || "" : "" },
        { name: "contact", label: "联系方式（留空不改）", type: "text", value: r ? r.contact || "" : "" },
        { name: "note", label: "备注（留空不改）", type: "textarea", value: r ? r.note || "" : "" },
      ], { submit: (picked) => {
        // 后端 exclude_unset + `if value is not None`：留空的键不送，免得把备注清空
        const body = {};
        if (picked.name) body.name = picked.name;
        if (picked.capacity) body.capacity = picked.capacity;
        if (picked.unit) body.unit = picked.unit;
        if (picked.location) body.location = picked.location;
        if (picked.contact) body.contact = picked.contact;
        if (picked.note) body.note = picked.note;
        if (!Object.keys(body).length) throw new Error("六项都留空了，没有要改的");
        return api(`/api/resources/${d.rsedit}`, { method: "PATCH", body: JSON.stringify(body) });
      } });
      if (done) route();
      return;
    }
    if (d.withdraw) {
      const picked = await spdModal("撤回资源", [
        { name: "reason", label: "撤回理由（会印在资源状态列上）", type: "text", value: "" },
      ]);
      if (!picked || !picked.reason) return;
      return postAction(`/api/resources/${d.withdraw}/withdraw`, { reason: picked.reason }, "#rs-msg");
    }
  };
}


async function renderRbac() {
  $("#page-desc").textContent = "内置六角色（代码声明，不可删停）与自定义角色的权限点授权";
  const [roleRows, modules] = await Promise.all([api("/api/rbac/roles"), api("/api/rbac/modules")]);
  let roles = roleRows;
  let viewing = "";
  const MODULE_OPTIONS = modules.map((m) => ({ value: m.module, label: `${m.module}（${m.permission_count}）` }));
  $("#page-body").innerHTML = `
    ${panel("角色", `
      <p class="hint">内置角色的权限来自代码内 require_roles 声明，不走授权表；自定义角色按权限点授权。</p>
      <form class="inline" id="role-form">
        <input name="key" placeholder="角色 key（小写字母数字下划线）" required>
        <input name="name" placeholder="角色名称" required><input name="description" placeholder="说明">
        <button>新建自定义角色</button></form>
      <p class="msg" id="role-msg"></p>
      <div id="rbac-roles"></div>
    `)}
    ${panel(`权限点模块（共 ${modules.reduce((a, m) => a + m.permission_count, 0)} 个写接口权限点）`, `
      <p class="hint">权限点由平台启动时从路由表自动登记，不手工维护——手工清单与真实接口的偏差最难查。</p>
      ${table(["模块", "权限点数"], modules, (m) =>
        `<tr><td>${esc(m.module)}</td><td>${m.permission_count}</td></tr>`)}
    `)}
    ${panel("权限点清单", `
      <p class="hint">上面那张表只给得出「这个模块有多少个」，具体是哪些接口在这里查。
        <b>只登记写接口</b>（POST/PUT/PATCH/DELETE）——读接口不进这套授权表，别在这儿找 GET。</p>
      <form class="inline" id="perm-filter">
        <select name="module"><option value="">全部模块</option>${
          modules.map((m) => `<option value="${esc(m.module)}">${esc(m.module)}</option>`).join("")}</select>
        <input name="keyword" placeholder="路径关键词（如 /orders）" style="min-width:200px">
        <button>查询</button></form>
      <p class="msg" id="perm-msg"></p>
      <div id="rbac-perms"></div>
    `)}
    ${panel("角色权限明细", `<div id="rbac-detail"><p class="empty">点上方「查看」</p></div>`)}`;
  const drawRoles = () => {
    $("#rbac-roles").innerHTML = table(["key", "名称", "类型", "权限点", "状态", "操作"], roles, (r) =>
      `<tr><td>${esc(r.key)}</td><td>${esc(r.name)}</td>` +
      `<td>${r.builtin ? '<span class="tag">内置</span>' : "自定义"}</td>` +
      `<td>${r.builtin ? esc(r.permission_source) : r.permission_count}</td>` +
      `<td>${r.active ? "启用" : "停用"}</td>` +
      `<td><button class="btn sm" data-view="${r.id}">查看</button>` +
      (r.builtin ? "" : `<button class="btn sm" data-grant="${r.id}">授权</button>` +
        `<button class="btn sm danger" data-del="${r.id}">删除</button>`) + `</td></tr>`);
  };
  const refreshRoles = async () => { roles = await api("/api/rbac/roles"); drawRoles(); };
  const drawDetail = async (roleId) => {
    viewing = String(roleId);
    const r = await api(`/api/rbac/roles/${encodeURIComponent(roleId)}/permissions`);
    $("#rbac-detail").innerHTML =
      `<p>${esc(r.role.name)}（${esc(r.role.key)}）共 ${r.permissions.length} 个权限点</p>`
      // note 是条件键，只有内置角色才有——它解释的正是"为什么这里是空的"
      + (r.note ? `<p class="hint">${esc(r.note)}</p>` : "")
      + table(["模块", "方法", "路径", "操作"], r.permissions, (p) =>
        `<tr><td>${esc(p.module)}</td><td><span class="tag">${esc(p.method)}</span></td><td>${esc(p.path)}</td>
         <td>${r.role.builtin ? "—"
           : `<button class="btn sm danger" data-revoke="${p.id}" data-rid="${r.role.id}">撤销</button>`}</td></tr>`);
  };
  const drawPerms = async () => {
    const f = new FormData($("#perm-filter"));
    const params = new URLSearchParams();
    if (f.get("module")) params.set("module", f.get("module"));
    if (f.get("keyword")) params.set("keyword", f.get("keyword"));
    const rows = await api(`/api/rbac/permissions?${params}`);
    $("#rbac-perms").innerHTML =
      table(["ID", "方法", "路径", "模块", "代码声明的角色", "操作"], rows, (p) =>
        `<tr><td>${p.id}</td><td><span class="tag">${esc(p.method)}</span></td><td>${esc(p.path)}</td>
         <td>${esc(p.module)}</td><td style="font-size:12px">${esc(p.builtin_roles) || "—"}</td>
         <td><button class="btn sm" data-grantone="${p.id}">授给角色</button></td></tr>`)
      // 后端这条是 limit(1000) 而不是 paginate：到顶了要说出来，不能让人以为"就这些"
      + `<p class="desc">共 ${rows.length} 条${rows.length >= 1000
        ? "——<b>已截到 1000 条</b>，请按模块或关键词收窄再看" : ""}。
        「代码声明的角色」是该接口 require_roles 里写的那几个，授权时的「复制内置角色」按的就是这一列。</p>`;
  };
  const grant = async (roleId, body, msgSel = "#role-msg") => {
    const res = await api(`/api/rbac/roles/${encodeURIComponent(roleId)}/permissions`,
      { method: "POST", body: JSON.stringify(body) });
    setMsg(msgSel, `授权完成：新增 ${res.granted} 个、原本已有 ${res.already_had} 个，该角色现共 ${res.total} 个权限点`
      + (res.unknown_permission_ids.length ? `；下列 id 不存在，已单列而非静默忽略：${res.unknown_permission_ids.join("、")}` : ""),
      true);
    await refreshRoles();
    if (viewing === String(roleId)) await drawDetail(roleId);
  };
  drawRoles();
  $("#role-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/rbac/roles", formJson(e.target, []), "#role-msg"); };
  $("#perm-filter").onsubmit = async (e) => {
    e.preventDefault();
    try { await drawPerms(); } catch (err) { setMsg("#perm-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.grant) {
        const role = roles.find((x) => x.id === Number(d.grant));
        const picked = await spdModal(`给「${role ? role.name : d.grant}」授权`, [
          { name: "module", label: "按模块整体授权（留空则不按模块）", type: "select", value: "",
            options: [{ value: "", label: "不按模块" }].concat(MODULE_OPTIONS) },
          { name: "copy_from_builtin", label: "以某个内置角色的默认授权为起点（留空则不复制）",
            type: "select", value: "",
            options: [{ value: "", label: "不复制" }].concat(
              roles.filter((x) => x.builtin).map((x) => ({ value: x.key, label: `${x.name}（${x.key}）` }))) },
        ]);
        if (!picked) return;
        const body = {};
        if (picked.module) body.modules = [picked.module];
        if (picked.copy_from_builtin) body.copy_from_builtin = picked.copy_from_builtin;
        // 后端对空目标回 422；两项都留空就是没选，当场说，不换一句 422
        if (!body.modules && !body.copy_from_builtin) return setMsg("#role-msg", "两项都留空了，等于没指定权限点", false);
        return await grant(d.grant, body);
      }
      if (d.grantone) {
        const custom = roles.filter((x) => !x.builtin);
        if (!custom.length) return setMsg("#perm-msg", "还没有自定义角色——内置角色的权限由代码声明，授不进去", false);
        const picked = await spdModal(`把权限点 ${d.grantone} 授给哪个角色`, [
          { name: "role_id", label: "自定义角色", type: "select", value: String(custom[0].id),
            options: custom.map((x) => ({ value: String(x.id), label: `${x.name}（${x.key}）` })) },
        ]);
        if (!picked) return;
        return await grant(picked.role_id, { permission_ids: [Number(d.grantone)] }, "#perm-msg");
      }
      if (d.revoke) {
        if (!confirm(`撤销权限点 ${d.revoke}？撤了这个角色就调不了该接口了，可以再授回来。`)) return;
        await api(`/api/rbac/roles/${encodeURIComponent(d.rid)}/permissions/${encodeURIComponent(d.revoke)}`,
          { method: "DELETE" });
        await refreshRoles();
        return await drawDetail(d.rid);
      }
      if (d.del) {
        if (!confirm("确认删除该自定义角色？")) return;
        return postAction(`/api/rbac/roles/${d.del}`, null, "#role-msg", "DELETE");
      }
      if (d.view) return await drawDetail(d.view);
    } catch (err) { setMsg("#role-msg", err.message, false); }
  };
  await drawPerms();
}

/** 正在看哪起公卫事件的处置记录（P2-477）：只留在内存里、不进存储。 */
const PH_EVENT_VIEW = { id: 0 };

async function renderPublicHealth() {
  $("#page-desc").textContent = "应急事件指挥（I-IV级）、诊间医防提醒、五域卫生监测";
  // 处置中的事件单独取一遍、排在最前（P2-457，同 P2-456）：清单只回最新 100 起，挤出窗口的就没有「处置记录 / 结案」
  const [recent, active, monitors, orgs] = await Promise.all([api("/api/publichealth/events"),
    api("/api/publichealth/events?status=active"), api("/api/publichealth/monitors"), api("/api/organizations")]);
  const events = actionableFirst(recent, active);
  // 处置记录（P2-477）：原先记得进、页面上看不到——哪起事件做过什么、谁做的，结案之后更是无从查起
  const viewing = events.find((ev) => ev.id === PH_EVENT_VIEW.id);
  const actions = viewing
    ? await api(`/api/publichealth/events/${viewing.id}/actions`).catch((err) => ({ error: err.message }))
    : [];
  const DM = { nutrition: "营养", environment: "环境", occupational: "职业", radiation: "放射", school: "学校" };
  // 卫生监测清单印机构与日期（P2-1436）：原先只有领域、指标、值/阈值、状态四列——各机构的记录混在一张表里，标红的「超标」
  // 看不出是哪家、哪天，没法下去复核（接口本就返回 org_id 与 record_date）。机构名按机构清单映射，映射不到回显编号；没填日期印「—」
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  const monitorTable = (rows) => table(["机构", "领域", "指标", "值/阈值", "日期", "状态"], rows, (m) =>
    `<tr><td>${esc(orgNames[m.org_id] || m.org_id)}</td><td>${esc(DM[m.domain] || m.domain)}</td><td>${esc(m.indicator)}</td>`
    + `<td>${m.value} / ${m.threshold}</td><td>${esc(m.record_date) || "—"}</td>`
    + `<td>${m.exceeded ? '<span class="tag red">超标</span>' : '<span class="tag green">正常</span>'}</td></tr>`);
  $("#page-body").innerHTML = `
    ${panel("事件立案", `
      <form class="inline" id="ev-form">
        <input name="title" placeholder="事件名称" required style="min-width:220px">
        <select name="level"><option>IV</option><option>III</option><option>II</option><option>I</option></select>
        <input name="disease_name" placeholder="相关病种"><button>立案</button></form>
      <h3 style="margin-top:12px">诊间医防提醒</h3>
      <form class="inline" id="rem-form"><input name="patient_id" type="number" placeholder="患者ID" required><button>查询提醒</button></form>
      <div id="rem-result"></div><p class="msg" id="ph-msg"></p>`)}
    ${panel("事件列表", table(["ID", "事件", "级别", "病种", "状态", "操作"], events, (ev) =>
      `<tr><td>${ev.id}</td><td>${esc(ev.title)}</td><td><span class="tag ${ev.level === "I" || ev.level === "II" ? "red" : "orange"}">${ev.level}级</span></td>
       <td>${esc(ev.disease_name)}</td><td><span class="tag ${ev.status === "active" ? "red" : "green"}">${ev.status === "active" ? "处置中" : "已结案"}</span></td>
       <td><button class="btn secondary" data-view="${ev.id}">查看处置</button>${ev.status === "active" ? `<button class="btn secondary" data-act="${ev.id}">登记处置</button><button class="btn secondary" data-close="${ev.id}">结案</button>` : ""}</td></tr>`)
      + (viewing ? `
      <h3 style="margin-top:12px">处置记录：${esc(viewing.title)} <button class="btn secondary" data-view="0">收起</button></h3>
      ${actions.error ? `<p class="msg err">${esc(actions.error)}</p>` : table(["时间", "处置动作", "执行人"], actions, (a) =>
        `<tr><td>${esc(a.at.slice(0, 16).replace("T", " "))}</td><td>${esc(a.action)}</td><td>${esc(a.actor || "—")}</td></tr>`)}` : ""))}
    ${panel("卫生监测（营养/环境/职业/放射/学校）", `
      <form class="inline" id="mon-form">
        <select name="domain">${Object.entries(DM).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="org_id" type="number" placeholder="机构ID" required><input name="indicator" placeholder="监测指标" required>
        <input name="value" type="number" step="any" placeholder="监测值" required><input name="threshold" type="number" step="any" placeholder="阈值" required>
        <input name="record_date" placeholder="日期"><button>登记</button></form>
      <form class="inline" id="mon-filter">
        <label style="font-size:13px"><input type="checkbox" name="exceeded" value="1"> 只看超标</label>
        <button>查询</button></form>
      <div id="mon-list">${monitorTable(monitors)}</div>`)}`;
  $("#ev-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/events", formJson(e.target), "#ph-msg"); };
  $("#mon-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/publichealth/monitors", formJson(e.target, ["org_id", "value", "threshold"]), "#ph-msg"); };
  // 只看超标（P2-1436）：接口本就收 exceeded、先筛再截最新 200 条，页面原先不用——超标的那几条挤在正常记录里翻不出来。
  // 照本页诊间提醒查询的写法：先清空、查不到把原因写出来
  $("#mon-filter").onsubmit = async (e) => {
    e.preventDefault();
    $("#mon-list").innerHTML = "";
    const params = new URLSearchParams();
    if (new FormData(e.target).get("exceeded")) params.set("exceeded", "true");
    let rows;
    try {
      rows = await api(`/api/publichealth/monitors?${params}`);
    } catch (err) {
      $("#mon-list").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    $("#mon-list").innerHTML = monitorTable(rows);
  };
  $("#rem-form").onsubmit = async (e) => {
    e.preventDefault();
    // 先清空、查不到把原因写出来（P2-378，与 P2-358 同一写法）：原先 api() 抛错没人接，上一次的结果照旧挂着
    $("#rem-result").innerHTML = "";
    let r;
    try {
      r = await api(`/api/publichealth/reminders/${new FormData(e.target).get("patient_id")}`);
    } catch (err) {
      $("#rem-result").innerHTML = `<p class="msg err">${esc(err.message)}</p>`;
      return;
    }
    $("#rem-result").innerHTML = r.reminders.length
      ? `<ul style="margin:8px 0 0 18px;font-size:13px">${r.reminders.map((x) => `<li>${esc(x.detail)}</li>`).join("")}</ul>`
      : '<p class="msg ok">无待办提醒</p>';
  };
  $("#page-body").onclick = async (e) => {
    const { act, close, view } = e.target.dataset;
    if (view) {
      PH_EVENT_VIEW.id = Number(view);
      return route();
    }
    if (act) {
      // P2-38：原先两连问，第二问「执行人」点取消照样提交（执行人记空）。合成一个表单，取消就是不记。
      // 执行人是自填的（后端不取登录账号，同 P2-41 的口径问题），这里只换形态、不改口径。
      const form = await spdModal("登记处置记录", [
        { name: "action", label: "处置动作", required: true, placeholder: "如：流调、隔离、消杀、样本送检" },
        { name: "actor", label: "执行人" },
      ]);
      if (!form) return;
      PH_EVENT_VIEW.id = Number(act);   // 记完展开这一起的处置记录：刚记的那条就在眼前
      return postAction(`/api/publichealth/events/${act}/actions`, { action: form.action, actor: form.actor }, "#ph-msg");
    }
    if (close) {
      // P2-43：原先点一下就结案；结案后不能再登记处置记录，页面上没有重开入口
      if (!await spdModal("公卫事件结案", [], { intro: "结案后该事件不能再登记处置记录，页面上不能重开。" })) return;
      return postAction(`/api/publichealth/events/${close}/close`, null, "#ph-msg");
    }
  };
}

// 月度薪酬看的期间（P2-853）：切换后整页重画时记着，空串 = 最近一个有记录的月份
let PAYROLL_PERIOD = "";

/* 人财物页员工、物资两张表的查找（P2-1594）：员工按姓名、物资按名称 / 编码。只留在内存里、不进存储（同 CONTRACT_FILTER）——
   查哪一位、哪一件是这一次办事的条件。 */
const HRF_FILTER = { employee: "", asset: "" };

async function renderHrFinance() {
  $("#page-desc").textContent = "人力资源（科室库/变动/合同/薪酬）、派驻下沉、财务集中核算与预算执行、物资出入库";
  const role = currentRole();
  const isDirector = ["director", "admin"].includes(role);
  // 员工、物资按查找取、读总数（P2-1594，同 P2-1547）：原先两张表不带参数只取清单缺省那一页（一页 500、按编号升序）——
  // 第 501 位起正是最新入职、最新建档的，挂科室、登记变动、签合同、出入库、调拨、报废这些按钮只摆在表的行上，他们没有行，
  // 页面也不提示截断。现在按姓名 / 名称 / 编码查（后端叠在可见范围之后、只收窄），总数读 X-Total-Count，列不全时标题写明
  // 「已列 N / 共 total」。不走续页取全（P2-1333 那种）：离职的员工、报废的物资都留在表里，只增不减，取全不封顶
  const findQuery = (keyword) => (keyword ? `?keyword=${encodeURIComponent(keyword)}` : "");
  const [empList, secStats, finance, assetList, departments, expiringContracts, orgs] = await Promise.all([
    api(`/api/mgmt/employees${findQuery(HRF_FILTER.employee)}`, { withTotal: true }), api("/api/mgmt/secondments/stats"),
    api("/api/mgmt/finance/summary"), api(`/api/mgmt/assets${findQuery(HRF_FILTER.asset)}`, { withTotal: true }),
    api("/api/mgmt/departments"), api("/api/mgmt/staff-contracts/expiring?days=60"), api("/api/organizations")]);
  const { rows: employees } = empList;
  const { rows: assets } = assetList;
  const listedOf = ({ rows, total }) => (total !== null && rows.length < total ? `已列 ${rows.length} / 共 ${total}` : rows.length);
  const empTitle = `${HRF_FILTER.employee ? "员工查找结果" : "员工"}（${listedOf(empList)}）· 变动留痕联动机构与状态`;
  const assetTitle = `${HRF_FILTER.asset ? "物资查找结果" : "物资"}（${listedOf(assetList)}）· 出入库全程留痕；报废与调拨都不可逆，后端无反向端点`;
  // 月度薪酬按期间取（P2-853）：原先不带期间，「合计发放」是开账以来所有月份的总和、下面只列最近 500 行——面板叫
  // 「月度薪酬」，P1-149 的修法与登记也都把它当「全县一个月发薪」的合计。缺省看最近一个有记录的月份，可切换
  let payroll = null;
  let payPeriod = PAYROLL_PERIOD;
  if (isDirector) {
    const all = payPeriod ? null : await api("/api/mgmt/payroll").catch(() => null);
    if (!payPeriod && all && all.records.length) payPeriod = all.records.map((r) => r.period).sort().pop();
    payroll = payPeriod
      ? await api(`/api/mgmt/payroll?period=${encodeURIComponent(payPeriod)}`).catch(() => null) : all;
  }
  const EST = { active: ["在岗", "green"], seconded: ["派驻中", "orange"], left: ["离职", ""] };
  const CHG_TYPES = { hire: "入职", regularize: "转正", transfer: "调动", leave: "离职" };
  const MV_TYPES = { inbound: "入库", issue: "领用", return: "归还", scrap: "报废" };
  // 取值真源是 models/assets.py:Asset.status 的列注释（idle 目前没有端点能置上，留着是为了不吞值）
  const ASSET_STATUS = { in_use: ["在用", "green"], idle: ["闲置", "orange"], scrapped: ["已报废", "red"] };
  const deptNames = Object.fromEntries(departments.map((d) => [d.id, d.name]));
  const orgNames = Object.fromEntries(orgs.map((o) => [o.id, o.name]));
  $("#page-body").innerHTML = `
    <div class="cards">
      <div class="card"><div class="label">在派人数</div><div class="value">${secStats.active_secondments}</div></div>
      <div class="card"><div class="label">医共体收入合计</div><div class="value">${finance.consolidated.income}</div></div>
      <div class="card"><div class="label">医共体结余</div><div class="value">${finance.consolidated.balance}</div></div>
      ${expiringContracts.length ? `<div class="card"><div class="label">60天内到期/已到期未续签合同</div><div class="value warn">${expiringContracts.length}</div></div>` : ""}</div>
    ${panel("员工 / 派驻 / 财务 / 物资录入", `
      <form class="inline" id="emp-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="name" placeholder="姓名" required>
        <input name="title" placeholder="职称"><input name="position" placeholder="岗位"><button>登记员工</button></form>
      <form class="inline" id="sec-form"><input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="to_org_id" type="number" placeholder="派驻机构ID" required><input name="start_date" placeholder="开始日期 YYYY-MM-DD" required>
        <select name="assignment_type" title="巡诊与短期支援不是下沉，选错会把国家监测指标做虚">${Object.entries(ASSIGN_TYPES).map(([v, t]) =>
          `<option value="${v}">${t}</option>`).join("")}</select><button>派驻下沉</button></form>
      <form class="inline" id="fin-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="period" placeholder="期间 YYYY-MM" required>
        <select name="category"><option value="income">收入</option><option value="expense">支出</option></select>
        <input name="item" placeholder="科目"><input name="amount" type="number" step="any" placeholder="金额" required><button>记账</button></form>
      <form class="inline" id="asset-form"><input name="org_id" type="number" placeholder="机构ID" required><input name="code" placeholder="物资编码" required>
        <input name="name" placeholder="名称" required><select name="category"><option value="office">办公用品</option><option value="equipment">非医疗设备</option></select>
        <input name="quantity" type="number" value="1" min="1" style="min-width:60px"><button>物资建档</button></form>
      <p class="msg" id="hrf-msg"></p>`)}
    ${panel("科室信息基础库（机构内编码唯一，员工跨机构挂接拦截）", `
      <form class="inline" id="dept-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="code" placeholder="科室编码" required><input name="name" placeholder="科室名称" required>
        <select name="category"><option value="clinical">临床</option><option value="medtech">医技</option><option value="admin">行政</option></select>
        <button>科室建档</button></form>
      ${table(["ID", "机构", "编码", "名称", "类别"], departments, (d) =>
        `<tr><td>${d.id}</td><td>${d.org_id}</td><td><span class="tag">${esc(d.code)}</span></td><td>${esc(d.name)}</td><td>${esc(d.category_name)}</td></tr>`)}`)}
    ${panel(empTitle, `<form class="inline" id="emp-find"><input name="keyword" value="${esc(HRF_FILTER.employee)}"
        placeholder="按姓名查（留空看全部）" maxlength="64"><button class="secondary">查找</button></form>`
      + table(["ID", "机构", "姓名", "职称", "科室", "状态", "操作"], employees, (em) => {
      return `<tr><td>${em.id}</td><td>${em.org_id}</td><td>${esc(em.name)}</td><td>${esc(em.title)}</td>
        <td>${em.dept_id ? esc(deptNames[em.dept_id] || em.dept_id) : "—"}</td><td>${statusTag(EST, em.status)}</td>
        <td><button class="btn secondary" data-empdept="${em.id}">挂科室</button>
            <button class="btn secondary" data-empchg="${em.id}">登记变动</button>
            <button class="btn" data-emphist="${em.id}">变动史</button>
            <button class="btn secondary" data-empct="${em.id}">签合同</button></td></tr>`;
    }))}
    <div class="panel hidden" id="empchg-panel"><h3>人员变动记录</h3><div id="empchg-list"></div></div>
    ${expiringContracts.length ? panel(`⚠ 合同到期提醒（60天内到期或已到期未续签 ${expiringContracts.length} 份，续签管理）`,
      table(["合同号", "员工", "止期"], expiringContracts, (c) =>
        `<tr><td><span class="tag">${esc(c.contract_no)}</span></td><td>${c.employee_id}</td><td><span class="tag orange">${esc(c.end_date)}</span></td></tr>`), { accent: "#b26a00" }) : ""}
    ${isDirector ? `${panel("月度薪酬（管理层：基础 + 绩效×系数）", `
      <form class="inline" id="pay-form">
        <input name="employee_id" type="number" placeholder="员工ID" required>
        <input name="period" placeholder="期间 YYYY-MM" required pattern="\\d{4}-\\d{2}">
        <input name="base_salary" type="number" step="any" placeholder="基础工资" required>
        <input name="perf_bonus" type="number" step="any" placeholder="绩效奖金" value="0">
        <input name="perf_coefficient" type="number" step="any" placeholder="绩效系数" value="1.0">
        <button>录入</button></form>
      <form class="inline" id="pay-filter"><label style="font-size:13px">查看期间
        <input name="period" type="month" value="${esc(payPeriod)}"></label><button class="secondary">查看</button></form>
      ${payroll ? `<p style="font-size:13px">${payPeriod ? `${esc(payPeriod)} ` : ""}合计发放：<b>${payroll.total_amount}</b> 元</p>${
        table(["ID", "员工", "期间", "基础", "绩效", "系数", "实发"], payroll.records, (r) =>
          `<tr><td>${r.id}</td><td>${r.employee_id}</td><td>${esc(r.period)}</td><td>${r.base_salary}</td>
           <td>${r.perf_bonus}</td><td>${r.perf_coefficient}</td><td><b>${r.total}</b></td></tr>`)}` : ""}`)}
    ${panel("预算编制与执行（管理层）", `
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
      <div id="bud-exec"></div>`)}` : ""}
    ${panel("各单位收支（全部期间）", table(["机构", "收入", "支出", "结余"], finance.orgs, (o) =>
      `<tr><td>${o.org_id}</td><td>${o.income}</td><td>${o.expense}</td><td>${o.balance}</td></tr>`))}
    ${panel(assetTitle, `<form class="inline" id="asset-find"><input name="keyword" value="${esc(HRF_FILTER.asset)}"
        placeholder="按名称或编码查（留空看全部）" maxlength="128"><button class="secondary">查找</button></form>`
      + table(["ID", "编码", "名称", "机构", "数量", "状态", "操作"], assets, (a) =>
      `<tr><td>${a.id}</td><td>${esc(a.code)}</td><td>${esc(a.name)}</td><td>${esc(orgNames[a.org_id] || a.org_id)}</td><td>${a.quantity}</td>
       <td>${statusTag(ASSET_STATUS, a.status)}</td>
       <td>${a.status !== "scrapped" ? `<button class="btn secondary" data-assetmv="${a.id}">出入库</button>
             <button class="btn secondary" data-assetxfer="${a.id}">调拨</button>
             <button class="btn danger" data-assetscrap="${a.id}">报废</button>` : ""}
           <button class="btn" data-assethist="${a.id}">记录</button></td></tr>`))}
    <div class="panel hidden" id="assetmv-panel"><h3>物资出入库记录</h3><div id="assetmv-list"></div></div>`;
  $("#emp-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/employees", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  $("#sec-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/secondments", formJson(e.target, ["employee_id", "to_org_id"]), "#hrf-msg"); };
  $("#fin-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/finance", formJson(e.target, ["org_id", "amount"]), "#hrf-msg"); };
  $("#asset-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/assets", formJson(e.target, ["org_id", "quantity"]), "#hrf-msg"); };
  $("#dept-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/departments", formJson(e.target, ["org_id"]), "#hrf-msg"); };
  // 查找只记在内存里、整页重画（P2-1594，同签约页 P2-1547 的筛选）
  $("#emp-find").onsubmit = (e) => { e.preventDefault(); HRF_FILTER.employee = String(new FormData(e.target).get("keyword") ?? "").trim(); route(); };
  $("#asset-find").onsubmit = (e) => { e.preventDefault(); HRF_FILTER.asset = String(new FormData(e.target).get("keyword") ?? "").trim(); route(); };
  const payFilter = $("#pay-filter");
  if (payFilter) payFilter.onsubmit = (e) => { e.preventDefault(); PAYROLL_PERIOD = e.target.period.value; route(); };
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
      // 红标按原值判「实际超预算」（P2-990）：原先按取整到两位的执行率判 > 100，预算 100 万、实际超支 40 元时执行率是
      // 100.0、标绿；预算到千万级，四百多元以内的超支都显示绿色
      $("#bud-exec").innerHTML = table(["类别", "预算", "实际", "执行率"], [
        ["收入", r.income], ["支出", r.expense]], ([label, d]) =>
        `<tr><td>${label}</td><td>${d.budget}</td><td>${d.actual}</td>
         <td>${d.execution_pct === null ? "—" : `<span class="tag ${d.actual > d.budget ? "red" : "green"}">${d.execution_pct}%</span>`}</td></tr>`);
    } catch (err) { setMsg("#hrf-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      // P2-38：挂科室手输科室 ID、变动输序号再三连问、签合同三连问、出入库输序号再两连问——
      // 全换成页内表单。科室只列员工所在机构的（后端本就只收同机构科室，列别家的只会换来 422）。
      if (d.empdept) {
        const emp = employees.find((x) => x.id === Number(d.empdept));
        const options = departments.filter((dp) => !emp || dp.org_id === emp.org_id)
          .map((dp) => ({ value: dp.id, label: `${dp.name}（${dp.code}）` }));
        if (!options.length) return setMsg("#hrf-msg", "该员工所在机构还没有科室，请先在科室库里建", false);
        const v = await spdModal(`挂科室：${emp ? emp.name : d.empdept}`, [
          { name: "dept_id", label: "科室（只列员工所在机构的）", type: "select", options }]);
        if (!v) return;
        await api(`/api/mgmt/employees/${d.empdept}/department?dept_id=${Number(v.dept_id)}`, { method: "POST" });
        route();
      }
      if (d.empchg) {
        // 框自己提交（P2-607）：调动没选机构、生效日期写错、说明写超了（后端 256 字），报错写在框里、框不关，写好的说明不用重填
        const done = await spdModal("登记人员变动", [
          { name: "change_type", label: "变动类型", type: "select",
            options: Object.entries(CHG_TYPES).map(([value, label]) => ({ value, label })) },
          { name: "to_org_id", label: "调入机构（仅调动时选）", type: "select",
            options: [{ value: "", label: "—" }, ...orgs.map((o) => ({ value: o.id, label: o.name }))] },
          { name: "effective_date", label: "生效日期（可空）", placeholder: "YYYY-MM-DD" },
          { name: "detail", label: "变动说明", type: "textarea" },
        ], { submit: (v) => {
          const body = { change_type: v.change_type, detail: v.detail, effective_date: v.effective_date };
          // 调动没选机构就发 null：让后端报"调动须指定调入机构"，别在前端另抄一份规则
          if (v.change_type === "transfer") body.to_org_id = v.to_org_id ? Number(v.to_org_id) : null;
          return api(`/api/mgmt/employees/${d.empchg}/changes`, { method: "POST", body: JSON.stringify(body) });
        } });
        if (done) route();
        return;
      }
      if (d.emphist) {
        const changes = await api(`/api/mgmt/employees/${d.emphist}/changes`);
        $("#empchg-panel").classList.remove("hidden");
        $("#empchg-list").innerHTML = table(["ID", "类型", "调入机构", "说明", "生效日期"], changes, (c) =>
          `<tr><td>${c.id}</td><td><span class="tag">${CHG_TYPES[c.change_type] || esc(c.change_type)}</span></td>
           <td>${c.to_org_id ?? "—"}</td><td>${esc(c.detail) || "—"}</td><td>${esc(c.effective_date) || "—"}</td></tr>`);
      }
      if (d.empct) {
        const emp = employees.find((x) => x.id === Number(d.empct));
        const v = await spdModal(`签劳动合同：${emp ? emp.name : d.empct}`, [
          { name: "contract_no", label: "合同编号", required: true },
          { name: "start_date", label: "起期", placeholder: "YYYY-MM-DD", required: true },
          { name: "end_date", label: "止期", placeholder: "YYYY-MM-DD", required: true },
        ]);
        if (!v) return;
        return postAction("/api/mgmt/staff-contracts", { employee_id: Number(d.empct), ...v }, "#hrf-msg");
      }
      if (d.assetmv) {
        const asset = assets.find((a) => a.id === Number(d.assetmv));
        // 框自己提交（P2-607）：出库超过现存量、归还超过已领未还、备注写超了（后端 256 字），报错写在框里、框不关
        const done = await spdModal(`物资出入库：${asset ? asset.name : d.assetmv}`, [
          { name: "movement_type", label: "动作", type: "select",
            options: Object.entries(MV_TYPES).map(([value, label]) => ({ value, label })) },
          { name: "quantity", label: "数量", type: "number", required: true },
          { name: "note", label: "备注", type: "textarea" },
        ], { submit: (v) => api(`/api/mgmt/assets/${d.assetmv}/movements`, { method: "POST", body: JSON.stringify(v) }) });
        if (done) route();
        return;
      }
      if (d.assetxfer) {
        const asset = assets.find((a) => a.id === Number(d.assetxfer));
        // 不把现属机构列进去：后端不拦"调给自己"，摆出来就是一次白点
        const options = orgs.filter((o) => o.id !== asset?.org_id).map((o) => ({ value: o.id, label: o.name }));
        if (!options.length) return setMsg("#hrf-msg", "没有可调入的其他机构", false);
        const picked = await spdModal(`物资调拨：${asset ? asset.name : d.assetxfer}`, [
          { name: "to_org_id", label: "调入机构（调出后归对方管，本机构不再能改）", type: "select", options },
        ]);
        if (!picked) return;
        await api(`/api/mgmt/assets/${d.assetxfer}/transfer?to_org_id=${picked.to_org_id}`, { method: "POST" });
        route();
      }
      if (d.assetscrap) {
        // 报废没有反向端点：确认框里说清楚，而不是点完才发现回不去
        if (!confirm("报废不可撤销（后端没有反向端点）：剩余数量记一笔报废出库、数量清零，此后这件物资不能再调拨或出入库。确认报废？")) return;
        await api(`/api/mgmt/assets/${d.assetscrap}/scrap`, { method: "POST" });
        route();
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
  // ADR-0009 第四批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  $("#page-body").innerHTML = `
    ${panel("公文起草 / 排班 / 质控登记", `
      <form class="inline" id="doc-form"><input name="title" placeholder="公文标题" required style="min-width:240px">
        <select name="doc_type"><option value="notice">通知</option><option value="policy">政策文件</option><option value="minutes">会议纪要</option></select>
        <input name="issuer" placeholder="发文单位"><button>起草</button></form>
      <form class="inline" id="roster-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="duty_date" placeholder="值班日期 YYYY-MM-DD" required><input name="shift" placeholder="班次" value="全天"><input name="doctor_name" placeholder="医师" required><button>排班</button></form>
      <form class="inline" id="qc-form"><select name="center_type">${Object.entries(CN).map(([v, t]) => `<option value="${v}">${t}中心</option>`).join("")}</select>
        <input name="item" placeholder="质控项目" required><select name="result"><option value="pass">合格</option><option value="fail">不合格</option></select>
        <input name="note" placeholder="备注"><input name="record_date" placeholder="日期"><button>登记质控</button></form>
      <p class="msg" id="oa-msg"></p>`)}
    ${panel("公文", table(["ID", "标题", "类型", "发文单位", "状态", "操作"], docs, (d) =>
      `<tr><td>${d.id}</td><td>${esc(d.title)}</td><td>${esc(d.doc_type_name)}</td><td>${esc(d.issuer)}</td>
       <td><span class="tag ${d.status === "published" ? "green" : "orange"}">${d.status === "published" ? "已发布" : "草稿"}</span></td>
       <td>${d.status === "draft" ? `<button class="btn secondary" data-pub="${d.id}">发布</button>` : "—"}</td></tr>`))}
    ${panel("排班", table(["中心", "日期", "班次", "医师"], rosters, (r) =>
      `<tr><td>${esc(CN[r.center_type] || r.center_type)}</td><td>${esc(r.duty_date)}</td><td>${esc(r.shift)}</td><td>${esc(r.doctor_name)}</td></tr>`))}
    ${panel("质控记录", table(["中心", "项目", "结果", "备注"], qc, (q) =>
      `<tr><td>${esc(CN[q.center_type] || q.center_type)}</td><td>${esc(q.item)}</td>
       <td><span class="tag ${q.result === "pass" ? "green" : "red"}">${q.result === "pass" ? "合格" : "不合格"}</span></td><td>${esc(q.note)}</td></tr>`))}`;
  $("#doc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/docs", formJson(e.target), "#oa-msg"); };
  $("#roster-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/rosters", formJson(e.target), "#oa-msg"); };
  $("#qc-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/mgmt/qc", formJson(e.target), "#oa-msg"); };
  $("#page-body").onclick = (e) => { if (e.target.dataset.pub) postAction(`/api/mgmt/docs/${e.target.dataset.pub}/publish`, null, "#oa-msg"); };
}

/* ---------------- 第四阶段新增页面 ---------------- */

// 存量空串等同已通知（M-1 整改：确认接收两态都收，下面「确认接收」按钮也两态都给），原先写「待回填」，像是要补录数据（P2-1026）
const CRIT_STATUS = { notified: ["已通知", "orange"], acknowledged: ["已确认", ""], resolved: ["已处置", "green"], "": ["已通知", "orange"] };

async function renderCritical() {
  $("#page-desc").textContent = "危急值闭环：通知 → 医师确认接收 → 处置反馈；超时未确认催办";
  const [critical, unacked] = await Promise.all([
    api("/api/exams/critical"), api("/api/exams/critical/unacknowledged")]);
  $("#page-body").innerHTML = `
    ${unacked.length ? panel(`⚠ 超时未确认催办（${unacked.length}）`, `${
      // 「最近通知」是超时的起算点（第十五批 S3-2）：修订改判为危急值的，报告时间是首次出具、通知是修订那一刻
      table(["报告ID", "申请单", "结论", "报告人", "报告时间", "最近通知"], unacked, (r) =>
        `<tr><td>${r.report_id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
         <td>${esc(r.reported_by)}</td><td>${esc(r.reported_at.slice(0, 16).replace("T", " "))}</td>
         <td>${esc(r.notified_at.slice(0, 16).replace("T", " "))}</td></tr>`)}`, { accent: "#c62828" }) : ""}
    ${panel("危急值清单", `<p class="msg" id="crit-msg"></p>${
      // 所见照原样换行显示（P2-1363）：LIS 回传的逐项结果（哪一项、多少、参考范围、标志）只在所见里，清单原先只列结论。
      // 报告时间（P2-1364）：清单原先不出时间，何时出具只能查库；与上面「超时未确认」一栏同一口径（落库时刻原样截到分钟）
      table(["报告ID", "申请单", "结论", "所见", "报告时间", "闭环状态", "操作"], critical, (r) => {
        const actions = (r.critical_status === "notified" || r.critical_status === "")
          ? `<button class="btn secondary" data-ack="${r.id}">确认接收</button>`
          : r.critical_status === "acknowledged"
          ? `<button class="btn secondary" data-resolve="${r.id}">处置反馈</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.request_id}</td><td><span class="tag red">${esc(r.conclusion)}</span></td>
          <td style="white-space:pre-wrap">${esc(r.finding) || "—"}</td>
          <td>${esc(r.reported_at.slice(0, 16).replace("T", " "))}</td>
          <td>${statusTag(CRIT_STATUS, r.critical_status)}</td>
          <td>${actions} <button class="btn" data-trail="${r.id}">留痕</button></td></tr>`;
      })}`)}
    <div class="panel hidden" id="crit-trail-panel"><h3>处置留痕轨迹</h3><div id="crit-trail"></div></div>`;
  $("#page-body").onclick = async (e) => {
    const { ack, resolve, trail } = e.target.dataset;
    try {
      // 确认接收、处置反馈办完即刷新铃铛的「待确认危急值」「未闭环危急值」（P2-1312）：原先只重画本页，铃铛挂旧数最长 30 秒、
      // 下拉里还列着这一条
      if (ack) { await api(`/api/exams/reports/${ack}/acknowledge`, { method: "POST" }); route(); pollTodos(); }
      if (resolve) {
        // P2-38：原先弹窗输入框点"取消"照样提交——危急值就此"闭环"，处置说明一个字没有。
        // 框自己提交（P2-607）：反馈写超了（后端 512 字）、别人已先处置，报错写在框里、框不关，写好的反馈不用重填
        const done = await spdModal("处置反馈", [
          { name: "note", label: "处置反馈说明", type: "textarea", placeholder: "如：已复查、已调整治疗" },
        ], { submit: (form) => api(`/api/exams/reports/${resolve}/resolve`,
          { method: "POST", body: JSON.stringify({ note: form.note }) }) });
        if (done) { route(); pollTodos(); }
      }
      if (trail) {
        const actions = await api(`/api/exams/reports/${trail}/critical-actions`);
        $("#crit-trail-panel").classList.remove("hidden");
        // 每一步的时刻（P2-1364）：何时通知、何时确认、何时处置，原先只有动作与操作人
        $("#crit-trail").innerHTML = table(["时间", "动作", "操作人"], actions, (a) =>
          `<tr><td>${esc(a.at.slice(0, 16).replace("T", " "))}</td><td>${esc(a.action)}</td><td>${esc(a.actor)}</td></tr>`);
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
    ${stats.by_item.length ? panel("按项目互认次数",
      barChart(stats.by_item.slice(0, 10).map((i) => [i.item_name, i.recognized_count]), { unit: " 次" })) : ""}
    ${panel("目录维护（admin）", `
      <form class="inline" id="rec-form">
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <select name="center_type">${Object.entries(CENTER_NAMES).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="mutual_scope"><option value="county">县域互认</option><option value="city">市级互认</option></select>
        <button>加入目录</button>
      </form><p class="msg" id="rec-msg"></p>
      ${table(["编码", "名称", "中心", "范围", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.item_code)}</td><td>${esc(i.item_name)}</td><td>${esc(CENTER_NAMES[i.center_type] || i.center_type)}</td>
         <td>${i.mutual_scope === "city" ? "市级" : "县域"}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-toggle="${i.id}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button></td></tr>`)}`)}
    ${panel("检查资源要素档案（设备/价格/时长/注意事项，admin 建档）", `
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
        `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${esc(CENTER_NAMES[r.center_type] || r.center_type)}</td><td>${esc(r.item_name)}</td>
         <td>${esc(r.device) || "—"}</td><td>${r.price} 元</td><td>${r.duration_min} 分</td><td>${esc(r.notes) || "—"}</td></tr>`)}`)}`;
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
  const [wards, beds, recent, inHospital, stats] = await Promise.all([
    api("/api/inpatient/wards"), api("/api/inpatient/beds"),
    api("/api/inpatient/admissions"), fetchAllPages(api, "/api/inpatient/admissions?status=admitted"),
    api("/api/inpatient/stats")]);
  // 在院的一个不落，其余照旧给最近 200 条（P2-154）：转床、开医嘱、病案首页、出院的按钮都挂在在院那几行上，原先只看
  // 最新 200 条住院，住得久的患者被新入院的挤出去，页面上没有一个按钮能给他办出院。在院的续页取全（P2-1333）：一页最多
  // 500 条，原先只取第一页，在院过 500 人时住得最久的那几位照样没有按钮
  const shown = new Set(inHospital.map((a) => a.id));
  const admissions = [...inHospital, ...recent.filter((a) => !shown.has(a.id))].sort((x, y) => y.id - x.id);
  const AS = { admitted: ["在院", "orange"], discharged: ["已出院", "green"] };
  const wardName = Object.fromEntries(wards.map((w) => [w.id, w.name]));
  $("#page-body").innerHTML = `
    ${stats.length ? panel("床位效率", table(["机构", "床位", "占用", "当前占床率", "在院", "累计出院"], stats, (s) =>
      `<tr><td>${esc(s.org_name)}</td><td>${s.beds_total}</td><td>${s.beds_occupied}</td>
       <td>${s.occupancy_pct}%</td><td>${s.in_hospital}</td><td>${s.discharged_total}</td></tr>`)) : ""}
    ${panel("病区/床位建档（admin）与入院登记", `
      <form class="inline" id="ward-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="name" placeholder="病区名称" required><button>建病区</button></form>
      <form class="inline" id="bed-form"><input name="ward_id" type="number" placeholder="病区ID" required>
        <input name="bed_no" placeholder="床号" required><button>建床位</button></form>
      <form class="inline" id="adm-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="ward_id" type="number" placeholder="病区ID" required><input name="bed_id" type="number" placeholder="床位ID" required>
        <input name="doctor_name" placeholder="主管医师"><input name="diagnosis_name" placeholder="入院诊断"><button>入院登记</button></form>
      <p class="msg" id="inp-msg"></p>`)}
    ${panel(`床位（${beds.filter((b) => b.status === "free").length} 空闲 / ${beds.length}）`,
      table(["ID", "病区", "床号", "状态"], beds, (b) =>
        `<tr><td>${b.id}</td><td>${esc(wardName[b.ward_id] || b.ward_id)}</td><td>${esc(b.bed_no)}</td>
         <td><span class="tag ${b.status === "free" ? "green" : "orange"}">${b.status === "free" ? "空闲" : "占用"}</span></td></tr>`))}
    ${panel("住院记录",
      table(["ID", "患者", "病区/床位", "诊断", "状态", "操作"], admissions, (a) => {
        // 已出院的也给「病案首页」（P2-1534）：后端建首页不看住院状态，HIS 推来的出院（A03 不设首页门禁）与存量导入的出院
        // 都没有首页，原先操作列是「—」，页面上永远补录不了。点了走同一套处理（先取，填过给只读首页，404 才弹填写表单）；
        // 住院行不带「有没有首页」，不为一个文案多发请求，统一写「病案首页」
        const actions = a.status === "admitted"
          ? `<button class="btn secondary" data-transfer="${a.id}">转床</button>
             <button class="btn secondary" data-order="${a.id}">开医嘱</button>
             <button class="btn secondary" data-summary="${a.id}">病案首页</button>
             <button class="btn danger" data-discharge="${a.id}">出院</button>`
          : `<button class="btn secondary" data-summary="${a.id}">病案首页</button>`;
        // 三种打印件都在后端按患者可见性再判一次；出院小结未出院时后端 409，故只给已出院的摆按钮
        const prints = `<button class="btn secondary" data-print-bill="${a.id}">打印费用清单</button>
             <button class="btn secondary" data-print-case="${a.id}">打印病案首页</button>`
          + (a.status === "admitted" ? "" : ` <button class="btn secondary" data-print-discharge="${a.id}">打印出院小结</button>`);
        // 病区 床号、患者姓名（P2-1335，后端随住院行带出）：原先「病区/床位」印床位主键 `bed_id`（全县连续编号）——「外科病区 / 7」
        // 其实是外科 02 床，病区里真有 07 床时指的就是另一位；「患者」列只有患者 ID
        return `<tr><td>${a.id}</td><td>${esc(a.patient_name) || a.patient_id}</td><td>${esc(a.ward_name)} ${esc(a.bed_no)}</td>
          <td>${esc(a.diagnosis_name)}</td><td>${statusTag(AS, a.status)}</td>
          <td>${actions} <button class="btn" data-orders="${a.id}">医嘱单</button> ${prints}</td></tr>`;
      }))}
    <div class="panel hidden" id="inp-orders-panel"><h3 id="inp-orders-title">医嘱单</h3><div id="inp-orders"></div>
      <div id="inp-exec"></div></div>`;
  let ordersSeq = 0;
  const drawExecutions = async (orderId) => {
    // 只按行上的 id 取（行本身来自按住院单查的医嘱列表），不做"输入任意医嘱ID"的入口
    const rows = await api(`/api/inpatient/orders/${encodeURIComponent(orderId)}/executions`);
    $("#inp-exec").innerHTML = `<h3 style="margin-top:14px">医嘱 ${esc(orderId)} 的执行记录</h3>
      ${table(["记录", "执行人", "执行时间", "皮试", "说明"], rows, (x) =>
        `<tr><td>${x.id}</td><td>${esc(x.executed_by_name) || x.executed_by}</td>
         <td>${esc((x.executed_at || "").slice(0, 16).replace("T", " "))}</td>
         <td>${x.skin_test_result === null ? "—"
           : x.skin_test_result === "positive" ? '<span class="tag red">阳性</span>'
           : '<span class="tag green">阴性</span>'}</td>
         <td>${esc(x.note) || "—"}</td></tr>`)}
      <p class="desc">关联护理记录 ${rows.length ? rows[0].nursing_record_count : 0} 条。
        护理记录挂在<b>医嘱</b>上而不是单次执行上，所以这个数每条执行都一样——
        它回答的是"这条医嘱有没有护理记录跟着"，不是"这一次执行有几条"。
        皮试列的「—」是<b>不需要皮试</b>，不是"没填"：后端那个字段可空，空就是不适用。</p>`;
  };
  $("#ward-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/wards", formJson(e.target, ["org_id"]), "#inp-msg"); };
  $("#bed-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/beds", formJson(e.target, ["ward_id"]), "#inp-msg"); };
  $("#adm-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/inpatient/admissions", formJson(e.target, ["patient_id", "ward_id", "bed_id"]), "#inp-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.printBill) return await openPrintPage(`/api/print/inpatient-bills/${d.printBill}`);
      if (d.printCase) return await openPrintPage(`/api/print/case-summaries/${d.printCase}`);
      if (d.printDischarge) return await openPrintPage(`/api/print/discharge-summaries/${d.printDischarge}`);
      // P2-38 / P1-68：转床两连问（手输病区与床位 ID）、开医嘱的内容 +「确定=长期，取消=临时」
      // 确认框（想放弃时点"取消"反而开出一条临时医嘱）、病案首页四连问，换成页内表单。
      // 病案首页原先**不送转归**，后端默认"好转"——质量指标的住院治愈好转率与死亡率取的正是它，
      // 经界面填的首页全算"好转"，死亡病例进不了死亡率。现在可选，备注也能填。
      if (d.transfer) {
        const a = admissions.find((x) => x.id === Number(d.transfer));
        const wardOrg = Object.fromEntries(wards.map((w) => [w.id, w.org_id]));
        // 后端只收同机构病区里的空闲床位（跨机构走双向转诊）；病区由床位带出，不再分开手输
        const free = beds.filter((b) => b.status === "free" && (!a || wardOrg[b.ward_id] === a.org_id));
        if (!free.length) return setMsg("#inp-msg", "本机构暂无空闲床位", false);
        const v = await spdModal(`转床（住院 ${d.transfer}）`, [
          // label 由 spdModal 自己 esc()，这里再转义就成了双重转义——所以不写成模板插值
          { name: "bed_id", label: "目标床位（本机构的空闲床位）", type: "select",
            options: free.map((b) => ({ value: b.id, label: (wardName[b.ward_id] || String(b.ward_id)) + " " + b.bed_no })) },
        ]);
        if (!v) return;
        const bed = free.find((b) => b.id === Number(v.bed_id));
        await api(`/api/inpatient/admissions/${d.transfer}/transfer`, { method: "POST",
          body: JSON.stringify({ ward_id: bed.ward_id, bed_id: bed.id }) });
        route();
      }
      if (d.order) {
        const v = await spdModal(`开医嘱（住院 ${d.order}）`, [
          { name: "order_type", label: "类型", type: "select",
            options: [{ value: "long", label: "长期医嘱" }, { value: "temp", label: "临时医嘱" }] },
          { name: "content", label: "医嘱内容", required: true },
        ]);
        if (!v) return;
        await api("/api/inpatient/orders", { method: "POST",
          body: JSON.stringify({ admission_id: Number(d.order), ...v }) });
        route();
      }
      if (d.summary) {
        // 先看填没填（P2-617）：一次住院只填一张首页，填过的原先照样弹填写表单、填完点确定才 409「病案首页已填写」，
        // 填好的内容在页面上也看不到（只能打印）。先按接口取，取到了给只读的首页，404「未填写」才弹填写表单
        let filled = null;
        try { filled = await api(`/api/inpatient/admissions/${d.summary}/case-summary`); }
        catch (err) { if (err.status !== 404) throw err; }
        if (filled) {
          // DRG 一行印后端给的 `drg_label`（P2-1536），与打印件「DRG 分组」那一格同一句：原先这里自己写 `drg_code || "未入组"`，
          // 兜底病例印「DRG：QY」、打印件印「未入组（QY，需病案首页复核）」（P2-1279），正式入组的也没有权重
          return spdModal(`病案首页（住院 ${d.summary}，已填写）`, [], { intro: [
            `出院诊断：${filled.discharge_diagnosis}`, `手术：${filled.operation || "—"}`,
            `总费用：${filled.total_cost} 元（其中药费 ${filled.drug_cost} 元）`, `转归：${filled.outcome}`,
            `DRG 分组：${filled.drg_label}`, `备注：${filled.note || "—"}`, `填写人：${filled.created_by_name}`,
          ].join("\n") });
        }
        // 出院诊断不预填入院诊断：入出院诊断符合率比的就是这两个，照抄过来它就只剩 100%
        // 框自己提交（P2-607）：费用填成负数或多于两位小数、诊断或备注写超了，报错写在框里、框不关，填好的首页不用重填
        const done = await spdModal(`病案首页（住院 ${d.summary}）`, [
          { name: "discharge_diagnosis", label: "出院诊断", required: true },
          { name: "operation", label: "手术名称（留空则从本次住院的术中记录带出）" },
          { name: "total_cost", label: "总费用（元）", type: "number" },
          { name: "drug_cost", label: "其中药费（元）", type: "number" },
          { name: "outcome", label: "转归", type: "select", value: "好转",
            options: ["治愈", "好转", "未愈", "死亡", "其他"].map((x) => ({ value: x, label: x })) },
          { name: "note", label: "备注", type: "textarea" },
        ], { submit: (v) => api(`/api/inpatient/admissions/${d.summary}/case-summary`,
          { method: "POST", body: JSON.stringify(v) }) });
        if (done) route();
      }
      if (d.discharge) {
        // 出院先确认（P2-1334，P2-43 的规矩）：原先点一下就办完，而出院撤不回（P2-753）——红色「出院」又紧挨「病案首页」，
        // 首页已填、费用已结的在院病人误点一次，医嘱全停、床位随即可被别人占用、患者收到「您已办理出院」
        if (!await spdModal(`出院（住院 ${d.discharge}）`, [], { intro: [
          "出院办理后不可撤销，点「确定」即：",
          "· 停止这次住院全部执行中的医嘱；",
          "· 释放床位，随即可收治他人；",
          "· 派出院随访，并通知患者「您已办理出院」（转归死亡的不派、不通知）；",
          "· 慢专病命中随访方案的，按出院日派生随访计划。",
          "点「取消」不办。",
        ].join("\n") })) return;
        await api(`/api/inpatient/admissions/${d.discharge}/discharge`, { method: "POST" }); route();
      }
      if (d.stopOrder) { await api(`/api/inpatient/orders/${d.stopOrder}/stop`, { method: "POST" }); route(); }
      if (d.execList) return await drawExecutions(d.execList);
      if (d.execAdd) {
        const picked = await spdModal(`登记执行（医嘱 ${d.execAdd}）`, [
          { name: "note", label: "执行说明（可留空）", type: "text", value: "" },
          { name: "skin_test_result", label: "皮试结果（不需要皮试的医嘱留「不适用」）",
            type: "select", value: "",
            options: [{ value: "", label: "不适用" }, { value: "negative", label: "阴性" },
              { value: "positive", label: "阳性" }] },
        ]);
        if (!picked) return;
        // 后端 `skin_test_result: str | None`，pattern 只认 negative/positive：
        // 空串会被 422 拦下，不需要皮试就**不送这个键**
        const body = { note: picked.note };
        if (picked.skin_test_result) body.skin_test_result = picked.skin_test_result;
        await api(`/api/inpatient/orders/${d.execAdd}/executions`,
          { method: "POST", body: JSON.stringify(body) });
        setMsg("#inp-msg", "已登记执行", true);
        return await drawExecutions(d.execAdd);
      }
      if (d.orders) {
        // 只画最后一次点的那一次住院、标题写上住院号（P2-1012）：原先先点甲、立刻改点乙，甲那次响应晚到就把甲的医嘱画在
        // 面板里，标题只写「医嘱单」——「登记执行」记到了甲的医嘱上（与 route() / loadSpd() 同一个毛病、同一种修法）
        const seq = ++ordersSeq;
        $("#inp-orders").innerHTML = "";
        $("#inp-exec").innerHTML = "";
        const orders = await api(`/api/inpatient/orders?admission_id=${d.orders}`);
        if (seq !== ordersSeq) return;
        $("#inp-orders-panel").classList.remove("hidden");
        $("#inp-orders-title").textContent = `医嘱单 · 住院 #${d.orders}`;
        $("#inp-exec").innerHTML = "";
        $("#inp-orders").innerHTML = table(["ID", "类型", "内容", "状态", "开立", "操作"], orders, (o) =>
          `<tr><td>${o.id}</td><td>${o.order_type === "long" ? "长期" : "临时"}</td><td>${esc(o.content)}</td>
           <td><span class="tag ${o.status === "active" ? "orange" : "green"}">${o.status === "active" ? "执行中" : "已停止"}</span></td>
           <td>${esc(o.created_by_name)}</td>
           <td><button class="btn secondary" data-exec-list="${o.id}">执行记录</button>
               ${o.status === "active"
                 ? `<button class="btn secondary" data-exec-add="${o.id}">登记执行</button>` +
                   `<button class="btn danger" data-stop-order="${o.id}">停止</button>`
                 : ""}</td></tr>`);
      }
    } catch (err) { setMsg("#inp-msg", err.message, false); }
  };
}

/* 块3：支付渠道/状态/对账差异类型 */
// 与 billing.PAYMENT_CHANNELS 同一张表（P2-1021）：原先少了「网关支付」，配好了支付网关页面上也选不到，只能直接调接口
const PAY_CHANNELS = { cash: "现金", card: "银行卡", insurance: "医保基金", online: "线上支付", gateway: "网关支付" };
const PAY_STATUS = { pending: ["待支付", "orange"], paid: ["已支付", "green"], refunded: ["已退款", ""], failed: ["支付失败", "red"] };
const RECON_DIFF = { missing_local: "通道有本地无", missing_remote: "本地有通道无", amount_mismatch: "金额不一致" };

/** 催缴预警表：首屏与按阈值重查共用一份，免得两处各写一遍表头。 */
function depositAlertTable(rows) {
  return table(["住院单", "患者", "机构", "押金余额", "未结费用", "缺口"], rows, (a) =>
    `<tr><td>${a.admission_id}</td><td>${esc(a.patient_name) || a.patient_id}</td><td>${a.org_id}</td>
     <td>${a.balance}</td><td>${a.unsettled}</td>
     <td><span class="tag ${a.gap < 0 ? "red" : "orange"}">${a.gap}</span></td></tr>`);
}

async function renderBilling() {
  $("#page-desc").textContent = "收费目录 → 计费明细 → 结算（医保分担）→ 统一支付（多渠道/退款）→ 日终对账差异核查";
  const today = localToday();
  const [items, settlements, stats, payments, batches, depAlerts] = await Promise.all([
    api("/api/billing/charge-items"), api("/api/billing/settlements"), api("/api/billing/stats"),
    api("/api/billing/payments"), api("/api/billing/reconciliation"),
    api("/api/billing/deposits/alerts")]);
  const BT = { outpatient: "门诊", inpatient: "住院" };
  // ADR-0009 第三批：面板外壳改用 `panel()`（定义见 core.js），迁一页、人工过一页。
  // 顶部的统计卡片区不是面板，原样保留。
  $("#page-body").innerHTML = `
    ${stats.length ? `<div class="cards">${stats.map((s) =>
      `<div class="card"><div class="label">${esc(BT[s.bill_type] || s.bill_type)}结算 ${s.count} 笔</div>
       <div class="value">${s.total_amount} 元</div>
       <div class="label">均次 ${s.avg_amount} 元 · 医保 ${s.insurance_ratio_pct}%</div></div>`).join("")}</div>` : ""}`
    + panel("收费项目目录（admin 维护）", `
      <form class="inline" id="ci-form"><input name="code" placeholder="编码" required>
        <input name="name" placeholder="名称" required>
        <select name="category"><option value="treatment">治疗处置</option><option value="drug">药品</option>
          <option value="exam">检查检验</option><option value="bed">床位</option><option value="other">其他</option></select>
        <input name="price" type="number" step="any" placeholder="单价(元)" required><button>加入目录</button></form>
      <p class="msg" id="bill-msg"></p>
      ${table(["编码", "名称", "类别", "单价", "状态", "操作"], items, (i) =>
        `<tr><td>${esc(i.code)}</td><td>${esc(i.name)}</td><td>${esc(i.category_name)}</td><td>${i.price}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-reprice="${i.id}">调价</button>
             <button class="btn secondary" data-history="${i.id}">调价历史</button>
             <button class="btn secondary" data-ci-edit="${i.id}" data-name="${esc(i.name)}"
              data-cat="${esc(i.category)}" data-active="${i.active ? 1 : 0}">维护</button></td></tr>`)}`)
    + panel("计费与结算", `
      <form class="inline" id="bd-form"><input name="patient_id" type="number" placeholder="患者ID" required>
        <input name="admission_id" type="number" placeholder="住院单ID(住院)"><input name="encounter_id" type="number" placeholder="就诊ID(门诊)">
        <input name="item_code" placeholder="收费编码" required><input name="quantity" type="number" value="1" min="1" style="min-width:70px"><button>计费</button></form>
      <form class="inline" id="settle-form">
        <select name="bill_type"><option value="inpatient">住院结算</option><option value="outpatient">门诊结算</option></select>
        <input name="admission_id" type="number" placeholder="住院单ID"><input name="encounter_id" type="number" placeholder="就诊ID">
        <input name="insurance_pay" type="number" step="any" placeholder="医保支付(元)" value="0"><button>结算</button></form>
      <p style="font-size:12.5px;color:#8a939e">住院费用未结清不可出院；结算自动汇总未结清明细并联动医保结算记录</p>
      <h3 style="margin-top:12px">计费明细查询</h3>
      <form class="inline" id="bd-query"><input name="patient_id" type="number" placeholder="患者ID">
        <input name="admission_id" type="number" placeholder="住院单ID"><input name="encounter_id" type="number" placeholder="就诊ID">
        <select name="settled"><option value="false">未结清</option><option value="true">已结算</option><option value="">全部</option></select>
        <button>查明细</button></form>
      <div id="bd-list"></div>`)
    + panel("结算单", table(["ID", "患者", "类型", "总额", "医保", "自付", "时间", "操作"], settlements, (s) =>
      `<tr><td>${s.id}</td><td>${s.patient_id}</td><td>${esc(BT[s.bill_type] || s.bill_type)}</td><td>${s.total_amount}</td>
       <td>${s.insurance_pay}</td><td>${s.self_pay}</td><td>${esc(s.created_at.slice(0, 16).replace("T", " "))}</td>
       <td><button class="btn secondary" data-print-settle="${s.id}">打印结算单</button></td></tr>`))
    + panel("住院押金（经办）", `
      <p class="desc">预交仅限在院患者；退费不得超余额（后端原子判定）；余额 = 预交 − 退费 − 结算冲抵，按流水现算</p>
      <form class="inline" id="dep-form">
        <input name="admission_id" type="number" placeholder="住院单ID" required>
        <input name="amount" type="number" step="any" placeholder="金额(元)" required>
        <select name="method"><option value="cash">现金</option><option value="card">刷卡</option><option value="online">线上</option></select>
        <button>预交押金</button>
      </form>
      <form class="inline" id="dep-refund-form" style="margin-top:8px">
        <input name="admission_id" type="number" placeholder="住院单ID" required>
        <input name="amount" type="number" step="any" placeholder="退费金额(元)" required>
        <select name="method"><option value="cash">现金</option><option value="card">刷卡</option><option value="online">线上</option></select>
        <button class="secondary">押金退费</button>
      </form>
      <form class="inline" id="dep-query-form" style="margin-top:8px">
        <input name="admission_id" type="number" placeholder="住院单ID" required>
        <button class="secondary">查余额与流水</button>
      </form>
      <p class="msg" id="dep-msg"></p>
      <div id="dep-box"></div>
      <h4 style="margin:14px 0 6px;font-size:14px">催缴预警</h4>
      <form class="inline" id="dep-alert-form">
        <input name="threshold" type="number" step="any" value="0" placeholder="阈值(元)" style="width:120px">
        <button class="secondary">按阈值筛</button>
      </form>
      <p class="desc">口径：缺口 = 押金余额 − 未结费用，缺口小于阈值即入列，按缺口从小到大排——最缺钱的排最前</p>
      <div id="dep-alert-box">${depositAlertTable(depAlerts)}</div>`)
    + panel("统一支付（经办）", `
      <form class="inline" id="pay-form">
        <input name="settlement_id" type="number" placeholder="结算单ID" required>
        <select name="channel">${Object.entries(PAY_CHANNELS).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="amount" type="number" step="any" placeholder="金额(元，空=押金冲抵后应补缴的自付额)">
        <button>发起支付</button></form>
      <p class="msg" id="pay-msg"></p>
      <p style="font-size:12.5px;color:#8a939e">渠道对接经 PaymentGateway 协议实现，演示环境使用内置 Mock 通道；仅已支付单可退款且不超可退余额</p>
      ${table(["ID", "结算单", "渠道", "金额", "已退", "状态", "外部流水号", "操作"], payments.slice(0, 30), (p) => {
        return `<tr><td>${p.id}</td><td>${p.settlement_id}</td><td>${esc(p.channel_name)}</td><td>${p.amount}</td>
          <td>${p.refunded_amount || 0}</td><td>${statusTag(PAY_STATUS, p.status)}${p.fail_reason ? `<div style="font-size:12px;color:#b23c3c">${esc(p.fail_reason)}</div>` : ""}</td>
          <td style="font-size:12px">${esc(p.trade_no) || "—"}</td>
          <td>${p.status === "paid" ? `<button class="btn secondary" data-refund="${p.id}">退款</button>` : ""}${
            p.refunded_amount > 0 ? ` <button class="btn secondary" data-refunds="${p.id}">退款记录</button>` : ""}${
            p.status !== "paid" && !(p.refunded_amount > 0) ? "—" : ""}</td></tr>`;
      })}`)
    + panel("日终对账", `
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
        </div>`).join("") || '<p class="desc">暂无对账单</p>'}`);
  const drawDeposits = async (admissionId) => {
    const [balance, rows] = await Promise.all([
      api(`/api/billing/deposits/balance?admission_id=${admissionId}`),
      api(`/api/billing/deposits?admission_id=${admissionId}&limit=50`),
    ]);
    $("#dep-box").innerHTML = `
      <div class="cards"><div class="card"><div class="label">住院单 ${balance.admission_id} 押金余额</div>
        <div class="value${balance.balance < 0 ? " warn" : ""}">${balance.balance} 元</div>
        <div class="label">预交 ${balance.prepaid} · 退费 ${balance.refunded} · 结算冲抵 ${balance.offset}</div></div></div>
      ${table(["ID", "类型", "金额", "方式", "经办人", "当时余额", "时间"], rows, (d) =>
        `<tr><td>${d.id}</td><td>${esc(d.deposit_type_name)}</td><td>${d.amount}</td>
         <td>${esc(d.method_name)}</td><td>${esc(d.operator) || "—"}</td><td>${d.balance}</td>
         <td>${esc(d.created_at.slice(0, 16).replace("T", " "))}</td></tr>`)}`;
  };
  $("#dep-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/billing/deposits", formJson(e.target, ["admission_id", "amount"]), "#dep-msg");
  };
  $("#dep-refund-form").onsubmit = (e) => {
    e.preventDefault();
    return postAction("/api/billing/deposits/refund", formJson(e.target, ["admission_id", "amount"]), "#dep-msg");
  };
  $("#dep-query-form").onsubmit = async (e) => {
    e.preventDefault();
    try {
      await drawDeposits(Number(new FormData(e.target).get("admission_id")));
      setMsg("#dep-msg", "");
    } catch (err) { $("#dep-box").innerHTML = ""; setMsg("#dep-msg", err.message, false); }
  };
  $("#dep-alert-form").onsubmit = async (e) => {
    e.preventDefault();
    const threshold = new FormData(e.target).get("threshold") || "0";
    try {
      $("#dep-alert-box").innerHTML =
        depositAlertTable(await api(`/api/billing/deposits/alerts?threshold=${encodeURIComponent(threshold)}`));
      setMsg("#dep-msg", "");
    } catch (err) { setMsg("#dep-msg", err.message, false); }
  };
  $("#ci-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/charge-items", formJson(e.target, ["price"]), "#bill-msg"); };
  $("#bd-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/billing/details", formJson(e.target, ["patient_id", "admission_id", "encounter_id", "quantity"]), "#bill-msg"); };
  // 计费明细查询（P2-493）：原先只能计、不能查——结算前看不到这次住院 / 就诊挂着哪些未结清的明细，计错了也无从发现
  $("#bd-query").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const query = new URLSearchParams([...f.entries()].filter(([, v]) => v !== ""));
    // 先清空、原因写在本面板（P2-1010，与 P2-378 同一写法）：原先查失败只把原因写进上方「收费项目目录」面板的消息行，
    // 明细区照旧挂着上一位的明细与合计——换了患者号查（无权 403），看的人会当成这一位欠着这些费
    $("#bd-list").innerHTML = "";
    const fail = (text) => { $("#bd-list").innerHTML = `<p class="msg err">${esc(text)}</p>`; };
    if (!["patient_id", "admission_id", "encounter_id"].some((k) => query.has(k))) {
      return fail("查明细请填患者ID、住院单ID 或就诊ID 之一");
    }
    try {
      const rows = await api(`/api/billing/details?${query}`);
      const total = rows.reduce((sum, d) => sum + Number(d.amount || 0), 0);
      $("#bd-list").innerHTML = `<p class="desc">共 ${rows.length} 条，合计 ${esc(total.toFixed(2))} 元${
        rows.length >= 500 ? "——<b>已截到 500 条</b>，请按住院单 / 就诊收窄" : ""}</p>
        ${table(["ID", "患者", "住院 / 就诊", "项目", "单价", "数量", "金额", "结算"], rows, (d) =>
          `<tr><td>${d.id}</td><td>${esc(d.patient_id)}</td>
           <td>${d.admission_id ? `住院 ${esc(d.admission_id)}` : `就诊 ${esc(d.encounter_id ?? "—")}`}</td>
           <td>${esc(d.item_name)}（${esc(d.item_code)}）</td><td>${esc(d.unit_price)}</td><td>${esc(d.quantity)}</td>
           <td>${esc(d.amount)}</td><td>${d.settled ? `已结算（结算单 ${esc(d.settlement_id)}）` : '<span class="tag orange">未结清</span>'}</td></tr>`)}`;
    } catch (err) { fail(err.message); }
  };
  $("#settle-form").onsubmit = async (e) => {
    e.preventDefault();
    // 只送结算类型对应的那个号（P2-913）：两个号框并排，报错后表单保留原值，换成门诊结算时上次填的住院号会一起送上去
    const body = formJson(e.target, ["admission_id", "encounter_id", "insurance_pay"]);
    delete body[body.bill_type === "inpatient" ? "encounter_id" : "admission_id"];
    // 回执说出来（P2-914）：原先走 postAction，成功即整页重画、回执整个丢掉——住院结算回执里的押金冲抵、应补缴、押金
    // 余额页面上哪里都不显示，冲抵后剩下的押金、要补缴多少，收费员只能再去查押金流水
    try {
      const r = await api("/api/billing/settlements", { method: "POST", body: JSON.stringify(body) });
      await route();
      const deposit = r.deposit_offset === undefined ? ""
        : `；押金冲抵 ${r.deposit_offset} 元，应补缴 ${r.payable_after_offset} 元，押金余额 ${r.deposit_balance} 元`;
      setMsg("#bill-msg", `已结算（结算单 #${r.id}）：总额 ${r.total_amount} 元，医保 ${r.insurance_pay} 元，自付 ${r.self_pay} 元${deposit}`);
    } catch (err) { setMsg("#bill-msg", err.message, false); }
  };
  $("#pay-form").onsubmit = async (e) => {
    e.preventDefault();
    const f = new FormData(e.target);
    const body = { settlement_id: Number(f.get("settlement_id")), channel: f.get("channel") };
    if (f.get("amount")) body.amount = Number(f.get("amount"));
    try {
      const order = await api("/api/billing/payments", { method: "POST", body: JSON.stringify(body) });
      await route();   // 先重画再写回执（P2-1013）：原先写完即被重画冲掉，流水号 / 失败原因闪一下就没了
      if (order.status === "pending") {
        // 网关渠道是异步语义（P2-1021）：受理≠到账，单子停在「待支付」等网关回调转已支付。回执原先只分 paid / 其余两种，
        // 受理成功也报「支付失败：」后面跟个空原因，付款链接 / 二维码串（只在这张回执里带回来）一并丢掉
        setMsg("#pay-msg", `已受理，待网关回调确认到账（流水号 ${order.trade_no || "—"}）${
          order.qr_code ? `；二维码串 ${order.qr_code}` : ""}`);
        if (isHttpUrl(order.pay_url)) {   // 只认 http(s)，别让网关应答里的 javascript: 链接进页面
          const link = document.createElement("a");
          link.href = order.pay_url;
          link.target = "_blank";
          link.rel = "noopener";
          link.textContent = "打开付款页";
          $("#pay-msg").append("；", link);
        }
      } else {
        setMsg("#pay-msg", order.status === "paid"
          ? `支付成功，流水号 ${order.trade_no}` : `支付失败：${order.fail_reason}`, order.status === "paid");
      }
    } catch (err) { setMsg("#pay-msg", err.message, false); }
  };
  // 金额留空的默认额按渠道取（P2-605，同 billing.create_payment 的 default_amount）：医保渠道记本单的统筹支付额，其余渠道
  // 记押金冲抵后应补缴的自付额。占位原先一律写后者——选了医保留空，收进的是统筹那一份
  const payChannel = $("#pay-form").elements.channel;
  const payHint = () => {
    $("#pay-form").elements.amount.placeholder = payChannel.value === "insurance"
      ? "金额(元，空=本单医保统筹支付额)" : "金额(元，空=押金冲抵后应补缴的自付额)";
  };
  payChannel.onchange = payHint;
  payHint();
  $("#recon-form").onsubmit = async (e) => {
    e.preventDefault();
    const date = new FormData(e.target).get("date");
    try {
      const batch = await api(`/api/billing/reconciliation/run?date=${encodeURIComponent(date)}`, { method: "POST" });
      await route();   // 先重画再写回执（P2-1013）：原先写完即被重画冲掉
      setMsg("#recon-msg", `对账完成：${batch.total_orders} 笔，差异 ${batch.unmatched} 笔（${batch.diff_amount} 元）`, batch.unmatched === 0);
    } catch (err) { setMsg("#recon-msg", err.message, false); }
  };
  $("#page-body").onclick = async (e) => {
    const { reprice, history, refund, refunds, printSettle, ciEdit } = e.target.dataset;
    try {
      if (printSettle) return await openPrintPage(`/api/print/settlements/${printSettle}`);
      if (ciEdit) {
        // 维护只改名称/分类/启停：**改价请走「调价」**，那条路会连依据与生效日期一起留痕，
        // 而价格是要对外公示的（后端 PATCH 收到 price 也会留调价历史，但依据一栏就空着了）
        const form = await spdModal("维护收费项目（改价请用「调价」，要留依据与生效日期）", [
          { name: "name", label: "名称", value: e.target.dataset.name, required: true },
          { name: "category", label: "类别", type: "select", value: e.target.dataset.cat,
            options: [{ value: "treatment", label: "治疗处置" }, { value: "drug", label: "药品" },
                      { value: "exam", label: "检查检验" }, { value: "bed", label: "床位" },
                      { value: "other", label: "其他" }] },
          { name: "active", label: "状态", type: "select", value: e.target.dataset.active,
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
        ]);
        if (!form) return;
        await api(`/api/billing/charge-items/${ciEdit}`, { method: "PATCH", body: JSON.stringify({
          name: form.name, category: form.category, active: form.active === "1" }) });
        route();
      } else if (reprice) {
        // 走 reprice 而不是 PATCH：价格要对外公示，调价依据与生效日期必须留下来
        const form = await spdModal("调价（留痕：依据 + 生效日期）", [
          { name: "new_price", label: "新单价（元）", type: "number", required: true },
          { name: "reason", label: "调价依据（如：省医保局2026年第3号文，可留空）" },
          { name: "effective_date", label: "生效日期 YYYY-MM-DD（可留空）" },
        ]);
        if (!form || !form.new_price) return;
        await api(`/api/billing/charge-items/${reprice}/reprice`, {
          method: "POST",
          body: JSON.stringify({ new_price: form.new_price, reason: form.reason || "",
                                 effective_date: form.effective_date || "" }) });
        route();
      } else if (history) {
        const rows = await api(`/api/billing/charge-items/${history}/price-history`);
        setMsg("#bill-msg", rows.length
          ? rows.map((r) => `${r.changed_at.slice(0, 10)} ${r.old_price}→${r.new_price}元${
              r.effective_date ? `（${r.effective_date}起）` : ""}${r.reason ? ` ${r.reason}` : ""}`).join("；")
          : "该项目尚无调价记录", true);
      } else if (refund) {
        // 填了金额就必须读得成数（P1-233）：金额框是文本框，原先「50元」「５０」「1,000」「¥50」经 Number() 得 NaN、
        // JSON 里成了 null，后端按「没填」整单全额退——退出去的钱收不回来。报在框里、不发请求，留空才是全额；
        // 框自己提交（P2-607），后端拒了（超可退余额等）同样留框
        const res = await spdModal("支付退款（留空 = 全额退款，不得超可退余额）", [
          { name: "amount", label: "退款金额（元，留空为全额）" },
          { name: "reason", label: "退款原因（记进逐笔退款流水，可留空）" },
        ], { submit: (form) => {
          if (form.amount && !(/^\d+(\.\d+)?$/.test(form.amount) && Number(form.amount) > 0)) {
            throw new Error("退款金额要填大于 0 的数字（如 50 或 12.5，不带单位），留空为全额退款");
          }
          const body = form.amount ? { amount: Number(form.amount) } : {};
          if (form.reason) body.reason = form.reason;
          return api(`/api/billing/payments/${refund}/refund`, { method: "POST", body: JSON.stringify(body) });
        } });
        if (!res) return;
        await route();   // 先重画再写回执（P2-1013）：原先写完即被重画冲掉，退款单号闪一下就没了
        setMsg("#pay-msg", `退款成功 ${res.refund_amount} 元，退款单号 ${res.refund_no}`);
      } else if (refunds) {
        // 逐笔退款流水（P2-730）：原先退款单号只在上面那条提示里闪一次，多次部分退款只剩累计额
        const rows = await api(`/api/billing/payments/${refunds}/refunds`);
        setMsg("#pay-msg", rows.length
          ? rows.map((r) => `${r.created_at.slice(0, 16).replace("T", " ")} 退 ${r.amount} 元（单号 ${r.refund_no || "—"}，${
              r.operator_name || "—"}${r.reason ? `：${r.reason}` : ""}）`).join("；")
          : "这张支付单没有逐笔退款记录（逐笔流水上线前的退款只有累计额）", true);
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

async function renderLabQc() {
  $("#page-desc").textContent = "检验室内质控（IQC）：质控品批号维护 → 测定值录入即判 Westgard 四规则 → 失控处理闭环与 L-J 数据";
  const lots = await api("/api/labqc/lots");
  $("#page-body").innerHTML = `
    ${panel("新建质控批号", `
      <form class="inline" id="lot-form">
        <input name="org_id" placeholder="机构ID" required style="width:90px">
        <input name="item_code" placeholder="项目编码" required>
        <input name="item_name" placeholder="项目名称" required>
        <input name="lot_no" placeholder="质控品批号" required>
        <input name="target_value" placeholder="靶值" required style="width:90px">
        <input name="sd" placeholder="SD" required style="width:70px">
        <button>建批号</button></form>
      <p class="msg" id="labqc-msg"></p>`)}
    ${panel("批号台账", table(["ID", "项目", "批号", "靶值", "SD", "状态", "操作"], lots, (l) =>
      `<tr><td>${l.id}</td><td>${esc(l.item_name)}（${esc(l.item_code)}）</td><td>${esc(l.lot_no)}</td>
       <td>${l.target_value}</td><td>${l.sd}</td>
       <td><span class="tag ${l.active ? "green" : ""}">${l.active ? "启用" : "停用"}</span></td>
       <td><button class="btn secondary" data-lot="${l.id}">测定值/L-J</button>
        <button class="btn" data-toggle="${l.id}" data-active="${l.active}">${l.active ? "停用" : "启用"}</button></td></tr>`))}
    <div class="panel hidden" id="lot-detail"></div>`;
  const drawLot = async (lotId) => {
    // 失控处理的原因、纠正措施、处理人与操作者取自测定值清单（P2-492）：L-J 数据只带「是否已处理」，处理登记写进去
    // 之后页面上只剩一个「已处理」——纠正措施的记录是失控处理的全部意义，原先却没有一处看得见。两份同一个 500 点窗口
    const [lj, measurements] = await Promise.all([
      api(`/api/labqc/lots/${lotId}/levey-jennings`), api(`/api/labqc/lots/${lotId}/measurements`)]);
    const detail = Object.fromEntries(measurements.map((m) => [m.id, m]));
    // 停用的批号不再摆录入表单（第十五批 S1-7）：后端对它 409「批号已停用，不可继续录入测定值」，原先照摆、填完才被拒
    const lotInactive = (lots.find((l) => String(l.id) === String(lotId)) || {}).active === false;
    const panel = $("#lot-detail");
    panel.classList.remove("hidden");
    panel.innerHTML = `
      <h3>${esc(lj.item_name)} · 批号 ${esc(lj.lot_no)}（靶值 ${lj.target_value} ± SD ${lj.sd}）</h3>
      <p>L-J 参考线：均值 ${lj.lines.mean} ｜ ±1SD [${lj.lines.sd1_lower}, ${lj.lines.sd1_upper}]
        ｜ ±2SD [${lj.lines.sd2_lower}, ${lj.lines.sd2_upper}] ｜ ±3SD [${lj.lines.sd3_lower}, ${lj.lines.sd3_upper}]</p>
      ${lj.points.length ? "" : `<p class="desc">该批号还没有测定点：靶值 / SD 录错了现在可以改，录入第一个测定值之后就不能再改。
        <button class="btn secondary" data-baseline="${esc(lotId)}">改靶值 / SD</button></p>`}
      ${lotInactive ? '<p class="desc">该批号已停用，不再录入测定值；历史测定与失控处理照常查看。</p>' : `
      <form class="inline" id="meas-form">
        <input name="value" placeholder="测得值" required style="width:100px">
        <label style="font-size:13px">测定时间（留空按录入时刻） <input name="measured_at" type="datetime-local"></label>
        <input name="operator" placeholder="操作者（可空）">
        <button>录入测定值</button></form>`}
      <p class="msg" id="meas-msg"></p>
      ${table(["ID", "测得值", "z", "测定时间", "操作者", "判定", "处理", "操作"], lj.points, (p) => {
        const m = detail[p.id] || {};
        // 处理时刻一并写出（P2-1472）：原先只有原因、纠正措施与处理人，核不了处理在发报告之前还是之后；时刻取清单出参，
        // 照全站惯例截到分钟（显示时区随 P1-105）
        const handledNote = m.handled
          ? `<div style="font-size:12px">原因：${esc(m.handle_reason)}；纠正措施：${esc(m.corrective_action)}（${esc(m.handled_by)}${
            m.handled_at ? `，${esc(m.handled_at.slice(0, 16).replace("T", " "))} 处理` : ""}）</div>` : "";
        return `<tr><td>${p.id}</td><td>${p.value}</td><td>${p.z}</td><td>${esc(p.measured_at)}</td><td>${esc(m.operator) || "—"}</td>
         <td>${p.out_of_control ? `<span class="tag red">失控 ${esc(p.violated_rules)}</span>`
            : p.warning ? '<span class="tag orange">1-2s 警告</span>' : '<span class="tag green">在控</span>'}</td>
         <td>${p.out_of_control ? (p.handled ? `<span class="tag green">已处理</span>${handledNote}` : '<span class="tag orange">未处理</span>') : "—"}</td>
         <td>${p.out_of_control && !p.handled ? `<button class="btn secondary" data-handle="${p.id}">失控处理</button>` : "—"}</td></tr>`;
      })}`;
    // 测定面板的消息框随整块重画换新，这里仍写明清空（P2-1471）：换批号、处理完失控点之后，上一张录入回执的警示不跟过来
    setMsg("#meas-msg", "");
    const measForm = $("#meas-form");
    if (measForm) measForm.onsubmit = async (e) => {
      e.preventDefault();
      const body = formJson(e.target, ["value"]);
      try {
        const created = await api(`/api/labqc/lots/${lotId}/measurements`, { method: "POST", body: JSON.stringify(body) });
        // 失控未处理的警示写进测定面板自己的消息框（P2-1471）：原先写在顶部「新建质控批号」面板的 #labqc-msg，离测定面板很远，
        // 又从不清——处理完、再录一个在控点、换到别的批号都还挂着。先重画（重画即清空这一格）再写回执（P2-1013），回执里
        // 没有警示就是空的。顶部那一格不再写测定的警示
        await drawLot(lotId);
        setMsg("#meas-msg", created.alert, false);
      } catch (err) { setMsg("#meas-msg", err.message, false); }
    };
    panel.onclick = async (e) => {
      const { handle, baseline } = e.target.dataset;
      // 改靶值 / SD（P2-1368）：靶值 / SD 是 Westgard 判定的基线，原先建好就改不了——SD 多敲一位（0.1 录成 1.0），之后永远
      // 判不出失控。只在还没有测定点时摆（有点了后端 409：既往判定怎么处理待裁定）；框里预填现值、由框自己提交，打开页面
      // 之后别人先录了点的，409 的原话写在框里
      if (baseline) {
        const saved = await spdModal("改靶值 / SD", [
          { name: "target_value", label: "靶值", type: "number", value: lj.target_value, required: true },
          { name: "sd", label: "SD（须大于 0）", type: "number", value: lj.sd, required: true },
        ], { intro: `${lj.item_name} · 批号 ${lj.lot_no}：还没有测定点，改了之后录入的测定值按新的靶值 / SD 判定。`,
          submit: (form) => api(`/api/labqc/lots/${lotId}`, { method: "PATCH", body: JSON.stringify(form) }) });
        if (saved) route();   // 批号台账里的靶值 / SD 一并换成新的
        return;
      }
      if (!handle) return;
      // P2-38：原先两连问（失控原因 → 纠正措施）。第二问点取消就交上一个空措施、被后端 422 拒回——
      // 想放弃的人看到的是一句报错。合成一个表单，两项都必填，取消就是放弃。
      const form = await spdModal("失控处理登记", [
        { name: "reason", label: "失控原因", required: true, placeholder: "如：质控品失效、仪器漂移" },
        { name: "corrective_action", label: "纠正措施", required: true, placeholder: "如：更换质控品复测、重新定标" },
      ]);
      if (!form) return;
      try {
        await api(`/api/labqc/measurements/${handle}/handle`, { method: "POST", body: JSON.stringify(form) });
        drawLot(lotId);
      } catch (err) { setMsg("#meas-msg", err.message, false); }
    };
  };
  $("#lot-form").onsubmit = (e) => {
    e.preventDefault();
    postAction("/api/labqc/lots", formJson(e.target, ["org_id", "target_value", "sd"]), "#labqc-msg");
  };
  $("#page-body").addEventListener("click", async (e) => {
    const { lot, toggle } = e.target.dataset;
    try {
      if (lot) await drawLot(lot);
      if (toggle) {
        await api(`/api/labqc/lots/${toggle}`, { method: "PATCH",
          body: JSON.stringify({ active: e.target.dataset.active !== "true" }) });
        route();
      }
    } catch (err) { setMsg("#labqc-msg", err.message, false); }
  });
}

async function renderQuality() {
  $("#page-desc").textContent = "不良事件上报（可匿名）→ 审核 → 整改；结构化病历实时环节质控；病历抽检评分；院感上报核实";
  const [events, estats, qstats, infections, mrSummary, mrRecords, qcRules, infStats] = await Promise.all([
    api("/api/quality/adverse-events"), api("/api/quality/adverse-events-stats"),
    api("/api/quality/record-qc-stats"), api("/api/quality/infection-reports"),
    api("/api/quality/records/qc-summary"), api("/api/quality/records"),
    api("/api/quality/record-qc-rules"), api("/api/quality/infection-stats")]);
  const AES = { reported: ["已上报", "orange"], reviewed: ["已审核", ""], rectified: ["已整改", "green"] };
  const AET = { medication: "用药", device: "器械", fall: "跌倒", pressure_sore: "压疮", transfusion: "输血", identification: "查对", other: "其他" };
  const SITE = { respiratory: "呼吸道", surgical_site: "手术部位", urinary: "泌尿道", bloodstream: "血流", gastrointestinal: "消化道", other: "其他" };
  const IST = { reported: ["待核实", "orange"], confirmed: ["已确认", "red"], excluded: ["已排除", "green"] };
  const cards = [
    ["不良事件", estats.total], ["整改闭环率", estats.closed_loop_pct + "%"],
    ["病历抽检", qstats.total], ["病历均分", qstats.avg_score], ["病历甲级率", qstats.grade_a_pct + "%"]];
  $("#page-body").innerHTML = `
    <div class="cards">${cards.map(([l, v]) => `<div class="card"><div class="label">${esc(l)}</div><div class="value">${esc(v)}</div></div>`).join("")}</div>
    ${panel("不良事件上报", `
      <form class="inline" id="ae-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <select name="event_type">${Object.entries(AET).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <select name="level"><option value="IV">IV级(隐患)</option><option value="III">III级(无后果)</option>
          <option value="II">II级(不良后果)</option><option value="I">I级(警告)</option></select>
        <input name="description" placeholder="事件经过" required style="min-width:220px">
        <label style="font-size:13px"><input type="checkbox" name="anonymous" value="true"> 匿名</label><button>上报</button></form>
      <p class="msg" id="qa-msg"></p>
      ${table(["ID", "类型", "等级", "经过", "报告人", "状态", "操作"], events, (ev) => {
        const actions = ev.status === "reported"
          ? `<button class="btn secondary" data-review="${ev.id}">审核</button>`
          : ev.status === "reviewed"
          ? `<button class="btn secondary" data-rectify="${ev.id}">登记整改</button>` : "—";
        return `<tr><td>${ev.id}</td><td>${esc(AET[ev.event_type] || ev.event_type)}</td><td>${esc(ev.level)}</td>
          <td>${esc(ev.description)}</td><td>${esc(ev.reporter_name) || "（匿名）"}</td>
          <td>${statusTag(AES, ev.status)}</td><td>${actions}</td></tr>`;
      })}`)}
    ${panel("不良事件附件（现场照片/佐证PDF，≤10MB）", `
      <form class="inline" id="ae-att-form">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <input type="file" name="file" accept="image/png,image/jpeg,image/gif,image/webp,application/pdf" required>
        <button>上传</button></form>
      <form class="inline" id="ae-att-query">
        <input name="event_id" type="number" placeholder="事件ID" required>
        <button>查附件</button></form>
      <p class="msg" id="ae-att-msg"></p><div id="ae-att-list"></div>`)}
    ${panel("结构化病历录入（医师）——提交即出环节质控评分", `
      <form id="mr-form">
        <div class="inline"><input name="encounter_id" type="number" placeholder="就诊ID" required>
          <span class="desc" style="font-size:12px">同一就诊仅一份病历，再次提交为修正并复评</span></div>
        ${MR_FIELDS.map(([key, label, hint, rows]) =>
          `<div style="margin-top:8px"><label style="font-size:13px">${label}<span class="desc" style="font-size:12px">（${hint}）</span></label>
           <textarea name="${key}" rows="${rows}" style="width:100%"></textarea></div>`).join("")}
        <div class="inline" style="margin-top:8px"><button>提交并质控评分</button></div></form>
      <p class="msg" id="mr-msg"></p><div id="mr-result"></div>`)}
    ${panel(`环节质控概览（甲 ${mrSummary.grade_distribution["甲"]} / 乙 ${mrSummary.grade_distribution["乙"]} / 丙 ${mrSummary.grade_distribution["丙"]}，均分 ${mrSummary.avg_score}）`, `
      ${table(["机构", "病历数", "均分", "甲", "乙", "丙", "甲级率"], mrSummary.by_org, (o) =>
        `<tr><td>${esc(o.name)}</td><td>${o.total}</td><td>${o.avg_score}</td>
         <td><span class="tag green">${o.grade_a}</span></td><td><span class="tag orange">${o.grade_b}</span></td>
         <td>${o.grade_c ? `<span class="tag red">${o.grade_c}</span>` : 0}</td><td>${o.grade_a_pct}%</td></tr>`)}
      <h3 style="margin-top:12px">按医师</h3>
      ${table(["医师", "病历数", "均分", "甲", "乙", "丙"], mrSummary.by_doctor, (d) =>
        `<tr><td>${esc(d.name) || "（未署名）"}</td><td>${d.total}</td><td>${d.avg_score}</td>
         <td>${d.grade_a}</td><td>${d.grade_b}</td><td>${d.grade_c}</td></tr>`)}
      <h3 style="margin-top:12px">最近病历</h3>
      ${table(["病历ID", "就诊ID", "医师", "主诉", "得分", "等级", "操作"], mrRecords.slice(0, 20), (r) =>
        `<tr><td>${r.id}</td><td>${r.encounter_id}</td><td>${esc(r.doctor_name)}</td>
         <td>${esc(r.chief_complaint) || "（未填）"}</td><td>${r.qc_score}</td>
         <td><span class="tag ${MR_GRADE_COLOR[r.qc_grade] || ""}">${r.qc_grade}级</span></td>
         <td><button class="btn secondary" data-mrqc="${r.id}">复评并看缺陷</button>
             <button class="btn secondary" data-mrdetail="${r.id}">详情</button></td></tr>`)}`)}
    ${panel("环节质控规则台账（管理员可改扣分与启停）", `
      <p class="desc">规则是环节质控评分的唯一依据：停用一条，此后提交的病历不再按它扣分——
        已评过的分数不会回溯重算（要重算走病历行的「复评」）</p>
      ${table(["编码", "名称", "环节", "判定", "扣分", "状态", "操作"], qcRules, (r) =>
        `<tr><td>${esc(r.code)}</td><td>${esc(r.name)}</td><td>${esc(r.field_name)}</td>
         <td>${esc(r.rule_name)}</td><td>-${r.deduct_points}</td>
         <td>${r.active ? '<span class="tag green">启用</span>' : '<span class="tag">停用</span>'}</td>
         <td><button class="btn secondary" data-ruleedit="${r.id}" data-points="${r.deduct_points}"
              data-active="${r.active ? 1 : 0}">调整</button></td></tr>`)}
      <p class="msg" id="rule-msg"></p>`)}
    ${panel("病历质控抽检（人工评分）", `
      <form class="inline" id="qc-rec-form">
        <select name="target_type"><option value="encounter">门急诊病历</option><option value="case_summary">病案首页</option></select>
        <input name="target_id" type="number" placeholder="门急诊填就诊ID / 病案首页填住院号" style="min-width:220px" required>
        <input name="score" type="number" min="0" max="100" placeholder="评分0-100" required>
        <input name="defects" placeholder="缺陷项（分号分隔）" style="min-width:200px"><button>评分</button></form>`)}
    ${panel(`院感上报（已确认 ${infStats.confirmed} 例 · 待核实 ${infStats.pending_verify} 例）`, `
      <p class="desc">按部位分布（仅已确认）：${Object.entries(infStats.by_site || {})
        .map(([k, v]) => `${esc(SITE[k] || k)} ${v}`).join("，") || "暂无确认病例"}</p>
      <form class="inline" id="inf-form"><input name="org_id" type="number" placeholder="机构ID" required>
        <input name="patient_id" type="number" placeholder="患者ID" required>
        <select name="infection_site">${Object.entries(SITE).map(([v, t]) => `<option value="${v}">${t}</option>`).join("")}</select>
        <input name="pathogen" placeholder="病原体"><input name="report_date" placeholder="日期 YYYY-MM-DD"><button>上报</button></form>
      ${table(["ID", "机构", "患者", "部位", "病原体", "状态", "操作"], infections, (r) => {
        const actions = r.status === "reported"
          ? `<button class="btn secondary" data-verify="${r.id}" data-ok="true">确认</button>
             <button class="btn" data-verify="${r.id}" data-ok="false">排除</button>` : "—";
        return `<tr><td>${r.id}</td><td>${r.org_id}</td><td>${r.patient_id}</td><td>${esc(SITE[r.infection_site] || r.infection_site)}</td>
          <td>${esc(r.pathogen)}</td><td>${statusTag(IST, r.status)}</td><td>${actions}</td></tr>`;
      })}`)}`;
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
  // 病案首页按住院号送（P2-998）：原先「对象ID」一格，病案首页一支按病案首页自己的编号查，而这个号在任何页面上都不出现，
  // 照住院号打的分挂到了别人的首页上；门急诊一支是就诊号（「最近病历」表的第二列，第一列是病历号）
  $("#qc-rec-form").onsubmit = (e) => {
    e.preventDefault();
    const body = formJson(e.target, ["target_id", "score"]);
    if (body.target_type === "case_summary") { body.admission_id = body.target_id; delete body.target_id; }
    postAction("/api/quality/record-qc", body, "#qa-msg");
  };
  const drawRecordDetail = async (recordId) => {
    const d = await api(`/api/quality/records/${recordId}`);
    const r = d.record;
    $("#mr-result").innerHTML = `
      <p style="font-size:13px">病历 #${r.id}（就诊 ${r.encounter_id} · ${esc(r.doctor_name) || "未署名"}）
        得分 <b>${r.qc_score}</b> 分
        <span class="tag ${MR_GRADE_COLOR[r.qc_grade] || ""}">${r.qc_grade}级</span>
        · 提交 ${esc((r.created_at || "").replace("T", " ").slice(0, 16))}
        · 最近修正 ${esc((r.updated_at || "").replace("T", " ").slice(0, 16))}</p>
      ${table(["环节", "内容"], MR_FIELDS.map(([key, label]) => [label, r[key]]), ([label, value]) =>
        `<tr><td style="white-space:nowrap">${esc(label)}</td><td>${esc(value) || "（未填）"}</td></tr>`)}
      <p class="desc" style="margin-top:8px">下面是<b>落库时</b>的缺陷快照（评分当时的结论）；
        规则改过之后要看新结论，请用同一行的「复评」</p>
      ${d.defects.length
        ? table(["规则", "环节", "缺陷描述", "扣分"], d.defects, (x) =>
            `<tr style="color:#b23c3c"><td>${esc(x.rule_code)} ${esc(x.rule_name)}</td><td>${esc(x.field_name)}</td>
             <td>${esc(x.message)}</td><td>-${x.deduct_points}</td></tr>`)
        : '<p class="msg ok">评分当时无缺陷项</p>'}`;
  };
  $("#inf-form").onsubmit = (e) => { e.preventDefault(); postAction("/api/quality/infection-reports", formJson(e.target, ["org_id", "patient_id"]), "#qa-msg"); };
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.review || d.rectify) {
        const rectify = Boolean(d.rectify);
        // 框自己提交（P2-607）：写超了（后端 1024 字）报错写在框里、框不关，写好的措施 / 意见不用重填；原先留空就关框、
        // 什么也不发生，现在由后端说不能留空（后端本就必填）
        const done = await spdModal(rectify ? "登记整改措施" : "不良事件审核", [
          { name: "note", label: rectify ? "整改措施" : "审核意见", type: "textarea", required: true },
        ], { submit: (form) => {
          // 两条路径分开写而不是拼动作名：孤儿闸门按字面匹配，拼出来的地址它看不见——
          // 第一版就是拼的，闸门当场把这两条**已接通**的端点判回孤儿（本轮第二次踩）
          const body = JSON.stringify({ note: form.note });
          if (rectify) return api(`/api/quality/adverse-events/${d.rectify}/rectify`, { method: "POST", body });
          return api(`/api/quality/adverse-events/${d.review}/review`, { method: "POST", body });
        } });
        if (done) route();
        return;
      }
      if (d.ruleedit) {
        const form = await spdModal("调整质控规则（扣分与启停）", [
          { name: "deduct_points", label: "扣分（0-100）", type: "number", value: d.points },
          { name: "active", label: "状态", type: "select", value: d.active,
            options: [{ value: "1", label: "启用" }, { value: "0", label: "停用" }] },
        ]);
        if (!form) return;
        // 后端 RecordQcRuleUpdate 只收这两项，且 exclude_unset——只送改动的那部分
        await api(`/api/quality/record-qc-rules/${d.ruleedit}`, { method: "PATCH", body: JSON.stringify({
          deduct_points: form.deduct_points, active: form.active === "1" }) });
        route();
        return;
      }
      if (d.mrdetail) {
        await drawRecordDetail(d.mrdetail);
        $("#mr-result").scrollIntoView({ behavior: "smooth", block: "center" });
        return;
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
  $("#page-desc").textContent = "绩效指标目录：权重调节、启停与改名（调整后按比例归一化计分）";
  const indicators = await api("/api/performance/indicators");
  $("#page-body").innerHTML = `
    ${panel("", `<p class="msg" id="pi-msg"></p>${
      table(["指标", "键", "权重", "状态", "操作"], indicators, (i) =>
        `<tr><td>${esc(i.name)}</td><td>${esc(i.key)}</td><td>${i.weight}</td>
         <td><span class="tag ${i.active ? "green" : "red"}">${i.active ? "启用" : "停用"}</span></td>
         <td><button class="btn secondary" data-weight="${esc(i.key)}">调权重</button>
             <button class="btn" data-toggle-ind="${esc(i.key)}" data-active="${i.active}">${i.active ? "停用" : "启用"}</button>
             <button class="btn" data-rename-ind="${esc(i.key)}" data-name="${esc(i.name)}">改名</button></td></tr>`)}`)}`;
  $("#page-body").onclick = async (e) => {
    const d = e.target.dataset;
    try {
      if (d.weight) {
        // P2-38：弹窗换成页内表单（数字框可带小数）；负数由后端报人话
        const form = await spdModal(`调权重：${d.weight}`, [
          { name: "weight", label: "新权重（≥0，自动按比例归一化）", type: "number", required: true }]);
        if (!form) return;
        await api(`/api/performance/indicators/${d.weight}`, { method: "PATCH", body: JSON.stringify({ weight: form.weight }) });
        route();
      }
      if (d.toggleInd) {
        await api(`/api/performance/indicators/${d.toggleInd}`, { method: "PATCH",
          body: JSON.stringify({ active: d.active !== "true" }) });
        route();
      }
      if (d.renameInd) {
        // 改名（P2-472）：接口一直收 name，页面只给调权重与启停——指标名写进各县的考核文件与报表标题，
        // 迁移 b5d9f3a71c2e 只报告不替人改、让现场「在指标目录里改」，界面上却没有这一格
        const form = await spdModal(`改名：${d.renameInd}`, [
          { name: "name", label: "指标名称（报表与考核明细里显示的名字）", value: d.name, required: true }]);
        if (!form) return;
        await api(`/api/performance/indicators/${d.renameInd}`, { method: "PATCH", body: JSON.stringify({ name: form.name }) });
        route();
      }
    } catch (err) { setMsg("#pi-msg", err.message, false); }
  };
}

