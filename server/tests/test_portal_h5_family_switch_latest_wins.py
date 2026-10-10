"""居民端「我的档案」家庭成员切换不比对请求序号、档案区不写是谁：点「母亲」又点回「本人」，母亲的档案晚到盖在「本人」标签下
（P2-1797，第五十三批扫描 AQ2-5）。

修前：`m.js` 的 `loadArchive` / `loadArchiveExtra` 都是「先写加载中 → await → 写」，不取序号（同文件号源 slotSeq、价格 priceSeq、
慢专病 spdSeq 都有）。扫描实测：高亮的是「陈本人」、viewingPatientId 为 null，档案区却是母亲的「高血压3级（极高危）」「血钾
6.8 mmol/L」危急值，知情同意区也是母亲的；这时签一项同意，按全局 viewingPatientId 记到本人名下，签完却按画那一块时闭包里的
查询重画，仍是母亲的清单，回执「已签署」，看着像没签上。档案出参自带 name，页面不显示。

修法：两块各取各的序号（附加区在签同意、提申请后会单独重画），过期的回包丢弃、出错那一支同样；档案区头写「健康档案：姓名」
（取出参的 name）；签同意、提申请之后的重画按当下的 viewingPatientId 取。

页面那条把 `m.js` 的 `renderFamily` / `loadArchive` / `loadArchiveExtra` 原文放进 node 跑（shared.js 整份加载）：`authApi` 按路径回
垫好的响应并记下写请求，可以把某个地址的回包压住；DOM 用一个够这几段用的小替身。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const OUT = { posts: [] };
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
globalThis.confirm = () => true;
/* 本人 patient_id=1、代管的母亲 patient_id=2（代管关系 7）。母亲有高血压与危急值、签过一项家庭代管授权；本人什么都没签 */
const FAMILY = [{ patient_id: 1, name: "陈本人", relation: "self", is_self: true },
  { patient_id: 2, member_id: 7, name: "陈母", relation: "parent", is_self: false }];
const ARCHIVE = {
  1: { name: "陈本人", ehc_no: "E1", chronic_care: [], exam_reports: [],
    encounters: [{ diagnosis_name: "急性上呼吸道感染", encounter_type: "outpatient", summary: "" }] },
  2: { name: "陈母", ehc_no: "E2",
    chronic_care: [{ disease: "hypertension", disease_name: "高血压", level: 3, next_followup_due: "2026-10-20",
      guidance_points: "" }],
    encounters: [{ diagnosis_name: "高血压3级（极高危）", encounter_type: "inpatient", summary: "" }],
    exam_reports: [{ conclusion: "血钾 6.8 mmol/L", critical: true }] },
};
const CONSENTS = { 1: [], 2: [{ scene: "family_delegate", text_version: "v1", method_name: "线上勾选", revoked_at: null }] };
const HELD = new Map();
function hold(path) { HELD.set(path, []); }
function release(path) { const queue = HELD.get(path) || []; HELD.delete(path); queue.forEach((go) => go()); }
function replyFor(method, path, body) {
  const bare = path.split("?")[0];
  const pid = /[?&]patient_id=2\b/.test(path) ? 2 : 1;
  if (method === "POST" && bare === "/api/portal/me/consents") {
    CONSENTS[body.patient_id || 1].push({ scene: body.scene, text_version: "v1", method_name: "线上勾选", revoked_at: null });
    return { id: 99 };
  }
  if (bare === "/api/portal/me/family") return FAMILY;
  if (bare === "/api/portal/me/archive") return ARCHIVE[pid];
  if (bare === "/api/portal/me/consents") return CONSENTS[pid];
  if (["/api/portal/me/enrollments/all", "/api/portal/me/corrections", "/api/access-logs/mine"].includes(bare)) return [];
  throw new Error(`没垫的接口：${method} ${path}`);
}
async function authApi(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  if (method !== "GET") OUT.posts.push({ path, body });
  if (HELD.has(path)) await new Promise((go) => HELD.get(path).push(go));
  return JSON.parse(JSON.stringify(replyFor(method, path, body)));
}
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0)); };
"""

