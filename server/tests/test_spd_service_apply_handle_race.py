"""驳回 / 受理居民服务申请与门诊直接签约建档同时到：患者在管、池行已纳管，居民端「我的申请」却显示「已驳回」（P2-1176，
第三十四批扫描 L1-5）。

直接签约建档把这位居民这个病种的待受理申请条件翻成「已受理 · 已直接签约建档」（P2-937，`WHERE status = 'pending'`），
P2-937 说「与受理 / 驳回抢同一行的只成一个」——可受理 / 驳回一侧（`handle_service_apply`）在锁外判「待受理」、之后往对象上
赋值，flush 出来的 UPDATE 只有 `WHERE id = ?`。读到「待受理」之后门诊刚建档并提交，修前驳回照样 200：档案 active、池行
enrolled，居民端显示「已驳回 · 暂不符合条件」，正是 P2-937 要消灭的状态；受理同样 200，把建档写下的「已直接签约建档」盖成
受理意见。受理分支写池行是同一个形状：读到「未纳管」之后池行被置为已纳管，照旧整行写回「目标」。

修法：申请「还是待受理」压进同一条 UPDATE（`concurrency.move_row`），抢输了回滚、409「该申请已处理」（与顺序发生时同一句）；
受理分支写池行带 `status != 'enrolled'`，抢输的不动池行（与顺序发生时「已纳管的不动」同一个结果）。

时序照 `test_spd_lifecycle_death_race._dies_meanwhile`：在判定之后、写入之前插一路建档——另一个会话里跑真实的
`create_enrollment`（直调路由函数，同 `test_spd_recall_no_duplicate._recall_direct`）。
"""
import contextlib

import pytest
from sqlalchemy import event as sa_event

from app.database import SessionLocal, engine

B = "/api/spd"


def _resident(client, phone):
    code = client.post("/api/portal/auth/sms/code", json={"phone": phone}).json()["debug_code"]
    resp = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21176 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _applied(client, admin, world):
    """一位居民递交了高血压管理的服务申请（待受理）；返回（申请编号, 患者编号, 居民端请求头）。"""
    world["n"] += 1
    phone = f"1390117{world['n']:04d}"
    made = client.post("/api/patients", headers=admin, json={
        "name": f"P21176 居民{world['n']}", "id_card": f"33010619660606{world['n']:04d}", "phone": phone})
    assert made.status_code in (200, 201), made.text
    heads = _resident(client, phone)
    applied = client.post("/api/portal/spd/service-applies", headers=heads, json={"program_code": "hypertension"})
    assert applied.status_code == 201, applied.text
    return applied.json()["id"], made.json()["id"], heads


def _enroll_direct(patient_id, org_id):
    """门诊直接签约建档：另一个会话里跑真实的 `create_enrollment`（建档、池行置已纳管、待受理申请办结为已受理）并提交。"""
    from app.models import User
    from app.spd.routers.population import EnrollIn, create_enrollment

    with SessionLocal() as other:
        user = other.query(User).filter(User.username == "admin").one()
        create_enrollment(EnrollIn(patient_id=patient_id, program_code="hypertension", org_id=org_id),
                          db=other, user=user)


def _enrolled_meanwhile(monkeypatch, patient_id, org_id):
    """`population.now_naive` 被调用时（判过「待受理」、写入之前），门诊直接签约建档并提交。"""
    from app.spd.routers import population

    real, fired = population.now_naive, []

    def racing():
        if not fired:
            fired.append(True)
            _enroll_direct(patient_id, org_id)
        return real()

    monkeypatch.setattr(population, "now_naive", racing)
    return fired


