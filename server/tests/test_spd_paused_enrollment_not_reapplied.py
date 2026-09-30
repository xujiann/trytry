"""召回中 / 脱管的慢专病档案：居民端首页说「没有签约的慢专病管理」、自查提示去申请、申请照收；照受理结果再建档后，出现两份
档案，原来那份的召回永远结不了（P2-1050，第三十批「居民端 vs 管理端」扫描 D4-4）。

`service.ENROLLMENT_ENDED_STATUSES` 旁写着「召回中 / 脱管不算（已结束）——人还挂在本机构、正在找回来」；P2-559 定的规矩是
「申请是给未纳管居民的」。居民端首页、自查的 can_apply、申请只认在管（active），建档只被在管唯一索引挡：召回中的居民被
引导重新申请、医护受理、再建一份在管档案，原档案「已召回」登记 409「已在…在管（档案 #2）」。

修法：首页另列脱管 / 召回中的病种（不混进在管的 programs）并提示联系签约团队；自查不提示申请、申请 409；建档遇同病种脱管 /
召回中的档案 409，提示在原档案上恢复。
"""
import pytest

B = "/api/spd"
P = "/api/portal/spd"
PHONE = "13900010500"
ANSWERS = {"family": "是", "salt": "是", "overweight": "是", "smoke": "是", "drink": "是", "symptom": "是"}


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21050 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21050 居民", "id_card": "330199198501011050", "gender": "男", "birth_date": "1985-01-01",
        "phone": PHONE}).json()
    with SessionLocal() as db:   # 召回登记之后的档案
        enrollment = SpdEnrollment(patient_id=patient["id"], program_code="hypertension", org_id=org, status="recalled")
        db.add(enrollment)
        db.commit()
        enrollment_id = enrollment.id
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    resident = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", headers=resident, json={
        "name": patient["name"], "id_card": patient["id_card"]})
    assert bound.status_code in (200, 409), bound.text
    return {"org": org, "patient": patient["id"], "enrollment": enrollment_id, "resident": resident}


def test_首页另列召回中的病种_不说没有签约(client, world):
    home = client.get(f"{P}/home", headers=world["resident"]).json()
    assert home["enrolled"] is False and home["programs"] == []   # 在管的口径不变
    assert [(p["program_code"], p["status_name"]) for p in home["paused_programs"]] == [("hypertension", "召回中")]


def test_自查不提示申请_申请409(client, world):
    screening = client.post(f"{P}/screenings", headers=world["resident"], json={
        "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": ANSWERS})
    assert screening.status_code == 201, screening.text
    assert screening.json()["can_apply"] is False   # 修前 True
    applied = client.post(f"{P}/service-applies", headers=world["resident"], json={"program_code": "hypertension"})
    assert applied.status_code == 409 and "召回中" in applied.json()["detail"], applied.text   # 修前 201


def test_同病种有召回中的档案_不另建(client, admin, world):
    created = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "org_id": world["org"]})
    assert created.status_code == 409, created.text   # 修前 201，两份档案
    assert f"召回中的档案（#{world['enrollment']}）" in created.json()["detail"]
