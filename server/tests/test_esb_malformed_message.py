"""集成平台一条形状不对的报文：手工消费 500、不进死信，定时出站那一轮整个中断，排在它后面的消息一条也投不出去（P1-176）。

`_run_step` 的约定是「失败以 ValueError/HTTPException 抛出」，三条消费路径只接这两类、据此计失败走重试 / 死信。可
解析函数碰上形状不对的报文（FHIR 的 `given` 写成字符串）抛 TypeError，投递地址写坏了 httpx 抛 `InvalidURL`（不是
`HTTPError`），编排的 `validate` 步骤 `required` 写成数字跑起来又是 TypeError——全都一路 500。修后：解析与投递的异常
收成 ValueError；三条路径对别的意外错误同样记这条消息一次失败（`_unexpected`）；步骤配置与投递地址在存的时候就查。
"""
import pytest

from app.database import SessionLocal
from app.models import EsbMessage
from app.routers import esb as esb_module
from test_esb_outbound import FakeHttpx, enqueue, register_endpoint

BAD_FHIR = {"resourceType": "Patient", "name": [{"family": "张", "given": "三"}], "gender": "male",
            "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "330281199001011761"}]}


def _message(message_id):
    with SessionLocal() as db:
        row = db.get(EsbMessage, message_id)
        return row.status, row.retry_count, row.last_error


def test_手工消费形状不对的报文_记失败走重试死信_不500(client, admin):
    ep = register_endpoint(client, admin, "P1176_IN", system_type="his")
    msg = enqueue(client, ep, "fhir_patient", BAD_FHIR, max_retries=2)
    first = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert first.status_code == 200, first.text   # 修前 500，消息原样待处理、重试计数与错误说明都是空的
    status, retries, error = _message(msg["id"])
    assert (status, retries) == ("failed", 1) and "报文解析失败" in error
    client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert _message(msg["id"])[0] == "dead"   # 达上限进死信，不再堵着


def test_定时出站_一条报文坏了不耽误后面的(client, admin, monkeypatch):
    fake = FakeHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    ep = register_endpoint(client, admin, "P1176_OUT", direction="outbound",
                           endpoint_url="https://province.example/p1176")
    bad = enqueue(client, ep, "fhir_patient", BAD_FHIR)
    good = enqueue(client, ep, "notice", {"text": "排在坏报文后面的通知"})
    with SessionLocal() as db:
        esb_module.consume_pending_outbound(db)   # 修前：这一轮 TypeError 整个中断
    assert _message(bad["id"])[:2] == ("failed", 1)
    assert _message(good["id"])[0] == "succeeded" and len(fake.calls) == 1


def test_定时出站_意外错误同样记失败_这一轮继续(client, admin, monkeypatch):
    ep = register_endpoint(client, admin, "P1176_OUT2", direction="outbound",
                           endpoint_url="https://province.example/p1176b")
    first = enqueue(client, ep, "notice", {"text": "第一条"})
    second = enqueue(client, ep, "notice", {"text": "第二条"})
    calls = []

    def flaky(endpoint, msg_type, body):
        calls.append(body["text"])
        if body["text"] == "第一条":
            raise RuntimeError("接口库里的意外")
        return "已投递"

    monkeypatch.setattr(esb_module, "_deliver", flaky)
    with SessionLocal() as db:
        esb_module.consume_pending_outbound(db)
    status, retries, error = _message(first["id"])
    assert (status, retries) == ("failed", 1) and "RuntimeError" in error
    assert _message(second["id"])[0] == "succeeded" and calls == ["第一条", "第二条"]


def test_编排里意外错误_记这一步失败_消息不卡在处理中(client, admin, monkeypatch):
    ep = register_endpoint(client, admin, "P1176_FLOW", system_type="his")
    flow = client.post("/api/esb/flows", headers=admin, json={"code": "P1176_F", "name": "建档编排", "steps": [
        {"type": "transform", "config": {"format": "fhir_patient"}}, {"type": "persist", "config": {"entity": "patient"}}]})
    assert flow.status_code == 201, flow.text
    good = {"resourceType": "Patient", "name": [{"text": "李编排"}], "gender": "female",
            "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "330281198802021762"}]}
    msg = enqueue(client, ep, "fhir_patient", good)

    def boom(db, data):
        raise RuntimeError("库报错")

    monkeypatch.setattr(esb_module, "create_patient_idempotent", boom)
    got = client.post(f"/api/esb/flows/P1176_F/run?message_id={msg['id']}", headers=admin)
    assert got.status_code == 200, got.text   # 修前 500
    assert (got.json()["status"], got.json()["message_status"]) == ("failed", "failed")
    assert "RuntimeError" in got.json()["step_results"][-1]["detail"]


@pytest.mark.parametrize("url", ["http://[::1/x", "ftp://province.example/in", "province.example/in"])
def test_投递地址存的时候就查(client, admin, url):
    got = client.post("/api/esb/endpoints", headers=admin, json={
        "code": f"P1176_URL{abs(hash(url)) % 1000}", "name": "坏地址", "system_type": "provincial",
        "direction": "outbound", "endpoint_url": url})
    assert got.status_code == 422, got.text   # 修前 201，投递时 InvalidURL 逃出重试 / 死信


def test_步骤配置的形状存的时候就查(client, admin):
    got = client.post("/api/esb/flows", headers=admin, json={"code": "P1176_BADCFG", "name": "坏配置", "steps": [
        {"type": "validate", "config": {"required": 5}}]})
    assert got.status_code == 422, got.text   # 修前 201，跑起来 TypeError
