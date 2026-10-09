"""路径「取消」带走的节点任务不记结束时刻；批量取消与结案收尾都记（P2-1599，第四十七批扫描 AK3-6）。

`PATCH /api/spd/path-instances/{id}` 改成 cancelled 时，实例自己记 `finished_at`，逐条翻掉的节点任务却只写了状态与
「路径取消」——任务详情的「创建 / 完成」一栏与执行明细节点任务的 `finished_at` 是空的，批量取消（`tasks.py` 批量动作）
与结案收尾（`service.close_open_work`）同样是「已取消」却都有时刻。事后查不出这些任务是什么时候随路径一起撤掉的。

修法：取消时先算一次 `now_naive()`，实例与带走的任务共用同一个值；已办结的任务不动（条件翻转照旧）。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21599 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P21599_PATH", name="P21599 两节点路径",
                                   status="published")
        db.add(template)
        db.flush()
        db.add_all([SpdPathNode(template_id=template.id, key="n1", name="首诊评估", seq=1),
                    SpdPathNode(template_id=template.id, key="n2", name="强化管理", seq=2)])
        db.commit()
        template_id = template.id
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21599 患者", "id_card": "330127197309091599"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org})
    assert enrollment.status_code == 201, enrollment.text
    return {"enrollment": enrollment.json()["id"], "template": template_id}


def _node_tasks(client, admin, instance_id):
    detail = client.get(f"{B}/path-instances/{instance_id}", headers=admin)
    assert detail.status_code == 200, detail.text
    return {node["key"]: node["tasks"] for node in detail.json()["nodes"]}


def test_路径取消_带走的任务记结束时刻_与实例同一个值_已办结的不动(client, admin, world):
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": world["enrollment"], "template_id": world["template"]})
    assert started.status_code == 201, started.text
    instance_id = started.json()["id"]
    [first] = _node_tasks(client, admin, instance_id)["n1"]
    done = client.post(f"{B}/tasks/{first['id']}/complete", headers=admin, json={"result": {"note": "已评估"}})
    assert done.status_code == 200 and done.json()["status"] == "done", done.text
    done_at = done.json()["finished_at"]
    assert done_at
    [second] = _node_tasks(client, admin, instance_id)["n2"]   # 办完首节点，推进到第二节点派出的任务
    assert second["status"] == "pending" and second["finished_at"] == ""

    cancelled = client.patch(f"{B}/path-instances/{instance_id}", headers=admin, json={"status": "cancelled"})
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    instance_finished = cancelled.json()["finished_at"]
    assert instance_finished

    taken = client.get(f"{B}/tasks/{second['id']}", headers=admin).json()
    assert taken["status"] == "cancelled" and taken["review_note"] == "路径取消"
    assert taken["finished_at"] == instance_finished   # 修前 ""：批量取消与结案收尾都记
    tasks = _node_tasks(client, admin, instance_id)
    assert tasks["n2"][0]["finished_at"] == instance_finished   # 执行明细的节点任务同一个时刻
    # 已办结的不被改：状态、结束时刻原样（条件翻转只翻未结束的）
    kept = client.get(f"{B}/tasks/{first['id']}", headers=admin).json()
    assert kept["status"] == "done" and kept["finished_at"] == done_at and kept["review_note"] != "路径取消"
