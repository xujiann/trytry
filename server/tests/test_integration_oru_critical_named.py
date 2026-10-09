"""ORU 回传的危急值，结论点名是哪一项、多少；两端危急值页显示所见（P2-1363，第四十批扫描 AD3-3）。

`integration._oru_report` 逐个 OBX 判出 HH / LL / AA，所见里也写着「钾：7.2 mmol/L（参考 3.5-5.5） [HH]」，结论却只拼
「电解质：共 4 项，异常 3 项，含危急值」。危急值的站内信正文（`exams.submit_report`）、定向广播、待办（`todos.py`）、
超时催办、管理端危急值操作台（`pages-clinical.js`）与医生移动端危急值卡片（`m/doctor.js`）给的都只是这句结论，两端
危急值页也不显示所见——村医在手机上确认接收时不知道是哪一项、多少。修前实测：钾 HH、钠 L、氯 L、钙 N 的电解质，站内信
「危急值：电解质 / 电解质：共 4 项，异常 3 项，含危急值」，危急值清单与待办也是这一句；`/api/exams/critical` 的出参本来
就带 `finding`，两端页面不显示。

修法：结论在「含危急值」后点名危急项（项目 值 单位 [标志]，多项用顿号接），按 `exam_reports.conclusion` 列宽截断、
末尾标「…」；没有危急项的结论一字不变。两端危急值页把所见照原样换行显示，一律过 `esc()`。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import login

from app.database import SessionLocal
from app.models import ExamReport

PATIENT_ID_CARD = "330102197001011234"
STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21363 县人民医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P21363 甲乡卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p21363_doc", "password": "passw0rd1", "role": "doctor", "org_id": town, "full_name": "甲医生"})
    assert doctor.status_code in (200, 201), doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21363 张三", "id_card": PATIENT_ID_CARD, "gender": "男", "birth_date": "1970-01-01"})
    assert patient.status_code in (200, 201), patient.text
    return {"town": town, "patient": patient.json()["id"], "doctor": login(client, "p21363_doc", "passw0rd1")}


def _send(client, admin, world, control_id, *obx, item=("LYTE", "电解质")):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["town"], "center_type": "lab",
        "item_code": item[0], "item_name": item[1]})
    assert req.status_code == 201, req.text
    message = "\r".join([
        f"MSH|^~\\&|LIS|COUNTY|MEDPLAT|HUB|20261004090000||ORU^R01|{control_id}|P|2.5",
        f"PID|1||{PATIENT_ID_CARD}^^^^ID||张三",
        f"OBR|1|{req.json()['id']}||{item[0]}^{item[1]}",
        *obx,
    ])
    resp = client.post("/api/integration/hl7v2/oru", headers={**admin, "X-Source-System": "LIS"},
                       json={"message": message})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _critical_row(client, headers, report_id):
    return next(r for r in client.get("/api/exams/critical", headers=headers).json() if r["id"] == report_id)


def test_单项危急_结论站内信待办都点名是哪一项多少(client, admin, world):
    body = _send(client, admin, world, "P21363A",
                 "OBX|1|NM|K^钾||7.2|mmol/L|3.5-5.5|HH",
                 "OBX|2|NM|NA^钠||128|mmol/L|137-147|L",
                 "OBX|3|NM|CL^氯||95|mmol/L|99-110|L",
                 "OBX|4|NM|CA^钙||2.30|mmol/L|2.10-2.60|N")
    assert body["critical"] is True
    expected = "电解质：共 4 项，异常 3 项，含危急值（钾 7.2 mmol/L [HH]）"   # 修前到「含危急值」为止
    row = _critical_row(client, world["doctor"], body["report_id"])
    assert row["conclusion"] == expected
    assert row["finding"].split("\n")[0] == "钾：7.2 mmol/L（参考 3.5-5.5） [HH]"   # 所见照旧逐项四行
    notes = client.get("/api/notifications", headers=world["doctor"]).json()
    assert [(n["title"], n["body"]) for n in notes if n["link_id"] == body["report_id"]] == [("危急值：电解质", expected)]
    todo = next(i for i in client.get("/api/todos", headers=world["doctor"]).json()["items"]
                if i["type"] == "critical_ack")
    assert {"id": body["report_id"], "request_id": body["request_id"], "conclusion": expected} in todo["list"]


def test_多项危急顿号接_非数值危急没有单位也点名_认不得的标志照旧写在后面(client, admin, world):
    body = _send(client, admin, world, "P21363B",
                 "OBX|1|NM|K^钾||7.2|mmol/L|3.5-5.5|HH",
                 "OBX|2|NM|NA^钠||118|mmol/L|137-147|LL~W",
                 "OBX|3|ST|BC^血培养||金黄色葡萄球菌生长|||AA",
                 "OBX|4|NM|GLU^血糖||7.0|mmol/L|3.9-6.1|H",
                 "OBX|5|NM|CRP^C反应蛋白||12|mg/L|0-10|W")
    row = _critical_row(client, admin, body["report_id"])
    assert row["conclusion"] == (
        "电解质：共 5 项，异常 4 项，含危急值（钾 7.2 mmol/L [HH]、钠 118 mmol/L [LL~W]、血培养 金黄色葡萄球菌生长 [AA]），"
        "另 1 项的异常标志平台不认得（W），以原文为准")


def test_没有危急项的结论一字不变(client, admin, world):
    body = _send(client, admin, world, "P21363C",
                 "OBX|1|NM|K^钾||5.8|mmol/L|3.5-5.5|H",
                 "OBX|2|NM|NA^钠||140|mmol/L|137-147|N")
    assert body["critical"] is False
    with SessionLocal() as db:   # 非危急报告不进危急值清单，直接读库
        assert db.get(ExamReport, body["report_id"]).conclusion == "电解质：共 2 项，异常 1 项"


def test_危急项多到超列宽_按列宽截断并标省略号(client, admin, world):
    obx = [f"OBX|{i}|NM|T{i:02d}^检验项目{i:02d}||{100 + i}.5|mmol/L|1-9|HH" for i in range(1, 81)]
    body = _send(client, admin, world, "P21363D", *obx, item=("BIG", "生化全套"))
    conclusion = _critical_row(client, admin, body["report_id"])["conclusion"]
    assert len(conclusion) == 1024 and conclusion.endswith("…"), (len(conclusion), conclusion[-20:])
    assert conclusion.startswith("生化全套：共 80 项，异常 80 项，含危急值（检验项目01 101.5 mmol/L [HH]、检验项目02 ")


# ---------- 两端危急值页显示所见 ----------

HOSTILE = "<img src=x onerror=alert(1)>"


def _esc(text: str) -> str:
    """与 shared.js 的 `esc()` 同一张表。"""
    return "".join({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}.get(c, c) for c in text)


def _top_level(source: str, head: str, end: str = "\n}\n") -> str:
    start = source.index(head)
    return source[start:source.index(end, start) + len(end)]


def _run_node(script: str, data) -> str:
    return subprocess.run(["node", "-e", script, json.dumps(data, ensure_ascii=False)], capture_output=True,
                          text=True, check=True, timeout=60).stdout


#: 两个危急值页把未处置的续页取全（P2-1711）：页面按 fetchAllPages 拼出来的地址取，桩里回清单行中未处置的那些
OPEN_PAGE = "/api/exams/critical?open=true&limit=500&offset=0"


def _open(rows):
    return [r for r in rows if r["critical_status"] != "resolved"]


#: 页面取数换成桩：`$()` 按选择器给一个记 innerHTML 的假元素，`api()` 按路径回给定的数据
_HARNESS = """
const els = {};
globalThis.document = { addEventListener() {}, cookie: "",
  querySelector(sel) { return (els[sel] ||= { textContent: "", innerHTML: "", classList: { add() {}, remove() {} },
                                             addEventListener() {} }); } };
