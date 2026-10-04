"""「非计划重返手术室」能勾在本次住院的第一台手术上（P2-1398，第四十一批「手术与麻醉闭环」扫描 AE2-9 的校验一半）。

模型列注释、申请表单的勾选说明都把它定义为同一次住院内因并发症等**再次**手术。修前实测：住院里唯一一台手术勾了重返 → 201，
做完之后 2026-10 的「非计划重返手术室率」算成 1/2（50.0%）。修法：本次住院此前没有任何未取消的手术申请时，勾重返 422。
「不做推断」说的是不能由「同一住院有第二台」推断出重返，与本条不矛盾——有前一台的，勾不勾仍以医师标记为准。勾错、漏勾的
事后更正是业务口径，不在此列。
"""
import pytest

from conftest import login

from app.database import SessionLocal
from app.models import SurgeryRequest

S = "/api/surgery"
NO_PRIOR = "本次住院此前没有手术，不能标为非计划重返手术室"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21398 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    # 申请人不得自批（职责分离）：申请由本院医生提，驳回由管理员来
    resp = client.post("/api/users", headers=admin, json={
        "username": "p21398_doc", "password": "passw0rd1", "full_name": "P21398 医生", "role": "doctor", "org_id": org})
    assert resp.status_code in (200, 201), resp.text
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P21398 外科"}).json()["id"]
    admissions = {}
    for n, (key, id_card) in enumerate((("首台", "330102197003031398"), ("已有一台", "330102197004041398"),
                                        ("前一台已取消", "330102197005051398"))):
        patient = client.post("/api/patients", headers=admin, json={"name": f"P21398 {key}", "id_card": id_card})
        assert patient.status_code in (200, 201), patient.text
        bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": f"P21398-{n}"}).json()
        adm = client.post("/api/inpatient/admissions", headers=admin, json={
            "patient_id": patient.json()["id"], "ward_id": ward, "bed_id": bed["id"], "diagnosis_name": "腹股沟疝"})
        assert adm.status_code == 201, adm.text
        admissions[key] = adm.json()["id"]
    return {"doctor": login(client, "p21398_doc", "passw0rd1"), **admissions}


def _request(client, world, admission, name, unplanned):
    return client.post(f"{S}/requests", headers=world["doctor"], json={
        "admission_id": admission, "surgery_name": name, "unplanned_return": unplanned})


def _names(admission):
    with SessionLocal() as db:
        return sorted(r.surgery_name for r in db.query(SurgeryRequest).filter(SurgeryRequest.admission_id == admission))


def test_本次住院第一台勾重返_422(client, world):
    resp = _request(client, world, world["首台"], "P21398 腹股沟疝修补术", True)
    assert resp.status_code == 422, resp.text   # 修前 201：重返率算成 1/2
    assert resp.json()["detail"] == NO_PRIOR
    assert _names(world["首台"]) == []
    # 不勾的照常
    assert _request(client, world, world["首台"], "P21398 腹股沟疝修补术", False).status_code == 201


def test_同住院已有一台未取消的_第二台勾重返照收(client, world):
    first = _request(client, world, world["已有一台"], "P21398 腹腔镜胆囊切除术", False)
    assert first.status_code == 201, first.text
    again = _request(client, world, world["已有一台"], "P21398 胆漏再探查术", True)
    assert again.status_code == 201, again.text
    assert again.json()["unplanned_return"] is True


def test_前一台已取消的仍不能勾重返(client, admin, world):
    first = _request(client, world, world["前一台已取消"], "P21398 阑尾切除术", False)
    assert first.status_code == 201, first.text
    rejected = client.post(f"{S}/requests/{first.json()['id']}/approve", headers=admin, json={"approved": False})
    assert rejected.status_code == 200 and rejected.json()["status"] == "cancelled", rejected.text
    resp = _request(client, world, world["前一台已取消"], "P21398 阑尾残端再探查术", True)
    assert resp.status_code == 422, resp.text   # 修前 201：一台没做的手术也能「重返」
    assert resp.json()["detail"] == NO_PRIOR
