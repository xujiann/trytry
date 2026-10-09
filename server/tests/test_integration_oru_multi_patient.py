"""一条 ORU^R01 带几位患者（几个 PID 组）时，每组拿紧挨在它前面的那个 PID 核对申请单患者（P2-1757，第五十二批扫描 AP2-2）。

ORU^R01 的 PATIENT_RESULT 组可以重复，每组的患者是它前面最近的那个 PID。`_do_hl7v2_oru` 原先 `pid = next(...)` 取全文
第一个 PID、所有 OBR 都拿它核（`_oru_groups` 只认 OBR / OBX）。修前实测：
- PID(甲) OBR(甲单1) OBX / PID(乙) OBR(甲单2) OBX「血钾 6.9 HH」：201，乙的结果连同危急值写进甲单2，危急值通知发给甲的
  开单机构——`_oru_request` 写着「每一组各自核，防串单」（P1-143 / P2-722），防串单在这里失效；
- PID(甲) OBR(甲单) / PID(乙) OBR(乙单)：合法的两位患者批量消息整条 422「PID 患者与申请单患者不一致」，错因也不对。

修法：分组时记下每个 OBR 之前最近的 PID，核验与 OBR-3 回退（P2-986 要求带 PID）都按本组的 PID 算；几组时 PID 对不上
点名第几个 OBR。单患者消息的回执与错因逐字不变。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamReport, ExamRequest

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


A_ID = _id_card("33010219800117175")
B_ID = _id_card("33010219810217175")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21757 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    a = client.post("/api/patients", headers=admin, json={"name": "P21757 甲", "id_card": A_ID, "gender": "男"})
    b = client.post("/api/patients", headers=admin, json={"name": "P21757 乙", "id_card": B_ID, "gender": "女"})
    assert a.status_code in (200, 201) and b.status_code in (200, 201), (a.text, b.text)
    return {"org": org, "甲": a.json()["id"], "乙": b.json()["id"]}


def _request(client, admin, world, who, code="K", name="电解质"):
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world[who], "from_org_id": world["org"], "center_type": "lab", "item_code": code, "item_name": name})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _oru(control_id, *segments):
    return "\r".join([f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|{control_id}|P|2.4", *segments])


def _post(client, admin, message):
    return client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": message})


def _state(request_id):
    with SessionLocal() as db:
        request = db.get(ExamRequest, request_id)
        report = db.query(ExamReport).filter(ExamReport.request_id == request_id).first()
        return request.patient_id, request.status, None if report is None else report.finding


GLU = "OBX|1|NM|GLU^血糖||5.1|mmol/L|3.9-6.1|N"
K_HH = "OBX|1|NM|K^血钾||6.9|mmol/L|3.5-5.3|HH"


def test_第二组PID是乙_OBR是甲的单_整条拒收并点名第几个OBR(client, admin, world):
    a1 = _request(client, admin, world, "甲", "GLU", "血糖")
    a2 = _request(client, admin, world, "甲")
    resp = _post(client, admin, _oru("P21757A",
                                     f"PID|1||{A_ID}^^^CN^ID||P21757 甲", f"OBR|1|{a1}||GLU^血糖", GLU,
                                     f"PID|2||{B_ID}^^^CN^ID||P21757 乙", f"OBR|1|{a2}||K^电解质", K_HH))
    assert resp.status_code == 422, resp.text   # 修前 201：乙的血钾 6.9 连同危急值写进甲单2
    assert resp.json()["detail"] == f"第 2 个 OBR（申请单 {a2}）：PID 患者与申请单患者不一致，结果拒收"
    assert _state(a2) == (world["甲"], "pending", None)   # 甲单2 不动
    assert _state(a1) == (world["甲"], "pending", None)   # 整条拒收，第一组也不出报告（P2-722）


def test_两位患者批量_各回各的单(client, admin, world):
    a = _request(client, admin, world, "甲", "GLU", "血糖")
    b = _request(client, admin, world, "乙", "GLU", "血糖")
    resp = _post(client, admin, _oru("P21757B",
                                     f"PID|1||{A_ID}^^^CN^ID||P21757 甲", f"OBR|1|{a}||GLU^血糖", GLU,
                                     f"PID|2||{B_ID}^^^CN^ID||P21757 乙", f"OBR|1|{b}||GLU^血糖",
                                     "OBX|1|NM|GLU^血糖||5.3|mmol/L|3.9-6.1|N"))
    assert resp.status_code == 201, resp.text   # 修前 422「PID 患者与申请单患者不一致」
    assert [r["request_id"] for r in resp.json()["reports"]] == [a, b]
    assert _state(a) == (world["甲"], "reported", "血糖：5.1 mmol/L（参考 3.9-6.1） [N]")
    assert _state(b) == (world["乙"], "reported", "血糖：5.3 mmol/L（参考 3.9-6.1） [N]")


def test_OBR3回退按本组的PID核(client, admin, world):
    """第二组 OBR-2 为空、按 OBR-3 回退（P2-986 要求带 PID）：核的是紧挨着它的甲，不是全文第一个乙。"""
    b = _request(client, admin, world, "乙", "GLU", "血糖")
    a = _request(client, admin, world, "甲")
    resp = _post(client, admin, _oru("P21757C",
                                     f"PID|1||{B_ID}^^^CN^ID||P21757 乙", f"OBR|1|{b}||GLU^血糖", GLU,
                                     f"PID|2||{A_ID}^^^CN^ID||P21757 甲", f"OBR|1||{a}|K^电解质", K_HH))
    assert resp.status_code == 201, resp.text   # 修前 422：甲单拿乙的证件号核
    assert (_state(a)[:2], _state(b)[:2]) == ((world["甲"], "reported"), (world["乙"], "reported"))


def test_单患者消息回执与错因逐字不变(client, admin, world):
    a = _request(client, admin, world, "甲", "GLU", "血糖")
    resp = _post(client, admin, _oru("P21757D", f"PID|1||{A_ID}^^^CN^ID||P21757 甲", f"OBR|1|{a}||GLU^血糖", GLU))
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["reports"] == [{"request_id": a, "report_id": body["report_id"], "obx_count": 1,
                                "abnormal_count": 0, "critical": False}]
    assert (body["event"], body["request_id"], body["obx_count"], body["abnormal_count"], body["critical"]) == (
        "ORU^R01", a, 1, 0, False)
    other = _request(client, admin, world, "甲")
    wrong = _post(client, admin, _oru("P21757E", f"PID|1||{B_ID}^^^CN^ID||P21757 乙", f"OBR|1|{other}||K^电解质", K_HH))
    assert (wrong.status_code, wrong.json()) == (422, {"detail": "PID 患者与申请单患者不一致，结果拒收"})
