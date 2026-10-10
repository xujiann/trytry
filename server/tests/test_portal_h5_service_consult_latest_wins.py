"""居民端两处就地重画没有串行化或序号：「在线服务」连点分段高亮与内容对不上；「看近两年的」绕过 loadSpd 的串行化；「在线咨询」
对话区画成晚到的 A 会话、不写是哪个会话，「继续沟通」按 A 的病种发出（P2-1799，第五十三批扫描 AQ2-7）。

修前（`m.js`）：
* `loadService` 无序号、无互斥——先点「签约」、没等出来又点「账单」，签约那次晚到，高亮的是「账单」、内容区是签约（同文件
  `loadSpd` 的注释原话：「慢的后落地就把新分段整个盖掉」）；
* 监测页空了给的「看近两年的」直接调 `renderSpdMeasure(box, 730)`，绕过 `loadSpd` 的串行化——两年的数据慢，期间切到别的
  分段，它后落地就把新分段盖掉；
* `showConsultThread` 不取序号：先点 A 会话（高血压）的「查看对话」、又点 B（糖尿病），A 的消息晚到，对话区画成 A 的且不写是
  哪个会话，在里面「继续沟通」发出去的病种是高血压（扫描实测 `{"program_code":"htn",…}`）。

修法：`loadService` 照 `loadSpd` 的串行化写法（序号 + 互斥 + 收尾补画，静态钉见 `test_mobile_render_serialization.py`）；「看近两年的」
走 `loadSpd(730)`，由它记下窗口、按最后一次的画；对话区取序号，过期的回包丢弃、出错那一支同样，头写病种与会话号，发送跟着画出来
的那一段走。

页面那条把 `m.js` 的这几段原文（含分段按钮的点击绑定）放进 node 跑（shared.js 整份加载）：`authApi` 按路径回垫好的响应并记下
写请求，可以把某个地址的回包压住；DOM 用一个够这几段用的小替身，分段按钮是几枚记得住高亮的桩子。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const OUT = { posts: [], gets: [] };
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
/* 两排分段按钮（index.html 里写死的那几枚，这里只放用到的） */
const SVC_BUTTONS = ["appointment", "contract", "bill"].map((k, i) =>
  makeEl("btn", { class: `seg-btn${i ? "" : " active"}`, "data-svc": k }));
const SPD_BUTTONS = ["home", "measure", "revisit", "consult"].map((k, i) =>
  makeEl("btn", { class: `seg-btn${i ? "" : " active"}`, "data-spd": k }));
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl(sel)),
  querySelectorAll: (sel) => (sel === ".seg-btn[data-svc]" ? SVC_BUTTONS : sel === "[data-spd]" ? SPD_BUTTONS : []),
  createElement: (tag) => makeEl(tag) };
globalThis.confirm = () => true;
globalThis.alert = () => {};
const CONSULTS = [
  { id: 11, program_code: "htn", program_name: "高血压", status: "open", created_at: "2026-10-01T09:00:00" },
  { id: 12, program_code: "dm", program_name: "糖尿病", status: "open", created_at: "2026-10-05T09:00:00" },
];
const REPLIES = {
  "/api/portal/me/contract": [{ org_name: "东镇卫生院", doctor_name: "吴医生", package: "basic", signed_date: "2026-01-01",
    status: "active", services: [], services_total: 0 }],
  "/api/portal/me/bills": [{ org_name: "县人民医院", bill_type: "outpatient", total_amount: 120, insurance_pay: 80,
    self_pay: 40, paid: false, outstanding: 40, date: "2026-10-02" }],
  "/api/portal/spd/consults": CONSULTS,
  "/api/portal/spd/home": { enrolled: true, programs: [{ program_code: "htn", program_name: "高血压" },
    { program_code: "dm", program_name: "糖尿病" }] },
  "/api/portal/spd/consults/11/messages": [{ sender: "doctor", content: "降压药按时吃，周五复测",
    created_at: "2026-10-01T10:00:00" }],
  "/api/portal/spd/consults/12/messages": [{ sender: "doctor", content: "空腹血糖 8.9 偏高，二甲双胍加量前先来门诊",
    created_at: "2026-10-05T10:00:00" }],
  "/api/portal/spd/measurements?limit=30&days=90": [],
  "/api/portal/spd/measurements?limit=30&days=730": [{ metric: "bp_sys", value: 150, unit: "mmHg", level: "high",
    source_name: "居民自测", measured_at: "2025-03-01T08:00:00" }],
  "/api/portal/spd/revisits": [{ plan_date: "2026-10-20", dept: "心内科", items: "血脂四项", status: "planned" }],
};
const HELD = new Map();
function hold(path) { HELD.set(path, []); }
function release(path) { const queue = HELD.get(path) || []; HELD.delete(path); queue.forEach((go) => go()); }
async function authApi(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET") {
    OUT.posts.push({ path, body: JSON.parse(options.body) });
    return { id: 12 };
  }
  OUT.gets.push(path);
  if (HELD.has(path)) await new Promise((go) => HELD.get(path).push(go));
  if (!(path in REPLIES)) throw new Error(`没垫的接口：${path}`);
  return JSON.parse(JSON.stringify(REPLIES[path]));
}
function isAuthed() { return true; }
function switchTab() {}
const flush = async () => { for (let i = 0; i < 8; i += 1) await new Promise((r) => setTimeout(r, 0)); };
"""

