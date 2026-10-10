"""医生移动端查房：体征 / 病程提交在途时切到下一床，回包一到按「此刻」清空输入、写回执——下一位录的被清空，回执写在下一位
名下（P2-1798，第五十三批扫描 AQ2-6）。

修前：`m/doctor.js` 查房的两张表单提交成功后，不看回包时停在谁那里，就把输入框清空、写「体征已录入」「病程已记录」（不带
姓名）。弱网时提交甲的体征，接着切到下一床乙、开始录乙的血压 168/102；甲的回包一到，乙的输入全被清空，回执写在乙的区块
上方，乙一条也没记上（扫描实测：当前查房对象 102，回执「体征已录入」，乙的四个输入框全空，后端甲 1 条、乙 0 条）。P1-231
修了「下拉一改就切换」，没覆盖提交在途换人。

修法：提交时记下住院号（请求也发给它）与交上去的原样，回包后仍停在同一位才照旧清空输入；已切走的只清还是这次交上去原样的
格子——下一位填过的留着，上一位没动过的（比如只量了血压的乙，表单里还挂着甲的体温、脉搏）不能跟着下一位交上去。回执写明
记在谁名下——与下拉选项同一句「病区 床号 姓名」（P2-1335），取得到什么写什么，在院清单里找不到时写住院号。

页面那条把 `m/doctor.js` 查房的 `loadRound` / `refreshRoundDetail`、下拉的 change 监听与两张表单的提交监听原文放进 node 跑
（shared.js 整份加载）：`api` 按路径回垫好的响应并记下写请求，可以把某个请求的回包压住；DOM 用一个够这几段用的小替身。
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
function makeEl(name) {
  const classes = new Set();
  const el = { name, innerHTML: "", textContent: "", value: "", className: "", listeners: {},
    classList: { add: (c) => classes.add(c), remove: (c) => classes.delete(c), contains: (c) => classes.has(c),
      toggle: (c, on) => ((on ?? !classes.has(c)) ? classes.add(c) : classes.delete(c)) },
    addEventListener(type, fn) { el.listeners[type] = fn; } };
  return el;
}
globalThis.document = { cookie: "", addEventListener() {}, querySelector: (sel) => (registry[sel] ||= makeEl(sel)) };
/* 在院两位：甲（住院 101）、乙（住院 102），同一病区 */
const ADMISSIONS = [
  { id: 101, status: "admitted", ward_name: "内科病区", bed_no: "3", patient_name: "王甲", diagnosis_name: "肺炎" },
  { id: 102, status: "admitted", ward_name: "内科病区", bed_no: "4", patient_name: "李乙", diagnosis_name: "高血压" },
];
const SAVED = { notes: { 101: [], 102: [] }, vitals: { 101: [], 102: [] } };
const HELD = new Map();
const FAIL = new Map();   // 要失败的写请求 → 报错文案（P2-1819）
function hold(key) { HELD.set(key, []); }
function release(key) { const queue = HELD.get(key) || []; HELD.delete(key); queue.forEach((go) => go()); }
function replyFor(method, path, body) {
  if (path.startsWith("/api/inpatient/admissions?status=admitted")) return ADMISSIONS;
  const m = /^\/api\/inpatient\/admissions\/(\d+)\/([\w-]+)$/.exec(path);
  if (!m) throw new Error(`没垫的接口：${method} ${path}`);
  const [, id, what] = m;
  if (what === "document-completeness") return { complete: true, missing: [] };
  const kind = what === "progress-notes" ? "notes" : "vitals";
  if (method === "POST") { SAVED[kind][id].push(body); return { id: SAVED[kind][id].length }; }
  return SAVED[kind][id];
}
async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const body = options.body ? JSON.parse(options.body) : null;
  if (method !== "GET") OUT.posts.push(`${method} ${path}`);
  const key = `${method} ${path}`;
  if (HELD.has(key)) await new Promise((go) => HELD.get(key).push(go));
  if (FAIL.has(key)) throw new Error(FAIL.get(key));
  const data = JSON.parse(JSON.stringify(replyFor(method, path, body)));
  return options.withTotal ? { rows: data, total: data.length } : data;
}
const flush = async () => { for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0)); };
"""

