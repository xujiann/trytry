"""居民端「在线服务」「慢专病」沿用「我的档案」里选中的家人却不写是谁：看过母亲的档案再去录自己的血压、做自查、发咨询、
约号，全记到母亲名下（P2-1776，第五十二批扫描 AP1-1）。

修前：`m.js` 的 `viewingPatientId` 全页共享、只在「我的档案」的成员标签里改，`svcQuery` / `spdQuery` 与各写操作照它带
`patient_id`（注释写明有意「与「我的档案」的成员切换保持一致」）；可两个页签上没有「当前对象」——`index.html` 的在线服务、
慢专病两段里都没有，写操作的回执也一律不写是谁：监测写「已保存，指标正常」，约号、发咨询、交任务、干预反馈交完只重画清单，
约号按钮只写「为该成员预约」，代管几位家人时分不清记在谁名下。居民端自己的规矩是切到家人视角要明说（「谁看过我的档案」
那段写着「始终显示您本人的调阅记录」），医护端同形的 P1-231 已修。

修法：两个页签顶部常驻「当前：姓名（关系 / 本人）」与「换人」（回到「我的档案」的成员切换，选人只在那一处）；本人的姓名
取 `/me`、代管成员的取 `/me/family` 已取到的那一行（`viewingWho`），不另发请求，插进页面的一律 esc()。约号、监测、自查与
申请、咨询、任务提交、干预反馈、线上自助随访的回执写明「已为 某某（关系）……」，约号按钮写成员姓名。沿用选择的行为不改。

页面那几条把 `m.js` 的渲染与写操作函数原文放进 node 跑（shared.js 整份加载）：`authApi` 按路径回垫好的响应并记下请求，
DOM 用一个够这几段用的小替身——`$(sel)` 同一个选择器回同一个元素，`querySelectorAll` 从 innerHTML 里认出带标记的元素，
卡片内表单（`inlineInput`）替成「照填一句就交」。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")

#: 本人与代管的母亲。母亲的名字带 `<b>`：插进页面的要转义，回执（textContent / alert）照原样
SELF, MEMBER = "王建国", "李桂兰<b>"
MEMBER_ID = 2

PRELUDE = r"""
const OUT = { requests: [], alerts: [], confirms: [], switched: [] };
const registry = {};
function attrsOf(text) {
  const attrs = {};
  for (const m of text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? "";
  return attrs;
}
function makeEl(name, attrs = {}) {
  const classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
  const dataset = {};
  Object.entries(attrs).forEach(([k, v]) => {
    if (k.startsWith("data-")) dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  });
  const el = {
    name, attrs, dataset, innerHTML: "", textContent: "", value: "", className: "", disabled: false, listeners: {},
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)) },
    selectedOptions: [{ dataset: { unit: "mmHg" } }],
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelector(sel) { return pick(el, sel)[0] || null; },
    querySelectorAll(sel) { return pick(el, sel); },
    closest() { return makeEl("card"); },
    insertAdjacentHTML(_pos, html) { el.innerHTML += html; },
    scrollIntoView() {},
  };
  return el;
}
/* 从 innerHTML 里认出匹配的元素（`.类名`、`[data-属性]`、`.类名[data-属性]`）；innerHTML 没变时回同一批，挂的监听取得回来 */
function pick(parent, sel) {
  const m = sel.match(/^(?:\.([\w-]+))?(?:\[([\w-]+)\])?$/);
  if (!m) return [];
  const cache = (parent.picked ||= new Map());
  const key = `${sel}\u0000${parent.innerHTML}`;
  if (!cache.has(key)) {
    cache.set(key, [...parent.innerHTML.matchAll(/<(\w+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>/g)]
      .map((t) => attrsOf(t[2]))
      .filter((a) => (!m[1] || (a.class || "").split(/\s+/).includes(m[1])) && (!m[2] || m[2] in a))
      .map((a) => makeEl("el", a)));
  }
  return cache.get(key);
}
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl(sel)),
  querySelectorAll: () => [], createElement: (tag) => makeEl(tag) };
globalThis.history = { replaceState() {} };
globalThis.confirm = (text) => { OUT.confirms.push(text); return true; };
globalThis.alert = (text) => { OUT.alerts.push(text); };

const ME = { bound: true, name: "王建国", ehc_no: "E001", phone: "139****0001", wechat_bound: true };
const FAMILY = [
  { patient_id: 1, name: "王建国", ehc_no: "E001", relation: "self", is_self: true },
  { patient_id: 2, name: "李桂兰<b>", ehc_no: "E002", relation: "parent", is_self: false, member_id: 9 },
];
const SLOT = { id: 11, org_name: "城关卫生院", resource_name: "全科门诊", slot_date: "2026-10-12",
  slot_time: "08:00-09:00", remaining: 3 };
