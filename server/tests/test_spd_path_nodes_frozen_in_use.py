"""有患者正在走的路径，把模板改回草稿（或停用）就能改删节点：在跑的实例随之变形，删掉当前节点，办完这一步路径即「已完成」（P2-97）。

「已发布的路径不可直接改节点，要改就复制新版本」——节点增改删三个接口都这么挡，理由写在代码里：在跑的实例会突然多出
一个没人知道的任务。可挡的条件是「模板状态 == 已发布」，而改状态的接口收草稿 / 已发布 / 停用三个值、不看来路：已发布的
改回草稿 200，节点随便动。在跑的实例每推进一步都按**当前**的节点表取下一节点——删掉它正停着的节点，推进时找不到当前节点，
路径直接记成「已完成」、进度 100%，后面的节点一个都不走；停用同理（停用的模板照样能改节点）。

修法：模板上还有执行中 / 暂停的实例时，节点不许增改删（409，与已发布同一句提示：复制新版本再改）；状态随便改，
节点动不了，在跑的实例就不会变形。没人在走的草稿 / 停用模板照旧能改。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathInstance, SpdPathNode, SpdPathTemplate, SpdProgram, SpdTask

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P297 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P297 患者", "id_card": "330127196703030297"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P297_PATH", name="P297 三节点路径", status="published")
        db.add(template)
        db.flush()
        nodes = [SpdPathNode(template_id=template.id, key=f"n{i}", name=f"第{i}步", seq=i) for i in (1, 2, 3)]
        db.add_all(nodes)
        db.flush()
        instance = SpdPathInstance(enrollment_id=enrollment, template_id=template.id, status="running",
                                   current_node_key="n1")
        db.add(instance)
        db.flush()
        task = SpdTask(patient_id=patient, enrollment_id=enrollment, instance_id=instance.id, node_key="n1",
                       task_type="path", title="P297 第1步", org_id=org, program_code="hypertension")
        db.add(task)
        db.commit()
        return {"template": template.id, "nodes": {n.key: n.id for n in nodes}, "instance": instance.id,
                "task": task.id}


def test_在跑的路径改回草稿_节点照样动不了_推进不变形(client, admin, world):
    for status in ("draft", "disabled"):
        resp = client.post(f"{B}/path-templates/{world['template']}/status", headers=admin, json={"status": status})
        assert resp.status_code == 200, resp.text   # 状态照改
        deleted = client.delete(f"{B}/path-nodes/{world['nodes']['n1']}", headers=admin)
        assert deleted.status_code == 409, (status, deleted.text)   # 修前 204：删掉的正是在跑实例停着的节点
        assert "复制新版本" in deleted.json()["detail"]
        patched = client.patch(f"{B}/path-nodes/{world['nodes']['n2']}", headers=admin, json={"name": "改名"})
        assert patched.status_code == 409, (status, patched.text)
        added = client.post(f"{B}/path-templates/{world['template']}/nodes", headers=admin,
                            json={"key": "n9", "name": "临时加的"})
        assert added.status_code == 409, (status, added.text)
    done = client.post(f"{B}/tasks/{world['task']}/complete", headers=admin, json={"result": {"note": "第1步办完"}})
    assert done.status_code == 200, done.text
    assert done.json()["advanced"]["current_node_key"] == "n2"   # 修前：n1 被删，路径直接「已完成」


def test_没人在走的草稿照旧能改节点(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdProgram

    with SessionLocal() as db:
        program_id = db.query(SpdProgram.id).filter_by(code="hypertension").scalar()
    template = client.post(f"{B}/path-templates", headers=admin, json={
        "program_id": program_id, "code": "P297_IDLE", "name": "P297 草稿"}).json()["id"]
    node = client.post(f"{B}/path-templates/{template}/nodes", headers=admin, json={"key": "a", "name": "甲"})
    assert node.status_code == 201, node.text
    assert client.patch(f"{B}/path-nodes/{node.json()['id']}", headers=admin, json={"name": "乙"}).status_code == 200
    assert client.delete(f"{B}/path-nodes/{node.json()['id']}", headers=admin).status_code == 204
