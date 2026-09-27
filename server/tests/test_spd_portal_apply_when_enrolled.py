"""已在管这个病种的居民，自查后不再提示「申请专病管理服务」，也申请不进去（P2-559，第十一批「居民端 vs 医护端」扫描 Y2-7）。

申请是给未纳管居民的（`SpdServiceApply` 注释「未纳管居民自查后提交」）；医护受理只是把人放进目标池，目标池里已纳管的
那条什么也不动——在管的居民申请了，受理什么都不发生，居民端却显示「已受理」。修后自查的 `can_apply` 对在管病种为假，
申请 409；没在管的病种照旧能申请。
"""
import pytest

from app.config import settings
from app.database import SessionLocal
from app.models import SmsCode

B = "/api/portal/spd"
PHONE = "13913305590"
HIGH_HYPERTENSION = {"family": "是", "salt": "是", "overweight": "是", "smoke": "是", "drink": "是", "symptom": "是"}
HIGH_DIABETES = {"age": "是", "family": "是", "overweight": "是", "inactive": "是", "gdm": "否", "symptom": "是"}


@pytest.fixture(scope="module")
def resident(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2559 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2559 居民", "id_card": "330127196001012559", "phone": PHONE}).json()["id"]
    assert client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).status_code == 201
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


def test_在管病种自查高危不提示申请_申请409(client, resident):
    screened = client.post(f"{B}/screenings", headers=resident, json={
        "program_code": "hypertension", "scale_code": "scr_hypertension", "answers": HIGH_HYPERTENSION})
    assert screened.status_code == 201, screened.text
    body = screened.json()
    assert (body["risk_level"], body["result"]) == ("high", "suspect")
    assert body["can_apply"] is False   # 修前 True：在管的人也弹「是否申请专病管理服务」
    applied = client.post(f"{B}/service-applies", headers=resident, json={
        "program_code": "hypertension", "screening_id": body["id"]})
    assert applied.status_code == 409, applied.text   # 修前 201，医护受理之后什么都不发生
    assert "已在专病管理中" in applied.json()["detail"]


def test_没在管的病种照旧可申请(client, resident):
    screened = client.post(f"{B}/screenings", headers=resident, json={
        "program_code": "diabetes", "scale_code": "scr_diabetes", "answers": HIGH_DIABETES}).json()
    assert screened["result"] == "suspect" and screened["can_apply"] is True
    applied = client.post(f"{B}/service-applies", headers=resident, json={
        "program_code": "diabetes", "screening_id": screened["id"]})
    assert applied.status_code == 201, applied.text
