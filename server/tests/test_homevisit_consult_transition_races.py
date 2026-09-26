"""上门服务工单的派单 / 完成 / 取消、在线咨询的回复 / 结束是锁外读改写（P2-404）。

五处都是「内存里判状态 → 往对象上赋值 → commit」，UPDATE 只有 `WHERE id = ?`：取消在派单途中提交，已取消的工单又被派
出去、带上执行人；完成在取消途中提交，工单成了「已取消」却挂着服务记录与完成时间（顺序做是 409「已完成工单不可取消」）；
咨询已回复并结束，迟到的第二个回复把它翻回「已回复」、整段盖掉第一位医生的答复，两路都 200。修后五处走
`concurrency.move_row`，抢输的回滚、报与顺序请求同一句 409。

时序：上门工单在两者之间必经的机构写权限判定里插进另一路的提交；在线咨询的回复 / 结束中间没有可插的调用，改在它往
咨询表发第一条 UPDATE 之前（引擎的 before_cursor_execute）插进另一路的提交——读到的是旧状态、写之前别人已提交，正是缺陷
的窗口（SQLite 上读的游标还开着时别的连接写不进去，所以插在写之前、不插在读之后）。
"""
from contextlib import contextmanager

import pytest
from sqlalchemy import event

from conftest import login

from app.database import SessionLocal, engine
from app.models import HomeVisitOrder, OnlineConsult


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2404 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    created = client.post("/api/users", headers=admin, json={
        "username": "p2404_doc", "password": "passw0rd1", "role": "doctor", "org_id": org, "full_name": "上门医生"})
    assert created.status_code == 201, created.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2404 患者", "id_card": "330127197309092404"}).json()["id"]
    return {"org": org, "patient": patient, "doctor": login(client, "p2404_doc", "passw0rd1")}


def _commit(model, row_id, **values):
    with SessionLocal() as other:
        row = other.get(model, row_id)
        for key, value in values.items():
            setattr(row, key, value)
        other.commit()


def _visit(client, world):
    got = client.post("/api/homevisits", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "service_type": "nursing", "demand": "P2404 换药"})
    assert got.status_code == 201, got.text
    return got.json()["id"]


def _visit_meanwhile(monkeypatch, visit_id, **values):
    from app.routers import homevisits

    real = homevisits.assert_obj_org_writable

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        _commit(HomeVisitOrder, visit_id, **values)
        return result

    monkeypatch.setattr(homevisits, "assert_obj_org_writable", racing)


@contextmanager
def _before_writing(table, row_id, **values):
    """处理函数往 `table` 发第一条 UPDATE 之前，另一路把 values 写进库并提交（只插一次）。"""
    fired = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith(f"UPDATE {table.upper()}"):
            fired.append(True)   # 先记上：下面另一路自己的 UPDATE 也会经过这里
            _commit(OnlineConsult, row_id, **values)

    event.listen(engine, "before_cursor_execute", listener)
    try:
        yield fired
    finally:
        event.remove(engine, "before_cursor_execute", listener)


def _row(model, row_id, *cols):
    with SessionLocal() as db:
        row = db.get(model, row_id)
        return tuple(getattr(row, c) for c in cols)


def test_派单途中工单被取消_不再派出去(client, world, monkeypatch):
    vid = _visit(client, world)
    _visit_meanwhile(monkeypatch, vid, status="cancelled")
    got = client.post(f"/api/homevisits/{vid}/dispatch", headers=world["doctor"], json={"assignee_name": "护士甲"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200：已取消的工单又派了出去
    assert _row(HomeVisitOrder, vid, "status", "assignee_name") == ("cancelled", "")


def test_取消途中工单被完成_不改成已取消(client, world, monkeypatch):
    vid = _visit(client, world)
    assert client.post(f"/api/homevisits/{vid}/dispatch", headers=world["doctor"],
                       json={"assignee_name": "护士乙"}).status_code == 200
    _visit_meanwhile(monkeypatch, vid, status="completed", service_note="已换药")
    got = client.post(f"/api/homevisits/{vid}/cancel", headers=world["doctor"])
    monkeypatch.undo()
    assert (got.status_code, got.json()["detail"]) == (409, "已完成工单不可取消"), got.text   # 修前 200
    assert _row(HomeVisitOrder, vid, "status") == ("completed",)


def test_完成途中工单被取消_不再记成完成(client, world, monkeypatch):
    vid = _visit(client, world)
    assert client.post(f"/api/homevisits/{vid}/dispatch", headers=world["doctor"],
                       json={"assignee_name": "护士丙"}).status_code == 200
    _visit_meanwhile(monkeypatch, vid, status="cancelled")
    got = client.post(f"/api/homevisits/{vid}/complete", headers=world["doctor"], json={"service_note": "已上门"})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert _row(HomeVisitOrder, vid, "status", "service_note") == ("cancelled", "")


def _consult(client, world):
    got = client.post("/api/telemedicine/consults", headers=world["doctor"], json={
        "patient_id": world["patient"], "org_id": world["org"], "question": "P2404 血压控制咨询"})
    assert got.status_code in (200, 201), got.text
    return got.json()["id"]


def test_两位医生同时回复_后到的一路409_先回的答复不被盖掉(client, world):
    cid = _consult(client, world)
    with _before_writing("online_consults", cid, status="replied", reply="先到的答复", doctor_name="甲医生") as fired:
        got = client.post(f"/api/telemedicine/consults/{cid}/reply", headers=world["doctor"],
                          json={"reply": "后到的答复", "doctor_name": "乙医生"})
    assert fired
    assert got.status_code == 409, got.text   # 修前 200：先回的答复被整段盖掉
    assert _row(OnlineConsult, cid, "status", "reply", "doctor_name") == ("replied", "先到的答复", "甲医生")


def test_结束途中咨询状态变了_不再改成已结束(client, world):
    cid = _consult(client, world)
    assert client.post(f"/api/telemedicine/consults/{cid}/reply", headers=world["doctor"],
                       json={"reply": "已答复", "doctor_name": "甲医生"}).status_code == 200
    with _before_writing("online_consults", cid, status="closed") as fired:
        got = client.post(f"/api/telemedicine/consults/{cid}/close", headers=world["doctor"])
    assert fired
    assert got.status_code == 409, got.text   # 修前 200（两路都说结束成功）


def test_不并发时照常流转(client, world):
    vid = _visit(client, world)
    assert client.post(f"/api/homevisits/{vid}/dispatch", headers=world["doctor"],
                       json={"assignee_name": "护士丁"}).json()["status"] == "dispatched"
    assert client.post(f"/api/homevisits/{vid}/complete", headers=world["doctor"],
                       json={"service_note": "已上门"}).json()["status"] == "completed"
    cid = _consult(client, world)
    assert client.post(f"/api/telemedicine/consults/{cid}/reply", headers=world["doctor"],
                       json={"reply": "答复", "doctor_name": "甲医生"}).json()["status"] == "replied"
    assert client.post(f"/api/telemedicine/consults/{cid}/close", headers=world["doctor"]).json()["status"] == "closed"
