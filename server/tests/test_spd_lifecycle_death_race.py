"""登记死亡与恢复 / 排除 / 召回成功并发：死亡被盖掉，死者又成了在管（P2-344）。

死亡是终态（P1-111）：生命周期接口先判「已登记死亡」再改状态——判是锁外读的，改是往对象上赋值、UPDATE 只有
`WHERE id = ?`。读到「没死」之后别人刚登记死亡并提交，这一路照旧写成在管（恢复、召回成功）或排除 / 召回 / 迁出；
后两种再「恢复」一次，死者就回到在管、照样派任务与随访。

修法：三处改状态都走条件翻转（`concurrency.move_row`，`WHERE status != 'dead'`），抢输了回滚、409。
这里把「判过了、还没写」钉成确定的时序：在判定之后、写入之前让另一路登记死亡并提交。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2344 纳管卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org, "n": 0}


def _enrollment(client, admin, world, event=None):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2344 患者{world['n']}", "id_card": f"33012719731010{world['n']:04d}"}).json()["id"]
    got = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert got.status_code == 201, got.text
    enrollment = got.json()["id"]
    if event:
        done = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": event, "reason": "P2344"})
        assert done.status_code == 200, done.text
    return enrollment


def _dies_meanwhile(monkeypatch, name, enrollment_id):
    """`population.<name>` 调完之后（判定之后、写入之前），另一路把档案登记成死亡并提交。"""
    from app.spd.models import SpdEnrollment
    from app.spd.routers import population

    real, fired = getattr(population, name), []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdEnrollment, enrollment_id).status = "dead"
                other.commit()
        return result

    monkeypatch.setattr(population, name, racing)
    return fired


def _status(enrollment_id):
    from app.spd.models import SpdEnrollment

    with SessionLocal() as db:
        return db.get(SpdEnrollment, enrollment_id).status


def test_恢复管理与登记死亡并发_不把死者恢复成在管(client, admin, world, monkeypatch):
    enrollment = _enrollment(client, admin, world, event="exclude")
    fired = _dies_meanwhile(monkeypatch, "assert_org_writable", enrollment)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": "resume"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "已登记死亡的档案不可恢复管理"}
    assert _status(enrollment) == "dead"         # 修前 active


@pytest.mark.parametrize("event", ["exclude", "recall", "migrate"])
def test_排除召回迁出与登记死亡并发_死亡不被盖掉(client, admin, world, monkeypatch, event):
    enrollment = _enrollment(client, admin, world)
    fired = _dies_meanwhile(monkeypatch, "assert_org_writable", enrollment)
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": event, "reason": "P2344"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "已登记死亡的档案不可再登记生命周期事件"}
    assert _status(enrollment) == "dead"


def test_召回成功与登记死亡并发_不把死者恢复成在管(client, admin, world, monkeypatch):
    from app.spd.models import SpdRecall

    enrollment = _enrollment(client, admin, world, event="recall")
    with SessionLocal() as db:
        recall = db.query(SpdRecall).filter(SpdRecall.enrollment_id == enrollment).one().id
    fired = _dies_meanwhile(monkeypatch, "_active_elsewhere_detail", enrollment)
    resp = client.post(f"{B}/recalls/{recall}/progress", headers=admin, json={"status": "returned"})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert _status(enrollment) == "dead"         # 修前 active


def test_没有竞争时照常恢复与排除(client, admin, world):
    enrollment = _enrollment(client, admin, world, event="exclude")
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": "resume"})
    assert resp.status_code == 200 and resp.json()["enrollment"]["status"] == "active", resp.text
    resp = client.post(f"{B}/enrollments/{enrollment}/lifecycle", headers=admin, json={"event": "exclude", "reason": "P2344"})
    assert resp.status_code == 200 and resp.json()["enrollment"]["status"] == "excluded", resp.text
