"""给工作人员投站内信把停用的账号也算进收件人，且按编号取前 200 个：老账号停用得多，新来的医师收不到危急值（P2-349）。

`notify.notify_staff` 按机构 + 角色取收件人，`order_by(User.id).limit(MAX_RECIPIENTS)`，不看账号状态。停用的账号登录不了，
消息投给它们等于没投；更糟的是它们占着上限——一家机构（含停用的）超过 200 个医师账号时，编号靠后的在用医师收不到。
两处调用都是危急值（出具与修订）。修法：只投在用的账号。
"""
from app.database import SessionLocal


def _org(client, admin, name):
    got = client.post("/api/organizations", headers=admin, json={
        "name": f"P2349 {name}", "org_type": "township", "level": "township"})
    assert got.status_code == 201, got.text
    return got.json()["id"]


def _doctor(db, username, org, status):
    from app.models import User

    user = User(username=username, password_hash="x", full_name=username, role="doctor", org_id=org, status=status)
    db.add(user)
    db.flush()
    return user.id


def _recipients(org):
    from app.models import Notification
    from app.notify import notify_staff

    title = f"P2349 危急值 {org}"
    with SessionLocal() as db:
        notify_staff(db, category="critical_value", title=title, org_id=org, roles=("doctor",))
        db.commit()
        return {n.user_id for n in db.query(Notification).filter(Notification.title == title).all()}


def test_停用的账号不收_在用的照收(client, admin):
    org = _org(client, admin, "停用账号卫生院")
    with SessionLocal() as db:
        _doctor(db, "p2349_off", org, "disabled")
        active = _doctor(db, "p2349_on", org, "active")
        db.commit()
    assert _recipients(org) == {active}   # 修前停用的也收


def test_停用的老账号不再占满上限(client, admin):
    from app.notify import MAX_RECIPIENTS

    org = _org(client, admin, "老账号卫生院")
    with SessionLocal() as db:
        for i in range(MAX_RECIPIENTS):
            _doctor(db, f"p2349_old{i}", org, "disabled")
        newest = _doctor(db, "p2349_newest", org, "active")
        db.commit()
    assert _recipients(org) == {newest}   # 修前前 200 个全是停用的，新来的在用医师收不到
