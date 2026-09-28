"""报告推送的收件人去重、只发在用的账号（第十六批「通知收件人」扫描 T2-6）。

推送任务的订阅人名单写成 [甲, 甲, 甲]，建任务照收（校验只拿去重后的名单查存在与停用），到点推送逐个投递——甲收三条
一模一样的「报告已生成」；订阅之后才停用的账号照发，消息落进登不上的收件箱。平台群发（`notify_staff`）早就只投在用
的账号（P2-349），这里同一口径。名单本身照存原样：报告实例的「我的」按它筛。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "T2-6 报告院", "org_type": "township", "level": "township"}).json()["id"]
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "T26_TPL", "name": "T2-6 日报", "sections": [{"type": "kpi", "title": "在管"}]})
    assert tpl.status_code == 201, tpl.text
    users = {}
    for name in ("keep", "later_off"):
        r = client.post("/api/users", headers=admin, json={
            "username": f"t26_{name}", "password": "Passw0rd!x", "role": "director", "org_id": org})
        assert r.status_code == 201, r.text
        users[name] = r.json()["id"]
    return {"org": org, "template": tpl.json()["id"], **users}


def test_订阅人写重了只收一条_订阅后停用的不再发(client, admin, world):
    from app.database import SessionLocal
    from app.models import Notification
    from app.spd.models import SpdReportInstance

    keep, off = world["keep"], world["later_off"]
    task = client.post(f"{B}/report-tasks", headers=admin, json={
        "template_id": world["template"], "name": "T2-6 推送", "push_time": "00:00",
        "subscriber_ids": [keep, keep, keep, off]})
    assert task.status_code == 201, task.text
    assert client.patch(f"/api/users/{off}/status", headers=admin, json={"status": "disabled"}).status_code == 200

    run = client.post("/api/jobs/spd_report_push/run", headers=admin)
    assert run.status_code == 201 and run.json()["status"] == "succeeded", run.text[:300]
    with SessionLocal() as db:
        (instance,) = db.query(SpdReportInstance).filter(SpdReportInstance.task_id == task.json()["id"]).all()
        got = sorted(u for (u,) in db.query(Notification.user_id).filter(
            Notification.link_type == "spd_report_instance", Notification.link_id == instance.id))
        assert got == [keep]                                           # 修前 [keep, keep, keep, off]
        assert instance.subscriber_ids == [keep, keep, keep, off]      # 名单照存原样，只是投递时去重、跳过停用的
