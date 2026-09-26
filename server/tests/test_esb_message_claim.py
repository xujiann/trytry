"""集成平台的消息「抢占」其实没抢：同一条消息被两处同时消费，投两次（P2-405）。

模块开头写着「多实例部署时消费端以 status=processing 抢占，避免重复消费」，可手工消费、定时出站、编排执行三条路径都是
锁外读了状态、再无条件写「处理中」：经办点「消费 / 重试」的同时定时出站正投着这一条、两位经办同时点、编排执行与手工
消费撞在一起，几路都读到「待处理」、都往下走。调度器的任务锁只挡得住两轮定时出站互相重叠。修后「转处理中」与判定
压进同一条 UPDATE：手工两路比「状态与重试次数还是读到的那样」，定时出站比选批次的同一个条件；抢输的手工一路 409、
定时出站跳过这一条。

时序：在处理函数往消息表发第一条 UPDATE 之前（引擎的 before_cursor_execute）插进另一路的提交——读到的是旧状态、
写之前别人已提交，正是缺陷的窗口（SQLite 上读的游标还开着时别的连接写不进去，所以插在写之前、不插在读之后）。
真 PG 上八路真并发见 `test_esb_message_claim_pg_races.py`。
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import EsbFlowRun, EsbMessage
from app.routers import esb as esb_module
from test_esb_outbound import FakeHttpx, enqueue, register_endpoint


@pytest.fixture()
def fake(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    return fake


def _commit(message_id, **values):
    with SessionLocal() as other:
        row = other.get(EsbMessage, message_id)
        for key, value in values.items():
            setattr(row, key, value)
        other.commit()


@contextmanager
def _before_claiming(message_id, **values):
    """处理函数往消息表发第一条 UPDATE 之前，另一路把 values 写进这条消息并提交（只插一次）。"""
    fired = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("UPDATE ESB_MESSAGES"):
            fired.append(True)   # 先记上：下面另一路自己的 UPDATE 也会经过这里
            _commit(message_id, **values)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _row(message_id):
    with SessionLocal() as db:
        row = db.get(EsbMessage, message_id)
        return row.status, row.retry_count


def _calls_to(fake, url):
    return [c for c in fake.calls if c["url"] == url]


def test_手工消费途中这一条已被别处投完_409_不再投第二次(client, admin, fake):
    url = "https://province.example/p2405a"
    ep = register_endpoint(client, admin, "P2405_OUT_A", direction="outbound", endpoint_url=url)
    msg = enqueue(client, ep, "notice", {"text": "只该投一次"})
    with _before_claiming(msg["id"], status="succeeded") as fired:
        got = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert fired
    assert got.status_code == 409 and "刚被别处消费" in got.json()["detail"], got.text   # 修前 200，又投了一次
    assert _calls_to(fake, url) == []
    assert _row(msg["id"]) == ("succeeded", 0)


def test_手工重试途中别处又重试失败一次_409_不跟着再投(client, admin, fake):
    url = "https://province.example/p2405b"
    ep = register_endpoint(client, admin, "P2405_OUT_B", direction="outbound", endpoint_url=url)
    msg = enqueue(client, ep, "notice", {"text": "失败待重试"}, max_retries=5)
    fake.status_code = 502
    client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert _row(msg["id"]) == ("failed", 1)
    fake.status_code = 200
    before = len(_calls_to(fake, url))
    # 状态仍是「失败待重试」，只是次数变了：只比状态的话这一路照样会再投
    with _before_claiming(msg["id"], retry_count=2) as fired:
        got = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert fired
    assert got.status_code == 409, got.text   # 修前 200
    assert len(_calls_to(fake, url)) == before
    assert _row(msg["id"]) == ("failed", 2)


def test_定时出站选出之后这一条已被别处投完_跳过_后面的照投(client, admin, fake):
    url = "https://province.example/p2405c"
    ep = register_endpoint(client, admin, "P2405_OUT_C", direction="outbound", endpoint_url=url)
    first = enqueue(client, ep, "notice", {"text": "被经办先投了"})
    second = enqueue(client, ep, "notice", {"text": "排在后面"})
    with _before_claiming(first["id"], status="succeeded") as fired:
        with SessionLocal() as db:
            esb_module.consume_pending_outbound(db)
    assert fired
    delivered = [c["content"].decode("utf-8") for c in _calls_to(fake, url)]
    assert len(delivered) == 1 and "排在后面" in delivered[0], delivered   # 修前两条都投，先那条投了第二次
    assert _row(first["id"]) == ("succeeded", 0) and _row(second["id"]) == ("succeeded", 0)


def test_编排执行途中消息已被消费_409_不记执行记录(client, admin):
    ep = register_endpoint(client, admin, "P2405_IN", system_type="his")
    flow = client.post("/api/esb/flows", headers=admin, json={
        "code": "P2405_FLOW", "name": "P2405 透传校验", "steps": [{"type": "validate", "config": {"required": ["k"]}}]})
    assert flow.status_code == 201, flow.text
    msg = enqueue(client, ep, "notice", {"k": "v"})
    with _before_claiming(msg["id"], status="succeeded") as fired:
        got = client.post(f"/api/esb/flows/P2405_FLOW/run?message_id={msg['id']}", headers=admin)
    assert fired
    assert got.status_code == 409, got.text   # 修前 200，这条消息又被编排走了一遍
    with SessionLocal() as db:
        assert db.query(EsbFlowRun).filter_by(message_id=msg["id"]).count() == 0
    assert _row(msg["id"]) == ("succeeded", 0)


def test_不并发时手工消费与编排照常(client, admin, fake):
    url = "https://province.example/p2405d"
    ep = register_endpoint(client, admin, "P2405_OUT_D", direction="outbound", endpoint_url=url)
    msg = enqueue(client, ep, "notice", {"text": "正常一条"})
    got = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert got.status_code == 200 and got.json()["status"] == "succeeded", got.text
    assert len(_calls_to(fake, url)) == 1
    inbound = register_endpoint(client, admin, "P2405_IN2", system_type="his")
    other = enqueue(client, inbound, "notice", {"k": "v"})
    run = client.post(f"/api/esb/flows/P2405_FLOW/run?message_id={other['id']}", headers=admin)
    assert run.status_code == 200 and run.json()["message_status"] == "succeeded", run.text
