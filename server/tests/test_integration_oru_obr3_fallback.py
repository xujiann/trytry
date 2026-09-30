"""ORU 的 OBR-2 为空时拿 LIS 自己的单号（OBR-3）当平台申请单号，不带 PID 就把结果连同危急值落到别人的申请单上（P2-986，
第二十八批「编号、单号与流水号」扫描 F3-1）。

`_oru_request` 原先 `OBR-2 or OBR-3` 取单号：OBR-2 是下单方（平台）单号，OBR-3 是执行方（LIS）自己编的号。PID 段可选，
不带就不核患者（带 OBR-2 的照旧可以不带，`test_integration_oru_pid_check.py::test_不带PID段的照旧受理`）。LIS 回传一份平台上
没有申请单的结果、OBR-2 为空、OBR-3 是它自己的样本号——这个号恰好等于乙的平台申请单号时，结果和危急值写进乙的申请单，
乙自己的真实结果随后回传 409「已报告」。FHIR 兄弟路径只认平台申请单号、没有回退。

修法：按 OBR-3 回退时必须带 PID（有证件号），照旧核对申请单患者；不带的 422，申请单不动。
"""
import pytest

ID_CARD = "330106197505050097"


def _oru(obr2, obr3, pid_id_card, control_id):
    lines = [f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260930100000||ORU^R01|{control_id}|P|2.4"]
    if pid_id_card is not None:
        lines.append(f"PID|1||{pid_id_card}^^^CN^ID||报文里的患者")
    lines += [f"OBR|1|{obr2}|{obr3}|K^血钾", "OBX|1|NM|K^血钾|1|6.9|mmol/L|3.5-5.3|HH"]
    return "\r".join(lines)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2986 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2986 乙", "id_card": ID_CARD, "gender": "男"}).json()["id"]
    return {"org": org, "patient": patient}


def _request(client, admin, world):
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "K", "item_name": "血钾"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post(client, admin, message):
    return client.post("/api/integration/hl7v2/oru", json={"message": message}, headers=admin)


def _status(client, admin, request_id):
    return next(r for r in client.get("/api/exams", headers=admin).json() if r["id"] == request_id)["status"]


def test_OBR2为空_按OBR3回退又不带PID_拒收(client, admin, world):
    request_id = _request(client, admin, world)
    resp = _post(client, admin, _oru("", request_id, None, "P2986A"))   # LIS 样本号恰好等于乙的申请单号
    assert resp.status_code == 422 and "OBR-2" in resp.json()["detail"], resp.text   # 修前 201，结果写进乙的申请单
    assert _status(client, admin, request_id) != "reported"
    mine = _post(client, admin, _oru(request_id, "", None, "P2986B"))   # 乙本人的结果（带平台单号）照常受理
    assert mine.status_code == 201, mine.text   # 修前 409「已报告」


def test_按OBR3回退_带PID且对得上照旧受理(client, admin, world):
    request_id = _request(client, admin, world)
    assert _post(client, admin, _oru("", request_id, ID_CARD, "P2986C")).status_code == 201


def test_按OBR3回退_PID对不上照旧拒收(client, admin, world):
    request_id = _request(client, admin, world)
    resp = _post(client, admin, _oru("", request_id, "330106199901019999", "P2986D"))
    assert resp.status_code == 422 and "不一致" in resp.json()["detail"], resp.text