HEADS = (
    "function setMsg(", "function kv(", "const LEVEL_TAGS = ", "const RELATION_NAMES = ", "const ENROLL_SOURCE_NAMES = ",
    "const CONSENT_SCENE_NAMES = ", "const CORRECT_FIELD_NAMES = ", "const CORRECTION_STATUS = ",
    "async function renderFamily(", "async function loadArchive(", "async function loadArchiveExtra(",
)

STEPS = r"""
const text = (sel) => $(sel).innerHTML.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ").trim();
const onChip = () => ((/<span class="chip on"[^>]*>\s*([^<]*)</.exec($("#family-switch").innerHTML) || [])[1] || "").trim();
const head = () => (/健康档案：([^<]*)</.exec($("#archive-result").innerHTML) || [])[1] || null;
const consentScenes = () => [...$("#archive-extra").innerHTML.matchAll(/<span class="k">场景<\/span><span>([^<]*)</g)]
  .map((m) => m[1]);
/* 点成员标签（点在名字上，不是 ×）：取当下画着的那一枚，挂的是最近一次 renderFamily 的处理 */
const tap = (pid) => $("#family-switch").querySelectorAll(".chip").find((c) => c.dataset.pid === String(pid))
  .listeners.click({ target: makeEl("em") });
const snap = () => ({ on: onChip(), viewing: viewingPatientId, head: head(), archive: text("#archive-result"),
  consents: consentScenes() });

await renderFamily();
await tap(1);   // 先把本人的档案画出来

// ① 点「母亲」、没等档案出来又点回「本人」，母亲的档案晚到
hold("/api/portal/me/archive?patient_id=2");
const toMother = tap(2);
await flush();
await tap(1);
release("/api/portal/me/archive?patient_id=2");
await toMother; await flush();
const late = snap();

// ② 母亲的档案到了、她的同意清单还在路上时点回「本人」，同意清单晚到
hold("/api/portal/me/consents?patient_id=2");
const toMother2 = tap(2);
await flush();
await tap(1);
release("/api/portal/me/consents?patient_id=2");
await toMother2; await flush();
const lateExtra = snap();

// ③ 停在母亲那里全部画完，再点「本人」——本人的档案还在路上，附加区这时还是母亲的同意清单；这时签一项「随访」同意
await tap(2);
const mother = snap();
hold("/api/portal/me/archive");
const toSelf = tap(1);
await flush();
$("#cs-scene").value = "followup";
await $("#consent-form").listeners.submit({ preventDefault() {} });
const signed = { ...snap(), msg: $("#consent-msg").textContent, post: OUT.posts[OUT.posts.length - 1] };
release("/api/portal/me/archive");
await toSelf; await flush();
const settled = snap();
return { late, lateExtra, mother, signed, settled };
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
    # 顶层的单行 `let`（当前查看对象、各处的请求序号……）整批取：都是字面量初值；修前没有的序号自然取不到，页面照修前的样子跑
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_切母亲再切回本人_母亲的回包晚到_档案区与知情同意区都是本人的_头上写本人():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    for case in ("late", "lateExtra"):
        got = out[case]
        assert got["on"] == "陈本人" and got["viewing"] is None, (case, got)
        assert got["head"] == "陈本人", (case, got)   # 修前档案区不写是谁
        assert "急性上呼吸道感染" in got["archive"], (case, got)
        assert "高血压3级" not in got["archive"] and "血钾" not in got["archive"], (case, got)   # 修前是母亲的诊断与危急值
        assert got["consents"] == [], (case, got)   # 修前知情同意区是母亲的那一条

    mother = out["mother"]   # 停在母亲那里：头上写母亲
    assert mother["on"] == "陈母" and mother["head"] == "陈母" and mother["consents"] == ["家庭代管授权"], mother

    signed = out["signed"]
    assert signed["post"] == {"path": "/api/portal/me/consents", "body": {
        "scene": "followup", "guardian_name": "", "guardian_id_card": "", "guardian_relation": ""}}, signed   # 记到本人名下
    assert signed["consents"] == ["随访"], signed   # 修前签完按母亲的查询重画，仍是母亲那一条，看着像没签上
    assert signed["msg"] == "已签署", signed

    settled = out["settled"]
    assert settled["on"] == "陈本人" and settled["head"] == "陈本人" and settled["consents"] == ["随访"], settled
