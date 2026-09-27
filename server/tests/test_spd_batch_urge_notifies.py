"""批量催办只加计数、不发消息：回执说全部处理了，责任人手上没有任何动静（P2-496，第九批「通知承诺」扫描 W2-3）。

单条 `POST /api/spd/tasks/{id}/urge` 的注释写着「催办：计数 +1 并给责任人发站内消息」；中心端任务页的「批量处理」
选「催办」走 `POST /api/spd/tasks/batch`，那一支只做了 `add_amount(... "urged_count", 1)`。同一个动作，单条催得到人、
批量催不到。

修法：两处共用一个 `_urge` 帮手（原子计数 + 给责任人发消息）；没有责任人的照旧只计数。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2496 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    doctor = client.post("/api/users", headers=admin, json={
        "username": "p2496_doc", "password": "passw0rd1", "full_name": "P2496 责任医生", "role": "doctor",
        "org_id": org})
    assert doctor.status_code in (200, 201), doctor.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2496 居民", "id_card": "330281198806062496"}).json()["id"]
    return {"org": org, "doctor": doctor.json()["id"], "patient": patient}


def _task(client, admin, world, title, assignee):
    resp = client.post(f"{B}/tasks", headers=admin, json={
        "patient_id": world["patient"], "title": title, "task_type": "followup", "org_id": world["org"],
        "assignee_id": assignee})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _urge_notices(world):
    from app.database import SessionLocal
    from app.models import Notification

    with SessionLocal() as db:
        return [(n.link_id, n.body) for n in db.query(Notification).filter(
            Notification.user_id == world["doctor"], Notification.title == "慢专病任务催办").order_by(Notification.id)]


def test_批量催办同样给责任人发消息(client, admin, world):
    first = _task(client, admin, world, "P2496 随访甲", world["doctor"])
    second = _task(client, admin, world, "P2496 随访乙", world["doctor"])
    before = _urge_notices(world)
    resp = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [first, second], "action": "urge"})
    assert resp.status_code == 200 and resp.json() == {"processed": 2, "skipped": []}, resp.text
    assert _urge_notices(world)[len(before):] == [   # 修前一条都没有
        (first, "任务「P2496 随访甲」已被催办（第1次），请尽快处理"),
        (second, "任务「P2496 随访乙」已被催办（第1次），请尽快处理"),
    ]
    # 批量与单条轮流催，计数接着数、消息里的次数跟着走
    single = client.post(f"{B}/tasks/{first}/urge", headers=admin)
    assert single.status_code == 200 and single.json()["urged_count"] == 2, single.text
    again = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [first], "action": "urge"})
    assert again.status_code == 200 and again.json()["processed"] == 1, again.text
    assert client.get(f"{B}/tasks/{first}", headers=admin).json()["urged_count"] == 3
    assert _urge_notices(world)[-1] == (first, "任务「P2496 随访甲」已被催办（第3次），请尽快处理")


def test_没有责任人的只计数(client, admin, world):
    orphan = _task(client, admin, world, "P2496 待接收", None)
    before = _urge_notices(world)
    resp = client.post(f"{B}/tasks/batch", headers=admin, json={"task_ids": [orphan], "action": "urge"})
    assert resp.status_code == 200 and resp.json()["processed"] == 1, resp.text
    assert client.get(f"{B}/tasks/{orphan}", headers=admin).json()["urged_count"] == 1
    assert _urge_notices(world) == before
