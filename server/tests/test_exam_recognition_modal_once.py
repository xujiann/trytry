"""共享诊断开单「先查互认」：取预检期间再交不叠第二张「可互认」框，框头写明患者（P2-1793 同形，第五十三批扫描 AQ2-2）。

修前：「提交申请」先取互认预检（`/api/exams/recognition-check`）、取回来才开 `spdModal`，取数在途时表单照样能交——连点两下
（或点完改了患者号再点）叠出两张一模一样的「可互认」框，框里只有已有报告的项目与结论、不写患者：互认 / 不互认交到哪一位
分不清；预检说不可互认的直接建单，连点两下就建出两张单。修法同慢专病执行随访（`test_spd_modal_prefetch_once.py`）：入口记一个
「开框中」、框关了才放下，框头写上患者号（预检出参不带患者）。

页面原样拿到 node 里跑（夹具 `tests/page_race.py`，可扣住个别回包），接口换成桩：只看页面上的先后与送出的请求。
"""
import shutil

import pytest

from page_race import function_source, read, run

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面脚本")

CHECK = {"recognizable": True, "request_id": 7, "item_name": "血钾测定", "conclusion": "血钾 4.1 mmol/L",
         "critical": False, "critical_status": ""}


def _const(source: str, name: str) -> str:
    start = source.index(f"const {name} = ")
    return source[start:source.index(";\n", start) + 2]


def _page_js() -> str:
    core, clinical, spd = read("core.js"), read("pages-clinical.js"), read("pages-spd.js")
    return (read("shared.js") + "\n" + function_source(spd, "function spdModal(")
            + "".join(function_source(core, head) for head in (
                "function table(", "function panel(", "function actionableFirst(", "function setMsg("))
            + "".join(_const(core, name) for name in ("CENTER_NAMES", "EXAM_STATUS", "SAMPLE_NEXT", "SAMPLE_STATUS"))
            + _const(clinical, "CRIT_STATUS") + function_source(core, "async function renderExams("))


def _responder(check):
    def respond(method, path, body):
        if path.startswith("/api/exams/recognition-check"):
            return 200, check
        if method == "POST" and path == "/api/exams":
            return 201, {"id": 99, **body}
        return 200, []   # 首屏的申请单、危急值、模板清单一律空
    return respond


STEPS = """
  await renderExams(); await idle();
  hold(/recognition-check/);
  const form = (pid) => ({ patient_id: String(pid), from_org_id: "2", center_type: "lab", item_code: "K",
                           item_name: "血钾测定", clinical_info: "" });
  submitForm("#exam-form", form(11));
  submitForm("#exam-form", form(12));   // 改了患者号又点一次「提交申请」
  await idle();
  const released = await release(/recognition-check/);
  const open = openModals();
  const titles = open.map(titleOf);
  if (open.length) await submitModal(open[open.length - 1], { decision: "accept" });
  return { released, titles, left: openModals().length, routed: ROUTED,
           orders: posts.filter((p) => p[1] === "/api/exams").map((p) => p[2]) };
"""


def test_可互认框_取预检期间再交不叠框_框头写患者号():
    out = run(None, None, _page_js(), STEPS, responder=_responder(CHECK))
    assert out["released"] == 1, out   # 第二次提交没再取预检
    assert out["titles"] == ["可互认：30 天内已有同项目报告（患者 11）"], out   # 修前两张一模一样的框、不写患者
    assert out["left"] == 0 and out["routed"] == 1, out
    assert [(o["patient_id"], o.get("accept_recognition_of")) for o in out["orders"]] == [(11, 7)], out


def test_不可互认直接建单_连点两下只建一张():
    out = run(None, None, _page_js(), STEPS, responder=_responder({"recognizable": False}))
    assert out["released"] == 1 and out["titles"] == [], out
    assert [o["patient_id"] for o in out["orders"]] == [11], out   # 修前两张单
