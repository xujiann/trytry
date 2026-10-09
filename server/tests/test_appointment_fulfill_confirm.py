"""预约「核销」点一下就生效、不确认、撤不回（P2-1701，第五十批扫描 AN1-4 的确认一半）。

修前（实测 `an1/r1_chain.py` 的「(c) 核销」段、`an1/r2_lists.py`）：预约记录同一行的「取消」早按 P2-43 先弹 `spdModal`
确认，「核销」却是 `if (fulfill) { await api(…/fulfill…); route(); }`——误点李二那一行 200，之后取消 409「当前状态 已就诊
不可取消」，没有任何回退的路由（`/unfulfill`、`/revert`、PATCH、PUT 全 404），号一直占着、再约王三 409「号源已约满」，
李二在手机上看到「已就诊」。确认闸门 `test_frontend_destructive_confirm_guard.py` 的「破坏性地址」判据里没有 `fulfill`，
对 `…/fulfill` 返回 False，所以一直绿着。

修法：核销前弹 `spdModal`，写明这条预约是谁的、哪个号（P2-1700 的认人键，`appointmentWho`；intro 由 spdModal 自己 esc()）
与「核销后不可撤回」；闸门判据补上 `fulfill`（补之前在全部前端文件上量过：命中两处，消毒供应「响应申领」早已先过模态框选批次，
只有这一处没确认）。撤销核销的口子不做（另行登记）。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from test_frontend_destructive_confirm_guard import destructive_calls

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


def test_闸门认得核销_预约页这一处已确认():
    hits = [(where, ok) for where, ok in destructive_calls() if "/api/appointments/${fulfill}/fulfill" in where]
    assert [ok for _, ok in hits] == [True], hits   # 修前判据不认 fulfill：一处都扫不到；补了判据、页面没改：[False]


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


#: `$()` 按选择器给一个假元素；`api()` 记下每次调用（GET 回 DATA.responses）；`spdModal()` 记下标题与 intro、回 DATA.modal
_HARNESS = """
const els = {};
const calls = [];
const modals = [];
let routed = 0;
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {} }, addEventListener() {} }); } };
const DATA = JSON.parse(process.argv[1]);
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
async function api(path, opts = {}) {
  calls.push([opts.method || "GET", path]);
  if (opts.method === "POST") return {};
  return DATA.responses[path];
}
async function route() { routed += 1; }
async function spdModal(title, fields, opts = {}) { modals.push([title, fields, opts.intro]); return DATA.modal; }
async function postAction() {}
function formJson() { return {}; }
"""

ROW = {"slot_id": 3, "patient_id": 2, "id": 9, "status": "booked", "patient_name": "李<四>", "slot_date": "2026-10-12",
       "slot_time": "09:00-10:00", "resource_name": "心内科<专家>", "org_name": "甲&乙县医院"}
RESPONSES = {
    "/api/appointments/slots": [], "/api/appointments": [ROW], "/api/appointments?status=booked": [ROW],
    "/api/appointments/blacklist": [], "/api/organizations": [],
}

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")


def _click_fulfill(modal) -> dict:
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function setMsg(") + _top_level(core, "function actionableFirst(")
              + _top_level(core, "function appointmentWho(") + _top_level(core, "async function renderAppointments(")
              + "(async () => { await renderAppointments();\n"
              "  await els['#page-body'].onclick({ target: { dataset: { fulfill: '9' } } });\n"
              "  process.stdout.write(JSON.stringify({ calls: calls.filter((c) => c[0] === 'POST'), modals, routed,\n"
              "    html: els['#page-body'].innerHTML })); })();\n")
    out = subprocess.run(["node", "-e", script, json.dumps({"responses": RESPONSES, "modal": modal}, ensure_ascii=False)],
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@needs_node
def test_页面_核销先确认_写明是谁哪个号与不可撤回_取消即不核销():
    out = _click_fulfill(None)
    assert 'data-fulfill="9"' in out["html"]
    assert out["calls"] == [] and out["routed"] == 0   # 修前点一下就 POST …/9/fulfill
    ((title, fields, intro),) = out["modals"]
    assert (title, fields) == ("到诊核销", [])
    # 认人键原文交给 spdModal（它自己 esc() intro，这里再 esc 就成了 &amp;lt;）
    assert "李<四> · 2026-10-12 09:00-10:00 · 心内科<专家>" in intro
    assert "核销后不可撤回" in intro


@needs_node
def test_页面_确定才核销():
    out = _click_fulfill({})
    assert out["calls"] == [["POST", "/api/appointments/9/fulfill"]]
    assert out["routed"] == 1
