"""真 PostgreSQL 上的流程实例跃迁并发（P1-30 · `workflow_transitions`）。

为什么必须在 PG 上跑：这张表的不变式长在父行 `workflow_instances` 上——"一个实例
离开某个节点恰好一次"。SQLite 的**库级写锁**把两路请求排成前后脚，晚到的那一路是在
赢家提交之后才读实例，于是它读到的是**下一个**节点、合法地推进下一步——那是顺序语义，
竞态窗口根本没打开。PG 逐语句取快照：八路可以都读到同一个节点，八路都以为自己该推进，
这才是 `db.get → 判 running → db.add 留痕 → 赋值 current_node → commit` 这套
check-then-act 真正会出事的地方（八条留痕、终态以最后提交者为准）。

闸门是 `workflows._move_instance` 的条件 UPDATE：

    UPDATE workflow_instances SET … WHERE id=? AND status='running' AND current_node=读到的节点

抢输的那几路在 PG 上要等赢家 **commit**，然后按新行版重算 WHERE（EvalPlanQual），
rowcount 为 0 → 回滚 → 按库里现状给 409，留痕一条都不落。

跑法（与 `test_postgres_real.py` 同一开关）：

    export MEDPLAT_PG_TEST_URL=postgresql+psycopg2://postgres@127.0.0.1:55432/medplat_test
    python -m pytest tests/test_workflow_transition_unique_races.py -q

不变量：八路并发**恰一路成功**、其余七路拿 409、`workflow_transitions` 里那个实例
**恰一条**留痕、实例的终态与那条留痕说的是同一件事。

本档**不清库**（这个测试库与他人共用）：只按需把迁移升到 heads，前置数据一律带随机
后缀自建，断言只看自己那张单子。
"""
import os
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

SERVER_DIR = Path(__file__).resolve().parents[1]

#: 节点被别人推走、实例还在流转中——顺序路径下没有对应文案的唯一一句
STALE_MOVE_DETAIL = "当前节点刚被其他人推进，请刷新后重试"

NODES = [
    {"key": "apply", "name": "科室申请", "role": "", "next": "pharmacy"},
    {"key": "pharmacy", "name": "药学审核", "role": "", "next": "approve"},
    {"key": "approve", "name": "院长审批", "role": "", "next": ""},
]

#: 撞上别的进程正在升级/写同一张表时的等待与重试（测试库共用，不独占）。
_RETRY_TIMES = 5
_RETRY_WAIT_SECONDS = 60


def _tables_ready(engine) -> bool:
    from sqlalchemy import inspect

    names = set(inspect(engine).get_table_names())
    return {"workflow_instances", "workflow_transitions", "users"} <= names


@pytest.fixture(scope="module")
def pg_engine():
    """连上测试库，**只在缺表时**把迁移升到 heads（绝不 DROP SCHEMA：库是共用的）。"""
    from sqlalchemy import create_engine

    engine = create_engine(PG_URL, pool_size=12, max_overflow=8)
    for attempt in range(_RETRY_TIMES):
        if _tables_ready(engine):
            break
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_DIR,
            env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 or _tables_ready(engine):
            break
        assert attempt < _RETRY_TIMES - 1, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
        time.sleep(_RETRY_WAIT_SECONDS)  # 多半是别的进程正在升同一个库，等它升完
    assert _tables_ready(engine), "测试库上没有 workflow_* 表——迁移没跑到"
    yield engine
    engine.dispose()


@pytest.fixture(scope="module")
def session_factory(pg_engine):
    from sqlalchemy.orm import sessionmaker

    return sessionmaker(bind=pg_engine)


@pytest.fixture(scope="module")
def actor_id(session_factory):
    """一个自己的操作员（用户名唯一列，共用测试库里必须各建各的）。"""
    from app.models import User

    with session_factory() as db:
        user = User(
            username=f"pg_wf_race_{uuid.uuid4().hex[:8]}",
            password_hash="x", full_name="PG流转并发操作员", role="admin",
        )
        db.add(user)
        db.commit()
        return user.id