#: 从 doctor.js 原文取的声明。打 * 的是这次新加的：修前没有就取成空串（页面照修前的样子跑，断言在内容上红，而不是取不到）
HEADS = (
    "function setMsg(", "function kv(", "function card(", "const NOTE_TYPE_NAMES = ", "*function roundWho(",
    "async function loadRound(", "async function refreshRoundDetail(", '$("#round-adm").addEventListener("change"',
    '$("#round-note").addEventListener("submit"', '$("#round-vital").addEventListener("submit"',
)

STEPS = r"""
const values = (sels) => Object.fromEntries(sels.map((s) => [s, $(s).value]));
const pickBed = async (id) => { $("#round-adm").value = String(id); await $("#round-adm").listeners.change(); };
await loadRound();                                   // 缺省选第一位：甲（住院 101）
$("#round-note-type").value = "daily";

// ① 体征：交甲的，回包前切到乙、开始录乙的血压
$("#rv-at").value = "2026-10-09T08:00"; $("#rv-temp").value = "38.5"; $("#rv-pulse").value = "96";
hold("POST /api/inpatient/admissions/101/vitals");
const vitalA = $("#round-vital").listeners.submit({ preventDefault() {} });
await flush();
await pickBed(102);
$("#rv-at").value = "2026-10-09T08:06"; $("#rv-sbp").value = "168"; $("#rv-dbp").value = "102";
release("POST /api/inpatient/admissions/101/vitals");
await vitalA; await flush();
const vital = { current: roundAdmissionId, msg: $("#round-vital-msg").textContent,
  inputs: values(["#rv-at", "#rv-temp", "#rv-pulse", "#rv-sbp", "#rv-dbp"]), saved: SAVED.vitals };

// ② 病程同形：交甲的，回包前切到乙、开始写乙的
await pickBed(101);
$("#round-content").value = "甲：体温 38.5℃，予物理降温"; $("#round-at").value = "2026-10-09T07:50";
hold("POST /api/inpatient/admissions/101/progress-notes");
const noteA = $("#round-note").listeners.submit({ preventDefault() {} });
await flush();
await pickBed(102);
$("#round-content").value = "乙：血压 168/102，加用氨氯地平";
release("POST /api/inpatient/admissions/101/progress-notes");
await noteA; await flush();
const note = { current: roundAdmissionId, msg: $("#round-msg").textContent, content: $("#round-content").value,
  at: $("#round-at").value };

// ③ 不切人：交完照旧清空，回执写这一位
await $("#round-note").listeners.submit({ preventDefault() {} });
await flush();
const same = { msg: $("#round-msg").textContent, content: $("#round-content").value, at: $("#round-at").value };
return { vital, note, same, posts: OUT.posts, notes: SAVED.notes };
"""


def _top(source: str, head: str) -> str:
    """顶层声明原文：函数取到它之后第一个顶格的 `}`，顶层的监听取到顶格的 `});`，常量取到语句末的 `;`（可以跨行）。"""
    if head.startswith("*"):
        head = head[1:]
        if head not in source:
            return ""
    start = source.index(head)
    if head.startswith(("function ", "async function ")):
        return source[start:source.index("\n}\n", start) + 3]
    if head.startswith("$("):
        return source[start:source.index("\n});\n", start) + 5]
    return source[start:source.index(";\n", start) + 2]


