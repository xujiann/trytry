"""随访办结后「下次随访」按当前阶段的管理目标排，不拿别的阶段的周期（P2-150）。

随访周期的说明写「取该病种当前阶段的管理目标配置，没配就按 90 天」；实现复用了判测量值用的 `target_for`，
它的第三级回落是「该病种任一阶段」（配了目标就该用上），还按指标逐个走完三级：
- 排在前面的指标（收缩压）只配在治疗期（30 天），稳定期配的是血脂（180 天）——稳定期患者办完随访，下次随访排在
  30 天后；
- 当前阶段什么都没配的（筛查期），也拿到治疗期的 30 天，而不是说好的 90 天。
"""
from datetime import timedelta

import pytest

from app import clock

B = "/api/spd"
PROGRAM = "p2150_htn"


@pytest.fixture(scope="module")
def world(client, admin):
    program = client.post(f"{B}/programs", headers=admin, json={
        "code": PROGRAM, "name": "P2150 高血压", "category": "chronic"})
    assert program.status_code == 201, program.text
    for body in ({"stage": "treat", "metric": "bp_sys", "target_high": 140, "followup_interval_days": 30},
                 {"stage": "stable", "metric": "ldl", "target_high": 2.6, "followup_interval_days": 180}):
        created = client.post(f"{B}/programs/{program.json()['id']}/targets", headers=admin, json=body)
        assert created.status_code == 201, created.text
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2150 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org}


def _next_followup_after_done(client, admin, world, stage, n):
    from app.database import SessionLocal
    from app.spd.models import SpdEnrollment, SpdTask

    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2150 患者{n}", "id_card": f"33010619710101{n:04d}", "gender": "男"}).json()["id"]
    with SessionLocal() as db:
        enrollment = SpdEnrollment(patient_id=patient, program_code=PROGRAM, org_id=world["org"], status="active",
                                   stage=stage)
        db.add(enrollment)
        db.flush()
        task = SpdTask(patient_id=patient, enrollment_id=enrollment.id, program_code=PROGRAM, org_id=world["org"],
                       title="P2150 随访", task_type="followup", status="pending")
        db.add(task)
        db.commit()
        task_id, enrollment_id = task.id, enrollment.id
    done = client.post(f"{B}/tasks/{task_id}/complete", headers=admin, json={"result": {"note": "已随访"}})
    assert done.status_code == 200, done.text
    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).next_followup_at


def _in(days):
    return (clock.today() + timedelta(days=days)).isoformat()


def test_稳定期按本阶段配的周期排(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "stable", 1) == _in(180)   # 修前 30：治疗期的收缩压周期


def test_治疗期照旧按治疗期的周期(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "treat", 2) == _in(30)


def test_本阶段没配的按90天(client, admin, world):
    assert _next_followup_after_done(client, admin, world, "screening", 3) == _in(90)   # 修前 30
