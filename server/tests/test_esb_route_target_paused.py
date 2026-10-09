"""编排的路由目标临时停用：这一次执行记失败，消息不计次、回到执行前的状态，启用后再执行照常投（P2-1728，第五十一批扫描 AO1-1）。

修前（c67fa3b 实测，扫描脚本 r1_route_inactive.py）：路由目标接入方被临时停用（省平台维护）后，按编排执行把消息认领了、
到路由步失败，照失败记账（`_record_failure`）——run#1 `failed 1`、run#2 `failed 2`、run#3 `dead 3`；启用回来执行编排与
手工消费都 409「消息当前状态 死信 不可再消费」，对端一条也收不到。`_steps_problem` 写的是「停用是临时的，启用回来照常
投」，兄弟路径 P2-180 / P2-662（消息自己的出站接入方停用）也是不动消息。

修后：状态码照旧（200 + 一条失败的执行记录），路由步说明写「路由目标接入方 X 已停用，消息未计次——启用后再执行」；消息的
状态、重试次数、下次重试时间、错误说明都不动，清单行照旧给「按编排重试」；启用后执行成功，对端只收到一次。停用的目标在认领
与执行任何一步之前就查（`_paused_route_target`）：前面各步一步都不做，否则每点一次就把路由到别的对端、建档这些重做一遍，而
这一次又不计次。查过之后、走到路由步之前才停用的，由循环里接住 `_TargetPaused` 放回认领（编排里建档那步中途提交过也一样）。
`test_esb.py::test_route_step_rejects_inactive_target` 只钉「执行记录失败、说明带已停用」，那一半照旧。
"""
import pytest

from app.database import SessionLocal
from app.models import EsbFlowRun, EsbMessage, Patient
from app.routers import esb as esb_module
from test_esb_outbound import FakeHttpx, enqueue, register_endpoint


@pytest.fixture
def fake(monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    return fake


def _paused(code):
    return f"路由目标接入方 {code} 已停用，消息未计次——启用后再执行"


def _target(client, admin, code):
    return register_endpoint(client, admin, code, direction="outbound", endpoint_url=f"https://prov.example/{code}")


def _flow(client, admin, code, steps):
    resp = client.post("/api/esb/flows", headers=admin, json={"code": code, "name": code, "steps": steps})
    assert resp.status_code == 201, resp.text


def _toggle(client, admin, endpoint, active):
    resp = client.patch(f"/api/esb/endpoints/{endpoint['id']}", headers=admin, json={"active": active})
    assert resp.status_code == 200 and resp.json()["active"] is active, resp.text


def _state(message_id):
    with SessionLocal() as db:
        row = db.get(EsbMessage, message_id)
        return row.status, row.retry_count, row.last_error, row.next_retry_at, row.updated_at


def _run(client, admin, code, message_id):
    resp = client.post(f"/api/esb/flows/{code}/run?message_id={message_id}", headers=admin)
    assert resp.status_code == 200, resp.text   # 状态码照旧
    return resp.json()


def test_路由目标停用时执行三次_执行记录都失败_消息仍可执行且零次_启用后对端只收到一次(client, admin, fake):
    inbound = register_endpoint(client, admin, "P21728_HIS", system_type="his")
    target = _target(client, admin, "P21728_PROV")
    _flow(client, admin, "P21728_F", [{"type": "validate", "config": {"required": ["x"]}},
                                      {"type": "route", "config": {"target_endpoint": "P21728_PROV"}}])
    message_id = enqueue(client, inbound, "referral_in", {"x": "1"})["id"]
    before = _state(message_id)
    _toggle(client, admin, target, False)   # 省平台维护

    for _ in range(3):
        body = _run(client, admin, "P21728_F", message_id)
        assert body["status"] == "failed" and body["error"] == _paused("P21728_PROV"), body
        assert body["step_results"][-1] == {"step": 2, "type": "route", "status": "failed",
                                            "detail": _paused("P21728_PROV")}
        assert (body["message_status"], body["retry_count"]) == ("queued", 0), body   # 修前 failed 1 → failed 2 → dead 3
        assert _state(message_id) == before   # 状态、次数、错误说明、下次重试时间、更新时间都没动
    with SessionLocal() as db:
        runs = [r.status for r in db.query(EsbFlowRun).filter(EsbFlowRun.message_id == message_id)]
    assert runs == ["failed"] * 3
    rows = client.get("/api/esb/messages", headers=admin, params={"endpoint_id": inbound["id"]}).json()
    assert [(m["id"], m["status"], m["retry_flow"]) for m in rows] == [(message_id, "queued", "P21728_F")]   # 页面给「按编排重试」
    assert fake.calls == []

    _toggle(client, admin, target, True)
    done = _run(client, admin, "P21728_F", message_id)
    assert (done["status"], done["message_status"], done["retry_count"]) == ("succeeded", "succeeded", 0), done   # 修前 409 死信
    assert [c["url"] for c in fake.calls] == [target["endpoint_url"]]


def test_失败待重试的消息碰上停用目标_次数与下次重试时间原样_不被推进死信(client, admin, fake):
    inbound = register_endpoint(client, admin, "P21728_HIS2", system_type="his")
    target = _target(client, admin, "P21728_PROV2")
    _flow(client, admin, "P21728_RETRY", [{"type": "route", "config": {"target_endpoint": "P21728_PROV2"}}])
    message_id = enqueue(client, inbound, "referral_in", {"x": "1"}, max_retries=2)["id"]
    fake.status_code = 503   # 对端先真出一次错：照常计次
    first = _run(client, admin, "P21728_RETRY", message_id)
    assert (first["message_status"], first["retry_count"]) == ("failed", 1), first
    before = _state(message_id)

    _toggle(client, admin, target, False)
    body = _run(client, admin, "P21728_RETRY", message_id)
    assert body["status"] == "failed" and body["step_results"][-1]["detail"] == _paused("P21728_PROV2"), body
    assert (body["message_status"], body["retry_count"]) == ("failed", 1), body   # 修前 dead 2（max_retries=2）
    assert _state(message_id) == before

    _toggle(client, admin, target, True)
    fake.status_code = 200
    done = _run(client, admin, "P21728_RETRY", message_id)
    assert (done["message_status"], done["retry_count"]) == ("succeeded", 1), done


def _patient_flow(client, admin, suffix, id_card, name):
    inbound = register_endpoint(client, admin, f"P21728_HIS{suffix}", system_type="his")
    target = _target(client, admin, f"P21728_PROV{suffix}")
    _flow(client, admin, f"P21728_PAT{suffix}", [{"type": "transform", "config": {"format": "fhir_patient"}},
                                                 {"type": "persist", "config": {"entity": "patient"}},
                                                 {"type": "route", "config": {"target_endpoint": f"P21728_PROV{suffix}"}}])
    resource = {"resourceType": "Patient", "name": [{"text": name}], "gender": "female",
                "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": id_card}]}
    message_id = enqueue(client, inbound, "fhir_patient", {"resource": resource})["id"]
    return target, message_id


