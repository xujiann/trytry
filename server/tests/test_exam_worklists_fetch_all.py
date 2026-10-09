"""共享诊断页、医生端待审检查只取一页 200 张，两个危急值页只取一页 100 条：最早的那批从页面消失（P2-1711，第五十批扫描 AN2-3）。

P2-1310 / P1-166 的修法本身仍封顶一页：

* 共享诊断中心（`core.js renderExams`）与医生移动端（`m/doctor.js loadExams`）按状态单独取待诊断、诊断中两种，但不带 limit——
  `GET /api/exams` 缺省 200 张。205 张待诊断时最早那 5 张（连同只画在申请单行上的「登记采样 / 发起转运 / 中心核收」按钮）
  从页面消失，铃铛「待诊断申请」却报 205（修前实测：`pending X-Total-Count 205 rows 200`、`oldest pending id=1 on page: False`）；
* `GET /api/exams/critical` 固定 `.limit(100)`、不能翻页，未处置的按报告号倒序——101 条已确认、还没反馈的危急值，最早那条两端都
  点不到「处置反馈」（修前实测：`critical rows 100 open rows 100 oldest acknowledged report 1 present: False`），正是 P1-166 原来
  的形状；清单是全县的（P1-69），确认之后又无人催（P2-904），未处置条数随时间累积。

修法：待诊断 / 诊断中两种两端都改用 `fetchAllPages` 续页取全（同 P2-1333 / P2-1671）；`/critical` 加可选 `open`——缺省不传，返回
与字节都与原先一致（对接规范里 HIS 轮询的就是它），传 `true` 只取未处置（待确认 + 已确认待反馈）、`false` 只取已处置，两者都走
`paginate`（`X-Total-Count`）；两个危急值页改为未处置的续页取全排在最前，再接缺省清单里最近已处置的，按 id 去重。
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import case, insert, true

from app.database import SessionLocal
from app.models import ExamReport, ExamRequest, User
from app.routers.exams import CRITICAL_LIST_LIMIT
from app.schemas import ExamReportOut

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"
PENDING = 205       # 比清单缺省的 200 张多
OPEN_CRITICAL = CRITICAL_LIST_LIMIT + 1   # 比危急值清单一页多一条

node = pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面函数")


@pytest.fixture(scope="module")
def world(client, admin):
    """205 张待诊断的检验申请（最早那张是本模块第一张单），101 份已确认、还没处置反馈的危急值，另有 3 份已处置的。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21711 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21711 患者", "id_card": "330127197207071711"}).json()["id"]
    first = client.post("/api/exams", headers=admin, json={
        "patient_id": patient, "from_org_id": org, "center_type": "lab", "item_code": "P21711-K", "item_name": "P21711 血钾"})
    assert first.status_code == 201, first.text
    with SessionLocal() as db:
        creator = db.query(User).filter_by(username="admin").one().id
        row = {"patient_id": patient, "from_org_id": org, "center_type": "lab", "item_code": "P21711-K",
               "item_name": "P21711 血钾", "created_by": creator}
        db.execute(insert(ExamRequest), [{**row, "status": "pending"} for _ in range(PENDING - 1)])
        reported = []
        for status in ["acknowledged"] * OPEN_CRITICAL + ["resolved"] * 3:
            req = ExamRequest(**row, status="reported")
            db.add(req)
            db.flush()
            report = ExamReport(request_id=req.id, conclusion=f"P21711 危急值 {status}", critical=True,
                                critical_status=status)
            db.add(report)
            db.flush()
            reported.append((report.id, status))
        db.commit()
    open_ids = [rid for rid, status in reported if status != "resolved"]
    return {"first": first.json()["id"], "oldest_open": min(open_ids), "open_ids": open_ids,
            "resolved_ids": [rid for rid, status in reported if status == "resolved"]}


# ---------- 接口 ----------


