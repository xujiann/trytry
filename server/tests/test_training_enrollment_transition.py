"""实训退报名 / 重报名的状态转换不是原子的：连点两次「退报名」名额放两个，连点两次「报名」一人占两个名额（P2-111）。

`cancel_enroll` 原先「内存里判 enrolled → 改 cancelled → 名额计数 -1」；`enroll_plan` 复用退过的那条记录重报时
「内存里判不是 enrolled → 名额计数 +1 → 改 enrolled」。名额计数一侧早是条件 UPDATE（`claim_quota` / `take_amount`，
不会越界），状态一侧没有。PG 的 READ COMMITTED 下同一个人的两次并发请求都读到同一个旧状态、都往下走：

- 连点两次「退报名」：两路都放一个名额——计数少了一个，容量满了还能再报进一个人；
- 退过报名的人连点两次「报名」：两路都占一个名额——一个人占两个名额，别人报不上。

修法与预约（P2-109）同一个写法：状态转换用条件 UPDATE（`WHERE status = 'enrolled'` / `'cancelled'`），影响 0 行即
回滚，文案与顺序重复的请求同一句；重报先转报名、再占额，占额失败连同转回一并回滚。

这里用「一路拿着先读到的对象、另一路先提交」把并发窗口钉成确定的时序（SQLite 与 PG 同样成立）；
PG 上真并发的不变量见 test_training_enrollment_transition_races.py。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2111 实训基地", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    usernames = []
    for i in range(3):
        name = f"p2111_stu{i}"
        created = client.post("/api/users", headers=admin, json={
            "username": name, "password": "pw123456", "full_name": f"P2111 学员{i}",
            "role": "doctor", "org_id": org})
        assert created.status_code in (200, 201), created.text
        usernames.append(name)
    plans = []
    for capacity in (5, 1):
        plan = client.post("/api/education/training-plans", headers=admin, json={
            "title": f"P2111 实训（容量 {capacity}）", "capacity": capacity, "org_id": org, "plan_date": "2026-10-08"})
        assert plan.status_code == 201, plan.text
        plans.append(plan.json()["id"])
    return {"org": org, "users": usernames, "plan": plans[0], "small_plan": plans[1]}


def _user(db, username):
    from app.models import User

    return db.query(User).filter_by(username=username).one()


def _enrolled_count(plan_id):
    from app.database import SessionLocal
    from app.models import TrainingPlan

    with SessionLocal() as db:
        return db.get(TrainingPlan, plan_id).enrolled_count


def _enrollment(db, plan_id, username):
    from app.models import TrainingEnrollment

    return db.query(TrainingEnrollment).filter_by(plan_id=plan_id, user_id=_user(db, username).id).one()


def _status(plan_id, username):
    from app.database import SessionLocal

    with SessionLocal() as db:
        return _enrollment(db, plan_id, username).status


def _call(fn, plan_id, username):
    from app.database import SessionLocal

    with SessionLocal() as db:
        return fn(plan_id, db=db, user=_user(db, username))


def test_连点两次退报名_名额只放一个(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.routers.education import cancel_enroll, enroll_plan

    plan, name = world["plan"], world["users"][0]
    _call(enroll_plan, plan, name)
    before = _enrolled_count(plan)
    with SessionLocal() as racer, SessionLocal() as winner:
        held = _enrollment(racer, plan, name)   # noqa: F841 — 第一路先读到已报名；留着引用，身份映射是弱引用
        cancel_enroll(plan, db=winner, user=_user(winner, name))              # 第二路先退成功
        with pytest.raises(HTTPException) as exc:
            cancel_enroll(plan, db=racer, user=_user(racer, name))            # 第一路拿着读到的已报名接着退
    assert exc.value.status_code == 404   # 修前不报错；与顺序重复退报名同一句
    assert exc.value.detail == "未报名该实训计划"
    assert _enrolled_count(plan) == before - 1   # 修前 -2：名额计数少了一个


def test_退过报名的人连点两次报名_只占一个名额(world):
    from fastapi import HTTPException

    from app.database import SessionLocal
    from app.routers.education import cancel_enroll, enroll_plan

    plan, name = world["plan"], world["users"][1]
    _call(enroll_plan, plan, name)
    _call(cancel_enroll, plan, name)
    before = _enrolled_count(plan)
    with SessionLocal() as racer, SessionLocal() as winner:
        held = _enrollment(racer, plan, name)   # noqa: F841 — 第一路先读到已取消
        enroll_plan(plan, db=winner, user=_user(winner, name))                # 第二路先重报成功
        with pytest.raises(HTTPException) as exc:
            enroll_plan(plan, db=racer, user=_user(racer, name))              # 第一路拿着读到的已取消接着报
    assert exc.value.status_code == 409   # 修前不报错
    assert exc.value.detail == "已报名该实训计划"   # 与顺序重复报名同一句
    assert _enrolled_count(plan) == before + 1   # 修前 +2：一个人占了两个名额
    assert _status(plan, name) == "enrolled"


def test_重报遇名额已满_报名照旧是已取消(world):
    """先转报名、再占额：占额失败要把转回 enrolled 的那一步一并退掉，不能留下一条不占名额的「已报名」。"""
    from fastapi import HTTPException

    from app.routers.education import cancel_enroll, enroll_plan

    plan, first, second = world["small_plan"], world["users"][0], world["users"][2]
    _call(enroll_plan, plan, first)
    _call(cancel_enroll, plan, first)
    _call(enroll_plan, plan, second)   # 名额被别人报走了，容量 1 已满
    with pytest.raises(HTTPException) as exc:
        _call(enroll_plan, plan, first)
    assert exc.value.status_code == 409 and exc.value.detail == "实训名额已满"
    assert _status(plan, first) == "cancelled"
    assert _enrolled_count(plan) == 1
