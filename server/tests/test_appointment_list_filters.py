"""管理端「预约记录」取得到压在最新 500 条之外的待核销预约（P2-1300，第三十八批扫描 AB2-3）。

修前：预约清单（`list_appointments`）只收 `patient_id` / `offset` / `limit`，按编号倒序缺省 500 条；预约页
`api("/api/appointments")` 不带参数，而到诊核销与取消按钮只在这张表里（P2-1063 写明它是唯一入口）。开发库实测：一周前约、
号源日期在今天的那条，之后又约了 520 条——页面取到 500 条（X-Total-Count = 521），那条不在；`?status=booked`、
`?slot_date=` 照样回同样的 500 条，被静默忽略。

修法：接口加可选 `status`（取值照预约状态列注释，`pattern` 限定、写错 422）与 `slot_date`（`require_date`，join 号源按
日期筛），都叠在可见范围之后；页面照 P2-408 / P2-456 先取 `status=booked` 排在最前、再接最新一页按 id 去重
（`core.js` 的 `actionableFirst`）。出参与分页契约不变。
"""
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import insert, select

from app.database import SessionLocal
from app.models import Appointment, AppointmentSlot
from conftest import business_today, login
from jssrc import strip_comments

CORE = strip_comments((Path(__file__).resolve().parents[1] / "app" / "static" / "core.js").read_text(encoding="utf-8"))


def test_预约页的已预约单独取_排在最前():
    start = CORE.index("async function renderAppointments()")
    body = CORE[start:CORE.index("\nasync function ", start + 1)]
    assert 'api("/api/appointments?status=booked")' in body   # 修前只取最新一页
    assert "const appointments = actionableFirst(recent, booked);" in body


@pytest.fixture(scope="module")
def world(client, admin):
    """今天就诊、一周前约的一条（最早的编号）；之后又约了 520 条，号源在昨天、已就诊或已取消。"""
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21300 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21300 患者", "id_card": "330782198701011300"}).json()["id"]
    today, yesterday = business_today().isoformat(), (business_today() - timedelta(days=1)).isoformat()
    slot = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": org, "resource_type": "outpatient", "resource_name": "P21300 王主任专家门诊",
        "slot_date": today, "slot_time": "09:00-10:00", "capacity": 5})
    assert slot.status_code == 201, slot.text
    first = client.post("/api/appointments", headers=admin, json={"slot_id": slot.json()["id"], "patient_id": patient})
    assert first.status_code == 201, first.text
    # 直接落库造量：一个号源一条预约，避开「同一号源同一人」的唯一约束
    statuses = [("fulfilled", "cancelled")[i % 2] for i in range(520)]
    with SessionLocal() as db:
        db.execute(insert(AppointmentSlot), [{
            "org_id": org, "resource_type": "outpatient", "resource_name": f"P21300 内科{i}", "slot_date": yesterday,
            "slot_time": "10:00-11:00", "capacity": 1, "booked": int(st == "fulfilled")} for i, st in enumerate(statuses)])
        slot_ids = db.scalars(select(AppointmentSlot.id).where(
            AppointmentSlot.org_id == org, AppointmentSlot.slot_date == yesterday).order_by(AppointmentSlot.id)).all()
        db.execute(insert(Appointment), [
            {"slot_id": sid, "patient_id": patient, "status": st} for sid, st in zip(slot_ids, statuses, strict=True)])
        db.commit()
    return {"first": first.json()["id"], "today": today, "yesterday": yesterday}


def test_最新500条之外的已预约_按状态取得到(client, admin, world):
    page = client.get("/api/appointments", headers=admin)
    assert len(page.json()) == 500 and world["first"] not in [a["id"] for a in page.json()]   # 前提：已被挤出最新一页
    booked = client.get("/api/appointments", headers=admin, params={"status": "booked"})
    assert world["first"] in [a["id"] for a in booked.json()]   # 修前 ?status= 被忽略，照回同样的 500 条
    assert {a["status"] for a in booked.json()} == {"booked"}


def test_按号源日期筛_可与状态叠加(client, admin, world):
    by_day = client.get("/api/appointments", headers=admin, params={"slot_date": world["today"]})
    assert [a["id"] for a in by_day.json()] == [world["first"]]   # 修前 ?slot_date= 被忽略，回最新 500 条
    assert by_day.headers["X-Total-Count"] == "1"
    done = client.get("/api/appointments", headers=admin, params={"slot_date": world["yesterday"], "status": "fulfilled"})
    assert done.headers["X-Total-Count"] == "260" and {a["status"] for a in done.json()} == {"fulfilled"}
    assert client.get("/api/appointments", headers=admin,
                      params={"slot_date": world["yesterday"], "status": "booked"}).json() == []


def test_状态与日期写错_422(client, admin):
    for bad in ("noshow", "BOOKED", "booked "):
        resp = client.get("/api/appointments", headers=admin, params={"status": bad})
        assert resp.status_code == 422, (bad, resp.text)   # 修前 200，照回最新一页
    resp = client.get("/api/appointments", headers=admin, params={"slot_date": "2026-9-1"})
    assert resp.status_code == 422 and resp.json()["detail"].startswith("slot_date："), resp.text


def test_新参数叠在可见范围之后_不绕过它(client, admin, world):
    """与这位患者毫无关系的卫生院经办：不带参数看不到，带上新参数也看不到（清单越权探针同一个问法）。"""
    other = client.post("/api/organizations", headers=admin, json={
        "name": "P21300 孤岛卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p21300_op", "password": "passw0rd1", "role": "operator", "org_id": other})
    assert created.status_code == 201, created.text
    operator = login(client, "p21300_op", "passw0rd1")
    for params in ({}, {"status": "booked"}, {"slot_date": world["today"]},
                   {"status": "booked", "slot_date": world["today"]}):
        resp = client.get("/api/appointments", headers=operator, params=params)
        assert resp.status_code == 200 and resp.json() == [], (params, resp.text)
