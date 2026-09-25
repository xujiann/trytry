"""慢专病路径推进的真并发取证（P1-118，真 PostgreSQL；默认跳过）。

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:5432/medplat_test
    python -m pytest tests/test_spd_path_advance_races.py -q

推进路径的两个入口都是「先数别的任务行、再改实例并派下一节点的任务」，判定读的行与写的行不是同一行，
PG 的 READ COMMITTED 下并发事务彼此看不见对方还没提交的写：

- **手工推进**（`advance_instance`）：先数当前节点还有没有未完成任务，没有就推进。两路同时点「推进」都数到 0、
  都从同一节点推进——下一节点的任务派出好几份，还一并报成功；
- **办结自动推进**（`_finish_task`）：办结一条任务后数同节点兄弟任务，没有未完成的就推进。同节点最后两条任务
  同时办结，两路各自看见对方那条还没办完（对方的「办结」尚未提交）——**谁都不推进，路径停在原节点**，
  没有任何报错，要等人发现「怎么一直不往下走」。

SQLite 的库级写锁让两个窗口在开发库上都排成先后，同一份代码永远绿。修法把两个入口都圈进路径实例这一行的
`serialized_on`，本档钉的就是这一点（变异实测见 TECH_DEBT P1-118）。

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
from app.spd.models import (
    SpdEnrollment,
    SpdPathInstance,
    SpdPathNode,
    SpdPathTemplate,
    SpdProgram,
    SpdTask,
)
from app.spd.routers.tasks import SubmitIn, advance_instance, complete_task

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"),
]

RACERS = 8
PAIRS = RACERS // 2   # 办结档：四条路径、每条当前节点两条任务，八路同时各办结一条
ROUNDS = 3            # 办结档停住是概率性的（修前实测一轮四条里停住 0～1 条），跑三轮共十二条路径
SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    """连既有测试库；空库就地补跑迁移，任何情况下都不清 schema（理由同 test_insurance_apply_unique_races）。
    池子按参赛路数开并先热身，否则冷池下测出来的是排队而不是并发（见 test_cost_allocation_races）。"""
    engine = create_engine(PG_URL, pool_size=RACERS, max_overflow=RACERS)
    if not all(sa_inspect(engine).has_table(t) for t in ("spd_path_instances", "spd_tasks")):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR, env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True, text=True,
        )
        assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def world(pg_engine):
    """一家机构、一名全域角色、一个三节点路径模板；各用例自己开患者 / 纳管 / 实例（名字带随机后缀）。"""
    Session = sessionmaker(bind=pg_engine)
    tag = uuid.uuid4().hex[:8]
    with Session() as db:
        org = Organization(name=f"PG路径并发院{tag}", org_type="township", level="township")
        user = User(username=f"pg_path_{tag}", password_hash="x", full_name=f"路径推进员{tag}", role="director")
        program = SpdProgram(code=f"PGADV{tag}", name=f"并发推进病种{tag}")
        db.add_all([org, user, program])
        db.flush()
        template = SpdPathTemplate(program_id=program.id, code=f"PT{tag}", name=f"并发推进路径{tag}")
        db.add(template)
        db.flush()
        db.add_all([
            SpdPathNode(template_id=template.id, key=key, name=f"节点{key}", seq=seq)
            for seq, key in enumerate(("n1", "n2", "n3"), start=1)
        ])
        db.commit()
        ids = {"tag": tag, "org_id": org.id, "user_id": user.id, "program_id": program.id,
               "program_code": program.code, "template_id": template.id, "patient_ids": []}

    yield ids

    with Session() as db:  # 共用库：自己造的行自己收拾（先子后父）
        enrollment_ids = [e.id for e in db.query(SpdEnrollment).filter(SpdEnrollment.patient_id.in_(ids["patient_ids"]))]
        db.query(SpdTask).filter(SpdTask.patient_id.in_(ids["patient_ids"])).delete(synchronize_session=False)
        db.query(SpdPathInstance).filter(SpdPathInstance.enrollment_id.in_(enrollment_ids)).delete(synchronize_session=False)
        db.query(SpdEnrollment).filter(SpdEnrollment.id.in_(enrollment_ids)).delete(synchronize_session=False)
        db.query(Patient).filter(Patient.id.in_(ids["patient_ids"])).delete(synchronize_session=False)
        db.query(SpdPathNode).filter_by(template_id=ids["template_id"]).delete()
        db.query(SpdPathTemplate).filter_by(id=ids["template_id"]).delete()
        db.query(SpdProgram).filter_by(id=ids["program_id"]).delete()
        db.query(User).filter_by(id=ids["user_id"]).delete()
        db.query(Organization).filter_by(id=ids["org_id"]).delete()
        db.commit()


def _new_instance(db, world, open_tasks_on_n1):
    """新开一位患者、纳管、一个停在 n1 的执行中实例，n1 上挂 open_tasks_on_n1 条待办。返回 (实例 id, 任务 id 列表)。"""
    suffix = uuid.uuid4().hex[:10]
    patient = Patient(name=f"路径并发患者{suffix}", id_card=f"3311{uuid.uuid4().int % 10**14:014d}",
                      gender="男", birth_date="1960-06-06", ehc_no=f"PG-PATH-{suffix}")
    db.add(patient)
    db.flush()
    world["patient_ids"].append(patient.id)
    enrollment = SpdEnrollment(patient_id=patient.id, program_code=world["program_code"], org_id=world["org_id"])
    db.add(enrollment)
    db.flush()
    instance = SpdPathInstance(enrollment_id=enrollment.id, template_id=world["template_id"],
                               status="running", current_node_key="n1")
    db.add(instance)
    db.flush()
    tasks = [
        SpdTask(patient_id=patient.id, enrollment_id=enrollment.id, instance_id=instance.id, node_key="n1",
                task_type="path", title=f"n1 任务{i}", org_id=world["org_id"], status="pending")
        for i in range(open_tasks_on_n1)
    ]
    db.add_all(tasks)
    db.commit()
    return instance.id, [t.id for t in tasks]


def _path_state(db, instance_id):
    instance = db.get(SpdPathInstance, instance_id)
    db.refresh(instance)
    by_node: dict[str, int] = {}
    for task in db.query(SpdTask).filter(SpdTask.instance_id == instance_id, SpdTask.status == "pending"):
        by_node[task.node_key] = by_node.get(task.node_key, 0) + 1
    return instance.current_node_key, by_node


def test_并发手工推进_恰一路推进_下一节点只派一份任务(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        instance_id, _ = _new_instance(db, world, open_tasks_on_n1=0)

    def worker(_i):
        with Session() as db:
            user = db.get(User, world["user_id"])
            try:
                out = advance_instance(instance_id, db=db, user=user)
            except HTTPException as exc:
                return ("rejected", exc.status_code, exc.detail)
            return ("ok", out["instance"]["current_node_key"])

    _warm_pool(pg_engine, RACERS)
    results, errors = _race_on_pg(worker, times=RACERS)
    assert not errors, f"异常不该漏给调用方：{errors}"
    ok = [r for r in results if r[0] == "ok"]
    rejected = [r for r in results if r[0] == "rejected"]
    assert len(ok) == 1, f"应恰一路推进，实际 {results}"   # 修前：多路都推进、各派一份
    assert all(code == 409 for _, code, _ in rejected), rejected
    with Session() as db:
        node, pending = _path_state(db, instance_id)
    assert node == "n2" and pending == {"n2": 1}, (node, pending)


def test_同节点两条任务同时办结_路径照样推进不停住(pg_engine, world):
    Session = sessionmaker(bind=pg_engine)
    states = {}
    for _round in range(ROUNDS):
        with Session() as db:
            pairs = [_new_instance(db, world, open_tasks_on_n1=2) for _ in range(PAIRS)]
        jobs = [task_id for _, task_ids in pairs for task_id in task_ids]

        def worker(i, jobs=jobs):
            with Session() as db:
                user = db.get(User, world["user_id"])
                try:
                    complete_task(jobs[i], SubmitIn(), db=db, user=user)
                except HTTPException as exc:
                    return ("rejected", exc.status_code, exc.detail)
                return ("ok", jobs[i])

        _warm_pool(pg_engine, RACERS)
        results, errors = _race_on_pg(worker, times=RACERS)
        assert not errors, f"异常不该漏给调用方：{errors}"
        assert all(r[0] == "ok" for r in results), results   # 八条任务各办各的，谁也不该被拒
        with Session() as db:
            states.update({instance_id: _path_state(db, instance_id) for instance_id, _ in pairs})
    stalled = {k: v for k, v in states.items() if v != ("n2", {"n2": 1})}
    assert not stalled, f"两条都办完了路径却没推进（或派重了）：{stalled}"   # 修前：停在 n1、n1 上零待办
