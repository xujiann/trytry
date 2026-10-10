"""医生移动端慢专病「我的待办」「我的患者」各只取 30 条，列不全也不说（P2-1801，第五十三批扫描 AQ1 报告「顺带看到」）。

修前：`m/doctor.js` 的 `loadSpdTodo` 取 `/api/spd/tasks?mine=true&open_only=true&limit=30`、`loadSpdPatients` 取
`/api/spd/enrollments?limit=30&…`，都不读总数：本人名下 35 条待办只列 30 张卡片，页面不提示截断；同一页签上方工作台卡片
「我的待办 N 条」与这一段同一口径，N 可以大于 30，对不上也看不出为什么。两个接口都经 `deps.paginate` 发 X-Total-Count。
同形已修：查房条数读总数（P2-1771）、P2-1550 / P2-1693。

修法：照 P2-1771 读 X-Total-Count（`api` 的 `withTotal`），列不全时在清单头写「已列 N / 共 M 条」，列全（或接口没给总数）时
与原先一字不差。要不要续页取全不在本条（只做截断可见）。

页面那条把 `m/doctor.js` 这两段原文放进 node 跑（shared.js 整份加载）：`api` 按 limit 截一页、带 withTotal 时一并回总数；DOM 用一个
够这两段用的小替身。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"

PRELUDE = r"""
const registry = {};
function attrsOf(text) {
  const attrs = {};
  for (const m of text.matchAll(/([\w-]+)(?:="([^"]*)")?/g)) attrs[m[1]] = m[2] ?? "";
  return attrs;
}
function makeEl(name, attrs = {}) {
  const dataset = {};
  Object.entries(attrs).forEach(([k, v]) => {
    if (k.startsWith("data-")) dataset[k.slice(5).replace(/-(\w)/g, (_, c) => c.toUpperCase())] = v;
  });
  const el = { name, attrs, dataset, innerHTML: "", textContent: "", listeners: {},
    addEventListener(type, fn) { el.listeners[type] = fn; },
    querySelectorAll(sel) {
      const m = sel.match(/^\[([\w-]+)\]$/);
      return m ? [...el.innerHTML.matchAll(/<(\w+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*>/g)].map((t) => attrsOf(t[2]))
        .filter((a) => m[1] in a).map((a) => makeEl("el", a)) : [];
    } };
  return el;
}
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl(sel)) };
/* 本人名下的待办与在管档案各 COUNT 条（每次跑之前由步骤设） */
const DATA = { count: 0 };
const todos = () => Array.from({ length: DATA.count }, (_, i) => ({ id: i + 1, title: `随访任务${i + 1}`,
  patient_name: `患者${i + 1}`, task_type: "followup", due_date: "2026-10-12", status: "claimed" }));
const enrollments = () => Array.from({ length: DATA.count }, (_, i) => ({ id: i + 1, patient_id: i + 1,
  patient_name: `患者${i + 1}`, program_code: "hypertension", risk_level: "mid", stage: "", next_followup_at: "" }));
async function api(path, options = {}) {
  const all = path.startsWith("/api/spd/tasks?") ? todos() : path.startsWith("/api/spd/enrollments?") ? enrollments() : null;
  if (!all) throw new Error(`没垫的接口：${path}`);
  const rows = all.slice(0, Number(new URLSearchParams(path.split("?")[1]).get("limit")));
  return options.withTotal ? { rows, total: all.length } : rows;   // 接口经 paginate 发 X-Total-Count
}
"""

#: 从 doctor.js 原文取的声明。打 * 的是这次新加的：修前没有就取成空串（页面照修前的样子跑，断言在内容上红，而不是取不到）
HEADS = ("function kv(", "function spdTodoOps(", "*function spdListedHint(", "async function loadSpdTodo(",
         "async function loadSpdPatients(")

STEPS = r"""
const run = async (count) => {
  DATA.count = count;
  const todo = $("#todo-box"), patients = $("#patient-box");
  await loadSpdTodo(todo);
  await loadSpdPatients(patients);
  const shape = (box) => ({ hint: (/<p class="hint">([^<]*)<\/p>/.exec(box.innerHTML) || [])[1] || null,
    cards: (box.innerHTML.match(/<div class="m-card">/g) || []).length, startsWithCard: box.innerHTML.startsWith('<div class="m-card">') });
  return { todo: shape(todo), patients: shape(patients) };
};
spdMe = { id: 5, is_village_doctor: false };
return { many: await run(35), few: await run(10) };
"""


def _top(source: str, head: str) -> str:
    """顶层函数原文：取到它之后第一个顶格的 `}`。"""
    if head.startswith("*"):
        head = head[1:]
        if head not in source:
            return ""
    start = source.index(head)
    return source[start:source.index("\n}\n", start) + 3]


def _script(steps: str) -> str:
    source = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    # 顶层的单行 `let`（本人 spdMe……）整批取：都是字面量初值
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_待办与在管患者列不全时写已列N共M_列全时不变():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    for block in ("todo", "patients"):
        many, few = out["many"][block], out["few"][block]
        assert many == {"hint": "已列 30 / 共 35 条", "cards": 30, "startsWithCard": False}, (block, many)   # 修前没有这一行
        assert few == {"hint": None, "cards": 10, "startsWithCard": True}, (block, few)   # 列全时与原先一字不差