const REPLIES = {
  "GET /api/portal/me": () => { if (OUT.signedOut) throw new Error("请先登录"); return ME; },
  "GET /api/portal/me/family": FAMILY,
  "GET /api/portal/me/slot-orgs": [],
  "GET /api/portal/me/slots": [SLOT],
  "POST /api/portal/me/appointments": { id: 1, slot_id: 11, status: "booked" },
  "GET /api/portal/spd/measurements": [],
  "POST /api/portal/spd/measurements": { level: "high" },
  "POST /api/portal/spd/tasks/31/submit": { id: 31, status: "submitted" },
  "GET /api/portal/spd/interventions": [{ id: 41, goal: "控制血压", content: "低盐饮食", status: "planned",
    status_name: "待执行", read: false }],
  "GET /api/portal/spd/health-prescriptions": [],
  "POST /api/portal/spd/interventions/41/feedback": { id: 41 },
  "GET /api/portal/spd/consults": [],
  "GET /api/portal/spd/home": { enrolled: false, programs: [], paused_programs: [] },
  "POST /api/portal/spd/consults": { id: 5 },
  "GET /api/portal/spd/scales": [{ id: 6, code: "scr_demo", name: "高血压自查", program_code: "hypertension", items: [] }],
  "GET /api/portal/spd/service-applies": [],
  "POST /api/portal/spd/screenings": { id: 7, risk_level: "high", advice: "建议尽快就医。", can_apply: true },
  "POST /api/portal/spd/service-applies": { id: 8 },
  "GET /api/portal/spd/followups": [{ id: 51, scene: "outpatient", planned_at: "2026-10-01", status: "planned",
    questions: [] }],
  "POST /api/portal/spd/followups/51/self-answer": { abnormal_level: "none" },
};
async function authApi(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  OUT.requests.push({ method, path, body: options.body ? JSON.parse(options.body) : null });
  const reply = REPLIES[`${method} ${path.split("?")[0]}`];
  if (reply === undefined) throw new Error(`没垫的接口：${method} ${path}`);
  return JSON.parse(JSON.stringify(typeof reply === "function" ? reply() : reply));
}
function isAuthed() { return true; }
function switchTab(tab) { OUT.switched.push(tab); }
async function loadService() {}
async function loadSpd() {}
async function loadArchive() {}
async function fetchMyAppointments() { return { rows: [], total: 0 }; }
async function fetchSpdTasks() {
  return { rows: [{ id: 31, title: "每日测血压", status: "pending", due_date: "2026-10-12" }], total: 1 };
}
/* 卡片内表单：照填一句（下拉取第一项）就交，交给页面的提交回调 */
async function inlineInput(_card, { options = null, submit = null } = {}) {
  const value = options ? options[0] : "已按时完成";
  if (submit) await submit(value);
  return value;
}
async function inlineQuestions(_card, _questions, _label, submit = null) {
  if (submit) await submit({});
  return {};
}
let scaleTokenFromQr = "";
"""

#: 从 m.js 原文取的声明。打 * 的是这次新加的：修前没有就取成空串（页面照修前的样子跑，断言在内容上红，而不是取不到）
HEADS = (
    "function setMsg(", "function kv(", "function spdQuery(", "function spdTagOf(", "const RELATION_NAMES = ",
    "let viewingPatientId = ", "*let selfName = ", "*let familyMembers = ", "*function viewingWho(",
    "*function renderViewingBar(", "async function renderFamily(", "async function renderServiceTab(",
    "async function renderSpdTab(", "const APPT_STATUS = ", "const SLOT_PAGE = ", "async function renderAppointments(",
    "const SPD_RISK_TAGS = ", "const SPD_LEVEL_TAGS = ", "async function renderSpdMeasure(",
    "async function renderSpdTasks(", "async function renderSpdFollowups(", "async function renderSpdPlans(",
    "const SPD_CONSULT_STATUS_TAGS = ", "async function renderSpdConsults(", "async function renderSpdScreen(",
)


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，跨行的对象常量取到顶格的 `};`，其余取这一行。"""
    if head.startswith("*"):
        head = head[1:]
        if head not in source:
            return ""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    line_end = source.index("\n", start)
    if source[start:line_end].endswith("{"):
        return source[start:source.index("\n};\n", start) + 4]
    return source[start:line_end + 1]


