"""慢专病团队工作台的「召回中」预警恒为 0（P2-130）。

`/api/spd/workbench/team` 的预警面板有一格「召回中 N」（模型注释 `recalled=召回中`）。实现把它写成在管查询
（`status == 'active'`）上再加 `status == 'recalled'`——active 且 recalled，永远 0；召回的患者又不在「在管」里，
于是一个被召回的患者在这一页上哪儿都看不见。

修法：「我的档案」范围（按角色 + 病种）与「在管」分开，召回数按范围里 recalled 的数。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2130 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2130_doc", "password": "pw123456", "full_name": "P2130 医生", "role": "doctor", "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    token = client.post("/api/auth/login", json={"username": "p2130_doc", "password": "pw123456"}).json()["access_token"]
    patients = [client.post("/api/patients", headers=admin, json={
        "name": f"P2130 患者{i}", "id_card": f"33010619660606{i:04d}", "gender": "男"}).json()["id"] for i in range(3)]
    with SessionLocal() as db:
        rows = [SpdEnrollment(patient_id=p, program_code="P2130_HTN", org_id=org, status="active",
                              doctor_user_id=doctor.json()["id"]) for p in patients]
        db.add_all(rows)
        db.commit()
        ids = [r.id for r in rows]
    return {"headers": {"Authorization": f"Bearer {token}"}, "enrollments": ids}


def _board(client, world):
    resp = client.get("/api/spd/workbench/team?role=member", headers=world["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_召回的患者在工作台上看得见(client, admin, world):
    before = _board(client, world)
    assert (before["patients"]["managed"], before["alerts"]["recall"]) == (3, 0)
    recalled = client.post(f"/api/spd/enrollments/{world['enrollments'][0]}/lifecycle", headers=admin,
                           json={"event": "recall", "reason": "三个月未随访"})
    assert recalled.status_code == 200, recalled.text
    after = _board(client, world)
    assert after["patients"]["managed"] == 2
    assert after["alerts"]["recall"] == 1   # 修前 0：在管少了一个，召回里也没有，这位患者哪儿都看不见


def test_召回数只数我的档案(client, admin, world):
    """范围照旧按角色：别的医生名下的召回不算进来。"""
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2130 他人患者", "id_card": "330106196606069999", "gender": "女"}).json()["id"]
    with SessionLocal() as db:
        org = db.get(SpdEnrollment, world["enrollments"][0]).org_id
        db.add(SpdEnrollment(patient_id=patient, program_code="P2130_HTN", org_id=org, status="recalled"))
        db.commit()
    assert _board(client, world)["alerts"]["recall"] == 1
