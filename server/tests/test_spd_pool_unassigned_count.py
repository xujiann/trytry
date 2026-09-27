"""中心工作台「待分发」不数已有责任人的目标人群（P2-601，第十二批「计数 vs 清单」扫描 Z1-3）。

「待分发」原先只看有没有团队：团队成员认领的患者（认领只记责任人与认领时间、不记团队）照数，而分发一律跳过已认领的
（P2-251，回执 `skipped_claimed`）——这几条永远挂在「待分发」里、怎么分也分不下去；按责任人分发（不选团队）的同样照数。
"""
import pytest

PROGRAM = "P2601_PG"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdCandidate

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2601 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = [client.post("/api/patients", headers=admin, json={   # 先经接口建好：会话里开着写再调接口会锁库
        "name": f"P2601 患者{n}", "id_card": f"33010619720707260{n}"}).json()["id"] for n in range(3)]
    ids = []
    with SessionLocal() as db:
        for patient in patients:
            candidate = SpdCandidate(patient_id=patient, program_code=PROGRAM, status="target", org_id=org)
            db.add(candidate)
            db.flush()
            ids.append(candidate.id)
        db.commit()
    return ids


def _unassigned(client, admin) -> int:
    resp = client.get("/api/spd/workbench/center", headers=admin)
    assert resp.status_code == 200, resp.text
    return resp.json()["pool"]["unassigned"]


def test_认领了的与按责任人分发了的不算待分发(client, admin, world):
    before = _unassigned(client, admin)
    claimed = client.post(f"/api/spd/candidates/{world[0]}/claim", headers=admin)
    assert claimed.status_code == 200, claimed.text
    assert _unassigned(client, admin) == before - 1   # 修前不变：认领不记团队
    again = client.post("/api/spd/candidates/distribute", headers=admin, json={"candidate_ids": [world[0]],
                                                                                 "assigned_user_id": 1})
    assert again.json()["skipped_claimed"] == [world[0]]   # 分发确实分不下去——这正是它不该算待分发的原因
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        user_id = db.query(User.id).filter_by(username="admin").scalar()
    distributed = client.post("/api/spd/candidates/distribute", headers=admin, json={
        "candidate_ids": [world[1]], "assigned_user_id": user_id})
    assert distributed.json()["distributed"] == 1, distributed.text
    assert _unassigned(client, admin) == before - 2   # 修前不变：按责任人分发不带团队
