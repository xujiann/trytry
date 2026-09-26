"""路径实例的个性化时限存了不用：`overrides[节点]["due_days"]` 照收照存，派任务一律按模板的时限（P2-258）。

`SpdPathInstance.overrides` 的列注释写着「个性化覆盖：{"node_key": {"due_days": 3}}，为空表示完全按模板」，实例的
docstring 写着「可个性化调整而不回改模板」，启动（`POST /path-instances`）与调整（`PATCH`）接口都收它——可派任务的三处
（启动派首节点、推进派下一节点、恢复派当前节点）一律取模板节点的 `due_days`；启动接口还是先派完首节点任务、再写覆盖。

修法：`service.node_due_days`——实例覆盖优先、没覆盖按模板，三处共用；覆盖随实例一起建。写得不成形的覆盖按模板。
"""
from datetime import timedelta

import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.spd.models import SpdPathNode, SpdPathTemplate, SpdProgram

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2258 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P2258_PATH", name="P2258 三节点路径",
                                   status="published")
        db.add(template)
        db.flush()
        db.add_all([
            SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1, due_days=7),
            SpdPathNode(template_id=template.id, key="n2", name="复诊", seq=2, due_days=14),
            SpdPathNode(template_id=template.id, key="n3", name="评估", seq=3, due_days=30),
        ])
        db.commit()
        return {"org": org, "template": template.id, "n": 0}


def _enrollment(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2258 患者{world['n']}", "id_card": f"33012719730909{world['n']:04d}"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    return enrollment.json()["id"]


def _task_on(instance_id, node_key):
    from app.database import SessionLocal
    from app.spd.models import SpdTask

    with SessionLocal() as db:
        task = db.query(SpdTask).filter_by(instance_id=instance_id, node_key=node_key).one()
        return task.id, task.due_date


def _in_days(days):
    from app import clock

    return (clock.today() + timedelta(days=days)).isoformat()


def test_启动与推进都按实例的个性化时限派任务(client, admin, world):
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": _enrollment(client, admin, world), "template_id": world["template"],
        "overrides": {"n1": {"due_days": 2}, "n2": {"due_days": 3}}})
    assert started.status_code == 201, started.text
    instance = started.json()["id"]
    first, due = _task_on(instance, "n1")
    assert due == _in_days(2)   # 修前 7 天：按模板
    done = client.post(f"{B}/tasks/{first}/complete", headers=admin, json={"result": {"note": "首诊完成"}})
    assert done.status_code == 200 and done.json()["advanced"]["current_node_key"] == "n2", done.text
    assert _task_on(instance, "n2")[1] == _in_days(3)   # 修前 14 天


def test_调整之后才加的覆盖_下一节点照样生效_没覆盖的按模板(client, admin, world):
    started = client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": _enrollment(client, admin, world), "template_id": world["template"]})
    instance = started.json()["id"]
    first, due = _task_on(instance, "n1")
    assert due == _in_days(7)   # 没覆盖：按模板
    patched = client.patch(f"{B}/path-instances/{instance}", headers=admin, json={"overrides": {"n2": {"due_days": 1}}})
    assert patched.status_code == 200, patched.text
    client.post(f"{B}/tasks/{first}/complete", headers=admin, json={})
    assert _task_on(instance, "n2")[1] == _in_days(1)   # 修前 14 天


@pytest.mark.parametrize("overrides", [
    {"n1": {"due_days": -1}}, {"n1": {"due_days": "3"}}, {"n1": {"due_days": True}}, {"n1": 5}, {"n1": {}},
], ids=["负数", "字符串", "布尔", "不是字典", "空"])
def test_覆盖写得不成形的按模板(overrides):
    from app.spd.models import SpdPathInstance, SpdPathNode
    from app.spd.service import node_due_days

    node = SpdPathNode(key="n1", due_days=7)
    assert node_due_days(SpdPathInstance(overrides=overrides), node) == 7
    assert node_due_days(SpdPathInstance(overrides={"n1": {"due_days": 0}}), node) == 0   # 0 天是合法的覆盖
