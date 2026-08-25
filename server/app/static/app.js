/* 管理端 · 页面注册表与启动（最后加载）。
 *
 * 注册表放在最后不是随意排的：`const PAGES = [{ render: renderDashboard }]`
 * 在**求值时**就要拿到函数引用，而函数声明的提升只在同一个文件内生效。
 * 放在前面加载，就会在这里 ReferenceError。
 */

const PAGES = [
  { group: "总览" },
  { id: "dashboard", title: "决策驾驶舱", render: renderDashboard },
  { id: "notifications", title: "站内消息", render: renderNotifications },
  { group: "基础平台" },
  { id: "orgs", title: "机构管理", render: renderOrgs },
  { id: "orggroups", title: "机构协作分组", render: renderOrgGroups },
  { id: "patients", title: "患者主索引", render: renderPatients },
  { id: "credentials", title: "就诊凭据", render: renderCredentials },
  { id: "dicts", title: "编码字典", render: renderDicts },
  { group: "业务协同" },
  { id: "exams", title: "共享诊断中心", render: renderExams },
  { id: "critical", title: "危急值操作台", render: renderCritical },
  { id: "recognition", title: "互认目录与统计", render: renderRecognition },
  { id: "consultations", title: "远程会诊", render: renderConsultations },
  { id: "referrals", title: "双向转诊", render: renderReferrals },
  { id: "emergency", title: "智慧急救", render: renderEmergency },
  { id: "emtimeline", title: "急救绿道时间轴", render: renderEmTimeline },
  { id: "inpatient", title: "住院管理", render: renderInpatient },
  { id: "clinicaldocs", title: "住院临床文书", render: renderClinicalDocs },
  { id: "outpatientdocs", title: "门急诊文书", render: renderOutpatientDocs },
  { id: "surgery", title: "手术麻醉", render: renderSurgery },
  { id: "billing", title: "费用结算", render: renderBilling },
  { id: "rx", title: "集中审方", render: renderRx },
  { id: "pharmacy", title: "中心药房", render: renderPharmacy },
  { id: "procure", title: "采购与盘点", render: renderProcure },
  { id: "medication", title: "药事监测", render: renderMedication },
  { id: "insurance", title: "医保协同", render: renderInsurance },
  { id: "blood", title: "血液管理", render: renderBlood },
  { group: "医防融合" },
  { id: "chronic", title: "慢病管理", render: renderChronic },
  { id: "diseaseprograms", title: "专病管理", render: renderDiseasePrograms },
  { id: "contracts", title: "家医签约", render: renderContracts },
  { id: "followups", title: "随访中心", render: renderFollowups },
  { id: "infectious", title: "传染病预警", render: renderInfectious },
  { id: "infdir", title: "传染病目录与迟报", render: renderInfectiousDir },
  { id: "publichealth", title: "公卫协同", render: renderPublicHealth },
  { id: "eldercare", title: "老年健康", render: renderEldercare },
  { id: "maternal", title: "妇幼保健", render: renderMaternal },
  { id: "certs", title: "证明与体检", render: renderCerts },
  { id: "vaccination", title: "疫苗接种", render: renderVaccination },
  { id: "vaccinesupply", title: "疫苗批次与冷链", render: renderVaccineSupply },
  { id: "pathology", title: "病理标本", render: renderPathology },
  { id: "tcmheritage", title: "名老中医传承与模拟诊疗", render: renderTcmHeritage },
  { id: "projects", title: "项目管理", render: renderProjects },
  { id: "surveillance", title: "多点触发监测", render: renderSurveillance },
  { group: "便民惠民" },
  { id: "appointments", title: "预约诊疗", render: renderAppointments },
  { id: "telemedicine", title: "互联网+诊疗", render: renderTelemedicine },
  { id: "tcm", title: "中医药服务", render: renderTcm },
  { id: "archive", title: "患者360视图", render: renderArchive },
  { id: "resources", title: "统一资源与排程", render: renderResources },
  { id: "rbac", title: "角色与权限", render: renderRbac },
  { id: "servicerequests", title: "统一申请单中心", render: renderServiceRequests },
  { group: "全域慢专病" },
  { id: "spdadmin", title: "平台管理端·运行中枢", render: renderSpdAdmin, roles: ["director"] },
  { id: "spdhc", title: "卫健管理端·决策监管", render: renderSpdHealthCommission, roles: ["director"] },
  { id: "spdexpert", title: "专病专家端·临床指导", render: renderSpdExpert },
  { id: "spdcenter", title: "全程管理中心端·统筹调度", render: renderSpdCenter },
  { id: "spdteam", title: "服务团队端·基层执行", render: renderSpdTeam },
  { id: "spdpatients", title: "筛查建档与纳管", render: renderSpdPatients },
  { id: "spdpath", title: "标准路径与任务中心", render: renderSpdPath },
  { id: "spdreferral", title: "逐级转诊闭环", render: renderSpdReferral },
  { id: "spdassess", title: "专病考核与积分", render: renderSpdAssess },
  { id: "spdfollowup", title: "智能随访服务端", render: renderSpdFollowup },
  { id: "spdreport", title: "智能辅助报告端", render: renderSpdReport },
  // 写操作的后端守卫是 config/_base.py 的 CONFIG_ROLES=("director","doctor")，
  // 这里跟着收窄——其它角色进来只能看，按钮点一个报一个 403。
  { id: "spdconfig", title: "配置中心·量表与素材", render: renderSpdConfig, roles: ["director", "doctor"] },
  { group: "综合管理" },
  { id: "performance", title: "绩效考核", render: renderPerformance, roles: ["director"] },
  { id: "perfind", title: "绩效指标调权", render: renderPerfIndicators, roles: ["director"] },
  { id: "quality", title: "质量安全", render: renderQuality },
  { id: "labqc", title: "检验室内质控", render: renderLabQc },
  { id: "clinind", title: "医疗质量指标", render: renderClinicalIndicators },
  { id: "drgs", title: "DRGs分析", render: renderDrgs },
  { id: "education", title: "远程医学教育", render: renderEducation },
  { id: "knowledge", title: "知识库", render: renderKnowledge },
  { id: "surveys", title: "满意度分析", render: renderSurveys },
  { id: "staffing", title: "人员下沉调度", render: renderStaffing },
  { id: "hrfinance", title: "人财物管理", render: renderHrFinance },
  { id: "fund", title: "医保基金总额付费", render: renderFund, roles: ["director"] },
  { id: "accounting", title: "会计核算", render: renderAccounting, roles: ["director"] },
  { id: "cost", title: "成本核算", render: renderCost, roles: ["director"] },
  { id: "materials", title: "物资采购与耗材", render: renderMaterials },
  { id: "analytics", title: "决策指标扩展", render: renderAnalytics },
  { id: "oaqc", title: "行政与质控", render: renderOaQc },
  { id: "cssd", title: "消毒供应", render: renderCssd },
  { id: "medwaste", title: "医废追溯", render: renderMedwaste },
  { group: "系统管理", roles: ["admin"] },
  { id: "esb", title: "集成平台", render: renderEsb, roles: ["admin"] },
  { id: "rules", title: "统一规则引擎", render: renderRules, roles: ["admin"] },
  { id: "workflows", title: "流程引擎", render: renderWorkflows },
  { id: "jobs", title: "定时任务", render: renderJobs, roles: ["director"] },
  { id: "monitor", title: "运行监控", render: renderMonitor, roles: ["admin"] },
  { id: "dataquality", title: "数据质控", render: renderDataQuality, roles: ["admin"] },
  { id: "printtpl", title: "打印模板", render: renderPrintTemplates, roles: ["admin"] },
  { id: "users", title: "用户管理", render: renderUsers, roles: ["admin"] },
  { id: "audit", title: "审计日志", render: renderAudit, roles: ["admin"] },
  { id: "access-logs", title: "调阅留痕", render: renderAccessLogs, roles: ["admin", "director"] },
  { id: "consents", title: "知情同意与行权", render: renderConsents, roles: ["admin", "director", "operator"] },
];