@pytest.fixture(scope="module")
def definition_key(session_factory):
    from app.models import WorkflowDefinition

    key = f"pg_wf_race_{uuid.uuid4().hex[:8]}"
    with session_factory() as db:
        db.add(WorkflowDefinition(key=key, name="PG流转并发流程", nodes=NODES))
        db.commit()
    return key


def _new_instance(session_factory, definition_key: str, actor_id: int, node: str = "apply") -> int:
    from app.models import WorkflowInstance

    with session_factory() as db:
        instance = WorkflowInstance(
            definition_key=definition_key, business_type="pg_wf_race", business_id=0,
            title=f"PG流转并发单-{uuid.uuid4().hex[:6]}", current_node=node,
            created_by=actor_id,
        )
        db.add(instance)
        db.commit()
        return instance.id


def _advance(session_factory, instance_id: int, actor_id: int, barrier) -> tuple[int, str | None]:
    """复刻 `routers/workflows.advance_instance` 的写序，返回 (status_code, detail)。

    栅栏卡在**读之后、写之前**：竞态窗口就是这一段（八路都读到同一个节点，才谈得上
    "同一格被推两次"）。只在线程启动处对齐是不够的——`db.get` 本身有先后，
    晚到的那一路会读到赢家推完之后的新节点，那是顺序推进、不是竞态。
    """
    from fastapi import HTTPException

    from app.models import WorkflowInstance, WorkflowTransition
    from app.routers.workflows import _move_instance, _stale_move_409

    with session_factory() as db:
        instance = db.get(WorkflowInstance, instance_id)
        if instance.status != "running":
            return 409, f"当前状态 {instance.status} 不可推进"
        current = next(n for n in NODES if n["key"] == instance.current_node)
        from_node = instance.current_node
        next_key = current.get("next", "")
        values = {"current_node": next_key} if next_key else {"status": "completed"}

        barrier.wait(timeout=30)
        try:
            if not _move_instance(db, instance.id, from_node, **values):
                raise _stale_move_409(db, instance, "推进")
        except HTTPException as exc:
            return exc.status_code, exc.detail
        db.add(WorkflowTransition(
            instance_id=instance.id, from_node=from_node, to_node=next_key,
            action="advance", comment="并发推进", actor_id=actor_id,
        ))
        db.commit()
        return 200, None


def _cancel(session_factory, instance_id: int, actor_id: int, barrier) -> tuple[int, str | None]:
    """复刻 `routers/workflows.cancel_instance` 的写序（栅栏同样卡在读与写之间）。"""
    from fastapi import HTTPException

    from app.models import WorkflowInstance, WorkflowTransition
    from app.routers.workflows import _move_instance, _stale_move_409

    with session_factory() as db:
        instance = db.get(WorkflowInstance, instance_id)
        if instance.status != "running":
            return 409, f"当前状态 {instance.status} 不可终止"
        from_node = instance.current_node

        barrier.wait(timeout=30)
        try:
            if not _move_instance(db, instance.id, from_node, status="cancelled"):
                raise _stale_move_409(db, instance, "终止")
        except HTTPException as exc:
            return exc.status_code, exc.detail
        db.add(WorkflowTransition(
            instance_id=instance.id, from_node=from_node, to_node="",
            action="cancel", comment="并发终止", actor_id=actor_id,
        ))
        db.commit()
        return 200, None


def _race(workers):
    """每个 worker 拿同一个栅栏，自己决定在哪一步对齐。

    等待点全部带 timeout：会阻塞的回归测试不是回归测试。
    """
    results: list = []
    errors: list[BaseException] = []
    lock = threading.Lock()
    barrier = threading.Barrier(len(workers))

    def run(worker):
        try:
            outcome = worker(barrier)
            with lock:
                results.append(outcome)
        except BaseException as exc:  # noqa: BLE001 - 收集断言用
            with lock:
                errors.append(exc)
            barrier.abort()  # 别让其余几路卡死在栅栏上

    threads = [threading.Thread(target=run, args=(w,)) for w in workers]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=90)
    return results, errors


def _transitions(session_factory, instance_id: int):
    from app.models import WorkflowTransition

    with session_factory() as db:
        return [
            (t.from_node, t.to_node, t.action)
            for t in db.query(WorkflowTransition)
            .filter(WorkflowTransition.instance_id == instance_id)
            .order_by(WorkflowTransition.id)
            .all()
        ]


