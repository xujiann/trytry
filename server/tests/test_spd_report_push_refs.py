"""报告推送任务的机构 / 订阅人编号不经校验：填错一个，定时推送整轮失败、所有报告都不出（P1-124）。

推送任务的 `org_ids` / `subscriber_ids` 是 JSON 列表、库里没有外键；可到点推送（`jobs.spd_report_push`）时
每个机构生成一份报告实例（`org_id` 外键到机构）、给每个订阅人投一条站内消息（`user_id` 外键到账号）。修前
建 / 改任务照单全收，实测：
- 机构填一个不存在的编号 201，推送每一轮撞外键、整轮回滚——同一轮里好好的任务，报告也一份都不出；
- 订阅人填一个不存在的编号 201，失败发生在调度收尾提交（try 之外）：手动运行 500，调度轮次抛出，
  不记失败、不告警、`next_run_at` 不前移（调度一侧的回归在 test_scheduler.py）。

修法：建 / 改任务查机构存在、订阅人存在且在用（与挂人的其余字段同一口径，P1-106），改档时原有的编号不再查；
推送时存量里悬空的机构与订阅人跳过，全悬空的一份也不出（不退回全域）。
"""
import pytest

B = "/api/spd"
MISSING = 987654321


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P124 报告院", "org_type": "township", "level": "township"}).json()["id"]
    tpl = client.post(f"{B}/report-templates", headers=admin, json={
        "code": "P124_TPL", "name": "P124 日报", "sections": [{"type": "kpi", "title": "在管"}]})
    assert tpl.status_code == 201, tpl.text
    users = {}
    for name in ("on", "off"):
        r = client.post("/api/users", headers=admin, json={
            "username": f"p124_{name}", "password": "Passw0rd!x", "role": "director", "org_id": org})
        assert r.status_code == 201, r.text
        users[name] = r.json()["id"]
    r = client.patch(f"/api/users/{users['off']}/status", headers=admin, json={"status": "disabled"})
    assert r.status_code == 200, r.text
    return {"org": org, "template": tpl.json()["id"], **users}


def _task(client, admin, world, name, **extra):
    return client.post(f"{B}/report-tasks", headers=admin, json={
        "template_id": world["template"], "name": name, "push_time": "00:00", **extra})


@pytest.mark.parametrize(("extra", "detail"), [
    (lambda w: {"org_ids": [w["org"], MISSING]}, f"机构不存在：{MISSING}"),
    (lambda w: {"subscriber_ids": [MISSING]}, f"订阅人不存在（user_id={MISSING}）"),
    (lambda w: {"subscriber_ids": [w["on"], w["off"]]}, None),
], ids=["机构不存在", "订阅人不存在", "订阅人已停用"])
def test_建推送任务_机构或订阅人不对_404点名(client, admin, world, extra, detail):
    resp = _task(client, admin, world, "P124 坏任务", **extra(world))
    expected = detail or f"订阅人已停用（user_id={world['off']}）"
    assert resp.status_code == 404 and resp.json() == {"detail": expected}, resp.text[:300]


def test_合法的照收(client, admin, world):
    resp = _task(client, admin, world, "P124 好任务", org_ids=[world["org"]], subscriber_ids=[world["on"]])
    assert resp.status_code == 201, resp.text


def test_改推送任务_新加的编号照查_原有的不再查(client, admin, world):
    """存量任务里已经悬空的机构、后来停用的订阅人，不该挡住改推送时点、暂停这类改动。"""
    from app.database import SessionLocal
    from app.spd.models import SpdReportTask

    with SessionLocal() as db:
        legacy = SpdReportTask(template_id=world["template"], name="P124 存量任务", status="paused",
                               org_ids=[MISSING], subscriber_ids=[world["off"]])
        db.add(legacy)
        db.commit()
        url = f"{B}/report-tasks/{legacy.id}"
    resp = client.patch(url, headers=admin, json={"push_time": "09:00", "org_ids": [MISSING, world["org"]],
                                                 "subscriber_ids": [world["off"], world["on"]]})
    assert resp.status_code == 200, resp.text
    resp = client.patch(url, headers=admin, json={"org_ids": [MISSING, MISSING + 1]})
    assert resp.status_code == 404 and resp.json() == {"detail": f"机构不存在：{MISSING + 1}"}, resp.text


def test_推送时存量悬空的机构与订阅人跳过_不拖垮整轮(client, admin, world):
    """修前：同一轮里只要有一个任务挂着悬空的机构，整轮回滚，所有任务的报告都不出（任务记 failed）。"""
    from app.database import SessionLocal
    from app.models import Notification
    from app.spd.models import SpdReportInstance, SpdReportTask

    good = _task(client, admin, world, "P124 全域任务", subscriber_ids=[world["on"]])
    assert good.status_code == 201, good.text
    with SessionLocal() as db:
        half = SpdReportTask(template_id=world["template"], name="P124 半悬空", push_time="00:00", status="active",
                             org_ids=[MISSING, world["org"]], subscriber_ids=[MISSING, world["on"]])
        dead = SpdReportTask(template_id=world["template"], name="P124 全悬空", push_time="00:00", status="active",
                             org_ids=[MISSING], subscriber_ids=[])
        db.add_all([half, dead])
        db.commit()
        ids = {"good": good.json()["id"], "half": half.id, "dead": dead.id}

    run = client.post("/api/jobs/spd_report_push/run", headers=admin)
    assert run.status_code == 201 and run.json()["status"] == "succeeded", run.text[:300]
    with SessionLocal() as db:
        def instances_of(task_id):
            return db.query(SpdReportInstance).filter(SpdReportInstance.task_id == task_id).all()
        assert [i.org_id for i in instances_of(ids["good"])] == [None]          # 同一轮里好好的任务照常出报告
        assert [i.org_id for i in instances_of(ids["half"])] == [world["org"]]  # 悬空的机构跳过，在的照出
        assert instances_of(ids["dead"]) == []                                  # 全悬空：一份也不出，不退回全域
        mine = [i.id for task in ("good", "half") for i in instances_of(ids[task])]
        notified = db.query(Notification.user_id).filter(Notification.link_type == "spd_report_instance",
                                                         Notification.link_id.in_(mine)).all()
        assert sorted(u for (u,) in notified) == [world["on"], world["on"]]     # 悬空订阅人跳过，在的照发
