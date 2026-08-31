"""D1 号源批量生成（POST /api/appointments/slots/batch）：模板×日期区间展开、
节假日/周末跳过、生成量上限保护、重复日期幂等跳过。"""
import pytest

from app.database import SessionLocal
from app.models import AppointmentSlot, Employee, Organization


@pytest.fixture(scope="module")
def org_and_doctor(client):
    db = SessionLocal()
    try:
        org = Organization(name="批量号源测试医院", org_type="lead_hospital", level="county")
        other = Organization(name="批量号源他院", org_type="township", level="township")
        db.add_all([org, other])
        db.flush()
        doctor = Employee(org_id=org.id, name="王批量", title="副高", position="心内科")
        outsider = Employee(org_id=other.id, name="外院李", title="中级", position="全科")
        db.add_all([doctor, outsider])
        db.commit()
        return {"org_id": org.id, "employee_id": doctor.id, "outsider_id": outsider.id}
    finally:
        db.close()


def _slot_count(org_id: int) -> int:
    db = SessionLocal()
    try:
        return db.query(AppointmentSlot).filter(AppointmentSlot.org_id == org_id).count()
    finally:
        db.close()


def test_batch_generate_with_skips_and_idempotent_rerun(client, admin, org_and_doctor):
    body = {
        "org_id": org_and_doctor["org_id"],
        "templates": [
            {"resource_type": "outpatient", "resource_name": "心内科上午",
             "employee_id": org_and_doctor["employee_id"], "slot_time": "08:00-12:00", "capacity": 20},
            {"resource_type": "outpatient", "resource_name": "心内科下午",
             "employee_id": org_and_doctor["employee_id"], "slot_time": "14:00-17:00", "capacity": 15},
        ],
        # 2026-09-07(一)～2026-09-13(日)：跳周末（12/13）与节假日 09-10 → 4 天
        "date_from": "2026-09-07",
        "date_to": "2026-09-13",
        "skip_dates": ["2026-09-10"],
        "skip_weekends": True,
    }
    first = client.post("/api/appointments/slots/batch", json=body, headers=admin)
    assert first.status_code == 201, first.text
    assert first.json() == {"created": 8, "skipped": 0}  # 4 天 × 2 模板
    assert _slot_count(org_and_doctor["org_id"]) == 8

    # 幂等重跑：机构+医师+资源+日期+时段 查重，全部跳过、总量不变
    again = client.post("/api/appointments/slots/batch", json=body, headers=admin)
    assert again.status_code == 201
    assert again.json() == {"created": 0, "skipped": 8}
    assert _slot_count(org_and_doctor["org_id"]) == 8

    # 扩区间补生成：已有日期跳过，只补新日期（开办期常见"补几天"）
    body2 = dict(body, date_to="2026-09-14")  # 多出周一 09-14
    patch = client.post("/api/appointments/slots/batch", json=body2, headers=admin)
    assert patch.json() == {"created": 2, "skipped": 8}
    assert _slot_count(org_and_doctor["org_id"]) == 10


def test_batch_limit_protection(client, admin, org_and_doctor):
    body = {
        "org_id": org_and_doctor["org_id"],
        "templates": [
            {"resource_type": "outpatient", "resource_name": f"模板{i}", "slot_time": f"0{i}:00"}
            for i in range(4)
        ],
        "date_from": "2026-01-01",
        "date_to": "2026-12-31",  # 365 天 × 4 模板 = 1460 > 1000
    }
    resp = client.post("/api/appointments/slots/batch", json=body, headers=admin)
    assert resp.status_code == 422
    assert "上限" in resp.json()["detail"]


def test_batch_validation_errors(client, admin, org_and_doctor):
    base = {
        "org_id": org_and_doctor["org_id"],
        "templates": [{"resource_type": "outpatient", "resource_name": "内科"}],
        "date_from": "2026-09-02",
        "date_to": "2026-09-01",
    }
    # 起止倒置
    resp = client.post("/api/appointments/slots/batch", json=base, headers=admin)
    assert resp.status_code == 422 and "date_from" in resp.json()["detail"]
    # 医师不属于该机构
    cross = dict(base, date_to="2026-09-03", templates=[
        {"resource_type": "outpatient", "resource_name": "内科",
         "employee_id": org_and_doctor["outsider_id"]}
    ])
    resp = client.post("/api/appointments/slots/batch", json=cross, headers=admin)
    assert resp.status_code == 422 and "不属于该机构" in resp.json()["detail"]
    # 机构不存在
    ghost = dict(base, org_id=999999, date_to="2026-09-03")
    assert client.post("/api/appointments/slots/batch", json=ghost, headers=admin).status_code == 404


def test_batch_requires_admin(client, org_and_doctor):
    resp = client.post(
        "/api/appointments/slots/batch",
        json={"org_id": org_and_doctor["org_id"], "templates": [
            {"resource_type": "outpatient", "resource_name": "内科"}],
            "date_from": "2026-09-01", "date_to": "2026-09-01"},
    )
    assert resp.status_code in (401, 403)