def test_目标已停用时一步都不做_前面的建档不重做(client, admin, fake):
    """停用在认领之前就查：转换、建档一步都不执行，消息原样，对端一条也没收到。"""
    target, message_id = _patient_flow(client, admin, "4", "330281199001021728", "路由停用先查")
    before = _state(message_id)
    _toggle(client, admin, target, False)
    for _ in range(2):
        body = _run(client, admin, "P21728_PAT4", message_id)
        assert body["step_results"] == [{"step": 3, "type": "route", "status": "failed",
                                          "detail": _paused("P21728_PROV4")}], body   # 前两步没执行
        assert (body["status"], body["message_status"], body["retry_count"]) == ("failed", "queued", 0), body
        assert _state(message_id) == before
    with SessionLocal() as db:
        assert db.query(Patient).filter(Patient.name == "路由停用先查").count() == 0   # 建档那步没走到
    assert fake.calls == []
    _toggle(client, admin, target, True)
    done = _run(client, admin, "P21728_PAT4", message_id)
    assert done["message_status"] == "succeeded" and "新建" in done["step_results"][1]["detail"], done
    assert [c["url"] for c in fake.calls] == [target["endpoint_url"]]


def test_查过之后才停用的_建档那步中途提交之后碰上_认领照样放回(client, admin, fake, monkeypatch):
    """认领前查过、走到路由步之前目标才被停用（这里让认领前那一查看不见停用来模拟）：`create_patient_idempotent` 新建档案
    时会提交，认领（「处理中」）随之落库——放回不能只靠回滚。"""
    monkeypatch.setattr(esb_module, "_paused_route_target", lambda db, steps: None)
    inbound = register_endpoint(client, admin, "P21728_HIS3", system_type="his")
    target = _target(client, admin, "P21728_PROV3")
    _flow(client, admin, "P21728_PAT", [{"type": "transform", "config": {"format": "fhir_patient"}},
                                        {"type": "persist", "config": {"entity": "patient"}},
                                        {"type": "route", "config": {"target_endpoint": "P21728_PROV3"}}])
    resource = {"resourceType": "Patient", "name": [{"text": "路由停用"}], "gender": "female",
                "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "330281199001011728"}]}
    message_id = enqueue(client, inbound, "fhir_patient", {"resource": resource})["id"]
    before = _state(message_id)
    _toggle(client, admin, target, False)

    body = _run(client, admin, "P21728_PAT", message_id)
    assert [(s["type"], s["status"]) for s in body["step_results"]] == [
        ("transform", "succeeded"), ("persist", "succeeded"), ("route", "failed")], body
    assert "新建" in body["step_results"][1]["detail"] and body["step_results"][2]["detail"] == _paused("P21728_PROV3")
    assert (body["message_status"], body["retry_count"]) == ("queued", 0), body   # 修前 failed 1
    assert _state(message_id) == before
    with SessionLocal() as db:
        assert db.query(Patient).filter(Patient.name == "路由停用").count() == 1   # 已中途提交的建档照实保留

    _toggle(client, admin, target, True)
    done = _run(client, admin, "P21728_PAT", message_id)
    assert done["message_status"] == "succeeded" and "已存在" in done["step_results"][1]["detail"], done
    assert [c["url"] for c in fake.calls] == [target["endpoint_url"]]
