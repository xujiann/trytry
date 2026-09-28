"""超期扫描与结案收尾给复诊追加日志时先锁住这一行、重读，不再盖掉这期间别人记下的日志（P2-708，第十八批「定时扫描 vs
页面现算」扫描 V2-4）。

复诊日志是 JSON 列整体覆写。手工编辑复诊（`update_revisit`）早就进 `serialized_on` 行锁、`refresh` 之后再追加（注释写着
「后写的把先写的那条日志盖掉——事后说不清是谁改的正是这条日志要防的事」）；超期扫描（`sweep_overdue`）与结案收尾
（`close_open_work`）却先把整批复诊 `.all()` 载入、逐条拿载入时的旧日志拼上一条再写回。扫描载入之后、置逾期之前，护士
点了「已联系」并写下「电话邀约：患者说周五来」，扫描随后按旧列表写回——提醒状态保住了，日志里只剩扫描那一条。

用例用会话的身份映射造出这个交错：扫描所在的会话先把复诊读进来（相当于整批载入），护士经接口写日志并提交，再在这个
会话里跑扫描 / 收尾——不重读，拿的就是会话里那份旧日志。真 PG 上两路由同一把行锁排队（与 `update_revisit` 同一个
`serialized_on`）。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.spd.models import SpdEnrollment, SpdRevisit
from app.spd.service import close_open_work, sweep_overdue

B = "/api/spd"
NURSE_NOTE = "电话邀约：患者说周五来"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2708 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2708 患者", "id_card": "330127196905052708"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    return {"patient": patient, "enrollment": enrollment.json()["id"]}


def _revisit(client, admin, world, plan_date):
    resp = client.post(f"{B}/revisits", headers=admin, json={
        "patient_id": world["patient"], "program_code": "hypertension", "plan_date": plan_date,
        "items": "复查血压", "source": "manual"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _nurse_contacts(client, admin, revisit_id):
    resp = client.patch(f"{B}/revisits/{revisit_id}", headers=admin,
                        json={"remind_status": "contacted", "note": NURSE_NOTE})
    assert resp.status_code == 200, resp.text
    assert [entry["note"] for entry in resp.json()["log"]] == [NURSE_NOTE]


def test_超期扫描置逾期_不盖掉扫描期间护士记下的日志(client, admin, world):
    plan = clock.today() + timedelta(days=3)
    rid = _revisit(client, admin, world, plan.isoformat())
    with SessionLocal() as db:
        loaded = db.get(SpdRevisit, rid)   # 扫描「载入」时读到的：留住引用，身份映射是弱引用、对象回收了就等于重读
        assert not loaded.log
        _nurse_contacts(client, admin, rid)
        assert sweep_overdue(db, plan + timedelta(days=1))["revisits"] >= 1
        db.commit()
    with SessionLocal() as db:
        row = db.get(SpdRevisit, rid)
        assert (row.status, row.remind_status) == ("overdue", "contacted")
        # 修前只剩扫描那一条：护士的「电话邀约」被载入时的旧列表盖掉
        assert [entry["note"] for entry in row.log] == [NURSE_NOTE, "超期扫描：计划日期已过，置为逾期"]


def test_结案收尾移除复诊_不盖掉这期间护士记下的日志(client, admin, world):
    rid = _revisit(client, admin, world, (clock.today() + timedelta(days=30)).isoformat())
    with SessionLocal() as db:
        loaded = db.get(SpdRevisit, rid)
        assert not loaded.log
        _nurse_contacts(client, admin, rid)
        enrollment = db.get(SpdEnrollment, world["enrollment"])
        assert close_open_work(db, enrollment, "death:病故")["revisits"] >= 1
        db.commit()
    with SessionLocal() as db:
        row = db.get(SpdRevisit, rid)
        assert row.status == "removed"
        assert [entry["note"] for entry in row.log] == [NURSE_NOTE, "death:病故"]   # 修前只剩收尾那一条
