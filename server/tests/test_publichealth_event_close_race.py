"""公卫事件记处置动作与结案并发：判了「处置中」之后结案先提交，这条动作照样落在已结案的事件上（P2-464）。

`POST /api/publichealth/events/{id}/actions` 原先锁外判「处置中」就插，`/close` 锁外判了就改状态。判完、插入之前
结案先提交，处置动作照样 201——已结案的事件上多出一条结案之后的处置记录；按顺序在结案之后记是 409「事件已结案」。
INSERT 不给事件那一行加锁，判定与写入压不进一条 SQL，修后两处都圈进这起事件那一行的临界区（`serialized_on`），
块内刷新之后再判。

时序（同 `test_emergency_milestone_order_race.py`）：A 路往处置动作表发 INSERT 之前（引擎的 before_cursor_execute）
起 B 路结案、等它最多两秒，然后另开一个会话读事件此刻的状态——修前 B 很快提交，A 要插的这条落在「已结案」上；
修后 B 卡在临界区外，A 插入时事件仍是「处置中」，A 提交出块后 B 才结案。
真 PG 同一时序见 `test_publichealth_event_close_pg_race.py`。
"""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import event as sa_event

from app.database import SessionLocal, engine
from app.models import PhEventAction, PublicHealthEvent
from app.routers.publichealth import ActionCreate, add_action, close_event


@pytest.fixture()
def event_id(client, admin):
    resp = client.post("/api/publichealth/events", headers=admin,
                       json={"title": "P2464 学校聚集性疫情", "level": "III", "disease_name": "诺如病毒"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _add(event_id, text):
    with SessionLocal() as db:
        try:
            add_action(event_id, ActionCreate(action=text, actor="流调组"), db=db)
            return 201
        except HTTPException as exc:
            return exc.status_code


def _close(event_id):
    with SessionLocal() as db:
        try:
            close_event(event_id, db=db)
            return 200
        except HTTPException as exc:
            return exc.status_code


def _status(event_id):
    with SessionLocal() as db:
        return db.get(PublicHealthEvent, event_id).status


def _actions(event_id):
    with SessionLocal() as db:
        return [a.action for a in db.query(PhEventAction).filter(PhEventAction.event_id == event_id)
                .order_by(PhEventAction.id)]


def test_记处置动作时结案并发_动作不落在已结案的事件上(event_id):
    other: dict = {}
    fired: list = []

    def run_close():
        other["code"] = _close(event_id)

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO PH_EVENT_ACTIONS"):
            fired.append(True)   # 先记上：下面另开会话读状态也会经过这里
            thread = threading.Thread(target=run_close)
            thread.start()
            thread.join(timeout=2)   # 修前结案很快提交；修后卡在临界区外，等满两秒放 A 往下插
            fired.append(_status(event_id))   # A 这条动作插进去的那一刻，事件是什么状态
            fired.append(thread)

    sa_event.listen(engine, "before_cursor_execute", listener)
    try:
        mine = _add(event_id, "封控教学楼、开展流调")
    finally:
        sa_event.remove(engine, "before_cursor_execute", listener)
    fired[2].join(timeout=30)

    assert fired and mine == 201
    assert fired[1] == "active", "修前：结案在判定与插入之间提交，这条动作落在已结案的事件上"
    assert other["code"] == 200   # 结案排在这条动作整个提交之后
    assert _status(event_id) == "closed"
    assert _actions(event_id) == ["封控教学楼、开展流调"]


def test_按顺序_结案之后再记动作是409_再结案也是409(event_id):
    assert _add(event_id, "启动 III 级响应") == 201
    assert _close(event_id) == 200
    assert _add(event_id, "结案之后补记") == 409
    assert _close(event_id) == 409
    assert _actions(event_id) == ["启动 III 级响应"]


def test_接口端_结案之后记动作409(client, admin, event_id):
    base = f"/api/publichealth/events/{event_id}"
    assert client.post(f"{base}/actions", headers=admin, json={"action": "派出流调队"}).status_code == 201
    closed = client.post(f"{base}/close", headers=admin)
    assert closed.status_code == 200 and closed.json()["status"] == "closed", closed.text
    late = client.post(f"{base}/actions", headers=admin, json={"action": "结案之后补记"})
    assert late.status_code == 409 and late.json() == {"detail": "事件已结案"}
