"""居民首页的纳管病种写上主管医生的姓名（P2-560，第十一批「居民端 vs 医护端」扫描 Y2-8）。

需求对照表居民端 #1、首页 docstring 都写「签约团队、主管医生」，首页却只给 `doctor_user_id` 一个整数，手机上一个字也不显示。
修后出参加 `doctor_name`，卡片写「主管医生」。只取姓名：医生没填姓名的给空串，不拿登录账号顶替（账号是登录凭据的一半，
不该亮给居民）。
"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import SmsCode

PHONE = "13913305600"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2560 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctors = {}
    for key, full_name in (("named", "王主管"), ("bare", "")):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2560_{key}", "password": "pass123456", "role": "doctor", "org_id": org,
            "full_name": full_name})
        assert created.status_code == 201, created.text
        doctors[key] = created.json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2560 居民", "id_card": "330127196001012560", "phone": PHONE}).json()["id"]
    for program, doctor in (("hypertension", doctors["named"]), ("diabetes", doctors["bare"])):
        resp = client.post("/api/spd/enrollments", headers=admin, json={
            "patient_id": patient, "program_code": program, "org_id": org, "doctor_user_id": doctor})
        assert resp.status_code == 201, resp.text
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
    return {"Authorization": f"Bearer {token}"}


def test_首页病种卡片带主管医生姓名_没填姓名不拿账号顶替(client, world):
    resp = client.get("/api/portal/spd/home", headers=world)
    assert resp.status_code == 200, resp.text
    names = {p["program_code"]: p["doctor_name"] for p in resp.json()["programs"]}
    assert names == {"hypertension": "王主管", "diabetes": ""}   # 修前没有这个键
