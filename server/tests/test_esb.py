"""块1：集成平台底座 ESB——入队鉴权/限流、编排逐步执行、失败重试与死信、统计口径。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

HL7_MESSAGE = (
    "MSH|^~\\&|HIS|COUNTY|MEDPLAT|COUNTY|20240101090000||ADT^A01|MSG0001|P|2.4\r"
    "PID|1||330281199203046014^^^CN^ID||张总线||19920304|M|||杭州市||13800001111"
)
FHIR_PATIENT = {
    "resourceType": "Patient",
    "identifier": [{"system": "urn:oid:2.16.156.10011.1.3", "value": "330281198802027015"}],
    "name": [{"text": "李编排"}],
    "gender": "female",
    "birthDate": "1988-02-02",
    "telecom": [{"system": "phone", "value": "13900002222"}],
}


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


def register_endpoint(client, admin, code, **kwargs):
    body = {
        "code": code,
        "name": kwargs.pop("name", f"接入方{code}"),
        "system_type": kwargs.pop("system_type", "his"),
        **kwargs,
    }
    resp = client.post("/api/esb/endpoints", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


def esb_headers(endpoint):
    return {"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]}


def enqueue(client, endpoint, msg_type, payload, max_retries=3):
    return client.post(
        "/api/esb/messages",
        json={"msg_type": msg_type, "payload": payload, "max_retries": max_retries},
        headers=esb_headers(endpoint),
    )


# ---------------------------------------------------------------------------
# 接入方注册与入队鉴权
# ---------------------------------------------------------------------------


def test_endpoint_register_returns_token_once_and_code_unique(client, admin):
    ep = register_endpoint(client, admin, "HIS_MAIN", system_type="his", direction="inbound")
    assert ep["auth_token"] and ep["system_type_name"] == "医院信息系统"
    # 编码唯一
    dup = client.post(
        "/api/esb/endpoints",
        json={"code": "HIS_MAIN", "name": "重复", "system_type": "lis"},
        headers=admin,
    )
    assert dup.status_code == 409
    # 列表不回显令牌（库内只存散列）
    listed = client.get("/api/esb/endpoints", headers=admin).json()
    assert all("auth_token" not in e for e in listed)


def test_enqueue_requires_valid_endpoint_token(client, admin):
    ep = register_endpoint(client, admin, "LIS_AUTH", system_type="lis")
    # 无令牌
    assert client.post("/api/esb/messages", json={"msg_type": "x", "payload": {"a": 1}}).status_code == 401
    # 令牌错误
    bad = client.post(
        "/api/esb/messages",
        json={"msg_type": "x", "payload": {"a": 1}},
        headers={"X-Esb-Endpoint": ep["code"], "X-Esb-Token": "wrong-token"},
    )
    assert bad.status_code == 401
    # 接入方编码不存在
    unknown = client.post(
        "/api/esb/messages",
        json={"msg_type": "x", "payload": {"a": 1}},
        headers={"X-Esb-Endpoint": "NOT_EXIST", "X-Esb-Token": ep["auth_token"]},
    )
    assert unknown.status_code == 401
    # 正确令牌入队成功
    ok = enqueue(client, ep, "generic", {"a": 1})
    assert ok.status_code == 201 and ok.json()["status"] == "queued"


def test_disabled_endpoint_rejected_and_token_rotation(client, admin):
    ep = register_endpoint(client, admin, "PACS_OFF", system_type="pacs")
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": False}, headers=admin)
    assert enqueue(client, ep, "generic", {"a": 1}).status_code == 403
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"active": True}, headers=admin)
    assert enqueue(client, ep, "generic", {"a": 1}).status_code == 201
    # 轮换令牌后旧令牌失效
    rotated = client.post(f"/api/esb/endpoints/{ep['id']}/rotate-token", headers=admin).json()
    assert enqueue(client, ep, "generic", {"a": 1}).status_code == 401
    assert enqueue(client, {**ep, "auth_token": rotated["auth_token"]}, "generic", {"a": 1}).status_code == 201


def test_enqueue_rate_limit_returns_429(client, admin):
    ep = register_endpoint(client, admin, "INS_LIMIT", system_type="insurance", rate_limit_per_min=2)
    assert enqueue(client, ep, "generic", {"n": 1}).status_code == 201
    assert enqueue(client, ep, "generic", {"n": 2}).status_code == 201
    third = enqueue(client, ep, "generic", {"n": 3})
    assert third.status_code == 429 and "限流" in third.json()["detail"]
    # 提高限额后限速器重建，可继续入队
    client.patch(f"/api/esb/endpoints/{ep['id']}", json={"rate_limit_per_min": 100}, headers=admin)
    assert enqueue(client, ep, "generic", {"n": 4}).status_code == 201


# ---------------------------------------------------------------------------
# 消费与重试
# ---------------------------------------------------------------------------


def test_process_hl7v2_message_creates_patient(client, admin):
    ep = register_endpoint(client, admin, "HIS_HL7", system_type="his")
    msg = enqueue(client, ep, "hl7v2_patient", {"message": HL7_MESSAGE}).json()
    resp = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "succeeded" and body["retry_count"] == 0
    assert "EHC" in body["detail"]
    # 已成功的消息不可重复消费
    assert client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).status_code == 409
    # 消费结果落既有交换日志（M11 监控口径）
    logs = client.get("/api/integration/exchange-logs?message_type=esb_hl7v2_patient", headers=admin).json()
    assert logs["logs"] and logs["logs"][0]["success"] is True


def test_process_failure_retries_then_dead_letter(client, admin):
    ep = register_endpoint(client, admin, "HIS_BAD", system_type="his")
    # 报文缺 PID 段 → 每次消费均失败
    msg = enqueue(client, ep, "hl7v2_patient", {"message": "MSH|^~\\&|HIS|C|||20240101||ADT^A01|X|P|2.4"}, max_retries=2).json()
    first = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert first["status"] == "failed" and first["retry_count"] == 1
    assert "PID" in first["last_error"] and first["next_retry_at"]
    second = client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).json()
    assert second["status"] == "dead" and second["retry_count"] == 2
    assert second["next_retry_at"] is None and second["last_error"]
    # 死信不可再消费
    assert client.post(f"/api/esb/messages/{msg['id']}/process", headers=admin).status_code == 409
    assert client.post("/api/esb/messages/999999/process", headers=admin).status_code == 404


def test_message_list_filter_and_pagination(client, admin):
    ep = register_endpoint(client, admin, "LIS_LIST", system_type="lis")
    for i in range(3):
        enqueue(client, ep, "generic", {"i": i})
    resp = client.get(f"/api/esb/messages?endpoint_id={ep['id']}&status=queued&limit=2", headers=admin)
    assert resp.status_code == 200
    assert resp.headers["X-Total-Count"] == "3" and len(resp.json()) == 2
    assert all(m["endpoint_code"] == "LIS_LIST" for m in resp.json())
    page2 = client.get(
        f"/api/esb/messages?endpoint_id={ep['id']}&status=queued&offset=2&limit=2", headers=admin
    ).json()
    assert len(page2) == 1
    assert client.get("/api/esb/messages?status=unknown", headers=admin).status_code == 422


# ---------------------------------------------------------------------------
# 流程编排
# ---------------------------------------------------------------------------


def test_flow_runs_steps_in_order(client, admin):
    ep = register_endpoint(client, admin, "HIS_FLOW", system_type="his")
    flow = client.post(
        "/api/esb/flows",
        json={
            "code": "FHIR_PATIENT_IN",
            "name": "FHIR 患者入站编排",
            "steps": [
                {"type": "transform", "config": {"format": "fhir_patient", "source_field": "resource"}},
                {"type": "validate", "config": {"required": ["name", "id_card"]}},
                {"type": "route", "config": {"target_endpoint": "HIS_FLOW"}},
                {"type": "persist", "config": {"entity": "patient"}},
            ],
        },
        headers=admin,
    )
    assert flow.status_code == 201 and flow.json()["step_count"] == 4
    msg = enqueue(client, ep, "fhir_patient", {"resource": FHIR_PATIENT}).json()
    run = client.post(
        f"/api/esb/flows/FHIR_PATIENT_IN/run?message_id={msg['id']}", headers=admin
    )
    assert run.status_code == 200, run.text
    body = run.json()
    assert body["status"] == "succeeded" and body["message_status"] == "succeeded"
    assert [s["step"] for s in body["step_results"]] == [1, 2, 3, 4]
    assert [s["type"] for s in body["step_results"]] == ["transform", "validate", "route", "persist"]
    assert all(s["status"] == "succeeded" for s in body["step_results"])
    assert "EHC" in body["step_results"][3]["detail"]
    # 患者确实建档
    patients = client.get("/api/patients?q=李编排", headers=admin).json()
    assert any(p["name"] == "李编排" for p in patients)
    # 执行记录可查
    runs = client.get(f"/api/esb/flow-runs?message_id={msg['id']}", headers=admin).json()
    assert len(runs) == 1 and runs[0]["flow_code"] == "FHIR_PATIENT_IN"


def test_flow_step_failure_stops_and_counts_retry(client, admin):
    ep = register_endpoint(client, admin, "HIS_FLOWBAD", system_type="his")
    client.post(
        "/api/esb/flows",
        json={
            "code": "STRICT_FLOW",
            "name": "严格校验编排",
            "steps": [
                {"type": "transform", "config": {"format": "hl7v2_patient"}},
                {"type": "validate", "config": {"required": ["name", "id_card", "phone"]}},
                {"type": "persist", "config": {"entity": "patient"}},
            ],
        },
        headers=admin,
    )
    # 报文无电话字段 → 第2步校验失败，第3步不执行
    no_phone = "MSH|^~\\&|HIS|C|||20240101||ADT^A01|M2|P|2.4\rPID|1||330281199510105019^^^CN^ID||王缺项||19951010|M"
    msg = enqueue(client, ep, "hl7v2_patient", {"message": no_phone}, max_retries=2).json()
    body = client.post(f"/api/esb/flows/STRICT_FLOW/run?message_id={msg['id']}", headers=admin).json()
    assert body["status"] == "failed" and len(body["step_results"]) == 2
    assert body["step_results"][1]["status"] == "failed" and "phone" in body["step_results"][1]["detail"]
    assert body["message_status"] == "failed" and body["retry_count"] == 1
    # 重跑仍失败 → 达上限转死信
    again = client.post(f"/api/esb/flows/STRICT_FLOW/run?message_id={msg['id']}", headers=admin).json()
    assert again["message_status"] == "dead" and again["retry_count"] == 2
    failed_runs = client.get("/api/esb/flow-runs?status=failed", headers=admin).json()
    assert len(failed_runs) >= 2


def test_flow_validation_and_guards(client, admin):
    ep = register_endpoint(client, admin, "HIS_GUARD", system_type="his")
    # 非法步骤类型
    bad = client.post(
        "/api/esb/flows",
        json={"code": "BAD_FLOW", "name": "非法", "steps": [{"type": "unknown"}]},
        headers=admin,
    )
    assert bad.status_code == 422
    client.post(
        "/api/esb/flows",
        json={
            "code": "OFF_FLOW",
            "name": "停用编排",
            "steps": [{"type": "validate", "config": {"required": ["a"]}}],
        },
        headers=admin,
    )
    msg = enqueue(client, ep, "generic", {"a": "1"}).json()
    # 流程不存在 / 消息不存在
    assert client.post(f"/api/esb/flows/NOPE/run?message_id={msg['id']}", headers=admin).status_code == 404
    assert client.post("/api/esb/flows/OFF_FLOW/run?message_id=999999", headers=admin).status_code == 404
    # 停用流程不可执行
    flow = next(f for f in client.get("/api/esb/flows", headers=admin).json() if f["code"] == "OFF_FLOW")
    client.patch(f"/api/esb/flows/{flow['id']}", json={"active": False}, headers=admin)
    assert client.post(f"/api/esb/flows/OFF_FLOW/run?message_id={msg['id']}", headers=admin).status_code == 409


def test_route_step_rejects_inactive_target(client, admin):
    ep = register_endpoint(client, admin, "HIS_RT", system_type="his")
    target = register_endpoint(client, admin, "PROV_RT", system_type="provincial", direction="outbound")
    client.patch(f"/api/esb/endpoints/{target['id']}", json={"active": False}, headers=admin)
    client.post(
        "/api/esb/flows",
        json={
            "code": "ROUTE_FLOW",
            "name": "上报省平台",
            "steps": [{"type": "route", "config": {"target_endpoint": "PROV_RT"}}],
        },
        headers=admin,
    )
    msg = enqueue(client, ep, "report", {"x": 1}).json()
    body = client.post(f"/api/esb/flows/ROUTE_FLOW/run?message_id={msg['id']}", headers=admin).json()
    assert body["status"] == "failed" and "已停用" in body["step_results"][0]["detail"]


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------


def test_stats_rates_and_backlog(client, admin):
    ep = register_endpoint(client, admin, "STAT_EP", system_type="lis")
    ok_msg = enqueue(client, ep, "generic", {"a": 1}).json()
    client.post(f"/api/esb/messages/{ok_msg['id']}/process", headers=admin)
    dead_msg = enqueue(client, ep, "generic", {}, max_retries=1).json()  # 空载荷必失败
    client.post(f"/api/esb/messages/{dead_msg['id']}/process", headers=admin)
    enqueue(client, ep, "generic", {"b": 2})  # 积压
    stats = client.get("/api/esb/stats", headers=admin).json()
    row = next(r for r in stats["by_endpoint"] if r["endpoint_code"] == "STAT_EP")
    assert row["total"] == 3 and row["succeeded"] == 1 and row["dead"] == 1
    assert row["queued"] == 1 and row["backlog"] == 1
    # 已终结 2 条中成功 1 条 → 50%
    assert row["success_rate_pct"] == 50.0 and row["failure_rate_pct"] == 50.0
    assert stats["totals"]["endpoints"] >= 1 and stats["totals"]["flows"] >= 1
    assert stats["totals"]["success_rate_pct"] + stats["totals"]["failure_rate_pct"] == 100.0


def test_non_admin_cannot_manage_endpoints(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "总线卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    client.post(
        "/api/users",
        json={"username": "esb_op", "password": "pass123456", "role": "operator", "org_id": org["id"]},
        headers=admin,
    )
    operator = login(client, "esb_op", "pass123456")
    assert client.post(
        "/api/esb/endpoints",
        json={"code": "OP_EP", "name": "越权", "system_type": "his"},
        headers=operator,
    ).status_code == 403
    # 经办可消费消息（运维岗位）与查看统计
    assert client.get("/api/esb/stats", headers=operator).status_code == 200
    ep = register_endpoint(client, admin, "OP_PROC", system_type="his")
    msg = enqueue(client, ep, "generic", {"a": 1}).json()
    assert client.post(f"/api/esb/messages/{msg['id']}/process", headers=operator).status_code == 200
    # 未登录不可查询队列
    assert client.get("/api/esb/messages").status_code == 401
