"""结算单不收与结算类型对不上的住院号 / 就诊号；居民端住院费用清单只取住院结算（P2-913，第二十五批「费用与业务状态」扫描 J3-5）。

`create_settlement` 把住院号、就诊号两个都原样写进结算单：丁的门诊结算带上丙的住院号 201，丙的居民端住院费用清单
「结算摘要」里就多出这张门诊结算（丙的费用合计 1000，两张结算 1200，对不上）；门诊结算带一个不存在的住院号，写库撞
外键被翻成「该住院记录已有结算单，不可重复结算」409。页面两个号框并排，报错后表单保留原值，换成门诊结算时上次填的
住院号会一起送上去。修后对不上的号 422；居民端清单只取住院结算（与「我的住院」同一句）；页面只送对应的那个号。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal
from app.models import Settlement, SmsCode, User

ITEM = "P2913-FEE"
PHONE = "13800002913"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2913 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2913 病区"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2913-1"}).json()["id"]
    client.post("/api/dictionaries/charge/entries", headers=admin, json={"code": ITEM, "name": "诊疗费(P2913)"})
    item = client.post("/api/billing/charge-items", headers=admin,
                       json={"code": ITEM, "name": "诊疗费(P2913)", "category": "treatment", "price": 1000})
    assert item.status_code in (201, 409), item.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2913 丙", "id_card": "330106197909092913", "gender": "男", "phone": PHONE}).json()["id"]
    adm = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed, "diagnosis_name": "肺炎"})
    assert adm.status_code == 201, adm.text
    return {"org": org, "patient": patient, "admission": adm.json()["id"]}


def test_结算类型对不上的号_422(client, admin, world):
    got = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "outpatient", "encounter_id": 999999, "admission_id": 999999, "insurance_pay": 0})
    assert got.status_code == 422, got.text   # 修前 409「该住院记录已有结算单」（其实是不存在的住院号撞外键）
    assert "不带住院号" in got.json()["detail"]
    got = client.post("/api/billing/settlements", headers=admin, json={
        "bill_type": "inpatient", "admission_id": world["admission"], "encounter_id": 1, "insurance_pay": 0})
    assert got.status_code == 422 and "不带就诊号" in got.json()["detail"], got.text


def test_居民端住院费用清单只列住院结算(client, admin, world):
    with SessionLocal() as db:   # 修前落下的：别人的门诊结算带着这次住院号
        operator = db.query(User.id).filter(User.username == "admin").scalar()
        db.add(Settlement(patient_id=world["patient"], org_id=world["org"], bill_type="outpatient",
                          admission_id=world["admission"], total_amount=200, insurance_pay=0, self_pay=200,
                          created_by=operator))
        db.query(SmsCode).filter(SmsCode.phone == PHONE).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE, "purpose": "login"}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    bill = client.get(f"/api/portal/me/admissions/{world['admission']}/bill",
                      headers={"Authorization": f"Bearer {token}"})
    assert bill.status_code == 200, bill.text
    assert bill.json()["settlements"] == []   # 修前列出那张 200 元的门诊结算


def test_页面结算只送对应的那个号():
    source = (Path(__file__).resolve().parent.parent / "app" / "static" / "pages-clinical.js").read_text(encoding="utf-8")
    start = source.index('$("#settle-form").onsubmit')
    handler = source[start:source.index("};", start)]
    assert 'delete body[body.bill_type === "inpatient" ? "encounter_id" : "admission_id"]' in handler