"""


@pytest.fixture(scope="module")
def critical_rows(client, admin, world):
    """一份真回传的危急值清单行，所见末尾再接一行带标签的文字（看转义）。"""
    body = _send(client, admin, world, "P21363E",
                 "OBX|1|NM|K^钾||7.2|mmol/L|3.5-5.5|HH",
                 "OBX|2|NM|NA^钠||128|mmol/L|137-147|L")
    row = _critical_row(client, admin, body["report_id"])
    row["finding"] += "\n" + HOSTILE
    return [row]


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_管理端危急值操作台_所见照原样换行显示且转义(critical_rows):
    core = (STATIC / "core.js").read_text(encoding="utf-8")
    page = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(core, "function table(") + _top_level(core, "function panel(")
              + _top_level(core, "function actionableFirst(")   # 未处置的排最前、按 id 去重（P2-1711）
              + _top_level(page, "const CRIT_STATUS = ")   # 状态表连同其后的 renderCritical
              + "\nconst DATA = JSON.parse(process.argv[1]);\nasync function api(path) { return DATA[path]; }\n"
              "renderCritical().then(() => process.stdout.write(els['#page-body'].innerHTML));\n")
    html = _run_node(script, {"/api/exams/critical": critical_rows, OPEN_PAGE: _open(critical_rows),
                              "/api/exams/critical/unacknowledged": []})
    finding = critical_rows[0]["finding"]
    assert "<th>所见</th>" in html
    assert f'<td style="white-space:pre-wrap">{_esc(finding)}</td>' in html   # 修前清单不显示所见
    assert "\n" in finding and HOSTILE not in html


@pytest.mark.skipif(shutil.which("node") is None, reason="没有 node 执行页面渲染")
def test_医生移动端危急值卡片_所见照原样换行显示且转义(critical_rows):
    doctor = (STATIC / "m" / "doctor.js").read_text(encoding="utf-8")
    script = (_HARNESS + (STATIC / "shared.js").read_text(encoding="utf-8")
              + _top_level(doctor, "function kv(") + _top_level(doctor, "function card(")
              + _top_level(doctor, "const CRITICAL_TAGS = ", "\n};\n") + _top_level(doctor, "async function loadCritical(")
              + "\nconst DATA = JSON.parse(process.argv[1]);\nasync function api(path) { return DATA[path]; }\n"
              "loadCritical().then(() => process.stdout.write(els['#critical-list'].innerHTML));\n")
    html = _run_node(script, {"/api/exams/critical": critical_rows, OPEN_PAGE: _open(critical_rows)})
    finding = critical_rows[0]["finding"]
    assert f'<p class="note-body">{_esc(finding)}</p>' in html   # 修前卡片只有结论
    assert _esc(critical_rows[0]["conclusion"]) in html and HOSTILE not in html
