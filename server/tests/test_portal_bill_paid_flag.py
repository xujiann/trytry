"""居民端账单的「已支付 / 待支付」按个人自付是否结清判，与收费处同一套算术（P1-152）。

原先 `paid` = 这张结算单有任何一张已支付的支付单：
- 押金全额冲抵个人自付的住院结算，收费处拒收（「押金已冲抵全部个人自付，无需再收」），居民端却永远「待支付」；
- 医保那一份以医保渠道入了账、个人自付分文未收的门诊结算，居民端成了「已支付」。
现在按 `billing.self_pay_outstanding`：自付 − 押金冲抵 − 非医保渠道已到账的净额 ≤ 0 即已结清。
"""
import pytest

from app.models import SmsCode
from app.routers.portal import _reset_portal_failures
from conftest import login as staff_login

PHONE = "13700015201"


@pytest.fixture(autouse=True)
def clean_state():
    _reset_portal_failures()
    yield
    _reset_portal_failures()


def _resident_login(client, phone):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    return {"Authorization": f"Bearer {body['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1152 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    client.post("/api/users", headers=admin, json={
        "username": "p1152_op", "password": "pass123456", "role": "operator", "org_id": org})
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P1152 居民", "id_card": "330106197909091526", "phone": PHONE}).json()["id"]
    for code, price in (("P1152-BED", 3000), ("P1152-CT", 1000)):
        client.post("/api/billing/charge-items", headers=admin, json={
            "code": code, "name": code, "category": "treatment", "price": price})
    return {"org": org, "patient": patient, "op": staff_login(client, "p1152_op", "pass123456")}


def _paid(client, headers, settlement_id):
    rows = client.get("/api/portal/me/bills", headers=headers).json()
    return next(r["paid"] for r in rows if r["id"] == settlement_id)


def test_押金全额冲抵的住院结算是已支付(client, admin, world):
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": world["org"], "name": "P1152 病区"}).json()
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "1"}).json()
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": world["patient"], "ward_id": ward["id"], "bed_id": bed["id"], "diagnosis_name": "肺炎"}).json()
    assert client.post("/api/billing/deposits", headers=admin, json={"admission_id": adm["id"], "amount": 5000}).status_code == 201
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": world["patient"], "admission_id": adm["id"], "item_code": "P1152-BED"})
    settle = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "inpatient", "admission_id": adm["id"], "insurance_pay": 2000}).json()
    assert (settle["self_pay"], settle["deposit_offset"], settle["payable_after_offset"]) == (1000, 1000, 0)
    refused = client.post("/api/billing/payments", headers=world["op"], json={
        "settlement_id": settle["id"], "channel": "cash"})
    assert refused.status_code == 422 and "无需再收" in refused.json()["detail"]   # 收费处认为已结清
    assert _paid(client, _resident_login(client, PHONE), settle["id"]) is True     # 修前 False：永远「待支付」


def test_只收了医保那一份_自付未收是待支付_收了自付才是已支付(client, admin, world):
    enc = client.post("/api/encounters", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "doctor_name": "门诊医生", "diagnosis_name": "头痛"}).json()
    client.post("/api/billing/details", headers=admin, json={
        "patient_id": world["patient"], "encounter_id": enc["id"], "item_code": "P1152-CT"})
    settle = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": enc["id"], "insurance_pay": 600}).json()
    ins = client.post("/api/billing/payments", headers=world["op"], json={
        "settlement_id": settle["id"], "channel": "insurance"})
    assert ins.status_code == 201 and ins.json()["status"] == "paid", ins.text
    resident = _resident_login(client, PHONE)
    assert _paid(client, resident, settle["id"]) is False     # 修前 True：医保入了账就算「已支付」
    cash = client.post("/api/billing/payments", headers=world["op"], json={
        "settlement_id": settle["id"], "channel": "cash"})
    assert cash.status_code == 201 and cash.json()["amount"] == 400, cash.text
    assert _paid(client, resident, settle["id"]) is True
