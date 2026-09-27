"""没有所属机构的主任点「立即执行」，挡掉当期的全域定时报告（P2-531，第十批「只做一次的承诺」扫描 X1-3）。

P2-255 让定时推送判「当期出过没有」时连机构一起判：本机构账号点「立即执行」出的那份机构是本机构，挡不住全域那份。
可没有所属机构的管理员 / 主任点一下，出的那份机构为空、期间标签不带后缀——与全域推送任务当期要出的那份一模一样，
定时推送照样当它「已出过」：当期不再生成，订阅人收不到（手工那份不投递订阅人）。

修后报告实例记下是不是手工出的（`manual`），定时推送判重只认自己出的。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2531 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P2531_TPL", "name": "P2531 日报", "period": "daily", "sections": [{"key": "summary", "title": "概况"}]})
    assert tpl.status_code == 201, tpl.text
    director = client.post("/api/users", headers=admin, json={
        "username": "p2531_dir", "password": "Passw0rd!x", "role": "director"})   # 不绑机构
    assert director.status_code == 201, director.text
    subscriber = client.post("/api/users", headers=admin, json={
        "username": "p2531_sub", "password": "Passw0rd!x", "role": "doctor", "org_id": org})
    assert subscriber.status_code == 201, subscriber.text
    token = client.post("/api/auth/login", json={"username": "p2531_dir", "password": "Passw0rd!x"}).json()
    task = client.post(f"{B}/report-tasks", headers=admin, json={
        "template_id": tpl.json()["id"], "name": "P2531 全县日报", "push_time": "00:00",
        "subscriber_ids": [subscriber.json()["id"]]})
    assert task.status_code == 201, task.text
    return {"task": task.json()["id"], "subscriber": subscriber.json()["id"],
            "auth": {"Authorization": f"Bearer {token['access_token']}"}}


def _instances(task_id):
    from app.database import SessionLocal
    from app.spd.models import SpdReportInstance

    with SessionLocal() as db:
        return [(i.org_id, i.period_label, i.manual) for i in
                db.query(SpdReportInstance).filter_by(task_id=task_id).order_by(SpdReportInstance.id)]


def _notices(user_id) -> int:
    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        return db.query(Notification).filter(Notification.user_id == user_id,
                                             Notification.category == "spd_report").count()


def test_无机构主任立即执行之后_定时推送照出当期并投递订阅人(client, world):
    from app.database import SessionLocal
    from app.spd.jobs import spd_report_push

    manual = client.post(f"{B}/report-instances", headers=world["auth"], json={"task_id": world["task"]})
    assert manual.status_code == 201, manual.text
    period = manual.json()["period_label"]
    assert _instances(world["task"]) == [(None, period, True)]
    with SessionLocal() as db:
        spd_report_push(db)
        db.commit()   # 调度框架在任务返回后提交，这里直接调函数、自己提交
    # 修前只有手工那一份：定时推送把它当成当期的全域报告，跳过不出、订阅人收不到
    assert _instances(world["task"]) == [(None, period, True), (None, period, False)]
    assert _notices(world["subscriber"]) == 1
    with SessionLocal() as db:   # 同一期再跑一轮不重复出
        spd_report_push(db)
        db.commit()
    assert len(_instances(world["task"])) == 2
    assert _notices(world["subscriber"]) == 1
