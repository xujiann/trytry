"""押金「方式」原样显示编码：居民端 cash / card / online，管理端结算冲抵的流水显示 settle（P2-375）。

居民端押金流水直接打 `method`；管理端前端自带一张 { cash, card, online } 表，结算冲抵写的 `settle` 不在表里、原样显示。
出参只有 `deposit_type_name` 带了中文，`method` 没有。

修法：两个出参都带 `method_name`（后端一张表，含 settle），两端显示它；管理端那张前端表删掉。
"""
from pathlib import Path

import pytest

from app.database import SessionLocal

STATIC = Path(__file__).resolve().parents[1] / "app" / "static"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2375 押金医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": "P2375 病区"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2375 患者", "id_card": "330782199004042375", "phone": "13800032375"}).json()["id"]
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward, "bed_no": "P2375-1"}).json()["id"]
    admission = client.post("/api/inpatient/admissions", headers=admin, json={
        "patient_id": patient, "ward_id": ward, "bed_id": bed}).json()["id"]
    op = client.post("/api/users", headers=admin, json={
        "username": "p2375_op", "password": "passw0rd1", "role": "operator", "full_name": "经办", "org_id": org})
    assert op.status_code == 201, op.text
    token = client.post("/api/auth/login", json={"username": "p2375_op", "password": "passw0rd1"}).json()
    return {"admission": admission, "op": {"Authorization": f"Bearer {token['access_token']}"}, "phone": "13800032375"}


def test_押金流水带方式名称(client, world):
    from app.models import Deposit

    got = client.post("/api/billing/deposits", headers=world["op"], json={
        "admission_id": world["admission"], "amount": 500, "method": "card"})
    assert got.status_code == 201, got.text
    assert got.json()["method_name"] == "刷卡"   # 修前没有这个键
    with SessionLocal() as db:   # 结算冲抵的流水记 settle（结算时写），这里直接落一条
        db.add(Deposit(admission_id=world["admission"], amount=100, deposit_type="offset", method="settle",
                       operator="结算"))
        db.commit()
    rows = client.get(f"/api/billing/deposits?admission_id={world['admission']}", headers=world["op"]).json()
    assert {r["method"]: r["method_name"] for r in rows} == {"card": "刷卡", "settle": "结算冲抵"}


def test_居民端押金流水带方式名称(client, world):
    from app.models import SmsCode

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == world["phone"]).delete()
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": world["phone"], "purpose": "login"}).json()
    login = client.post("/api/portal/auth/sms/login", json={"phone": world["phone"], "code": code["debug_code"]})
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    got = client.get(f"/api/portal/me/deposits?admission_id={world['admission']}", headers=headers)
    assert got.status_code == 200, got.text
    assert {i["method"]: i["method_name"] for i in got.json()["items"]} == {"card": "刷卡", "settle": "结算冲抵"}


def test_两端页面显示方式名称():
    resident = (STATIC / "m" / "m.js").read_text(encoding="utf-8")
    desk = (STATIC / "pages-clinical.js").read_text(encoding="utf-8")
    assert "i.method_name" in resident   # 修前 esc(i.method || "—")
    assert "d.method_name" in desk and "DEPOSIT_METHODS" not in desk   # 修前前端自带一张缺 settle 的表
