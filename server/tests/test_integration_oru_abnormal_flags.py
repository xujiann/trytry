"""HL7 检验结果入站的异常标志按 HL7 表 0078 判，认不得的不当正常（P1-213，第十七批「缺失 vs 零」扫描 U1-1）。

`_ABNORMAL_FLAGS` 原先只有 H / L / A / HH / LL 五个、且整串比对：AA（表 0078「非数值结果的危急，与数值结果的危急界值
同义」——血培养阳性这类）、`<` / `>`（超出仪器量程）、重复写法 `H~W` 一律当正常。LIS 回传一条 AA 的血培养，报告结论写
「共 1 项，异常 0 项」，危急值标记是假——不置「已通知」、不进危急值闭环，驾驶舱的未闭环危急值里也没有它。
认不得的标志（变化趋势 W、药敏 S……）原先同样悄悄算正常；修后结论里写明「平台不认得」，以原文为准。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamReport

PATIENT_ID_CARD = "330106197211133017"


def _oru(request_id, control_id, *obx):
    lines = [f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260928100000||ORU^R01|{control_id}|P|2.4",
             f"OBR|1|{request_id}||P1213^检验组合", *obx]
    return "\r".join(lines)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1213 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1213 检验患者", "id_card": PATIENT_ID_CARD, "gender": "女"})
    assert patient.status_code in (200, 201), patient.text
    return {"org": org, "patient": patient.json()["id"]}


def _send(client, admin, world, control_id, *obx):
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "P1213", "item_name": "检验组合"})
    assert req.status_code == 201, req.text
    resp = client.post("/api/integration/hl7v2/oru", headers=admin,
                       json={"message": _oru(req.json()["id"], control_id, *obx)})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _critical_ids(client, admin):
    return {r["id"] for r in client.get("/api/exams/critical", headers=admin).json()}


def test_AA_非数值结果的危急_进危急值闭环(client, admin, world):
    body = _send(client, admin, world, "P1213A", "OBX|1|ST|BC^血培养||金黄色葡萄球菌生长|||AA")
    assert (body["abnormal_count"], body["critical"]) == (1, True)   # 修前 (0, False)
    assert body["report_id"] in _critical_ids(client, admin)          # 修前不在危急值清单里


def test_超出仪器量程的两个方向都算异常(client, admin, world):
    body = _send(client, admin, world, "P1213B",
                    "OBX|1|NM|GLU^血糖||>33.3|mmol/L|3.9-6.1|>",
                    "OBX|2|NM|K^血钾||<1.0|mmol/L|3.5-5.3|<")
    assert (body["abnormal_count"], body["critical"]) == (2, False)   # 修前 (0, False)


def test_重复的异常标志逐个判(client, admin, world):
    body = _send(client, admin, world, "P1213C",
                    "OBX|1|NM|NA^血钠||150|mmol/L|137-147|H~W",
                    "OBX|2|NM|K^血钾||2.1|mmol/L|3.5-5.3|LL~W")
    assert (body["abnormal_count"], body["critical"]) == (2, True)   # 修前 (0, False)：整串比对认不出


def test_认不得的标志写进结论_不当正常(client, admin, world):
    body = _send(client, admin, world, "P1213D",
                    "OBX|1|NM|CRP^C反应蛋白||12|mg/L|0-10|W",
                    "OBX|2|ST|AMK^阿米卡星||≤2||≤16|S",
                    "OBX|3|NM|TG^甘油三酯||1.2|mmol/L|0.4-1.7|N")
    assert (body["abnormal_count"], body["critical"]) == (0, False)
    assert body["report_id"] not in _critical_ids(client, admin)
    assert _conclusion(body["report_id"]) == (
        "检验组合：共 3 项，异常 0 项，另 2 项的异常标志平台不认得（W、S），以原文为准")   # 修前只有「共 3 项，异常 0 项」


def test_正常与没给标志的_结论照旧(client, admin, world):
    body = _send(client, admin, world, "P1213E",
                    "OBX|1|NM|TG^甘油三酯||1.2|mmol/L|0.4-1.7|N",
                    "OBX|2|NM|TC^总胆固醇||4.1|mmol/L|0-5.2|")
    assert (body["abnormal_count"], body["critical"]) == (0, False)
    assert _conclusion(body["report_id"]) == "检验组合：共 2 项，异常 0 项"


def _conclusion(report_id):
    with SessionLocal() as db:   # 报告没有单条读接口（页面走打印件），直接读库
        return db.get(ExamReport, report_id).conclusion
