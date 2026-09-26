"""HIS 推送的 ADT^A03 出院与平台出院一样派出院随访任务（P2-160）。

A03 的说明写「出院镜像同步……平台侧发起的出院有病案首页已填写、费用已结清门禁；HIS 推送的 A03 是既成事实的镜像，
不设门禁……差异特此写明」——写明的差异只有门禁。实现却连统一随访中心的「出院随访」任务也不派（平台出院按
T2.4「出院即派生出院随访任务」派）：以 HIS 为出院来源的机构，每一例出院都悄悄漏出随访中心。
"""
import pytest

ID_CARD = "330106197706061606"


def _adt(event, control_id, pv1=""):
    lines = [f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260926090000||{event}|{control_id}|P|2.4",
             f"PID|1||{ID_CARD}^^^CN^ID||P2160 出院患者||19770606|F"]
    if pv1:
        lines.append(pv1)
    return "\r".join(lines)


@pytest.fixture(scope="module")
def admitted(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2160 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2160 内科"}).json()["id"]
    client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "8"})
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={
        "message": _adt("ADT^A01", "P2160A01", pv1="PV1|1|I|P2160 内科^1^8||||1001^李^主任")})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_A03出院派出院随访任务(client, admin, admitted):
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": _adt("ADT^A03", "P2160A03")})
    assert resp.status_code == 201, resp.text
    patient_id = admitted["patient"]["id"]
    tasks = client.get(f"/api/followups?category=discharge&patient_id={patient_id}", headers=admin).json()
    assert [t["source_id"] for t in tasks] == [admitted["admission_id"]]   # 修前 []：随访中心里没有这例出院
