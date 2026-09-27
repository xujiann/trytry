"""超期扫描按节点配置升级时只往上抬优先级（P2-608，第十二批「批量 vs 单条」扫描 Z4-10）。

手工升级（单条 P2-194、批量 P2-246）早就改成两条带条件的 UPDATE、只往上抬；超期扫描里节点超时动作为「升级」的那一支
还是 `task.escalated = True; task.priority = max(task.priority, 2)`——用的是整批载入时读到的旧优先级，随扫描提交写回：
扫描期间别人刚把任务调成「特急」，扫描写回的「紧急」把它压了回去。读改写闸门只扫路由，服务层的这一处在盲区里。

这里用「扫描载入整批之后、第一条写库之前，另一路先把优先级调到特急并提交」把并发窗口钉成确定的时序。
"""
from datetime import date

import pytest

from app.database import SessionLocal
from app.spd.models import SpdTask

B = "/api/spd"


@pytest.fixture(scope="module")
def task_id(client, admin):
    hyp = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    tpl = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": hyp["id"], "code": "P2608_TPL", "name": "P2608 超时即升级"}).json()["id"]
    node = client.post(f"{B}/path-templates/{tpl}/nodes", headers=admin, json={
        "key": "n1", "name": "首诊", "seq": 1, "timeout_action": "escalate"})
    assert node.status_code == 201, node.text
    assert client.post(f"{B}/path-templates/{tpl}/status", headers=admin, json={"status": "published"}).status_code == 200
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2608 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2608 患者", "id_card": "330106197404042608"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).json()["id"]
    started = client.post(f"{B}/path-instances", headers=admin, json={"enrollment_id": enrollment, "template_id": tpl})
    assert started.status_code in (200, 201), started.text
    with SessionLocal() as db:
        task = db.query(SpdTask).filter(SpdTask.patient_id == patient, SpdTask.node_key == "n1").one()
        task.status, task.due_date, task.priority, task.escalated = "pending", "2020-01-01", 1, False
        db.commit()
        return task.id


def test_扫描载入之后别人调成特急_升级不把它压回紧急(task_id, monkeypatch):
    from app.spd import service

    real_move = service.move_task
    fired = []

    def raised_meanwhile(db, tid, *args, **kwargs):
        if not fired:   # 扫描载入整批之后、第一条写库之前：另一路先把它调成特急
            fired.append(True)
            with SessionLocal() as other:
                other.get(SpdTask, task_id).priority = 3
                other.commit()
        return real_move(db, tid, *args, **kwargs)

    monkeypatch.setattr(service, "move_task", raised_meanwhile)
    with SessionLocal() as db:
        result = service.sweep_overdue(db, date(2026, 9, 27))
        db.commit()
    assert fired and result["escalated"] >= 1
    with SessionLocal() as db:
        task = db.get(SpdTask, task_id)
        assert (task.status, task.escalated, task.priority) == ("overdue", True, 3)   # 修前 priority 2：特急被压回紧急