HEADS = (
    "function setMsg(", "function kv(", "const RELATION_NAMES = ", "function viewingWho(", "function svcQuery(",
    "const PACKAGES = ", "const SERVICE_TYPES = ", "async function renderContracts(", "async function renderBills(",
    "async function loadService(", 'document.querySelectorAll(".seg-btn[data-svc]").forEach(', "function spdQuery(",
    "function spdTagOf(", "const SPD_LEVEL_TAGS = ", "const SPD_REVISIT_STATUS = ", "const SPD_CONSULT_STATUS_TAGS = ",
    "async function loadSpd(", "async function renderSpdMeasure(", "async function renderSpdRevisits(",
    "async function renderSpdConsults(", "async function showConsultThread(", 'document.querySelectorAll("[data-spd]").forEach(',
)

STEPS = r"""
const text = (sel) => $(sel).innerHTML.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const lit = (buttons, key) => buttons.filter((b) => b.classList.contains("active")).map((b) => b.dataset[key]);
const svc = (k) => SVC_BUTTONS.find((b) => b.dataset.svc === k);
const seg = (k) => SPD_BUTTONS.find((b) => b.dataset.spd === k);

// ① 在线服务：点「签约」，没等出来又点「账单」，签约那次晚到
hold("/api/portal/me/contract");
svc("contract").listeners.click();
await flush();
svc("bill").listeners.click();
await flush();
release("/api/portal/me/contract");
await flush();
const service = { lit: lit(SVC_BUTTONS, "svc"), content: text("#service-result") };

// ② 在线咨询：先点 A 会话（高血压）的「查看对话」，又点 B（糖尿病），A 的消息晚到；然后在对话区「继续沟通」
seg("consult").listeners.click();
await flush();
const box = $("#spd-result");
const open = (id) => box.querySelectorAll(".consult-open").find((b) => b.dataset.consult === String(id)).listeners.click();
hold("/api/portal/spd/consults/11/messages");
open(11);
await flush();
open(12);
await flush();
release("/api/portal/spd/consults/11/messages");
await flush();
const thread = $("#spd-consult-thread").innerHTML;
$("#spd-thread-input").value = "血糖仪读数还是 9 左右，二甲双胍要不要加量？";
await $("#spd-thread-send").listeners.click();
await flush();
const consult = { title: (/<div class="sec-title">(对话记录[^<]*)</.exec(thread) || [])[1] || "",
  isA: thread.includes("降压药"), isB: thread.includes("空腹血糖"), posts: OUT.posts };

// ③ 监测：近 90 天没有记录，点「看近两年的」——两年的数据慢，没等出来切到「复诊」
seg("measure").listeners.click();
await flush();
const empty90 = text("#spd-result");
hold("/api/portal/spd/measurements?limit=30&days=730");
box.querySelector("[data-spd-older]").listeners.click();
await flush();
seg("revisit").listeners.click();
await flush();
release("/api/portal/spd/measurements?limit=30&days=730");
await flush();
const raced = { lit: lit(SPD_BUTTONS, "spd"), content: text("#spd-result") };
// 不打岔时「看近两年的」照样画出近两年的记录
seg("measure").listeners.click();
await flush();
box.querySelector("[data-spd-older]").listeners.click();
await flush();
const twoYears = text("#spd-result");
return { service, consult, empty90, raced, twoYears, gets: OUT.gets };
"""


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，顶层的绑定语句取到顶格的 `});`，常量取到语句末的 `;`（可以跨行）。"""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    if head.startswith("document."):
        return source[start:source.index("\n});\n", start) + 5]
    return source[start:source.index(";\n", start) + 2]


def _script(steps: str) -> str:
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    # 顶层的单行 `let`（当前分段、各处的请求序号与互斥……）整批取：都是字面量初值；修前没有的自然取不到，页面照修前的样子跑
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_连点分段_乱序回包后高亮与内容一致_对话区是后点开的那一段():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    service = out["service"]
    assert service["lit"] == ["bill"], service
    assert "县人民医院" in service["content"] and "东镇卫生院" not in service["content"], service   # 修前内容区是签约

    consult = out["consult"]
    assert consult["isB"] and not consult["isA"], consult   # 修前对话区画成晚到的 A
    assert consult["title"] == "对话记录 · 糖尿病（会话 #12）", consult   # 修前只写「对话记录」
    assert consult["posts"] == [{"path": "/api/portal/spd/consults", "body": {
        "program_code": "dm", "content": "血糖仪读数还是 9 左右，二甲双胍要不要加量？"}}], consult   # 修前按 A 的 htn 发出

    assert "近 90 天没有记录" in out["empty90"] and "看近两年的" in out["empty90"], out["empty90"]
    raced = out["raced"]
    assert raced["lit"] == ["revisit"], raced
    assert "心内科" in raced["content"] and "近两年" not in raced["content"], raced   # 修前两年的监测记录盖掉了复诊
    assert "近两年的记录（最新 30 条）" in out["twoYears"] and "150" in out["twoYears"], out["twoYears"]
    assert out["gets"].count("/api/portal/spd/measurements?limit=30&days=730") == 2, out["gets"]