def test_待诊断超过一页_铃铛数着最早那张_缺省清单里没有(client, admin, world):
    """前提：最早那张待诊断的单只在续页里——页面不续页就取不到它。"""
    first_page = client.get("/api/exams?status=pending", headers=admin)
    assert first_page.headers["x-total-count"] == str(PENDING)
    assert world["first"] not in [r["id"] for r in first_page.json()]
    bell = {s["type"]: s for s in client.get("/api/todos", headers=admin).json()["items"]}
    assert bell["exam_diagnosis"]["count"] == PENDING


def test_危急值清单缺省调用逐字节不变_不出总数头(client, admin, world):
    """HIS 轮询的就是缺省这一句（对接规范第三章（二）第 4 步）：未处置的排最前、之后最近已处置的，一页 100 条。"""
    resp = client.get("/api/exams/critical", headers=admin)
    with SessionLocal() as db:
        rows = (db.query(ExamReport).filter(ExamReport.critical == true())
                .order_by(case((ExamReport.critical_status == "resolved", 1), else_=0), ExamReport.id.desc())
                .limit(CRITICAL_LIST_LIMIT).all())
        expected = [ExamReportOut.model_validate(r).model_dump(mode="json") for r in rows]
    assert resp.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert "x-total-count" not in resp.headers
    assert world["oldest_open"] not in [r["id"] for r in resp.json()]   # 缺省一页照旧封顶：最早那条在续页里


def test_危急值清单_open真只取未处置_翻得到页_带总数(client, admin, world):
    resp = client.get("/api/exams/critical", headers=admin, params={"open": "true", "limit": 500})
    assert resp.status_code == 200, resp.text
    assert resp.headers["x-total-count"] == str(OPEN_CRITICAL)   # 修前没有这个参数：不出总数头
    rows = resp.json()
    assert [r["id"] for r in rows] == sorted(world["open_ids"], reverse=True)   # 按报告号倒序，最早那条在
    assert {r["critical_status"] for r in rows} == {"acknowledged"}
    tail = client.get("/api/exams/critical", headers=admin, params={"open": "true", "offset": 100, "limit": 100}).json()
    assert [r["id"] for r in tail] == [world["oldest_open"]]   # 一页 100 条之外的那一条翻得到


def test_危急值清单_open假只取已处置(client, admin, world):
    resp = client.get("/api/exams/critical", headers=admin, params={"open": "false"})
    assert resp.headers["x-total-count"] == "3"
    assert [r["id"] for r in resp.json()] == sorted(world["resolved_ids"], reverse=True)


# ---------- 页面 ----------


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


def _const(source: str, name: str) -> str:
    start = source.index(f"const {name} = ")
    return source[start:source.index(";\n", start) + 2]


#: 假 DOM 与页面取数：`$()` 按选择器给一个记 innerHTML、登记监听的假元素；`api()` 经标准输入输出转给 Python 侧的真接口
_HARNESS = r"""
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", className: "", value: "", dataset: {},
    classList: { add() {}, remove() {} }, listeners: {}, addEventListener(type, fn) { this.listeners[type] = fn; } }); } };
const rl = require("readline").createInterface({ input: process.stdin });
const lines = rl[Symbol.asyncIterator]();
const requested = [];
async function api(path) {
  requested.push(path);
  process.stdout.write(JSON.stringify({ path }) + "\n");
  const resp = JSON.parse((await lines.next()).value);
  if (resp.status >= 400) throw new Error(JSON.stringify(resp.body));
  return resp.body;
}
"""


