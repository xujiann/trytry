"""居民端的慢专病转诊在下转之后仍写上转去的那家（P2-558，第十一批「居民端 vs 医护端」扫描 Y2-6）。

下转时 `target_org_id` / `current_org_id` 都改写成下转目标，居民端转诊卡片的「转入」跟着从县医院变成下转去的卫生院；
详情也没有机构名（需求对照表居民端 #17「进入详情查询转诊机构」）。修后「转入」取上转去的那家（县级医院接收那一步的
机构），详情给出转出、转入、下转至三家。
"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import SmsCode

B = "/api/spd"
PHONE = "13913305580"


def _login(client, username):
    token = client.post("/api/auth/login", json={"username": username, "password": "pass123456"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P2558 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    town = client.post("/api/organizations", headers=admin, json={
        "name": "P2558 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
    village = client.post("/api/organizations", headers=admin, json={
        "name": "P2558 村卫生室", "org_type": "village", "level": "village", "parent_id": town}).json()["id"]
    h = {}
    for key, org in (("county", county), ("town", town), ("village", village)):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2558_{key}", "password": "pass123456", "role": "doctor", "org_id": org})
        assert created.status_code == 201, created.text
        h[key] = _login(client, f"p2558_{key}")
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2558 居民", "id_card": "330127195803032558", "phone": PHONE}).json()["id"]
    assert client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": village}).status_code == 201
    case = client.post(f"{B}/referrals", headers=h["village"], json={
        "patient_id": patient, "program_code": "hypertension", "target_org_id": county, "reason": "P2558 血压控制不佳"})
    assert case.status_code == 201, case.text
    cid = case.json()["id"]
    for who, path, body in (("town", "review", {"action": "pass"}), ("county", "review", {"action": "pass"}),
                            ("county", "arrive", {"effective_visit": True}),
                            ("county", "down", {"target_org_id": town})):
        resp = client.post(f"{B}/referrals/{cid}/{path}", headers=h[who], json=body)
        assert resp.status_code == 200, (path, resp.text)
    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    old = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old
    return {"case": cid, "resident": {"Authorization": f"Bearer {token}"}}


def test_下转之后转入仍是上转去的县医院_详情给三家机构(client, world):
    feed = client.get("/api/portal/me/referrals/all", headers=world["resident"], params={"source": "spd"}).json()
    card = next(r for r in feed if r["id"] == world["case"])
    assert (card["from_org"], card["to_org"]) == ("P2558 村卫生室", "P2558 县医院")   # 修前转入是下转去的卫生院
    detail = client.get(card["detail_path"], headers=world["resident"])
    assert detail.status_code == 200, detail.text
    body = detail.json()
    assert (body["from_org"], body["to_org"], body["down_to_org"]) == ("P2558 村卫生室", "P2558 县医院", "P2558 卫生院")
