"""停用的出站接入方不手工投递：消息留在队里，启用后照常消费（P2-180）。

定时消费只取「出站且启用」的端点、编排的路由步骤拒停用目标，手工「消费/重试」原先什么都不看——省平台维护期间
停用了端点，经办逐条点就逐条真投、失败三次进死信，恢复启用后一条也不再投；端点是因为密钥泄露停用的，
消息照样投给了刻意切断的目标。
"""
from test_esb import enqueue, register_endpoint


def test_停用的出站端点手工消费409_消息原样留在队里_启用后照常消费(client, admin):
    ep = register_endpoint(client, admin, "P2180_OUT", direction="outbound")
    queued = enqueue(client, ep, "generic", {"a": 1})
    assert queued.status_code == 201, queued.text
    message_id = queued.json()["id"]
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": False}, headers=admin)

    resp = client.post(f"/api/esb/messages/{message_id}/process", headers=admin)
    assert resp.status_code == 409, resp.text   # 修前 200：照样「投递」
    assert "已停用" in resp.json()["detail"]
    rows = client.get(f"/api/esb/messages?endpoint_id={ep['id']}", headers=admin).json()
    assert [(m["id"], m["status"], m["retry_count"]) for m in rows] == [(message_id, "queued", 0)]

    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": True}, headers=admin)
    done = client.post(f"/api/esb/messages/{message_id}/process", headers=admin)
    assert done.status_code == 200 and done.json()["status"] == "succeeded", done.text


def test_停用的入站端点手工消费照旧(client, admin):
    """入站积压停用后能不能手工消化待裁定，这次不动它。"""
    ep = register_endpoint(client, admin, "P2180_IN")
    queued = enqueue(client, ep, "generic", {"a": 1}).json()["id"]
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": False}, headers=admin)
    resp = client.post(f"/api/esb/messages/{queued}/process", headers=admin)
    assert resp.status_code == 200 and resp.json()["status"] == "succeeded", resp.text


def test_对消息执行编排同样不碰停用出站端点的积压(client, admin):
    """第十五批 S1-2：编排入口原先只判流程启用——路由到停用端点的执行三次进死信、启用后手工消费 409「死信不可再消费」，
    只有校验步的流程直接把消息记成已成功。修后与手工消费同一句 409、消息原样留在队里，启用后照常执行。"""
    ep = register_endpoint(client, admin, "S12_OUT", direction="outbound")
    message_id = enqueue(client, ep, "generic", {"a": 1}).json()["id"]
    flow = client.post("/api/esb/flows", headers=admin, json={
        "code": "S12_ROUTE", "name": "S1-2 路由编排",
        "steps": [{"type": "route", "config": {"target_endpoint": "S12_OUT"}}]})
    assert flow.status_code == 201, flow.text
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": False}, headers=admin)

    for _ in range(3):
        resp = client.post(f"/api/esb/flows/S12_ROUTE/run?message_id={message_id}", headers=admin)
        assert resp.status_code == 409 and "已停用" in resp.json()["detail"], resp.text   # 修前 200：failed 两次、第三次死信
    rows = client.get(f"/api/esb/messages?endpoint_id={ep['id']}", headers=admin).json()
    assert [(m["id"], m["status"], m["retry_count"]) for m in rows] == [(message_id, "queued", 0)]

    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": True}, headers=admin)
    done = client.post(f"/api/esb/flows/S12_ROUTE/run?message_id={message_id}", headers=admin)
    assert done.status_code == 200 and done.json()["message_status"] == "succeeded", done.text
