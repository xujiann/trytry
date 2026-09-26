"""HL7 检验结果回传的 PID 核验核的是申请单患者本人：证件号不属于平台上任何人的，原先照样写进申请单患者名下（P1-143）。

ORU^R01 的注释写「PID 一致性核验（可选段）：报文声明的患者与申请单不一致时拒收，防串单」；实现却是按 PID-3 的证件号
在平台上找人，**找到了且不是申请单患者**才拒收——证件号不属于平台上任何人（检验科院内自建档、没进平台的患者）时
照单全收：别人的检验结果连同危急值闭环一起落到申请单患者身上，他自己的结果随后回传反而 409。
"""
import pytest

ID_CARD = "33010619800101123X"


def _oru(request_id, pid_id_card, control_id):
    lines = [f"MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260926100000||ORU^R01|{control_id}|P|2.4"]
    if pid_id_card is not None:
        lines.append(f"PID|1||{pid_id_card}^^^CN^ID||报文里的患者")
    lines += [f"OBR|1|{request_id}||K^血钾", "OBX|1|NM|K^血钾|1|6.9|mmol/L|3.5-5.3|HH"]
    return "\r".join(lines)


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2154 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2154 申请单患者", "id_card": ID_CARD, "gender": "男"}).json()["id"]
    return {"org": org, "patient": patient}


def _request(client, admin, world):
    resp = client.post("/api/exams", headers=admin, json={
        "patient_id": world["patient"], "from_org_id": world["org"], "center_type": "lab",
        "item_code": "K", "item_name": "血钾"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _post(client, admin, message):
    return client.post("/api/integration/hl7v2/oru", json={"message": message}, headers=admin)


def test_证件号不属于平台上任何人_拒收(client, admin, world):
    request_id = _request(client, admin, world)
    resp = _post(client, admin, _oru(request_id, "330106199901019999", "P2154A"))
    assert resp.status_code == 422, resp.text   # 修前 201：别人的血钾 6.9 连同危急值写进这位患者名下
    assert "不一致" in resp.json()["detail"]
    reported = client.get("/api/exams?status=reported", headers=admin).json()
    assert request_id not in [r["id"] for r in reported]


def test_申请单患者本人_末位小写x也认(client, admin, world):
    request_id = _request(client, admin, world)
    assert _post(client, admin, _oru(request_id, ID_CARD.lower(), "P2154B")).status_code == 201


def test_不带PID段的照旧受理(client, admin, world):
    request_id = _request(client, admin, world)
    assert _post(client, admin, _oru(request_id, None, "P2154C")).status_code == 201
