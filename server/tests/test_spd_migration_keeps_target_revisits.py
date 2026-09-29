"""迁入确认收尾原档案时，目标机构自己的医生排的同病种复诊不跟着移除（P2-849，第二十三批「同一患者名下多份并存」扫描 Y3-5）。

`close_open_work` 收随访时只收「本机构（或没挂机构）」的（P1-129：迁入确认时目标机构自己的随访不能被原档案的结案带走），
复诊却只按（患者, 病种）取：迁出待确认期间，目标机构的医生给患者排好的复诊，目标机构一确认就成了「已移除」（日志
「迁出至其他机构」，待呼叫一并撤掉），同一时候排的随访原样保留。复诊表没有机构列，修后按复诊医生所在机构认：目标机构
医生的留下；原机构医生的、没填医生的照旧随原档案移除。死亡 / 排除不受影响。
"""
from datetime import timedelta

import pytest

from app import clock

B = "/api/spd"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "passw0rd1"}).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def world(client, admin):
    orgs, doctors, headers = {}, {}, {}
    for key in ("east", "west"):
        orgs[key] = client.post("/api/organizations", headers=admin, json={
            "name": f"P2849 {key}卫生院", "org_type": "township", "level": "township"}).json()["id"]
        made = client.post("/api/users", headers=admin, json={
            "username": f"p2849_{key}", "password": "passw0rd1", "full_name": f"p2849_{key}", "role": "doctor",
            "org_id": orgs[key]})
        assert made.status_code in (200, 201), made.text
        doctors[key] = made.json()["id"]
        headers[key] = _login(client, f"p2849_{key}")
    return {"orgs": orgs, "doctors": doctors, "headers": headers}


def _patient_moving_west(client, admin, world, n):
    """东镇在管的高血压患者，登记迁往西镇、待确认；西镇接诊过（看得见）。"""
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2849 患者{n}", "id_card": f"33010219620202284{n}"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["orgs"]["east"],
        "doctor_user_id": world["doctors"]["east"]})
    assert enrolled.status_code == 201, enrolled.text
    served = client.post("/api/encounters", headers=world["headers"]["west"], json={
        "patient_id": patient, "org_id": world["orgs"]["west"], "diagnosis_name": "高血压"})
    assert served.status_code in (200, 201), served.text
    moved = client.post(f"{B}/enrollments/{enrolled.json()['id']}/lifecycle", headers=admin, json={
        "event": "migrate", "reason": "搬家", "target_org_id": world["orgs"]["west"]})
    assert moved.status_code == 200, moved.text
    return patient, enrolled.json()["id"], moved.json()["event_id"]


def _revisit(client, headers, patient, doctor=None):
    body = {"patient_id": patient, "program_code": "hypertension",
            "plan_date": (clock.today() + timedelta(days=10)).isoformat()}
    if doctor is not None:
        body["doctor_user_id"] = doctor
    made = client.post(f"{B}/revisits", headers=headers, json=body)
    assert made.status_code == 201, made.text
    return made.json()["id"]


def _statuses(client, admin, patient):
    return {r["id"]: r["status"] for r in
            client.get(f"{B}/revisits", headers=admin, params={"patient_id": patient}).json()}


def test_迁入确认_目标机构医生排的复诊留下_原机构的照旧移除(client, admin, world):
    patient, _, event_id = _patient_moving_west(client, admin, world, 1)
    west = _revisit(client, world["headers"]["west"], patient, world["doctors"]["west"])
    east = _revisit(client, world["headers"]["east"], patient, world["doctors"]["east"])
    nobody = _revisit(client, world["headers"]["west"], patient)   # 没填医生：认不出是谁排的
    confirmed = client.post(f"{B}/lifecycle-events/{event_id}/confirm", headers=world["headers"]["west"])
    assert confirmed.status_code == 200, confirmed.text
    assert _statuses(client, admin, patient) == {west: "planned", east: "removed", nobody: "removed"}   # 修前西镇那条也 removed


def test_死亡照旧同病种一并移除(client, admin, world):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2849 死亡", "id_card": "330102196202022849"}).json()["id"]
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["orgs"]["east"]})
    assert enrolled.status_code == 201, enrolled.text
    served = client.post("/api/encounters", headers=world["headers"]["west"], json={
        "patient_id": patient, "org_id": world["orgs"]["west"], "diagnosis_name": "高血压"})
    assert served.status_code in (200, 201), served.text
    west = _revisit(client, world["headers"]["west"], patient, world["doctors"]["west"])
    died = client.post(f"{B}/enrollments/{enrolled.json()['id']}/lifecycle", headers=admin,
                       json={"event": "death", "reason": "病故"})
    assert died.status_code == 200, died.text
    assert _statuses(client, admin, patient) == {west: "removed"}
