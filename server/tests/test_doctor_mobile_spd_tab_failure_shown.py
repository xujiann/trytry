"""医生移动端「慢专病」页签取工作台失败时整块空白或挂着旧数、不说原因；动作已成功、紧跟的重画失败却被当成动作失败（P2-1800，
第五十三批扫描 AQ1-1）。

修前（`m/doctor.js`）：
* `loadSpdTab` 第一句 `await api("/api/spd/workbench/doctor-mobile")` 没有 try，`switchTab` 以 `TABS[tab]()` 调用、不 await 不 catch——
  500、断网、428 口令到期时留下一个未处理的 rejection，`#spd-wb`、`#spd-list` 都不动：首次进页一片空白，同一次登录里再进则挂着
  上一次的「我的待办 / 今日随访 / 待复核转诊」数。同文件其余七个取数函数都自己兜错、把原因写进本块（如 `loadRound`）。
* `spdPost` 先写「操作成功」再 `await loadSpdTab()`，重画一失败报错就抛回动作：卡片表单写「请求失败(502)」、填的理由还留着，
  非卡片按钮则把「操作成功」盖成报错——看着像没交上，医生再交一次就重复开在途上转单（P2-538）、重复挂佐证附件。上传佐证、
  签到 / 兑换同一写法。

修法：`loadSpdTab` 自己兜错——取不到工作台把原因写进 `#spd-wb`，照常调 `loadSpdList`（它自己兜错），并把这个错回给调用方；
动作成功之后统一走 `spdActionDone`：回执先写上再重画，重画取不到工作台时回执照留、另写一句「已办理，刷新失败：…」，不抛回卡片表单。

页面那条把 `m/doctor.js` 慢专病页签的几段原文放进 node 跑（shared.js 整份加载）：`api` 按路径回垫好的响应，可以让全部 GET、只有
工作台、或写请求按指定状态码失败；未处理的 rejection 记下来；DOM 用一个够这几段用的小替身。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const OUT = { posts: [], unhandled: [], created: [] };
process.on("unhandledRejection", (e) => { OUT.unhandled.push(String(e && e.message)); });
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
    name, attrs, dataset, innerHTML: "", textContent: "", value: "", className: "", listeners: {}, files: [],
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)) },
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelector(sel) { return pick(el, sel)[0] || null; },
    querySelectorAll(sel) { return pick(el, sel); },
    click() {},
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
  querySelectorAll: () => [], createElement: (tag) => { const el = makeEl(tag); OUT.created.push(el); return el; } };
globalThis.window = { scrollTo() {} };
/* 失败开关：fail 让全部 GET 失败；failWorkbench 只让工作台失败；postFail 让写请求失败 */
const STATE = { fail: null, failWorkbench: null, postFail: null };
const WORKBENCH = { user: { id: 5, name: "张医生", member_roles: ["doctor"], is_village_doctor: false },
  todo: { open: 3, due_today: 1, overdue: 0 }, calendar: { today: "2026-10-10", followups: 2, revisits: 0 },
  referrals: { pending_review: 1, pending_accept: 0, pending_receive: 0 }, patients: { mine: 12, village: 0 },
  points: { balance: 105 }, performance: null };
const TODOS = [{ id: 9, title: "上门测压留照", patient_name: "王甲", task_type: "followup", due_date: "2026-10-12",
  status: "claimed", require_evidence: true, evidence: [] }];
const GETS = {
  "/api/spd/workbench/doctor-mobile": WORKBENCH,
  "/api/spd/tasks?mine=true&open_only=true&limit=30": TODOS,
  "/api/spd/point-accounts/me": { balance: 100, earned: 120, used: 20, records: [] },
  "/api/spd/goods": [],
  "/api/spd/redeems?mine=true&limit=20": [],
};
const POSTS = { "/api/spd/point-accounts/signin": { points: 5, balance: 105 }, "/api/spd/tasks/9/evidence": { evidence: [31] } };
const httpError = ({ status, detail }) => Object.assign(new Error(errorText(detail, `请求失败(${status})`)), { status });
async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  if (method !== "GET") {
    OUT.posts.push(`${method} ${path}`);
    if (STATE.postFail) throw httpError(STATE.postFail);
    return JSON.parse(JSON.stringify(POSTS[path] || { id: 1 }));
  }
  if (STATE.fail) throw httpError(STATE.fail);
  if (path === "/api/spd/workbench/doctor-mobile" && STATE.failWorkbench) throw httpError(STATE.failWorkbench);
  if (!(path in GETS)) throw new Error(`没垫的接口：${path}`);
  const data = JSON.parse(JSON.stringify(GETS[path]));
  return options.withTotal ? { rows: data, total: Array.isArray(data) ? data.length : null } : data;
}
async function uploadAttachment() { return { id: 31 }; }
function isAuthed() { return true; }
async function loadTodos() {}
async function loadCritical() {}
async function loadExams() {}
async function loadRound() {}
async function loadSurgery() {}
async function loadChronic() {}
async function loadPatientTab() {}
const flush = async () => { for (let i = 0; i < 8; i += 1) await new Promise((r) => setTimeout(r, 0)); };
"""

