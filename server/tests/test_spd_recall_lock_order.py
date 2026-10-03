"""召回成功与登记死亡的加锁次序相反：PG 上互等成死锁，一路 500（P2-1178，第三十四批扫描 L1-13）。

登记死亡（`lifecycle_event`）先条件翻转档案行（行锁），收尾时再把这份档案没结束的召回置为失败（`_end_open_recalls`，召回行的
行锁）；登记召回进度（`update_recall`）原先先锁召回行（`serialized_on(SpdRecall)`），召回成功时再改档案行（`_reactivate`）。两路
同时到，PG 上各握一把、等对方那把，互等成死锁：抛 DeadlockDetected、没人接住，一路 500，登记死亡或召回成功要重来（整笔回滚，
不留半截数据）。`service.close_open_work` 的注释写明「与办结同一个加锁顺序，PG 上不互等」，这一对没跟上。SQLite 只有库级写锁，
复现出来只是约 5 秒后 database is locked——所以这里按代码钉加锁次序。

修法：`update_recall` 先 `serialized_on(SpdEnrollment, 档案编号)` 再锁召回行，与死亡一路同一个加锁顺序；判定、写入、提交与文案不变。

钉法：跑真实的接口，按先后记下每一路第一次去锁档案行、召回行——`serialized_on`（PG 上就是 SELECT … FOR UPDATE）与 UPDATE
（行锁）都算——两路必须同序（修前召回一路是先召回、后档案）；SQLite 上功能照旧。
"""
import pytest
from sqlalchemy import event as sa_event

from app.database import SessionLocal, engine

B = "/api/spd"
ROWS = ("spd_enrollments", "spd_recalls")


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21178 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _recalled(client, admin, world):
    """一份在管档案登记了召回（召回中、召回记录待联系）；返回（档案编号, 召回编号）。"""
    from app.spd.models import SpdRecall

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P21178 患者{world['n']}", "id_card": f"33010619720202{world['n']:04d}"}).json()["id"]
    got = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert got.status_code == 201, got.text
    enrollment = got.json()["id"]
    recalled = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin,
                           json={"event": "recall", "reason": "失访三个月"})
    assert recalled.status_code == 200, recalled.text
    with SessionLocal() as db:
        return enrollment, db.query(SpdRecall.id).filter(SpdRecall.enrollment_id == enrollment).scalar()


def _lock_order(monkeypatch, act):
    """跑一遍 `act()`，按先后返回它第一次去锁档案行、召回行的表名：`serialized_on` 与 UPDATE 都算。"""
    from app.spd.routers import population

    touched: list[str] = []
    real = population.serialized_on

    def recording(db, model, row_id):
        touched.append(model.__tablename__)
        return real(db, model, row_id)

    def listener(conn, cursor, statement, parameters, context, executemany):
        head = statement.lstrip().lower()
        touched.extend(table for table in ROWS if head.startswith(f"update {table} "))

    monkeypatch.setattr(population, "serialized_on", recording)
    sa_event.listen(engine, "before_cursor_execute", listener)
    try:
        act()
    finally:
        sa_event.remove(engine, "before_cursor_execute", listener)
        monkeypatch.undo()
    first: list[str] = []
    for table in touched:
        if table in ROWS and table not in first:
            first.append(table)
    return first


def _status(model, row_id):
    with SessionLocal() as db:
        return db.get(model, row_id).status


def test_召回成功与登记死亡同一个加锁顺序_先档案后召回(client, admin, world, monkeypatch):
    from app.spd.models import SpdEnrollment, SpdRecall

    enrollment, recall = _recalled(client, admin, world)

    def returned():
        resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                           json={"status": "returned", "result": "已重新纳管"})
        assert resp.status_code == 200, resp.text

    recall_order = _lock_order(monkeypatch, returned)
    assert _status(SpdEnrollment, enrollment) == "active" and _status(SpdRecall, recall) == "returned"

    dying, open_recall = _recalled(client, admin, world)

    def death():
        resp = client.post(f"{B}/enrollments/{dying}/lifecycle", headers=admin,
                           json={"event": "death", "reason": "P21178"})
        assert resp.status_code == 200 and resp.json()["closed"]["recalls"] == 1, resp.text

    death_order = _lock_order(monkeypatch, death)
    assert _status(SpdRecall, open_recall) == "failed"

    assert death_order == ["spd_enrollments", "spd_recalls"]
    assert recall_order == death_order   # 修前 ['spd_recalls', 'spd_enrollments']：与死亡一路相反，PG 上互等成死锁


def test_登记召回联系也先锁档案行(client, admin, world, monkeypatch):
    _, recall = _recalled(client, admin, world)

    def contacted():
        resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                           json={"status": "contacted", "contact_note": "电话已接"})
        assert resp.status_code == 200, resp.text

    assert _lock_order(monkeypatch, contacted) == ["spd_enrollments", "spd_recalls"]   # 修前 ['spd_recalls']


def test_没有竞争时召回进度照旧(client, admin, world):
    from app.spd.models import SpdEnrollment, SpdLifecycleEvent

    enrollment, recall = _recalled(client, admin, world)
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin,
                       json={"status": "contacted", "contact_note": "电话已接", "result": "愿意复诊"})
    assert resp.status_code == 200 and resp.json() == {"id": recall, "status": "contacted", "result": "愿意复诊"}
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin, json={"status": "returned"})
    assert resp.status_code == 200 and resp.json() == {"id": recall, "status": "returned", "result": "愿意复诊"}
    assert _status(SpdEnrollment, enrollment) == "active"
    with SessionLocal() as db:
        events = [e.event for e in db.query(SpdLifecycleEvent).filter(SpdLifecycleEvent.enrollment_id == enrollment)
                  .order_by(SpdLifecycleEvent.id)]
    assert events == ["recall", "resume"]
    ended = client.post(f"{B}/recalls/{recall}/progress", headers=admin, json={"status": "failed"})
    assert ended.status_code == 409 and ended.json() == {"detail": "该召回已结束，不能再登记进度；要再召回请重新发起"}

    dying, open_recall = _recalled(client, admin, world)
    with SessionLocal() as db:   # 死者的召回已随死亡收尾（P2-260）；这里直接置死亡，看召回进度那道 409 还在
        db.get(SpdEnrollment, dying).status = "dead"
        db.commit()
    blocked = client.post(f"{B}/recalls/{open_recall}/progress", headers=admin, json={"status": "returned"})
    assert blocked.status_code == 409 and blocked.json() == {"detail": "患者已登记死亡，召回已终止"}