def _instance_state(session_factory, instance_id: int) -> tuple[str, str]:
    from app.models import WorkflowInstance

    with session_factory() as db:
        instance = db.get(WorkflowInstance, instance_id)
        return instance.current_node, instance.status


def test_八路并发推进同一节点恰一路成功其余拿到同一句409(session_factory, definition_key, actor_id):
    """旧写法在这里写出八条 `apply → pharmacy`：留痕上看是同一道审批被批了八次。

    条件 UPDATE 之后恰一路命中；其余七路的状态没被改（实例还在 running），
    只是节点被推走了——那句 409 是本次新增的唯一一句文案。
    """
    iid = _new_instance(session_factory, definition_key, actor_id)
    results, errors = _race([
        lambda barrier: _advance(session_factory, iid, actor_id, barrier) for _ in range(8)
    ])
    assert not errors, f"并发推进不该把异常漏给调用方：{errors}"
    assert len(results) == 8, f"八路都要有结论：{results}"

    assert sorted(code for code, _ in results) == [200] + [409] * 7, f"必须恰一路推进成功：{results}"
    assert {detail for code, detail in results if code == 409} == {STALE_MOVE_DETAIL}, results
    assert _transitions(session_factory, iid) == [("apply", "pharmacy", "advance")], (
        "同一个节点写出了多条流转留痕——审批记录会说这道关被批了不止一次"
    )
    assert _instance_state(session_factory, iid) == ("pharmacy", "running")


def test_并发推进终态节点只完成一次且文案与顺序请求一致(session_factory, definition_key, actor_id):
    """终态那一步的抢输者，状态已被赢家改成 completed——复用顺序请求那句 409。

    这条钉的是"文案兼容"：并发抢输与"本来就晚了一步"对调用方**没有区别**。
    """
    iid = _new_instance(session_factory, definition_key, actor_id, node="approve")
    results, errors = _race([
        lambda barrier: _advance(session_factory, iid, actor_id, barrier) for _ in range(8)
    ])
    assert not errors, f"并发推进不该把异常漏给调用方：{errors}"
    assert sorted(code for code, _ in results) == [200] + [409] * 7, results
    assert {detail for code, detail in results if code == 409} == {"当前状态 completed 不可推进"}, results
    assert _transitions(session_factory, iid) == [("approve", "", "advance")]
    assert _instance_state(session_factory, iid) == ("approve", "completed")


def test_推进与终止并发只有一方成功(session_factory, definition_key, actor_id):
    """推进撞终止：旧写法两条留痕都落库，终态看谁最后提交（终止过的单子会"活过来"）。

    现在两者抢同一个条件 UPDATE，恰一方成功，留痕恰一条，且实例终态与那条留痕
    说的是同一件事。抢输者的文案分两种，都与顺序路径对得上（见断言）。
    """
    iid = _new_instance(session_factory, definition_key, actor_id)
    workers = [
        (lambda barrier: _advance(session_factory, iid, actor_id, barrier)) if i % 2 == 0
        else (lambda barrier: _cancel(session_factory, iid, actor_id, barrier))
        for i in range(8)
    ]
    results, errors = _race(workers)
    assert not errors, f"推进/终止并发不该把异常漏给调用方：{errors}"
    assert sorted(code for code, _ in results) == [200] + [409] * 7, f"只许一方成功：{results}"

    rows = _transitions(session_factory, iid)
    assert len(rows) == 1, f"推进与终止各留一条痕——单子到底走到哪一步就没人说得清了：{rows}"
    (from_node, to_node, action) = rows[0]
    assert from_node == "apply"
    details = {detail for code, detail in results if code == 409}
    if action == "advance":
        assert (to_node, _instance_state(session_factory, iid)) == ("pharmacy", ("pharmacy", "running"))
        # 赢家没改状态、只挪了节点：推进与终止的抢输者都落在这一句上
        assert details == {STALE_MOVE_DETAIL}, results
    else:
        assert (to_node, _instance_state(session_factory, iid)) == ("", ("apply", "cancelled"))
        assert details <= {"当前状态 cancelled 不可推进", "当前状态 cancelled 不可终止"}, results
        assert details, results