@contextlib.contextmanager
def enrolled_before_pool_write(candidate_id):
    """下一条改目标池行的 UPDATE 发出之前，先在同一条连接上把这一行置为「已纳管」。

    等价于 PG 上别人签约建档恰在「读到未纳管」之后、这条 UPDATE 之前提交（READ COMMITTED 逐语句取快照）。这一路此前已有
    写入，SQLite 的库级写锁让另一个会话在这之间提交不了，只能在同一条连接上插；时序钉法同 `test_spd_recall_no_duplicate`
    的 before_cursor_execute。
    """
    fired: list = []

    def listener(conn, cursor, statement, parameters, context, executemany):
        if not fired and statement.lstrip().upper().startswith("UPDATE SPD_CANDIDATES"):
            fired.append(True)
            cursor.connection.execute("UPDATE spd_candidates SET status = 'enrolled' WHERE id = ?", (candidate_id,))

    sa_event.listen(engine, "before_cursor_execute", listener)
    try:
        yield fired
    finally:
        sa_event.remove(engine, "before_cursor_execute", listener)


def _apply_row(apply_id):
    from app.spd.models import SpdServiceApply

    with SessionLocal() as db:
        row = db.get(SpdServiceApply, apply_id)
        return row.status, row.handle_note


def _statuses(patient_id):
    """（池行状态, 档案状态）。"""
    from app.spd.models import SpdCandidate, SpdEnrollment

    with SessionLocal() as db:
        return ([c.status for c in db.query(SpdCandidate).filter(SpdCandidate.patient_id == patient_id)],
                [e.status for e in db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id)])


@pytest.mark.parametrize("decision,note", [("rejected", "暂不符合条件"), ("accepted", "P21176 受理")])
def test_驳回受理与直接签约建档并发_409_居民端不显示已驳回(client, admin, world, monkeypatch, decision, note):
    apply_id, patient, heads = _applied(client, admin, world)
    fired = _enrolled_meanwhile(monkeypatch, patient, world["org"])
    resp = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin,
                       json={"status": decision, "handle_note": note})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "该申请已处理"}   # 与顺序发生时同一句
    # 修前驳回写成 rejected、受理把「已直接签约建档」盖成受理意见
    assert _apply_row(apply_id) == ("accepted", "已直接签约建档")
    assert _statuses(patient) == (["enrolled"], ["active"])
    mine = client.get("/api/portal/spd/service-applies", headers=heads).json()
    assert [(a["status"], a["handle_note"]) for a in mine] == [("accepted", "已直接签约建档")]   # 修前 已驳回


def test_受理写池行之前池行刚被置为已纳管_不写回目标(client, admin, world):
    from app.spd.models import SpdCandidate

    apply_id, patient, _ = _applied(client, admin, world)
    with SessionLocal() as db:   # 池里已有这位居民（筛查进来的疑似）
        row = SpdCandidate(patient_id=patient, program_code="hypertension", status="suspect", source="screening",
                           org_id=world["org"], risk_level="mid", matched_rules=[], reason="用例夹具")
        db.add(row)
        db.commit()
        candidate = row.id
    with enrolled_before_pool_write(candidate) as fired:
        resp = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin, json={"status": "accepted"})
    assert fired
    assert resp.status_code == 200, resp.text   # 与顺序发生时一样：已纳管的池行不动，申请照常受理
    assert _statuses(patient)[0] == ["enrolled"]   # 修前写回 target


def test_没有竞争时照常受理与驳回(client, admin, world):
    apply_id, patient, heads = _applied(client, admin, world)
    resp = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin, json={"status": "accepted"})
    assert resp.status_code == 200 and resp.json() == {"id": apply_id, "status": "accepted"}, resp.text
    assert _statuses(patient) == (["target"], [])   # 受理即进目标池
    again = client.post(f"{B}/service-applies/{apply_id}/handle", headers=admin, json={"status": "rejected"})
    assert again.status_code == 409 and again.json() == {"detail": "该申请已处理"}
    other, _, _ = _applied(client, admin, world)
    resp = client.post(f"{B}/service-applies/{other}/handle", headers=admin,
                       json={"status": "rejected", "handle_note": "暂不符合条件"})
    assert resp.status_code == 200 and resp.json() == {"id": other, "status": "rejected"}, resp.text
    assert _apply_row(other) == ("rejected", "暂不符合条件")
