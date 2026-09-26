"""路径实例的暂停 / 取消不看终态是不是刚被别人走到：已完成的路径被写成已取消（P2-343）。

`PATCH /api/spd/path-instances/{id}` 的「已完成 / 已取消的路径不可调整」是锁外读的；随后暂停与取消直接往对象上赋值，
flush 出来的 UPDATE 只有 `WHERE id = ?`。读到「未结束」之后别人刚办完最后一个节点（办结在实例锁里把它改成已完成），
这一路照旧写成已取消 / 暂停：走完的路径显示为已取消，暂停的还能再「恢复」。恢复那一支早就在锁里重读，只有这两支漏了。

修法：暂停与取消走条件翻转（`concurrency.move_row`，`WHERE status IN ('running', 'paused')`），抢输了按库里此刻的状态 409。
这里把「预检过了、还没写」钉成确定的时序：归属校验（在终态预检之前）之后、写入之前，让另一路先把实例走完。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2343 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P2343_PATH", name="P2343 单节点路径", status="published")
        db.add(template)
        db.flush()
        db.add(SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1))
        db.commit()
        template_id = template.id
    return {"org": org, "template": template_id, "n": 0}


def _running_instance(client, admin, world):
    from app.spd.models import SpdPathInstance

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2343 患者{world['n']}", "id_card": f"33012719720909{world['n']:04d}"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    with SessionLocal() as db:
        instance = SpdPathInstance(enrollment_id=enrollment.json()["id"], template_id=world["template"],
                                   status="running", current_node_key="n1")
        db.add(instance)
        db.commit()
        return instance.id


def _completed_meanwhile(monkeypatch, instance_id):
    """归属校验之后（终态预检之前、写入之前），另一路把实例走完并提交。"""
    from app.spd.models import SpdPathInstance
    from app.spd.routers import tasks

    real, fired = tasks.assert_org_writable, []

    def racing(*args, **kwargs):
        result = real(*args, **kwargs)
        if not fired:
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdPathInstance, instance_id).status = "completed"
                other.commit()
        return result

    monkeypatch.setattr(tasks, "assert_org_writable", racing)
    return fired


def _status(instance_id):
    from app.spd.models import SpdPathInstance

    with SessionLocal() as db:
        return db.get(SpdPathInstance, instance_id).status


@pytest.mark.parametrize("to_status", ["cancelled", "paused"])
def test_预检之后路径被走完_暂停取消都409_不把已完成盖掉(client, admin, world, monkeypatch, to_status):
    instance = _running_instance(client, admin, world)
    fired = _completed_meanwhile(monkeypatch, instance)
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": to_status})
    monkeypatch.undo()
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200
    assert resp.json() == {"detail": "已完成的路径不可调整"}
    assert _status(instance) == "completed"      # 修前被盖成 cancelled / paused


def test_没有竞争时照常暂停取消(client, admin, world):
    instance = _running_instance(client, admin, world)
    paused = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "paused"})
    assert paused.status_code == 200 and paused.json()["status"] == "paused", paused.text
    cancelled = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "cancelled"})
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
    assert cancelled.json()["finished_at"]