def _run(client, headers, code: str, main: str) -> dict:
    """在 node 里跑页面代码，再执行 `main`（async 函数体，return 结果）；页面发的 GET 逐条转给真接口。"""
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8") + code
              + f"\n(async () => {{ {main} }})()"
              ".then((out) => process.stdout.write(JSON.stringify({ done: out }) + '\\n'))"
              ".catch((err) => process.stdout.write(JSON.stringify({ error: String((err && err.stack) || err) }) + '\\n'));\n")
    proc = subprocess.Popen(["node", "-e", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True)
    try:
        for _ in range(50):
            line = proc.stdout.readline()
            assert line, f"node 没给出结果就退出了：{proc.stderr.read()}"
            message = json.loads(line)
            if "done" in message:
                return message["done"]
            assert "error" not in message, message["error"]
            resp = client.get(message["path"], headers=headers)
            proc.stdin.write(json.dumps({"status": resp.status_code, "body": resp.json()}, ensure_ascii=False) + "\n")
            proc.stdin.flush()
        raise AssertionError("页面请求停不下来")
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


CORE = (STATIC / "core.js").read_text(encoding="utf-8")
CLINICAL = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
DOCTOR = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
DESKTOP = (_top_level(CORE, "function table(") + _top_level(CORE, "function panel(")
           + _top_level(CORE, "function actionableFirst(") + _top_level(CORE, "function setMsg("))
MOBILE = (_top_level(DOCTOR, "function setMsg(") + _top_level(DOCTOR, "function kv(") + _top_level(DOCTOR, "function card("))


@node
def test_共享诊断页_最早那张待诊断单与样本物流按钮在页面上(client, admin, world):
    code = (DESKTOP + "".join(_const(CORE, n) for n in ("CENTER_NAMES", "EXAM_STATUS", "SAMPLE_NEXT", "SAMPLE_STATUS"))
            + _const(CLINICAL, "CRIT_STATUS") + _top_level(CORE, "async function renderExams()"))
    out = _run(client, admin, code, "await renderExams(); return { html: els['#page-body'].innerHTML, requested };")
    first = world["first"]
    # 修前只取最新 200 张：最早那张不在，它的「领取」与只画在申请单行上的「登记采样」都够不着
    assert f'data-claim="{first}"' in out["html"], out["requested"]
    assert f'data-sample="{first}">登记采样</button>' in out["html"]


@node
def test_医生移动端待审检查_最早那张待诊断单在卡片里(client, admin, world):
    code = MOBILE + _const(DOCTOR, "CENTER_NAMES") + _const(DOCTOR, "EXAM_STATUS") + _top_level(
        DOCTOR, "async function loadExams()")
    out = _run(client, admin, code, "await loadExams(); return { html: els['#exam-list'].innerHTML, requested };")
    assert f'data-claim="{world["first"]}"' in out["html"], out["requested"]   # 修前最早那张不在


def _listed(client, admin, html: str, attr: str, world) -> None:
    """页面上每条危急值一个留痕按钮：未处置的全在、按报告号倒序排最前，之后接缺省清单里其余的（已处置的），没有重复。"""
    ids = [int(x) for x in re.findall(rf'{attr}="(\d+)"', html)]
    default = [r["id"] for r in client.get("/api/exams/critical", headers=admin).json()]
    open_first = sorted(world["open_ids"], reverse=True)
    assert ids == open_first + [rid for rid in default if rid not in set(open_first)], ids
    assert html.count("data-resolve=") == OPEN_CRITICAL   # 修前一页 100 条


@node
def test_管理端危急值操作台_最早那条已确认的有处置反馈按钮(client, admin, world):
    code = DESKTOP + _top_level(CLINICAL, "const CRIT_STATUS = ")   # 状态表连同其后的 renderCritical
    out = _run(client, admin, code, "await renderCritical(); return { html: els['#page-body'].innerHTML, requested };")
    assert f'data-resolve="{world["oldest_open"]}"' in out["html"], out["requested"]   # 修前一页 100 条：最早那条点不到
    _listed(client, admin, out["html"], "data-trail", world)


@node
def test_医生移动端危急值页_最早那条已确认的有处置反馈按钮(client, admin, world):
    code = MOBILE + _top_level(DOCTOR, "const CRITICAL_TAGS = ", "\n};\n") + _top_level(DOCTOR, "async function loadCritical(")
    out = _run(client, admin, code, "await loadCritical(); return { html: els['#critical-list'].innerHTML, requested };")
    assert f'data-resolve="{world["oldest_open"]}"' in out["html"], out["requested"]   # 修前最早那条点不到
    _listed(client, admin, out["html"], "data-trace", world)
