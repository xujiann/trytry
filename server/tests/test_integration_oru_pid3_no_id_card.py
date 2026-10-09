"""ORU 的 PID-3 只给病案号时说清是没带身份证号，不再说成「患者不一致」（P2-1759，第五十二批扫描 AP2-11）。

`_oru_request` 拿 PID-3 里取出的值（`_pid3_id_card`，与建档同一取法）去核申请单患者。PID-3 只有病案号
`MR0012345^^^HIS^MR` 时取出的就是「MR0012345」，修前照样拿去核、回 422「PID 患者与申请单患者不一致，结果拒收」——
LIS 那头去查患者是不是挂错了，其实是没带证件号；建档侧对同一取值先报「PID-3 身份证号缺失或格式不正确」。

修法：取出的值不像证件号（与建档同一判据：不足 15 位）时 422「PID-3 未带身份证号，无法核对患者」；真不一致的照旧原文案。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamRequest

ID_CARD = "330102197505051759"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21759 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={"name": "P21759 患者", "id_card": ID_CARD, "gender": "男"})
    assert patient.status_code in (200, 201), patient.text
    return {"org": org, "patient": patient.json()["id"]}


def _request(client, admin, world):
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "CRP", "item_name": "C反应蛋白"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post(client, admin, control_id, *groups):
    """groups：(PID-3, OBR-2, OBR-3)。"""
    lines = [f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|{control_id}|P|2.4"]
    for pid3, obr2, obr3 in groups:
        lines += [f"PID|1||{pid3}||P21759 患者", f"OBR|1|{obr2}|{obr3}|CRP^C反应蛋白", "OBX|1|NM|CRP^C反应蛋白||3|mg/L|0-10|N"]
    return client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": "\r".join(lines)})


def _status(request_id):
    with SessionLocal() as db:
        return db.get(ExamRequest, request_id).status


def test_PID3只有病案号_说清没带身份证号(client, admin, world):
    request_id = _request(client, admin, world)
    resp = _post(client, admin, "P21759A", ("MR0012345^^^HIS^MR", request_id, ""))
    assert (resp.status_code, resp.json()) == (422, {"detail": "PID-3 未带身份证号，无法核对患者"})   # 修前「…不一致…」
    assert _status(request_id) == "pending"


def test_按OBR3回退_PID3只有病案号_同样说清(client, admin, world):
    request_id = _request(client, admin, world)
    resp = _post(client, admin, "P21759B", ("MR0012345^^^HIS^MR", "", request_id))
    assert (resp.status_code, resp.json()) == (422, {"detail": "PID-3 未带身份证号，无法核对患者"})


def test_几组时点名第几个OBR(client, admin, world):
    first, second = _request(client, admin, world), _request(client, admin, world)
    resp = _post(client, admin, "P21759C", (f"{ID_CARD}^^^CN^ID", first, ""), ("MR0012345^^^HIS^MR", second, ""))
    assert (resp.status_code, resp.json()) == (
        422, {"detail": f"第 2 个 OBR（申请单 {second}）：PID-3 未带身份证号，无法核对患者"})
    assert (_status(first), _status(second)) == ("pending", "pending")


def test_真不一致的照旧原文案_病案号与证件号都带的照旧受理(client, admin, world):
    request_id = _request(client, admin, world)
    wrong = _post(client, admin, "P21759D", ("330106199901019999^^^CN^ID", request_id, ""))
    assert (wrong.status_code, wrong.json()) == (422, {"detail": "PID 患者与申请单患者不一致，结果拒收"})
    ok = _post(client, admin, "P21759E", (f"MR0012345^^^HIS^MR~{ID_CARD}^^^CN^ID", request_id, ""))
    assert ok.status_code == 201, ok.text
    assert _status(request_id) == "reported"