$("#login-form").onsubmit = async (e) => {
  e.preventDefault();
  try {
    // X-Token-Transport: cookie —— 声明走 Cookie 会话：令牌进 HttpOnly Cookie（G3）
    const data = await api("/api/auth/login", { method: "POST",
      headers: { "X-Token-Transport": "cookie" },
      body: JSON.stringify({
        username: $("#login-username").value, password: $("#login-password").value }) });
    // P1-23：不再把 access_token 写入 localStorage；旧存量一并清掉（切换 Cookie 模式）
    token = "";
    localStorage.removeItem("medplat_token");
    localStorage.setItem("medplat_role", data.role);
    localStorage.setItem(CSRF_KEY, readCookie(CSRF_KEY));
    enterApp();
  } catch (err) { $("#login-error").textContent = err.message; }
};
window.addEventListener("hashchange", route);

// Cookie 会话页面刷新后仍在（Cookie 持久）：以 role 标记判断登录态，
// Cookie 失效时首个 api() 401 会统一走 logout() 回到登录页
if (isAuthed()) enterApp();
else $("#login-view").classList.remove("hidden");

/* ============================================================================
 * 阶段六B：阶段一~五模块的管理端页面
 * 复用既有 table / formJson / postAction / barChart 四件套，不引入新依赖。
 * ==========================================================================*/

