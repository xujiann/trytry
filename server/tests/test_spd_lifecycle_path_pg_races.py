"""慢专病档案生命周期与路径实例调整的真并发取证（P2-344 / P2-343，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_lifecycle_path_pg_races.py -q

两处原先都是「锁外判状态 → 往对象上赋值」，UPDATE 只有 `WHERE id = ?`：

- 登记死亡与恢复管理同时到：恢复那一路读到「没死」，照旧把状态写回在管——死亡（终态，P1-111）被盖掉，死者照样派任务；
- 同一条路径几路同时取消：几路都读到「未结束」、都 200，结束时间被挪来挪去；暂停与取消交错，已取消的被写回暂停、还能「恢复」。

修后两处都走条件翻转（`concurrency.move_row`：档案 `WHERE status != 'dead'`、实例 `WHERE status IN (执行中, 暂停)`），行锁让后到
的一路等前一路提交、再按新状态重判。SQLite 一侧用确定时序钉逻辑（`test_spd_lifecycle_death_race.py`、`test_spd_path_adjust_race.py`）。

这里钉 PG 上真并发的不变量：**死亡恰一路登记成、终态是死亡；取消恰一路成、终态是已取消**。

**这套库是多人共用的**：只用带随机后缀的自建数据，跑完自己收拾，从不 DROP SCHEMA；空库就地补跑迁移（只做加法）。
"""
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker

from test_disease_enrollment_unique_races import _warm_pool  # 连接池热身，理由见 test_cost_allocation_races
from test_postgres_real import _race_on_pg  # Barrier 真并发的既有夹具，不另造一份

from app.models import Organization, Patient, User
from app.spd.models import (SpdEnrollment, SpdLifecycleEvent, SpdPathInstance, SpdPathNode, SpdPathTemplate,
                            SpdProgram, SpdTask)
from app.spd.routers.population import LifecycleIn, lifecycle_event
from app.spd.routers.tasks import InstanceAdjustIn, adjust_path_instance

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("spd_enrollments", "spd_path_instances")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    _warm_pool(engine, RACERS)
    yield engine
    engine.dispose()


def _sessions(pg_engine):
    """与 `app.database.SessionLocal` 同一个配置（autoflush 关）。"""
    return sessionmaker(bind=pg_engine, autoflush=False)


@pytest.fixture(scope="module")
def world(pg_engine):
    """机构、患者、全域管理员（过机构写权限）、一个自建病种与一套两节点的路径模板；跑完把自己造的行全收拾掉。"""
    Session = _sessions(pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG生命周期院{tag}", org_type="township", level="township")
        patient = Patient(name=f"生命周期并发患者{tag}", id_card=f"3318{uuid.uuid4().int % 10**14:014d}",
                          ehc_no=f"PG-LC-{tag}")
        program = SpdProgram(code=f"PGLC{tag}", name=f"并发病种{tag}")
        db.add_all([org, patient, program])
        db.flush()
        admin = User(username=f"pg_lc_admin_{tag}", password_hash="x", full_name="全域管理员", role="admin")
        template = SpdPathTemplate(program_id=program.id, code=f"PGLC{tag}", name="并发路径", status="published")
        db.add_all([admin, template])
        db.flush()
        db.add_all([SpdPathNode(template_id=template.id, key="a", name="首诊", next_key="b"),
                    SpdPathNode(template_id=template.id, key="b", name="复诊")])
        db.commit()
        ids = {"tag": tag, "org": org.id, "patient": patient.id, "program": program.id, "program_code": program.code,
               "admin": admin.id, "template": template.id, "enrollments": [], "instances": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        db.query(SpdTask).filter(SpdTask.instance_id.in_(ids["instances"] or [0])).delete(synchronize_session=False)
        db.query(SpdPathInstance).filter(SpdPathInstance.id.in_(ids["instances"] or [0])).delete(synchronize_session=False)
        db.query(SpdLifecycleEvent).filter(
            SpdLifecycleEvent.enrollment_id.in_(ids["enrollments"] or [0])).delete(synchronize_session=False)
        db.query(SpdTask).filter(SpdTask.enrollment_id.in_(ids["enrollments"] or [0])).delete(synchronize_session=False)
        db.query(SpdEnrollment).filter(SpdEnrollment.id.in_(ids["enrollments"] or [0])).delete(synchronize_session=False)
        db.query(SpdPathNode).filter_by(template_id=ids["template"]).delete()
        db.query(SpdPathTemplate).filter_by(id=ids["template"]).delete()
        db.query(User).filter_by(id=ids["admin"]).delete()
        db.query(SpdProgram).filter_by(id=ids["program"]).delete()
        db.query(Patient).filter_by(id=ids["patient"]).delete()
        db.query(Organization).filter_by(id=ids["org"]).delete()
        db.commit()


def _enrollment(Session, world, status):
    with Session() as db:
        row = SpdEnrollment(patient_id=world["patient"], program_code=world["program_code"], org_id=world["org"],
                            status=status)
        db.add(row)
        db.commit()
        world["enrollments"].append(row.id)
        return row.id


def _race(Session, world, act):
    """八路同时对同一行走一步；每路回 (序号, "ok") 或 (序号, 状态码, 说明)。"""
    def worker(index):
        with Session() as db:
            try:
                act(db, db.get(User, world["admin"]), index)
                return (index, "ok")
            except HTTPException as exc:
                return (index, exc.status_code, exc.detail)

    results, errors = _race_on_pg(worker, RACERS)
    assert not errors, errors
    assert len(results) == RACERS, results
    assert all(r[1] == "ok" or r[1] == 409 for r in results), results
    return results


def test_登记死亡与恢复管理并发_死亡恰一路登记成_终态是死亡(pg_engine, world):
    Session = _sessions(pg_engine)
    eid = _enrollment(Session, world, "excluded")

    def act(db, user, i):
        event = "death" if i % 2 else "resume"
        lifecycle_event(eid, LifecycleIn(event=event, reason="并发取证"), db=db, user=user)

    results = _race(Session, world, act)
    deaths = [r for r in results if r[0] % 2 and r[1] == "ok"]
    assert len(deaths) == 1, results   # 先到的那次死亡之后，其余几次都按「已登记死亡」409
    with Session() as db:
        assert db.get(SpdEnrollment, eid).status == "dead", results   # 修前：恢复那一路把死者写回在管
        assert db.query(SpdLifecycleEvent).filter_by(enrollment_id=eid, event="death").count() == 1


def test_路径几路同时取消与暂停_取消恰一路成_终态是已取消(pg_engine, world):
    Session = _sessions(pg_engine)
    eid = _enrollment(Session, world, "active")
    with Session() as db:
        instance = SpdPathInstance(enrollment_id=eid, template_id=world["template"], template_code=world["program_code"],
                                   current_node_key="a", status="running")
        db.add(instance)
        db.commit()
        iid = instance.id
        world["instances"].append(iid)

    def act(db, user, i):
        adjust_path_instance(iid, InstanceAdjustIn(status="cancelled" if i % 2 else "paused"), db=db, user=user)

    results = _race(Session, world, act)
    cancels = [r for r in results if r[0] % 2 and r[1] == "ok"]
    assert len(cancels) == 1, results   # 修前：几路取消都读到「未结束」、都 200
    with Session() as db:
        row = db.get(SpdPathInstance, iid)
        assert row.status == "cancelled" and row.finished_at is not None, results   # 修前：已取消的被写回暂停
