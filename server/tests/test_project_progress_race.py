"""「已完成⇒进度 100%」被两个并发的改档打破（P2-416）。

P2-58 让结项校验与存量合并后再判，可判的是锁外读到的另一列：一人只改状态结项（读到进度 100）、另一人同时只把
进度改成 60（读到状态「进行中」），两路都 200，库里成了「已完成但进度 60%」——正是 `update_project` 的 docstring
说不允许的那种。修后只改其中一列时，先发一条带条件、值不变的 UPDATE 占住这一行，判过的那一列变了就 409。

时序：处理函数往项目表发第一条 UPDATE 之前（引擎的 before_cursor_execute）插进另一路的提交——读到的是旧值、
写之前别人已提交，正是缺陷的窗口。真 PG 八路并发见 `test_project_progress_pg_races.py`。
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from app.database import SessionLocal, engine
from app.models import AdminProject


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2416 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _project(client, admin, org, name):
    created = client.post("/api/projects", headers=admin, json={"org_id": org, "name": name})
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    assert client.patch(f"/api/projects/{pid}", headers=admin,
                        json={"status": "ongoing", "progress_pct": 100}).status_code == 200
    return pid


@contextmanager
def _before_writing(project_id, **values):
    fired = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("UPDATE ADMIN_PROJECTS"):
            fired.append(True)   # 先记上：下面另一路自己的 UPDATE 也会经过这里
            with SessionLocal() as other:
                row = other.get(AdminProject, project_id)
                for key, value in values.items():
                    setattr(row, key, value)
                other.commit()

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _state(pid):
    with SessionLocal() as db:
        row = db.get(AdminProject, pid)
        return row.status, row.progress_pct


def test_改进度途中别人结项了_409_不成已完成但进度60(client, admin, org):
    pid = _project(client, admin, org, "P2416 改进度撞结项")
    with _before_writing(pid, status="done") as fired:
        got = client.patch(f"/api/projects/{pid}", headers=admin, json={"progress_pct": 60})
    assert fired
    assert got.status_code == 409, got.text   # 修前 200
    assert _state(pid) == ("done", 100)       # 修前 ("done", 60)


def test_结项途中别人把进度改成60_409_不成已完成但进度60(client, admin, org):
    pid = _project(client, admin, org, "P2416 结项撞改进度")
    with _before_writing(pid, progress_pct=60) as fired:
        got = client.patch(f"/api/projects/{pid}", headers=admin, json={"status": "done"})
    assert fired
    assert got.status_code == 409, got.text   # 修前 200
    assert _state(pid) == ("ongoing", 60)


def test_不并发时照常_只改负责人不设条件(client, admin, org):
    pid = _project(client, admin, org, "P2416 照常")
    assert client.patch(f"/api/projects/{pid}", headers=admin, json={"status": "done"}).status_code == 200
    got = client.patch(f"/api/projects/{pid}", headers=admin, json={"owner_name": "新负责人"})
    assert got.status_code == 200 and got.json()["owner_name"] == "新负责人", got.text
    assert _state(pid) == ("done", 100)
