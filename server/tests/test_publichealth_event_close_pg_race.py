"""公卫事件记处置动作与结案并发的真 PostgreSQL 取证（P2-464；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_publichealth_event_close_pg_race.py -q

原先两处都是锁外判状态：记动作的一路读到「处置中」、还没插，结案的一路照样改状态提交（A 只读过事件那一行，
没有任何锁挡着 B），A 随后插入——动作落在已结案的事件上。修后两处都 `serialized_on`（PG 上 `SELECT … FOR UPDATE`），
B 的锁要等 A 提交才拿得到。时序与 SQLite 那条相同（`test_publichealth_event_close_race.py`）：A 往处置动作表发
INSERT 之前起 B 路结案、等它最多两秒，再另开一个会话读事件此刻的状态。

**这套库是多人共用的**：只用自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import event as sa_event
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from app.models import PhEventAction, PublicHealthEvent
from app.routers.publichealth import ActionCreate, add_action, close_event

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。"""
    engine = create_engine(PG_URL, pool_size=4, max_overflow=4)
    if not sa_inspect(engine).has_table("ph_event_actions"):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture()
def event_id(pg_engine):
    """一起处置中的事件；跑完把它和它的处置动作收拾掉（先子后父）。"""
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    with Session() as db:
        row = PublicHealthEvent(title=f"PG聚集性疫情{uuid.uuid4().hex[:8]}", level="III", disease_name="诺如病毒")
        db.add(row)
        db.commit()
        eid = row.id

    yield eid

    with Session() as db:
        db.query(PhEventAction).filter_by(event_id=eid).delete()
        db.query(PublicHealthEvent).filter_by(id=eid).delete()
        db.commit()


def test_记处置动作时结案并发_动作不落在已结案的事件上(pg_engine, event_id):
    Session = sessionmaker(bind=pg_engine, autoflush=False)
    other: dict = {}
    fired: list = []

    def run_close():
        with Session() as db:
            try:
                close_event(event_id, db=db)
                other["code"] = 200
            except HTTPException as exc:
                other["code"] = exc.status_code

    def status_now():
        with Session() as db:
            return db.get(PublicHealthEvent, event_id).status

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("INSERT INTO PH_EVENT_ACTIONS"):
            fired.append(True)   # 先记上：下面另开会话读状态也会经过这里
            thread = threading.Thread(target=run_close)
            thread.start()
            thread.join(timeout=2)   # 修前 B 不受任何锁阻挡、很快提交；修后等 A 的行锁
            fired.append(status_now())   # 普通 SELECT 不等行锁：读的是此刻已提交的状态
            fired.append(thread)

    sa_event.listen(pg_engine, "before_cursor_execute", listener)
    try:
        with Session() as db:
            add_action(event_id, ActionCreate(action="封控教学楼、开展流调", actor="流调组"), db=db)
    finally:
        sa_event.remove(pg_engine, "before_cursor_execute", listener)
    fired[2].join(timeout=30)

    assert fired[1] == "active", "修前：结案在判定与插入之间提交，这条动作落在已结案的事件上"
    assert other["code"] == 200   # 结案排在这条动作整个提交之后
    with Session() as db:
        assert db.get(PublicHealthEvent, event_id).status == "closed"
        assert [a.action for a in db.query(PhEventAction).filter_by(event_id=event_id)] == ["封控教学楼、开展流调"]