def _script(steps: str) -> str:
    source = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    # 顶层的单行 `let`（当前查房对象、在院清单、请求序号……）整批取：都是字面量初值
    lets = "".join(m.group(0) + "\n" for m in re.finditer(r"^let \w+ = [^\n]*;$", source, re.M))
    return (PRELUDE + (STATIC / "shared.js").read_text(encoding="utf-8") + "\n" + lets
            + "".join(_top(source, head) for head in HEADS)
            + f"\n(async () => {{\n{steps}\n}})().then("
            + "(r) => process.stdout.write(JSON.stringify(r)), (e) => { console.error(e); process.exit(1); });\n")


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_提交在途切到下一床_下一位的输入留着_回执写明是哪一位():
    done = subprocess.run(["node", "-e", _script(STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)

    vital = out["vital"]
    assert vital["current"] == 102, vital
    assert vital["msg"] == "体征已录入（内科病区 3 王甲）", vital   # 修前「体征已录入」写在乙的区块上方
    # 修前乙填的测量时刻与血压全被清空；甲留在表单里没被乙动过的体温、脉搏清掉，不跟着乙交上去
    assert vital["inputs"] == {"#rv-at": "2026-10-09T08:06", "#rv-temp": "", "#rv-pulse": "", "#rv-sbp": "168",
                               "#rv-dbp": "102"}, vital
    assert vital["saved"] == {"101": [{"measured_at": "2026-10-09 08:00", "temperature": 38.5, "pulse": 96}], "102": []}

    note = out["note"]
    assert note["current"] == 102, note
    assert note["msg"] == "病程已记录（内科病区 3 王甲）", note   # 修前「病程已记录」
    assert note["content"] == "乙：血压 168/102，加用氨氯地平", note   # 修前乙写了一半的被清空
    assert note["at"] == "", note   # 甲那条的记录时间乙没动过：清掉，不成了乙那条的记录时间

    same = out["same"]   # 没切人：交完清空、回执写这一位
    assert same == {"msg": "病程已记录（内科病区 4 李乙）", "content": "", "at": ""}, same
    assert out["notes"]["101"] == [{"note_type": "daily", "content": "甲：体温 38.5℃，予物理降温",
                                    "recorded_at": "2026-10-09 07:50"}], out["notes"]
    assert out["notes"]["102"] == [{"note_type": "daily", "content": "乙：血压 168/102，加用氨氯地平"}], out["notes"]
    assert out["posts"] == ["POST /api/inpatient/admissions/101/vitals", "POST /api/inpatient/admissions/101/progress-notes",
                            "POST /api/inpatient/admissions/102/progress-notes"], out["posts"]


FAIL_STEPS = r"""
const pickBed = async (id) => { $("#round-adm").value = String(id); await $("#round-adm").listeners.change(); };
await loadRound();                                   // 缺省选第一位：甲（住院 101）
$("#round-note-type").value = "daily";

// ① 体征：交甲的，回包前切到乙、开始录乙的；甲那次没交上
$("#rv-at").value = "2026-10-09T08:00"; $("#rv-temp").value = "38.5";
hold("POST /api/inpatient/admissions/101/vitals");
FAIL.set("POST /api/inpatient/admissions/101/vitals", "请求失败(502)");
const vitalA = $("#round-vital").listeners.submit({ preventDefault() {} });
await flush();
await pickBed(102);
$("#rv-sbp").value = "168";
release("POST /api/inpatient/admissions/101/vitals");
await vitalA; await flush();
const vital = { msg: $("#round-vital-msg").textContent, sbp: $("#rv-sbp").value };

// ② 病程同形
await pickBed(101);
$("#round-content").value = "甲：体温 38.5℃，予物理降温";
hold("POST /api/inpatient/admissions/101/progress-notes");
FAIL.set("POST /api/inpatient/admissions/101/progress-notes", "请求失败(502)");
const noteA = $("#round-note").listeners.submit({ preventDefault() {} });
await flush();
await pickBed(102);
release("POST /api/inpatient/admissions/101/progress-notes");
await noteA; await flush();
const note = { msg: $("#round-msg").textContent };

// ③ 不切人时没交上：照旧只写原因
FAIL.set("POST /api/inpatient/admissions/102/vitals", "请求失败(503)");
$("#rv-at").value = "2026-10-09T09:00";
await $("#round-vital").listeners.submit({ preventDefault() {} });
await flush();
return { vital, note, same: { msg: $("#round-vital-msg").textContent } };
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")
def test_提交在途切到下一床_上一位没交上时报错写明是谁():
    """P2-1819（第五十三批 P2-1798 修复回报旁见）：P2-1798 让提交在途切床后回执写明「病程已记录（病区 床号 姓名）」，可上一位
    那次**没交上**时，报错照旧只有「请求失败(502)」、写在下一位的区块上方——医生会以为是眼前这一位的没交上，上一位的体征 / 病程
    就这么漏了。修后已切走时报错写成「体征没有录上（病区 床号 姓名）：原因」；没切人时照旧只写原因。"""
    done = subprocess.run(["node", "-e", _script(FAIL_STEPS)], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    out = json.loads(done.stdout)
    assert out["vital"] == {"msg": "体征没有录上（内科病区 3 王甲）：请求失败(502)", "sbp": "168"}, out   # 修前「请求失败(502)」
    assert out["note"] == {"msg": "病程没有记上（内科病区 3 王甲）：请求失败(502)"}, out
    assert out["same"] == {"msg": "请求失败(503)"}, out
