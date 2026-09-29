"""「这一胎哪天结束」（P2-233）对三类存量行按「日子未知」处理，不 500、不误拦、不漏拦（P2-895，第二十四批「存量行 vs 新规则」扫描 Z3-6）。

1. 一档两条分娩：唯一索引 `uq_delivery_record` 遇存量重复不建（迁移 b9c8d7e6f5a4），`.scalar()` 让补录孕期唐筛 500；
2. 没填访视日期、录入时刻是补列时回填的 1970 哨兵（迁移 d9f0a1b2c3e4）：按 1970-01-01 算，这一胎任何筛查都 409；
3. 访视日期是 P1-61 之前的非规范写法「2026/03/10」：按字符串比比任何规范日期都「早」（'-' < '/'），9 月新一胎的高风险
   唐筛照样录进旧档案、把旧档案标成高危（P2-211 要防的正是这个）。
修后分娩取最早的一条；1970 哨兵不参与推算；日期按日历读（`legacy_date`），读不成的不参与。
"""
from datetime import datetime

import pytest
from sqlalchemy import text

from app.database import SessionLocal, engine
from app.models import DeliveryRecord, MaternalVisit


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2895 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin, card):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2895 孕妇", "id_card": card, "gender": "女"}).json()["id"]
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _screen(client, admin, record, day, result="low_risk"):
    return client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "down", "screen_date": day, "result": result})


def test_一档两条分娩_取最早的一条_不500(client, admin, org):
    record = _record(client, admin, "330106199303032895")
    with engine.begin() as conn:   # 存量重复让迁移没建成唯一索引的库
        conn.execute(text("DROP INDEX IF EXISTS uq_delivery_record"))
    with SessionLocal() as db:
        db.add_all([DeliveryRecord(record_id=record, org_id=org, delivery_date=day) for day in ("2026-03-12", "2026-03-10")])
        db.commit()
    assert _screen(client, admin, record, "2026-01-20").status_code == 201   # 修前 500 MultipleResultsFound
    late = _screen(client, admin, record, "2026-03-11")
    assert late.status_code == 409 and "分娩日期 2026-03-10" in late.json()["detail"], late.text


def test_访视录入时刻是1970哨兵_不参与推算(client, admin, org):
    record = _record(client, admin, "330106199404042895")
    with SessionLocal() as db:
        db.add(MaternalVisit(record_id=record, visit_type="postpartum", visit_date="",
                             created_at=datetime(1970, 1, 1)))
        db.commit()
    got = _screen(client, admin, record, "2026-05-10")
    assert got.status_code == 201, got.text   # 修前 409「晚于这一胎的产后访视日期 1970-01-01」


def test_非规范写法的访视日期按日历读(client, admin, org):
    record = _record(client, admin, "330106199505052895")
    with SessionLocal() as db:
        db.add(MaternalVisit(record_id=record, visit_type="postpartum", visit_date="2026/03/10"))
        db.commit()
    got = _screen(client, admin, record, "2026-09-15", result="high_risk")
    assert got.status_code == 409, got.text   # 修前 201：旧档案被标成高危
    assert "产后访视日期 2026-03-10" in got.json()["detail"]
    rows = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in rows if r["id"] == record)["high_risk"] is False