#: 从 doctor.js 原文取的声明。打 * 的是这次新加的：修前没有就取成空串（页面照修前的样子跑，断言在内容上红，而不是取不到）
HEADS = (
    "function setMsg(", "function kv(", "async function loadSpdTab(", "async function loadSpdList(",
    "function spdTodoOps(", "*function spdListedHint(", "async function loadSpdTodo(", "const REDEEM_STATUS_NAMES = ",
    "async function loadSpdPerf(",
    "*async function spdActionDone(", "async function spdPost(", "const TABS = ", "function switchTab(",
)

STEPS = r"""
const text = (sel) => $(sel).innerHTML.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();

// ① 先正常进一次（工作台有数），再在口令到期（全部接口 428）时切进来
switchTab("spd"); await flush();
const fresh = text("#spd-wb");
STATE.fail = { status: 428, detail: "口令已超过有效期，请先修改密码" };
switchTab("spd"); await flush();
const expired = { wb: text("#spd-wb"), list: text("#spd-list"), unhandled: [...OUT.unhandled] };
STATE.fail = null;

// ② 动作成功、紧跟的重画取不到工作台（网关 502）：卡片表单的提交（inline）与非卡片按钮（接收）
switchTab("spd"); await flush();
STATE.failWorkbench = { status: 502 };
let thrown = null;
try { await spdPost("/api/spd/tasks/7/complete", { result: { note: "已电话随访" } }, true); } catch (err) { thrown = err.message; }
const inline = { thrown, msg: $("#spd-msg").textContent, wb: text("#spd-wb") };
await spdPost("/api/spd/tasks/8/claim");
const claim = $("#spd-msg").textContent;

// ③ 动作本身失败：卡片表单照旧收到报错，非卡片按钮照旧写原因
STATE.postFail = { status: 409, detail: "该任务已结束" };
let refused = null;
try { await spdPost("/api/spd/tasks/7/complete", { result: { note: "" } }, true); } catch (err) { refused = err.message; }
await spdPost("/api/spd/tasks/8/claim");
const failed = { refused, msg: $("#spd-msg").textContent };
STATE.postFail = null;

// ④ 上传佐证：传上了、重画取不到工作台
STATE.failWorkbench = null;
switchTab("spd"); await flush();
STATE.failWorkbench = { status: 502 };
$("#spd-list").querySelector("[data-spd-evidence]").listeners.click();
const input = OUT.created[OUT.created.length - 1];
input.files = [{ name: "bp.jpg" }];
await input.onchange(); await flush();
const evidence = $("#spd-msg").textContent;

// ⑤ 签到：签上了、重画取不到工作台
STATE.failWorkbench = null;
activeDoctorSpd = "perf"; await loadSpdList(); await flush();
STATE.failWorkbench = { status: 502 };
$("#spd-list").querySelector("[data-spd-signin]").listeners.click(); await flush();
const signin = $("#spd-msg").textContent;
return { fresh, expired, inline, claim, failed, evidence, signin, posts: OUT.posts, unhandled: OUT.unhandled };
"""


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，常量取到语句末的 `;`（可以跨行）。"""
    if head.startswith("*"):
        head = head[1:]
        if head not in source:
            return ""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    return source[start:source.index(";\n", start) + 2]


def _script(steps: str) -> str:
    source = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    # 顶层的单行 `let`（当前分段、本人、请求序号……）整批取：都是字面量初值
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_取不到工作台写出原因_动作成功重画失败不当成动作失败():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    assert "我的待办 3 条（今日到期 1）" in out["fresh"], out["fresh"]   # 夹具自证：正常时画出工作台
    expired = out["expired"]
    assert expired["wb"] == "口令已超过有效期，请先修改密码", expired   # 修前首次空白、再进挂着上一次的数
    assert expired["list"] == "口令已超过有效期，请先修改密码", expired   # 清单照常取，原因写在清单里
    assert expired["unhandled"] == [], expired   # 修前留下一个未处理的 rejection

    inline = out["inline"]
    assert inline["thrown"] is None, inline   # 修前「请求失败(502)」抛回卡片表单，填的理由留着、像没交上
    assert inline["msg"] == "操作成功；已办理，刷新失败：请求失败(502)", inline
    assert inline["wb"] == "请求失败(502)", inline
    assert out["claim"] == "操作成功；已办理，刷新失败：请求失败(502)", out["claim"]   # 修前盖成「请求失败(502)」

    assert out["failed"] == {"refused": "该任务已结束", "msg": "该任务已结束"}, out["failed"]   # 动作本身失败照旧报

    assert out["evidence"] == "佐证已上传（共 1 份），办结时一并核验；已办理，刷新失败：请求失败(502)", out["evidence"]
    assert out["signin"] == "签到成功：+5 分，余额 105；已办理，刷新失败：请求失败(502)", out["signin"]
    assert out["posts"] == ["POST /api/spd/tasks/7/complete", "POST /api/spd/tasks/8/claim",
                            "POST /api/spd/tasks/7/complete", "POST /api/spd/tasks/8/claim",
                            "POST /api/spd/tasks/9/evidence", "POST /api/spd/point-accounts/signin"], out["posts"]
    assert out["unhandled"] == [], out["unhandled"]
