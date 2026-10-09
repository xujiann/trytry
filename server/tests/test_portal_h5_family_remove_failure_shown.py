"""居民端解除代管失败时报错写进缺省收起的「添加家庭成员」面板，点了 × 毫无反应、成员标签照旧（P2-1779，第五十二批扫描
AP1-5 家庭成员那一半）。

修前：`m.js` 的 `renderFamily` 里 × 的处理按 P2-378 接住了 DELETE 的报错，却写进 `#family-msg`——它在 `index.html` 缺省收起的
`<details id="family-add">` 里。另一台设备已解除（404「家庭成员不存在」）或网络失败时，页面上什么也看不见，那位成员的标签
也照旧挂着，再点还是 404。（同条的微信补绑回跳那一半属认证流程，不在这里改。）

修法：成员标签下面加一行消息（`#family-switch-msg`），解除的结果写那里；404 时按服务端现状重画标签——那位去掉、写明
「已解除」；别的失败（断网、服务端出错）服务端没说关系没了，标签照旧，只写原因。

页面那条把 `m.js` 的 `renderFamily` 原文放进 node 跑，`authApi` 按脚本回服务端现状，DOM 用一个够这一段用的小替身。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_消息行在成员标签旁_不在收起的添加面板里():
    html = (STATIC / "m" / "index.html").read_text(encoding="utf-8")
    switch, msg = html.index('<div id="family-switch"'), html.index('<p id="family-switch-msg"')
    fold = html.index('<details id="family-add"')
    fold_end = html.index("</details>", fold)
    assert switch < msg < fold, html[switch:fold]   # 修前没有这一行，报错只能写进 fold 里的 #family-msg
    assert '<p id="family-msg"' in html[fold:fold_end]


PRELUDE = r"""
const OUT = { requests: [], archiveLoads: 0 };
const registry = {};
function attrsOf(text) {
  const attrs = {};
  for (const m of text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? "";
  return attrs;
}
function makeEl(attrs = {}) {
  const classes = new Set((attrs.class || "").split(/\s+/).filter(Boolean));
  const dataset = {};
  Object.entries(attrs).forEach(([k, v]) => {
    if (k.startsWith("data-")) dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  });
  const el = { attrs, dataset, innerHTML: "", textContent: "", className: "", listeners: {},
    classList: { contains: (c) => classes.has(c) },
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelectorAll(sel) { return pick(el, sel); } };
  return el;
}
/* 从 innerHTML 里认出带某个类名的元素；innerHTML 没变时回同一批，挂的监听取得回来 */
function pick(parent, sel) {
  const cls = sel.replace(/^\./, "");
  const cache = (parent.picked ||= new Map());
  const key = `${sel}\u0000${parent.innerHTML}`;
  if (!cache.has(key)) {
    cache.set(key, [...parent.innerHTML.matchAll(/<(\w+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>/g)]
      .map((t) => attrsOf(t[2])).filter((a) => (a.class || "").split(/\s+/).includes(cls)).map((a) => makeEl(a)));
  }
  return cache.get(key);
}
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl()) };
globalThis.confirm = () => true;
/* 服务端现状：本人、母亲（代管关系 9）、儿子（代管关系 10） */
const SERVER = { family: [
  { patient_id: 1, name: "王建国", ehc_no: "E001", relation: "self", is_self: true },
  { patient_id: 2, name: "李桂兰", ehc_no: "E002", relation: "parent", is_self: false, member_id: 9 },
  { patient_id: 3, name: "王小明", ehc_no: "E003", relation: "child", is_self: false, member_id: 10 },
], offline: false };
async function authApi(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  OUT.requests.push(`${method} ${path}`);
  if (SERVER.offline) throw new TypeError("Failed to fetch");   // 断网：fetch 自己就抛了，没有状态码
  if (method === "GET") return JSON.parse(JSON.stringify(SERVER.family));
  const id = Number(path.split("/").pop());
  if (!SERVER.family.some((m) => m.member_id === id)) {
    const err = new Error("家庭成员不存在");
    err.status = 404;
    throw err;
  }
  SERVER.family = SERVER.family.filter((m) => m.member_id !== id);
  return { removed: true };
}
async function loadArchive() { OUT.archiveLoads += 1; }
"""

STEPS = r"""
const box = $("#family-switch"), msg = $("#family-switch-msg");
const clickX = async (pid, memberId) => {
  const chip = box.querySelectorAll(".chip").find((c) => c.dataset.pid === String(pid));
  await chip.listeners.click({ target: makeEl({ class: "x", "data-member": String(memberId) }) });
};
const state = () => ({ chips: box.querySelectorAll(".chip").map((c) => c.dataset.pid), msg: msg.textContent,
  cls: msg.className, foldMsg: $("#family-msg").textContent, viewing: viewingPatientId, loads: OUT.archiveLoads });
viewingPatientId = 2;            // 正看着母亲
await renderFamily();
const before = state();
SERVER.family = SERVER.family.filter((m) => m.member_id !== 9);   // ① 另一台设备上已经解除了母亲
await clickX(2, 9);
const gone = state();
SERVER.offline = true;           // ② 断网时解除儿子
await clickX(3, 10);
const offline = state();
return { before, gone, offline, requests: OUT.requests };
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_解除失败写在标签旁_404按服务端现状去掉那位_断网标签照旧():
    source = (STATIC / "m" / "m.js").read_text(encoding="utf-8")

    def top(head: str, optional: bool = False) -> str:
        if optional and head not in source:
            return ""
        start = source.index(head)
        if head.startswith(("function ", "async function ")):
            return source[start:source.index("\n}\n", start) + 3]
        return source[start:source.index("\n", start) + 1]

    script = (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n"
              + top("function setMsg(") + top("let viewingPatientId = ") + top("const RELATION_NAMES = ")
              + top("let familyMembers = ", optional=True) + top("async function renderFamily(")
              + f"\n(async () => {{\n{STEPS}\n}})().then("
              + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["before"]["chips"] == ["1", "2", "3"] and out["before"]["msg"] == ""
    gone = out["gone"]
    assert gone["msg"] == "「李桂兰」的代管已解除（可能已在其他设备上解除）" and gone["cls"] == "msg ok", gone   # 修前空着
    assert gone["foldMsg"] == "", gone              # 修前「家庭成员不存在」写进收起的添加面板
    assert gone["chips"] == ["1", "3"], gone        # 修前母亲的标签照旧挂着
    assert gone["viewing"] is None and gone["loads"] == 1, gone   # 正看着的那位没了，回落本人、档案重取
    offline = out["offline"]
    assert offline["msg"] == "解除代管没有成功：Failed to fetch" and offline["cls"] == "msg err", offline
    assert offline["chips"] == ["1", "3"] and offline["loads"] == 1, offline   # 服务端没说关系没了：标签照旧
    assert out["requests"] == ["GET /api/portal/me/family", "DELETE /api/portal/me/family/9", "GET /api/portal/me/family",
                               "DELETE /api/portal/me/family/10"], out["requests"]
