"""居民「标记完成」是锁外读改写：医生移除干预方案的同时居民点了完成，removed 被写回 done（P2-302）。

P2-67 让居民端不能再把已移除的方案翻成「已完成」——可那是一道锁外预检：`feedback_intervention` 读到方案还没移除 →
赋值 `status = "done"` → commit，UPDATE 只有 `WHERE id = ?`。医生恰在其间移除这条方案（或档案结束时 `close_open_work`
一并收掉），居民这边随后提交，removed 被改回 done、重新算进完成数。

修法：翻转与「没被移除」压进同一条 UPDATE（`service.mark_intervention_done`），与顺序请求同一句 409。这里把「读到之后、
写入之前，医生先把它移除了」钉成确定的时序：在两者之间必经的「首次标记已读取当前时刻」里插进另一路的提交。
"""
import pytest
from test_portal_services import login

from app.database import SessionLocal

PHONE = "13700023020"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin,
                      json={"name": "P2302 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/spd/programs", headers=admin,
                       json={"code": "p2302_prog", "name": "P2302 病种", "category": "chronic"})
    assert resp.status_code == 201, resp.text
    patient = client.post("/api/patients", headers=admin,
                          json={"name": "P2302 居民", "id_card": "330192198001012302", "phone": PHONE}).json()
    enroll = client.post("/api/spd/enrollments", headers=admin,
                         json={"patient_id": patient["id"], "program_code": "p2302_prog", "org_id": org})
    assert enroll.status_code == 201, enroll.text
    return {"patient": patient, "resident": login(client, PHONE)}


def _intervene(client, admin, world, goal):
    resp = client.post("/api/spd/interventions", headers=admin, json={
        "patient_ids": [world["patient"]["id"]], "program_code": "p2302_prog", "goal": goal, "content": "每日步行"})
    assert resp.status_code == 201, resp.text
    items = client.get("/api/portal/spd/interventions", headers=world["resident"]).json()
    return next(i for i in items if i["goal"] == goal)["id"]


def _status(intervention_id):
    from app.spd.models import SpdIntervention

    with SessionLocal() as db:
        return db.get(SpdIntervention, intervention_id).status


def test_医生移除与居民标记完成交错_不被写回已完成(client, admin, world, monkeypatch):
    from app.spd.models import SpdIntervention
    from app.spd.routers import portal

    intervention_id = _intervene(client, admin, world, "P2302 交错")
    real = portal.now_naive

    def racing():
        with SessionLocal() as other:   # 居民这一路读到方案之后，医生先把它移除了
            other.get(SpdIntervention, intervention_id).status = "removed"
            other.commit()
        return real()

    monkeypatch.setattr(portal, "now_naive", racing)
    got = client.post(f"/api/portal/spd/interventions/{intervention_id}/feedback", headers=world["resident"],
                      json={"feedback": "照做了", "done": True})
    monkeypatch.undo()
    assert got.status_code == 409, got.text   # 修前 200
    assert got.json()["detail"] == "该干预方案已被医生移除，不能再标记完成"   # 与顺序请求同一句
    assert _status(intervention_id) == "removed"   # 修前 done


def test_不并发时照常标记完成_再点一次照旧(client, admin, world):
    intervention_id = _intervene(client, admin, world, "P2302 照常")
    for _ in range(2):
        got = client.post(f"/api/portal/spd/interventions/{intervention_id}/feedback", headers=world["resident"],
                          json={"feedback": "每天走了一小时", "done": True})
        assert got.status_code == 200 and got.json() == {"id": intervention_id, "status": "done", "read": True}, got.text
    assert _status(intervention_id) == "done"
