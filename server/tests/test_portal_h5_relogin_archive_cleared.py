"""居民端换人登录：上一位的档案、家人标签、调阅记录留在页面里，下一位登录后第一个往返内与新姓名同屏（P2-1796，第五十三批
扫描 AQ2-4）。

修前：`m.js` 的 `signOutLocally`（点「退出」与掉线共用）按 P2-1219 清了慢专病结果区 `#spd-result`，「我的档案」页却只切回
登录框——账号栏、家人标签 `#family-switch`、档案区 `#archive-result`、附加区 `#archive-extra`（含「谁看过我的档案」）原样
留在页面里。家人共用手机或自助机，下一位登录时 `renderArchiveTab` 先 `showPane("archive")` 写上新姓名、再 `await renderFamily()`
等 `/me/family` 一个往返，这期间屏幕上是新姓名配上一位的家人、诊断、危急值与调阅记录（扫描实测：账号栏「周乙」，家人标签
「孙甲 本人 孙甲之母 父母」，档案区「抑郁发作…HIV 抗体初筛阳性」）。

修法：退出即把档案页按登录人画的几块一并清空（账号栏、补绑行、家人标签及其消息行、档案区、附加区），与慢专病结果区同一处、
同一理由。端到端用例 `test_居民端在慢专病页签退出或掉线_本人档案不留在屏幕上`（P2-1219）补了这几块为空的断言。

页面那条把 `m.js` 里档案页的几个函数与 `signOutLocally` 原文放进 node 跑（shared.js 整份加载）：`authApi` 按「发请求那一刻
登录的是谁」回垫好的响应，可以把某个地址的回包压住；DOM 用一个够这几段用的小替身。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const OUT = { requests: [] };
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
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelector(sel) { return pick(el, sel)[0] || null; },
    querySelectorAll(sel) { return pick(el, sel); },
  };
  return el;
}
/* 从 innerHTML 里认出带某个类名的元素；innerHTML 没变时回同一批，挂的监听取得回来 */
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
globalThis.confirm = () => true;
globalThis.alert = () => {};
/* 同一台手机先后两位登录：甲（代管着母亲，有就诊、危急值与调阅记录）、乙（只有本人，什么都还没有） */
const SERVER = {
  甲: {
    me: { bound: true, name: "孙甲", ehc_no: "E-JIA", phone: "13800000001", wechat_bound: true },
    family: [{ patient_id: 1, name: "孙甲", relation: "self", is_self: true },
      { patient_id: 3, member_id: 9, name: "孙甲之母", relation: "parent", is_self: false }],
    archive: { name: "孙甲", ehc_no: "E-JIA", chronic_care: [],
      encounters: [{ diagnosis_name: "抑郁发作", encounter_type: "outpatient", summary: "" }],
      exam_reports: [{ conclusion: "HIV 抗体初筛阳性，待确证", critical: true }] },
    views: [{ at: "2026-10-08T09:00:00", viewer: "李医生", viewer_org_name: "县人民医院", resource_name: "就诊记录",
      basis_name: "本机构就诊" }],
  },
  乙: {
    me: { bound: true, name: "周乙", ehc_no: "E-YI", phone: "13800000002", wechat_bound: true },
    family: [{ patient_id: 2, name: "周乙", relation: "self", is_self: true }],
    archive: { name: "周乙", ehc_no: "E-YI", chronic_care: [], encounters: [], exam_reports: [] },
    views: [],
  },
};
const STATE = { authed: false, who: null };
const HELD = new Map();
function hold(path) { HELD.set(path, []); }
function release(path) { const queue = HELD.get(path) || []; HELD.delete(path); queue.forEach((go) => go()); }
function replyFor(who, method, path) {
  const s = SERVER[who];
  const bare = path.split("?")[0];
  if (bare === "/api/portal/me") return s.me;
  if (bare === "/api/portal/me/family") return s.family;
  if (bare === "/api/portal/me/archive") return s.archive;
  if (bare === "/api/access-logs/mine") return s.views;
  if (["/api/portal/me/enrollments/all", "/api/portal/me/consents", "/api/portal/me/corrections"].includes(bare)) return [];
  throw new Error(`没垫的接口：${method} ${path}`);
}
async function authApi(path, options = {}) {
  if (!STATE.authed) throw new Error("请先登录");
  const method = (options.method || "GET").toUpperCase();
  const who = STATE.who;   // 回包按发请求那一刻登录的那位
  OUT.requests.push(`${who} ${method} ${path}`);
  const bare = path.split("?")[0];
  if (HELD.has(bare)) await new Promise((go) => HELD.get(bare).push(go));
  return JSON.parse(JSON.stringify(replyFor(who, method, path)));
}
function isAuthed() { return STATE.authed; }
function clearAuth() { STATE.authed = false; }
async function api() { throw new Error("档案页不走免登录接口"); }
async function renderServiceTab() {}
async function renderSurveyTab() {}
async function renderNotifyTab() {}
async function renderSpdTab() {}
async function refreshNotifyDot() {}
async function bindWeChat() {}
function switchTab() {}
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0)); };
"""

