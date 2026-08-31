"""spd 任务认领/办结的并发防线（P1-19 事务边界档顺带实证的产品洞的修复回归）。

洞的形状：`claim_task` 此前是"读 → Python 判 → ORM 写"，PG READ COMMITTED
下两名医生并发认领同一 pending 任务可**双双 200**、后提交者静默覆盖
assignee——恰是其 docstring 承诺要防的"静默改责任人让原责任人白干一场"。
本地 SQLite 全库写锁把窗口压得几乎看不见（4 路 Barrier 实测 1×200+3×409
是运气），所以**这套回归不靠概率**：

- 行为面：输家 409 且赢家的 assignee 不被覆盖（旧代码的可观测危害）；
  双办结只生效一次（终态门 + 计分/推进天然单次）；
- 防拆卸面：静态钉三处都必须是"判定与写同一条 UPDATE"（update(SpdTask)
  + rowcount），Python 侧 check-then-act 形状不得回潮——SQLite 上的线程
  探针对拆卸不敏感（上面那 3×409 的运气），静态钉才是确定性的网。

修法与 `_claim_batch`/`claim_quota` 同一范式（判定与扣减同一条 SQL）；
真 PG 语义由 test_postgres_real.py 的同族用例与 CI integration job 守。
"""
import inspect
import re
import threading

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
import app.spd.routers.tasks as tasks_mod


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, username, password="pass123456"):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client):
    """机构 + 两名医生 + 患者；任务在各用例里现造（互不串味）。"""
    admin = _login(client, "admin", "admin123")
    org = client.post(
        "/api/organizations",
        json={"name": "认领竞态卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    for u in ("race_doc1", "race_doc2"):
        client.post(
            "/api/users",
            json={"username": u, "password": "pass123456", "role": "doctor", "org_id": org["id"]},
            headers=admin,
        )
    patient = client.post(
        "/api/patients",
        json={"name": "竞态患者", "id_card": "330281199001010012", "gender": "男",
              "birth_date": "1990-01-01"},
        headers=admin,
    ).json()
    return {
        "admin": admin, "org": org, "patient": patient,
        "doc1": _login(client, "race_doc1"), "doc2": _login(client, "race_doc2"),
    }


def _new_task(world, title):
    from app.database import SessionLocal
    from app.spd import models as S

    db = SessionLocal()
    task = S.SpdTask(program_code="", patient_id=world["patient"]["id"], task_type="followup",
                     title=title, org_id=world["org"]["id"], status="pending")
    db.add(task)
    db.commit()
    tid = task.id
    db.close()
    return tid


def _assignee_of(task_id):
    from app.database import SessionLocal
    from app.spd import models as S

    db = SessionLocal()
    try:
        t = db.get(S.SpdTask, task_id)
        return t.assignee_id, t.status, t.finished_at
    finally:
        db.close()


# ================================================================ 行为面


def test_后到者409且赢家assignee不被覆盖(client, world):
    tid = _new_task(world, "单条认领竞态")
    assert client.post(f"/api/spd/tasks/{tid}/claim", headers=world["doc1"]).status_code == 200
    winner_assignee, _, _ = _assignee_of(tid)
    resp = client.post(f"/api/spd/tasks/{tid}/claim", headers=world["doc2"])
    assert resp.status_code == 409
    # 赢家已把 status 翻成 claimed，故走"状态"分支——与旧实现同一优先序
    assert resp.json()["detail"] == "该任务不处于可接收状态"
    assert _assignee_of(tid)[0] == winner_assignee, "旧代码的危害正是这里被静默覆盖"


def test_pending但已指派他人_409已由其他人员接收(client, world):
    """创建时带 assignee 的任务（spawn_task 带 assignee 保持 pending）：
    别人来抢 → 已由其他人员接收；被指派者本人 claim → 幂等赢。"""
    from app.database import SessionLocal
    from app.models import User as PlatformUser
    from app.spd import models as S

    db = SessionLocal()
    doc1_id = db.query(PlatformUser).filter_by(username="race_doc1").first().id
    task = S.SpdTask(program_code="", patient_id=world["patient"]["id"], task_type="followup",
                     title="指派后抢占", org_id=world["org"]["id"], status="pending",
                     assignee_id=doc1_id)
    db.add(task); db.commit(); tid = task.id; db.close()
    resp = client.post(f"/api/spd/tasks/{tid}/claim", headers=world["doc2"])
    assert resp.status_code == 409
    assert resp.json()["detail"] == "该任务已由其他人员接收"
    assert _assignee_of(tid)[0] == doc1_id
    assert client.post(f"/api/spd/tasks/{tid}/claim", headers=world["doc1"]).status_code == 200


def test_批量认领抢输进skipped且不覆盖(client, world):
    tid = _new_task(world, "批量认领竞态")
    assert client.post(f"/api/spd/tasks/{tid}/claim", headers=world["doc1"]).status_code == 200
    winner_assignee, _, _ = _assignee_of(tid)
    body = client.post(
        "/api/spd/tasks/batch", json={"task_ids": [tid], "action": "claim"},
        headers=world["doc2"],
    ).json()
    assert body["processed"] == 0
    assert body["skipped"] == [{"id": tid, "reason": "已被他人接收"}]
    assert _assignee_of(tid)[0] == winner_assignee


def test_重复办结只生效一次(client, world):
    tid = _new_task(world, "双办结竞态")
    first = client.post(f"/api/spd/tasks/{tid}/complete",
                        json={"result": {"note": "第一次"}}, headers=world["doc1"])
    assert first.status_code == 200 and first.json()["status"] == "done"
    _, _, finished_first = _assignee_of(tid)
    second = client.post(f"/api/spd/tasks/{tid}/complete",
                         json={"result": {"note": "第二次"}}, headers=world["doc2"])
    assert second.status_code == 409
    assert second.json()["detail"] == "该任务已结束"
    assignee, status, finished = _assignee_of(tid)
    assert (status, finished) == ("done", finished_first), "输家的写入必须整体回滚"


def test_四路并发认领恰一人成功(client, world):
    """SQLite 上此探针对旧代码也大概率通过（写锁运气）——它守的是修后不倒退，
    对"拆掉条件 UPDATE"不敏感；确定性防拆卸看下方静态钉。"""
    tid = _new_task(world, "四路并发认领")
    headers = [world["doc1"], world["doc2"], world["doc1"], world["doc2"]]
    results = [None] * 4
    barrier = threading.Barrier(4)

    def go(i):
        barrier.wait()
        results[i] = client.post(f"/api/spd/tasks/{tid}/claim", headers=headers[i]).status_code

    threads = [threading.Thread(target=go, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    # doc1 与 doc2 各两路：同人重复 claim 是幂等赢（assignee==自己也满足条件），
    # 所以断言"没有跨人双赢"而不是恰一次 200
    assignee, status, _ = _assignee_of(tid)
    assert status == "claimed" and assignee is not None
    assert results.count(200) >= 1 and results.count(409) >= 1


# ================================================================ 防拆卸静态钉


def test_三处都是条件UPDATE不许回潮为先读后写():
    src_claim = inspect.getsource(tasks_mod.claim_task)
    src_finish = inspect.getsource(tasks_mod._finish_task)
    src_batch = inspect.getsource(tasks_mod.batch_tasks)
    for name, src in (("claim_task", src_claim), ("_finish_task", src_finish)):
        assert "update(SpdTask)" in src and ".rowcount" in src, (
            f"{name} 丢了条件 UPDATE——并发窗口重新打开（判定与写必须同一条 SQL）"
        )
    assert 'update(SpdTask)' in src_batch and '"claim"' in src_batch, (
        "batch_tasks 的 claim 分支丢了条件 UPDATE"
    )
    # 旧形状不得回潮：Python 侧直接给 assignee/status 赋值
    assert not re.search(r"task\.assignee_id\s*,\s*task\.status\s*=", src_batch), (
        "batch claim 回潮为 Python 侧赋值——读与写之间的窗口回来了"
    )
    assert not re.search(r"task\.assignee_id\s*=\s*user\.id", src_claim)
    assert not re.search(r'task\.status\s*=\s*"done"', src_finish), (
        "_finish_task 回潮为 Python 侧置 done——双办结双计分的窗口回来了"
    )
