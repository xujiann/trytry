"""绿道节点的时序校验被并发补记穿过去：库里成了「先救治后发病」（P2-419）。

记节点要与本例已记的节点比时序（不得「先救治后发病」，L-12）。判定读的是**别的**节点、写的是一条 INSERT——
INSERT 不给任何既有行加锁，唯一约束 `(case_id, milestone)` 也只挡同一节点记两次：两人同时补记「发病 10:00」与
「开始救治 09:00」，各读到对方还没记、都判「不矛盾」，两路都 201，时效分析（到院→救治等）算出负数。修后判定与
写入（含提交）圈在这例急救事件那一行的临界区里（`serialized_on`）。

时序：A 路往节点表发 INSERT 之前（引擎的 before_cursor_execute）起 B 路、等它最多两秒——修前 B 读到 A 还没记、
先提交，A 随后照插；修后 B 卡在这例事件的临界区外，等 A 提交出块才读到「发病 10:00」，422。
真 PG 八路并发见 `test_emergency_milestone_pg_races.py`。
"""
import threading

import pytest
from fastapi import HTTPException
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import EmergencyMilestone
from app.routers.emergency import MilestoneCreate, record_milestone


@pytest.fixture()
def case_id(client, admin):
    resp = client.post("/api/emergency/cases", headers=admin,
                       json={"location": "P2419 事发地", "symptom": "胸痛", "channel_type": "chest_pain"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _record(case_id, milestone, occurred_at):
    with SessionLocal() as db:
        try:
            record_milestone(case_id, MilestoneCreate(milestone=milestone, occurred_at=occurred_at), db=db)
            return 201
        except HTTPException as exc:
            return exc.status_code


def _recorded(case_id):
    with SessionLocal() as db:
        rows = db.query(EmergencyMilestone).filter(EmergencyMilestone.case_id == case_id).all()
        return {row.milestone: row.occurred_at for row in rows}


def test_两人同时补记互相矛盾的节点_后到的422_不成先救治后发病(case_id):
    other: dict = {}
    fired: list = []

    def run_other():
        other["code"] = _record(case_id, "treatment", "2026-09-27 09:00")

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO EMERGENCY_MILESTONES"):
            fired.append(True)   # 先记上：B 路自己的 INSERT 也会经过这里
            thread = threading.Thread(target=run_other)
            thread.start()
            thread.join(timeout=2)   # 修前 B 很快跑完；修后 B 卡在临界区外，等满两秒放 A 往下走
            fired.append(thread)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        mine = _record(case_id, "onset", "2026-09-27 10:00")
    finally:
        event.remove(engine, "before_cursor_execute", listener)
    fired[1].join(timeout=30)

    assert fired and mine == 201
    assert other["code"] == 422, other   # 修前 201：两路都判「不矛盾」
    assert _recorded(case_id) == {"onset": "2026-09-27 10:00"}   # 修前还有「开始救治 09:00」


def test_不矛盾的两个节点照常先后记上(case_id):
    assert _record(case_id, "onset", "2026-09-27 08:00") == 201
    assert _record(case_id, "treatment", "2026-09-27 09:00") == 201
    assert _recorded(case_id) == {"onset": "2026-09-27 08:00", "treatment": "2026-09-27 09:00"}