#: 从 m.js 原文取的声明（顶层的单行 `let` 另外整批取，见 `_script`）
HEADS = (
    "function setMsg(", "function kv(", "function showPane(", "const LEVEL_TAGS = ", "const RELATION_NAMES = ",
    "const ENROLL_SOURCE_NAMES = ", "const CONSENT_SCENE_NAMES = ", "const CORRECT_FIELD_NAMES = ",
    "const CORRECTION_STATUS = ", "const WX_BIND_FLAG = ", "async function renderArchiveTab(",
    "function renderAccountBinding(", "async function renderFamily(", "async function loadArchive(",
    "async function loadArchiveExtra(", "function signOutLocally(",
)

STEPS = r"""
const text = (sel) => $(sel).innerHTML.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
/* 甲的东西：姓名（含家人「孙甲之母」）、诊断、检查结论、调阅人 */
const LEAKS = ["孙甲", "抑郁发作", "HIV", "李医生"];
const leaked = (sel) => LEAKS.filter((w) => $(sel).innerHTML.includes(w));
const BLOCKS = ["#account-bar", "#account-bind", "#family-switch", "#archive-result", "#archive-extra"];

STATE.authed = true; STATE.who = "甲";          // 甲登录、看自己的档案
await renderArchiveTab();
const jia = { bar: text("#account-bar"), family: text("#family-switch"), archive: leaked("#archive-result"),
  extra: leaked("#archive-extra") };
setMsg("#family-switch-msg", "「孙甲之母」的代管已解除（可能已在其他设备上解除）", true);   // 上一次解除留下的那一行

signOutLocally();                               // 甲点「退出」（掉线走同一段）
await flush();
const after = { login: !$("#pane-login").classList.contains("hidden"),
  blocks: Object.fromEntries(BLOCKS.map((sel) => [sel, $(sel).innerHTML])), msg: $("#family-switch-msg").textContent };

STATE.authed = true; STATE.who = "乙";          // 乙在同一台手机上登录：/me/family 的回包还在路上
hold("/api/portal/me/family");
const pending = renderArchiveTab();
await flush();
const during = { shown: !$("#pane-archive").classList.contains("hidden"), bar: text("#account-bar"),
  leaks: Object.fromEntries(BLOCKS.map((sel) => [sel, leaked(sel)])) };
release("/api/portal/me/family");
await pending;
const done = { family: text("#family-switch"), archive: text("#archive-result"), extra: leaked("#archive-extra") };
return { jia, after, during, done, requests: OUT.requests };
"""


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，跨行的对象常量取到顶格的 `};`，其余取这一行。"""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    line_end = source.index("\n", start)
    if source[start:line_end].endswith("{"):
        return source[start:source.index("\n};\n", start) + 4]
    return source[start:line_end + 1]


def _script(steps: str) -> str:
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    # 顶层的单行 `let`（当前查看对象、各处的请求序号……）整批取：都是字面量初值，取全了不必跟着改名单
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_甲退出后档案页几块清空_乙登录第一个往返内看不到甲的家人诊断与调阅记录():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    jia = out["jia"]   # 夹具自证：甲的档案页确实画出了这些
    assert "孙甲" in jia["bar"] and "孙甲之母" in jia["family"], jia
    assert {"抑郁发作", "HIV"} <= set(jia["archive"]) and jia["extra"] == ["李医生"], jia

    after = out["after"]
    assert after["login"], after
    assert after["blocks"] == {sel: "" for sel in after["blocks"]}, after   # 修前甲的账号栏、家人、档案、调阅记录原样留着
    assert after["msg"] == "", after   # 写着甲家人姓名的那一行也不留

    during = out["during"]
    assert during["shown"] and "周乙" in during["bar"], during   # 乙的档案页已露出、账号栏是乙
    assert during["leaks"] == {sel: [] for sel in during["leaks"]}, during   # 修前与乙的姓名同屏的是甲的家人、诊断、调阅记录

    done_ = out["done"]
    assert "周乙" in done_["family"] and "孙甲" not in done_["family"], done_
    assert "就诊记录（0）" in done_["archive"] and done_["extra"] == [], done_
    # 乙登录后的请求都以乙的身份发出（夹具自证：上面看到的不是乙的回包里混进了甲）
    assert all(r.startswith("乙 ") for r in out["requests"][out["requests"].index("乙 GET /api/portal/me"):]), out["requests"]
