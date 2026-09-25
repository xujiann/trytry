"""界面上恢复一条「因进入条件暂停」的路径，不判条件、不派任务：路径停在没有任务的节点上，再点「推进」就把它整个跳过（P1-131）。

路径推到下一节点时进入条件不满足，实例暂停（`advance_path`）：当前节点已是新节点，任务没派。正规的恢复在「推进」接口里——
重判当前节点的进入条件，满足才恢复并派本节点任务。可管理端的「推进节点」按钮只对执行中的实例给出，暂停的实例页面上只有
「调整 → 执行中（恢复）」，走的是改档接口（`PATCH /path-instances/{id}`）：它把状态改回执行中就完了——不判条件、不派任务。
实例于是停在一个一条任务都没有的节点上，「推进节点」随之出现，一点就推到再下一个节点：配了进入条件的节点被整个跳过（P1-4
要的「进入条件在流转时也生效」在界面上形同虚设）。

修法：恢复一处定义（`_resume_paused`，改档与推进共用）——当前节点还没有任务的（因条件暂停），重判进入条件（不满足 409）并派
本节点任务；已有任务的（手工暂停）只改回执行中，不再重复派。
"""
import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P131 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P131_PATH", name="P131 两节点路径", status="published")
        db.add(template)
        db.flush()
        db.add_all([
            SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1),
            SpdPathNode(template_id=template.id, key="n2", name="高危复诊", seq=2,
                        enter_condition=[{"field": "risk_level", "op": "==", "value": "high"}]),
            SpdPathNode(template_id=template.id, key="n3", name="结案评估", seq=3),
        ])
        db.commit()
        template_id = template.id
    return {"org": org, "template": template_id, "n": 0}