def run_page(steps: str):
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    script = (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
              + "".join(_top(source, head) for head in HEADS)
              + f"\n(async () => {{\n{steps}\n}})().then("
              + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def test_两个页签顶部写明当前对象_本人与代管成员_换人回我的档案():
    out = run_page("""
      const bars = () => [$("#service-who").innerHTML, $("#spd-who").innerHTML];
      await renderServiceTab(); await renderSpdTab();
      const self = bars();
      await renderFamily();            // 「我的档案」取 /me/family、画成员标签
      viewingPatientId = 2;            // 点了母亲那一枚（选人照旧只在「我的档案」里改）
      await renderServiceTab(); await renderSpdTab();
      const member = bars();
      const sw = $("#spd-who").querySelector("[data-viewing-switch]");
      if (sw) sw.listeners.click();
      OUT.signedOut = true;            // 掉线 / 退出后重画：姓名不留在页面上
      await renderServiceTab(); await renderSpdTab();
      return { self, member, switched: OUT.switched, signedOut: bars() };
    """)
    for html in out["self"]:
        assert f"当前：{SELF}（本人）" in html, html   # 修前两个页签上根本没有当前对象
        assert "data-viewing-switch" in html and "换人" in html
    for html in out["member"]:
        assert "当前：李桂兰&lt;b&gt;（父母）" in html, html
        assert "记在李桂兰&lt;b&gt;名下" in html and "<b>" not in html   # 姓名过 esc()
    assert out["switched"] == ["archive"]   # 「换人」回到「我的档案」的成员切换
    assert out["signedOut"] == ["", ""]


_WRITES = """
  const box = makeEl("box");
  const receipts = {};
  await renderSpdTab();
  await renderSpdMeasure(box);                     // 本人视角录一条：回执写本人
  $("#spd-value").value = "128";
  await $("#spd-measure-form").listeners.submit({ preventDefault() {} });
  receipts.selfMeasure = $("#spd-measure-msg").textContent;
  await renderFamily();
  viewingPatientId = 2;                            // 切到母亲
  await renderServiceTab(); await renderSpdTab();
  await renderAppointments(box);
  receipts.slotList = $("#slot-list").innerHTML;
  await $("#slot-list").querySelector(".book-slot").listeners.click();
  receipts.book = $("#appt-msg").textContent;
  await renderSpdMeasure(box);
  $("#spd-value").value = "165";
  await $("#spd-measure-form").listeners.submit({ preventDefault() {} });
  receipts.measure = $("#spd-measure-msg").textContent;
  await renderSpdTasks(box);
  await box.querySelector("[data-spd-task]").listeners.click();
  receipts.task = $("#spd-task-msg").textContent;
  await renderSpdPlans(box);
  await box.querySelector("[data-spd-read]").listeners.click();
  receipts.plan = $("#spd-plan-msg").textContent;
  await renderSpdConsults(box);
  $("#spd-consult-input").value = "最近早上头晕";
  await $("#spd-consult-send").listeners.click();
  receipts.consult = $("#spd-consult-msg").textContent;
  $("#spd-scale").value = "scr_demo";
  await renderSpdScreen(box);
  await $("#spd-screen-submit").listeners.click();
  receipts.screen = $("#spd-screen-msg").textContent;
  await renderSpdFollowups(box);
  await box.querySelector("[data-spd-self]").listeners.click();
  return { receipts, alerts: OUT.alerts, confirms: OUT.confirms,
    writes: OUT.requests.filter((r) => r.method === "POST") };
"""


def test_切到家人之后_写操作的回执写的是家人的姓名():
    out = run_page(_WRITES)
    r, who = out["receipts"], f"{MEMBER}（父母）"
    assert r["selfMeasure"] == f"已为{SELF}（本人）保存，指标偏高，请关注", r   # 修前「已保存，指标偏高，请关注」
    assert "为李桂兰&lt;b&gt;预约" in r["slotList"] and "该成员" not in r["slotList"]   # 修前「为该成员预约」
    assert r["book"] == f"已为{who}预约：城关卫生院 全科门诊 2026-10-12 08:00-09:00", r   # 修前约完不写回执
    assert r["measure"] == f"已为{who}保存，指标偏高，请关注", r
    assert r["task"] == f"已为{who}提交任务「每日测血压」", r
    assert r["plan"] == f"已为{who}标记已读并反馈", r
    assert r["consult"] == f"已为{who}发出咨询", r
    assert r["screen"].startswith(f"{who}的自查——风险等级：高危。"), r
    assert r["screen"].endswith("已申请专病管理服务，请等待基层医生复核。"), r
    assert out["confirms"] == [f"检测到中高风险，是否为{who}申请专病管理服务？"]
    assert out["alerts"] == [f"已为{who}提交，感谢配合"]
    # 沿用选择的行为不改：本人那条不带 patient_id，切到母亲之后的写操作全都记在母亲名下
    writes = out["writes"]
    assert [w["path"] for w in writes] == [
        "/api/portal/spd/measurements", "/api/portal/me/appointments", "/api/portal/spd/measurements",
        "/api/portal/spd/tasks/31/submit", "/api/portal/spd/interventions/41/feedback", "/api/portal/spd/consults",
        "/api/portal/spd/screenings", "/api/portal/spd/service-applies", "/api/portal/spd/followups/51/self-answer"]
    assert "patient_id" not in writes[0]["body"]
    assert all(w["body"]["patient_id"] == MEMBER_ID for w in writes[1:]), writes


def test_两个页签的页面上有当前对象位():
    html = (STATIC / "m" / "index.html").read_text(encoding="utf-8")
    for body_id, bar_id in (("service-body", "service-who"), ("spd-body", "spd-who")):
        block = html[html.index(f'<div id="{body_id}"'):]
        block = block[:block.index('<div class="seg">')]
        assert re.search(rf'<div id="{bar_id}"[^>]*>', block), (body_id, block)   # 修前分段控件上面什么都没有
