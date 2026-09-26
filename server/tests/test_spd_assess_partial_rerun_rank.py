"""考核局部重跑只在重跑的几个对象里排名次：同一期出两个第 1（P2-298）。

`POST /api/spd/scores/run` 带 `object_ids` 时只算这几个对象——这是有意的（补算个别对象）；可名次也只在这几个里排、
写回：重跑的那个记成第 1，没重跑的原第 1 名还是第 1。考核结果直接进绩效，名次表上两个第 1。

修法：写完分数后按本方案本期的全部分数重排名次（并列按对象编号），与重跑了哪几个无关。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
PERIOD = "2031-02"


@pytest.fixture(scope="module")
def world(client, admin):
    indicator = client.post(f"{B}/indicators", headers=admin, json={
        "code": "p2298_count", "name": "P2298 任务数", "data_source": "task", "object_type": "org",
        "target_value": 10, "score_rule": {"type": "ratio", "full": 100, "target": 10}})
    assert indicator.status_code == 201, indicator.text
    plan = client.post(f"{B}/assess-plans", headers=admin, json={
        "code": "p2298_plan", "name": "P2298 方案", "level": "village", "object_type": "org",
        "period_type": "month", "items": [{"indicator_code": "p2298_count", "weight": 100}]})
    assert plan.status_code == 201, plan.text
    orgs = [client.post("/api/organizations", headers=admin, json={
        "name": f"P2298 村{i}", "org_type": "village", "level": "village"}).json()["id"] for i in (1, 2, 3)]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2298 患者", "id_card": "330127197309092298"}).json()["id"]
    return {"plan": plan.json()["id"], "orgs": orgs, "patient": patient}


def _ranks(client, admin, world):
    rows = client.get(f"{B}/scores", params={"plan_id": world["plan"], "period": PERIOD}, headers=admin).json()
    return {r["object_id"]: r["rank"] for r in rows}


def test_局部重跑之后名次仍按全部对象排_不出两个第1(client, admin, world):
    from datetime import datetime

    from app.spd.models import SpdTask

    a, b, c = world["orgs"]
    run = client.post(f"{B}/scores/run", headers=admin,
                      json={"plan_id": world["plan"], "period": PERIOD, "object_ids": [a, b, c]})
    assert run.status_code == 200, run.text
    assert _ranks(client, admin, world) == {a: 1, b: 2, c: 3}   # 都是 0 分，并列按编号

    with SessionLocal() as db:   # 丙村本期多了 5 条任务
        for n in range(5):
            db.add(SpdTask(program_code="", patient_id=world["patient"], task_type="followup", title=f"P2298 {n}",
                           org_id=c, status="done", priority=1, due_date="2031-02-10",
                           created_at=datetime(2031, 2, 10, 9, 0, 0)))
        db.commit()
    rerun = client.post(f"{B}/scores/run", headers=admin,
                        json={"plan_id": world["plan"], "period": PERIOD, "object_ids": [c]})
    assert rerun.status_code == 200, rerun.text
    assert rerun.json()["top"][0]["rank"] == 1
    assert _ranks(client, admin, world) == {c: 1, a: 2, b: 3}   # 修前 {c: 1, a: 1, b: 2}
