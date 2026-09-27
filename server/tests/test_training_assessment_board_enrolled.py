"""实训考核榜只算还在报名的学员（P2-625，第十三批「正向 vs 逆向」扫描 Q1-4）。

退报名只翻报名状态、放回名额，不动考核记录；考核榜原先按本计划全部考核行算人数与合格率。于是：容量 2 的计划，
甲乙报名、录了成绩，乙退报名（名额放回），丙报进来再录一份——榜单 3 人，超过名额；乙在名册上是「已取消」，
合格率里照算。

考过之后还能退报名是既有的业务（不及格的退出本期、下期再报，`test_gapfill` 钉着「退报后名额释放」），不改它；
改的是榜单口径：人数、合格数、合格率只算还在报名的，每行成绩多带 `enrolled` 标明是否计入（退了的成绩照样看得到）。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2625 实训基地", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    users = {}
    for tag in ("甲", "乙", "丙"):
        created = client.post("/api/users", headers=admin, json={
            "username": f"p2625_{tag}", "password": "pw123456", "full_name": f"P2625 学员{tag}",
            "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        users[tag] = created.json()["id"]
    plan = client.post("/api/education/training-plans", headers=admin, json={
        "title": "P2625 急救实训（容量 2）", "capacity": 2, "org_id": org, "plan_date": "2026-10-08"})
    assert plan.status_code == 201, plan.text
    return {"org": org, "plan": plan.json()["id"], **users}


def _call(fn, plan_id, username):
    from app.database import SessionLocal
    from app.models import User

    with SessionLocal() as db:
        return fn(plan_id, db=db, user=db.query(User).filter_by(username=username).one())


def _enroll(world, tag):
    from app.routers.education import enroll_plan

    return _call(enroll_plan, world["plan"], f"p2625_{tag}")


def _cancel(world, tag):
    from app.routers.education import cancel_enroll

    return _call(cancel_enroll, world["plan"], f"p2625_{tag}")


def _assess(client, admin, world, tag, score):
    return client.post(f"/api/education/training-plans/{world['plan']}/assessments", headers=admin,
                       json={"user_id": world[tag], "score": score})


def _board(client, admin, world):
    return client.get(f"/api/education/training-plans/{world['plan']}/assessments", headers=admin).json()


def test_退了报名的成绩不计入榜单_人数不超过名额(client, admin, world):
    _enroll(world, "甲")
    _enroll(world, "乙")
    assert _assess(client, admin, world, "甲", 90).status_code == 201
    assert _assess(client, admin, world, "乙", 95).status_code == 201
    assert _cancel(world, "乙")["status"] == "cancelled"   # 考过还能退：既有业务，照旧
    _enroll(world, "丙")                                    # 名额放回给下一位，照旧
    assert _assess(client, admin, world, "丙", 50).status_code == 201
    board = _board(client, admin, world)
    assert (board["total"], board["passed"], board["pass_rate_pct"]) == (2, 1, 50.0), board   # 修前 3 / 2 / 66.67
    flags = {row["user_id"]: row["enrolled"] for row in board["items"]}
    assert flags == {world["甲"]: True, world["乙"]: False, world["丙"]: True}   # 退了的成绩照样看得到，标明不计入


def test_重新报名的成绩又计入(client, admin, world):
    _cancel(world, "丙")
    _enroll(world, "乙")
    board = _board(client, admin, world)
    assert (board["total"], board["passed"]) == (2, 2), board   # 甲 90、乙 95；丙已退