def _instance_at_n1(client, admin, world):
    """一位低危在管患者 + 一个停在 n1、挂着一条待办的执行中实例。返回 (纳管档案 id, 实例 id, n1 任务 id)。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPathInstance, SpdTask

    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P131 患者{world['n']}", "id_card": f"33012719720808{world['n']:04d}"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"], "risk_level": "low"})
    assert enrollment.status_code == 201, enrollment.text
    enrollment_id = enrollment.json()["id"]
    with SessionLocal() as db:
        instance = SpdPathInstance(enrollment_id=enrollment_id, template_id=world["template"], status="running",
                                   current_node_key="n1")
        db.add(instance)
        db.flush()
        task = SpdTask(patient_id=patient, enrollment_id=enrollment_id, instance_id=instance.id, node_key="n1",
                       task_type="path", title="P131 首诊", org_id=world["org"], program_code="hypertension")
        db.add(task)
        db.commit()
        return enrollment_id, instance.id, task.id


def _tasks_on(instance_id, node_key):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        return db.query(SpdTask).filter_by(instance_id=instance_id, node_key=node_key).count()


def _paused_at_n2(client, admin, world):
    enrollment, instance, task = _instance_at_n1(client, admin, world)
    done = client.post(f"{B}/tasks/{task}/complete", headers=admin, json={"result": {"note": "首诊完成"}})
    assert done.status_code == 200 and done.json()["advanced"]["status"] == "paused", done.text   # n2 条件不满足即暂停
    assert _tasks_on(instance, "n2") == 0
    return enrollment, instance


def test_条件仍不满足_界面上恢复409_不跳过这个节点(client, admin, world):
    _, instance = _paused_at_n2(client, admin, world)
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "running"})
    assert resp.status_code == 409, resp.text   # 修前 200：改回执行中，节点上一条任务都没有
    assert resp.json() == {"detail": "进入「高危复诊」的条件仍未满足，无法恢复"}
    detail = client.get(f"{B}/path-instances/{instance}", headers=admin).json()
    assert (detail["status"], detail["current_node_key"]) == ("paused", "n2")


def test_条件满足后界面上恢复_派本节点任务_推进拦得住(client, admin, world):
    enrollment, instance = _paused_at_n2(client, admin, world)
    assert client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"risk_level": "high"}).status_code == 200
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "running"})
    assert resp.status_code == 200 and resp.json()["status"] == "running", resp.text
    assert _tasks_on(instance, "n2") == 1   # 修前 0
    # 本节点的任务没办完，推进拦住——修前这一下直接推到 n3，高危复诊整个跳过
    adv = client.post(f"{B}/path-instances/{instance}/advance", headers=admin)
    assert adv.status_code == 409 and adv.json() == {"detail": "当前节点仍有未完成任务，不能推进"}, adv.text


def test_手工暂停再恢复_不重复派任务(client, admin, world):
    _, instance, _ = _instance_at_n1(client, admin, world)
    for status in ("paused", "running"):
        resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": status})
        assert resp.status_code == 200 and resp.json()["status"] == status, resp.text
    assert _tasks_on(instance, "n1") == 1
    # 推进接口恢复手工暂停的实例同理：节点上已有任务，不再派第二份（原先恢复一律再派一条）
    client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "paused"})
    resp = client.post(f"{B}/path-instances/{instance}/advance", headers=admin)
    assert resp.status_code == 200 and resp.json()["status"] == "running", resp.text
    assert _tasks_on(instance, "n1") == 1   # 修前 2


def test_已取消的路径不可再调整_结束时间不动(client, admin, world):
    """P2-88：改档接口只挡已完成的实例。已取消的再「取消」一次 200、结束时间挪到这一刻；改回执行中 / 暂停则把一条任务全被
    取消了的路径「复活」，停在没有未结束任务的节点上（页面对已取消的不给「调整」，写得进去的是接口调用方）。"""
    _, instance, _ = _instance_at_n1(client, admin, world)
    cancelled = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "cancelled"})
    assert cancelled.status_code == 200, cancelled.text
    finished_at = cancelled.json()["finished_at"]
    for status in ("cancelled", "running", "paused"):
        resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": status})
        assert resp.status_code == 409 and resp.json() == {"detail": "已取消的路径不可调整"}, (status, resp.text)  # 修前 200
    detail = client.get(f"{B}/path-instances/{instance}", headers=admin).json()
    assert (detail["status"], detail["finished_at"]) == ("cancelled", finished_at)


def test_回到走过的节点时因条件暂停_恢复照样重判条件并派任务(client, admin, world):
    """路径可以按 next_key 回到走过的节点（复评不达标回干预）。第二次进同一个节点时条件不满足而暂停，节点上躺着的是上一趟
    办完的任务——「节点上有没有任务」判不出这一趟进没进来，要看实例最近一条任务是不是这个节点的。"""
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P131_LOOP", name="P131 回环路径", status="published")
        db.add(template)
        db.flush()
        db.add_all([
            SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1),
            SpdPathNode(template_id=template.id, key="n2", name="高危复诊", seq=2,
                        enter_condition=[{"field": "risk_level", "op": "==", "value": "high"}]),
            SpdPathNode(template_id=template.id, key="n3", name="复评", seq=3, next_key="n2"),
        ])
        db.commit()
        loop_template = template.id
    enrollment, instance, task = _instance_at_n1(client, admin, world)
    with SessionLocal() as db:
        from app.spd.models import SpdPathInstance

        db.get(SpdPathInstance, instance).template_id = loop_template
        db.commit()

    def done(node_key):
        from app.spd.models import SpdTask

        with SessionLocal() as db:
            task_id = db.query(SpdTask.id).filter_by(instance_id=instance, node_key=node_key, status="pending") \
                .order_by(SpdTask.id.desc()).first()[0]
        resp = client.post(f"{B}/tasks/{task_id}/complete", headers=admin, json={"result": {"note": node_key}})
        assert resp.status_code == 200, resp.text
        return resp.json().get("advanced", {})

    client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"risk_level": "high"})
    assert done("n1")["current_node_key"] == "n2"          # 高危：第一趟直接进 n2
    assert done("n2")["current_node_key"] == "n3"
    client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"risk_level": "low"})
    assert done("n3")["status"] == "paused"                # 复评后回 n2，已不是高危：暂停在 n2
    assert _tasks_on(instance, "n2") == 1                  # 只有上一趟办完的那条
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "running"})
    assert resp.status_code == 409, resp.text              # 按「节点上有任务」判会当成手工暂停、直接改回执行中
    client.patch(f"{B}/enrollments/{enrollment}", headers=admin, json={"risk_level": "high"})
    resp = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"status": "running"})
    assert resp.status_code == 200, resp.text
    assert _tasks_on(instance, "n2") == 2                  # 这一趟的任务派出来了
