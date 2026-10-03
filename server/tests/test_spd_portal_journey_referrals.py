"""「我的慢专病」把同一张转诊单在「已迁出」「在管」两张病种卡片里各列一次（P2-1182，第三十四批扫描 L4-8）。

`portal.journey` 每份纳管档案一张卡片：路径与任务按档案（enrollment_id）取，转诊却按「患者 + 病种」取。跨机构迁出确认
（或排除后再签约）之后同病种有两份档案，同一张转诊单在两张卡片里各列一次，而「我的全部转诊」只有这一条。

修法：转诊按它挂的档案（`SpdReferralCase.enrollment_id`）取，与任务同一个口径；没挂档案的存量单（enrollment_id 为空）只挂在
这个病种最新、在管优先的那份档案下。
"""
import pytest

from app.config import settings
from app.database import SessionLocal

B = "/api/spd"
PHONE = "13912201182"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Patient, ResidentAccount
    from app.spd.models import SpdReferralCase

    org_a = client.post("/api/organizations", headers=admin, json={
        "name": "P21182 甲卫生院", "org_type": "township", "level": "township"}).json()["id"]
    org_b = client.post("/api/organizations", headers=admin, json={
        "name": "P21182 乙卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        me = Patient(ehc_no="EHC-P21182", name="P21182 居民", id_card="330106196001011182", gender="男",
                     birth_date="1960-01-01", phone=PHONE)
        db.add(me)
        db.flush()
        db.add(ResidentAccount(phone=PHONE, patient_id=me.id, nickname="P21182", wechat_openid="", status="active"))
        db.commit()
        patient = me.id
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org_a})
    assert enrolled.status_code == 201, enrolled.text
    old = enrolled.json()["id"]
    referral = client.post(f"{B}/referrals", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "reason": "血压控制不佳上转"})
    assert referral.status_code == 201 and referral.json()["enrollment_id"] == old, referral.text
    migrated = client.post(f"{B}/enrollments/{old}/lifecycle", headers=admin, json={
        "event": "migrate", "target_org_id": org_b, "reason": "搬家"})
    assert migrated.status_code == 200, migrated.text
    confirmed = client.post(f"{B}/lifecycle-events/{migrated.json()['event_id']}/confirm", headers=admin)
    assert confirmed.status_code == 200, confirmed.text
    with SessionLocal() as db:
        # 存量单：P1-139 之前开的、没挂档案
        legacy = SpdReferralCase(patient_id=patient, program_code="hypertension", enrollment_id=None,
                                 initiator_org_id=org_a, reason="存量转诊单")
        db.add(legacy)
        db.commit()
        legacy_id = legacy.id
    old_echo = settings.sms_debug_echo
    settings.sms_debug_echo = True
    try:
        code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
        token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    finally:
        settings.sms_debug_echo = old_echo
    return {"referral": referral.json()["id"], "legacy": legacy_id,
            "resident": {"Authorization": f"Bearer {token}"}}


def _cards(client, world):
    """{档案状态: 这张卡片列出的转诊单号}——同病种一张已迁出、一张在管。"""
    resp = client.get("/api/portal/spd/journey", headers=world["resident"])
    assert resp.status_code == 200, resp.text
    programs = [p for p in resp.json()["programs"] if p["program_code"] == "hypertension"]
    assert sorted(p["status"] for p in programs) == ["active", "migrated"], programs
    return {p["status"]: [r["id"] for r in p["referrals"]] for p in programs}


def test_迁出确认后_转诊单只列在它挂的那份档案下(client, world):
    cards = _cards(client, world)
    assert world["referral"] in cards["migrated"]
    assert world["referral"] not in cards["active"]   # 修前两张卡片各列一次


def test_没挂档案的存量单_只挂在该病种在管的那份(client, world):
    cards = _cards(client, world)
    assert world["legacy"] in cards["active"]
    assert world["legacy"] not in cards["migrated"]   # 修前两张卡片各列一次
    listed = [rid for ids in cards.values() for rid in ids]
    assert sorted(listed) == sorted([world["referral"], world["legacy"]])   # 合计各一次
