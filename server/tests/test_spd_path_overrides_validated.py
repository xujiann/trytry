"""路径实例的个性化覆盖写入前查结构：只认模板里的节点、只认 due_days、时限 0～3650 天（P2-717，第十八批「数值入参的
符号与业务上下界」扫描 V3-6）。

覆盖自 P2-258 起按节点求值（`node_due_days` 取它派任务），启动（`POST /path-instances`）与调整（`PATCH`）却只要是个
对象就收——宽字典闸门里还登记着「只存只回显」。时限 36500 天照存，派出去的任务一百年后才到期；10**9 天在派任务那一刻
`today + timedelta` 溢出、500；节点键写错、`due_days` 拼错的悄悄不生效。模板节点自己的时限早就是 0～3650 天。

修法：两处写入与模板节点同一个界，节点键须在模板里、只认 due_days（`service.path_overrides_problem`）；存量里写入口
查结构之前存下的超界时限，派任务时按模板（不因为它 500）。
"""
from datetime import timedelta

import pytest

from app import clock
from app.database import SessionLocal
from app.spd.models import SpdPathInstance, SpdPathNode, SpdPathTemplate, SpdProgram, SpdTask

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2717 路径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    with SessionLocal() as db:
        program = db.query(SpdProgram).filter_by(code="hypertension").one()
        template = SpdPathTemplate(program_id=program.id, code="P2717_PATH", name="P2717 两节点路径", status="published")
        db.add(template)
        db.flush()
        db.add_all([SpdPathNode(template_id=template.id, key="n1", name="首诊", seq=1, due_days=7),
                    SpdPathNode(template_id=template.id, key="n2", name="复诊", seq=2, due_days=14)])
        db.commit()
        return {"org": org, "template": template.id, "n": 0}


def _enrollment(client, admin, world):
    world["n"] += 1
    patient = client.post("/api/patients", headers=admin, json={
        "name": f"P2717 患者{world['n']}", "id_card": f"33012719740909{world['n']:04d}"}).json()["id"]
    enrollment = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": world["org"]})
    assert enrollment.status_code == 201, enrollment.text
    return enrollment.json()["id"]


def _start(client, admin, world, enrollment, overrides):
    return client.post(f"{B}/path-instances", headers=admin, json={
        "enrollment_id": enrollment, "template_id": world["template"], "overrides": overrides})


BAD = [
    ({"n1": {"due_days": 36500}}, "0～3650"), ({"n1": {"due_days": 10**9}}, "0～3650"),
    ({"n1": {"due_days": -1}}, "0～3650"), ({"n1": {"due_days": "3"}}, "0～3650"), ({"n1": {"due_days": True}}, "0～3650"),
    ({"n9": {"due_days": 3}}, "不在这条路径的模板里"), ({"n1": {"due": 3}}, "只认 due_days"), ({"n1": 5}, "这样的对象"),
]
BAD_IDS = ["一百年", "溢出", "负数", "字符串", "布尔", "节点键不在模板里", "键名拼错", "不是对象"]


@pytest.mark.parametrize(("overrides", "hint"), BAD, ids=BAD_IDS)
def test_启动时覆盖写坏的422_不建实例(client, admin, world, overrides, hint):
    enrollment = _enrollment(client, admin, world)
    resp = _start(client, admin, world, enrollment, overrides)
    assert resp.status_code == 422 and hint in resp.json()["detail"], resp.text   # 修前 201（或溢出 500）
    with SessionLocal() as db:
        assert db.query(SpdPathInstance).filter_by(enrollment_id=enrollment).count() == 0


def test_启动时合法的覆盖照收_上界当天可取_空对象按模板(client, admin, world):
    started = _start(client, admin, world, _enrollment(client, admin, world), {"n1": {"due_days": 3650}, "n2": {}})
    assert started.status_code == 201, started.text
    with SessionLocal() as db:
        task = db.query(SpdTask).filter_by(instance_id=started.json()["id"], node_key="n1").one()
        assert task.due_date == (clock.today() + timedelta(days=3650)).isoformat()


@pytest.mark.parametrize(("overrides", "hint"), BAD, ids=BAD_IDS)
def test_调整时覆盖写坏的422_原覆盖不变(client, admin, world, overrides, hint):
    started = _start(client, admin, world, _enrollment(client, admin, world), {"n2": {"due_days": 3}})
    assert started.status_code == 201, started.text
    resp = client.patch(f"{B}/path-instances/{started.json()['id']}", headers=admin, json={"overrides": overrides})
    assert resp.status_code == 422 and hint in resp.json()["detail"], resp.text   # 修前 200、照存
    with SessionLocal() as db:
        assert db.get(SpdPathInstance, started.json()["id"]).overrides == {"n2": {"due_days": 3}}


@pytest.mark.parametrize("days", [3651, 10**9])
def test_存量里超界的时限派任务按模板_不溢出(days):
    from app.spd.service import node_due_days

    node = SpdPathNode(key="n1", due_days=7)
    assert node_due_days(SpdPathInstance(overrides={"n1": {"due_days": days}}), node) == 7   # 修前原样返回，10**9 派任务溢出
    assert node_due_days(SpdPathInstance(overrides={"n1": {"due_days": 3650}}), node) == 3650


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_覆盖里的NaN_Infinity同各配置校验一样挡下(value):
    """接口层的测试客户端不肯把 NaN 编进 JSON，与 P2-466 的用例一样直接调校验：标准库 `json.loads` 照收这两个记号。"""
    from app.spd.service import path_overrides_problem

    assert path_overrides_problem({"n1": {"due_days": value}}, {"n1"}) == "overrides.n1.due_days 不能是 NaN / Infinity"
