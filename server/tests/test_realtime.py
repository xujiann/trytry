"""M5 实时消息：WebSocket 通知广播与任务待办中心。"""
import pytest
from starlette.websockets import WebSocketDisconnect


@pytest.fixture(scope="module")
def admin_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return resp.json()["access_token"]


@pytest.fixture(scope="module")
def admin_headers(admin_token):
    return {"Authorization": f"Bearer {admin_token}"}


def _make_user(client, admin_headers, username, role):
    client.post(
        "/api/users",
        json={"username": username, "password": f"{role}pass123", "role": role},
        headers=admin_headers,
    )
    resp = client.post(
        "/api/auth/login", json={"username": username, "password": f"{role}pass123"}
    )
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def setup(client, admin_headers):
    org = client.post(
        "/api/organizations",
        json={"name": "实时测试医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin_headers,
    ).json()
    org2 = client.post(
        "/api/organizations",
        json={"name": "实时测试卫生院", "org_type": "township", "level": "township"},
        headers=admin_headers,
    ).json()
    patient = client.post(
        "/api/patients",
        json={"name": "实时患者", "id_card": "320981199210105555"},
        headers=admin_headers,
    ).json()
    return {
        "org": org,
        "org2": org2,
        "patient": patient,
        "doctor": _make_user(client, admin_headers, "doc_rt", "doctor"),
        "pharmacist": _make_user(client, admin_headers, "pharm_rt", "pharmacist"),
        "operator": _make_user(client, admin_headers, "op_rt", "operator"),
    }


def test_ws_rejects_invalid_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/notifications?token=bad-token") as ws:
            ws.receive_json()


def test_ws_broadcasts_critical_report(client, admin_token, admin_headers, setup):
    req = client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org2"]["id"],
            "center_type": "lab",
            "item_code": "K",
            "item_name": "血钾",
        },
        headers=admin_headers,
    ).json()

    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as ws:
        client.post(
            f"/api/exams/{req['id']}/report",
            json={"conclusion": "血钾 7.2 mmol/L 危急", "critical": True, "reported_by": "检验科"},
            headers=setup["doctor"],
        )
        message = ws.receive_json()
        assert message["type"] == "critical_report"
        assert message["request_id"] == req["id"]
        assert "危急" in message["conclusion"]


def test_ws_broadcasts_stock_shortage_on_transfer(client, admin_token, admin_headers, setup):
    client.post(
        "/api/pharmacy/stocks",
        json={
            "org_id": setup["org"]["id"],
            "drug_code": "RT01",
            "drug_name": "实时测试药",
            "quantity": 10,
            "threshold": 8,
        },
        headers=admin_headers,
    )
    with client.websocket_connect(f"/ws/notifications?token={admin_token}") as ws:
        # 调拨5盒后余5 < 阈值8 → 触发缺药预警广播
        client.post(
            "/api/pharmacy/transfers",
            json={
                "drug_code": "RT01",
                "from_org_id": setup["org"]["id"],
                "to_org_id": setup["org2"]["id"],
                "quantity": 5,
            },
            headers=admin_headers,
        )
        message = ws.receive_json()
        assert message["type"] == "stock_shortage"
        assert message["drug_code"] == "RT01"
        assert message["quantity"] == 5
        assert message["threshold"] == 8


def test_todos_by_role(client, admin_headers, setup):
    # 造一张待药师审处方（超剂量）
    client.post(
        "/api/prescriptions/rules",
        json={"drug_code": "RT02", "max_daily_dose": 10, "dose_unit": "mg"},
        headers=admin_headers,
    )
    client.post(
        "/api/prescriptions",
        json={
            "patient_id": setup["patient"]["id"],
            "org_id": setup["org"]["id"],
            "diagnosis_name": "高血压",
            "items": [{"drug_code": "RT02", "drug_name": "测试降压药", "daily_dose": 99}],
        },
        headers=setup["doctor"],
    )
    # 造一张待诊断申请
    client.post(
        "/api/exams",
        json={
            "patient_id": setup["patient"]["id"],
            "from_org_id": setup["org2"]["id"],
            "center_type": "imaging",
            "item_code": "DR",
            "item_name": "胸片",
        },
        headers=admin_headers,
    )

    # 药师：只看待审处方
    pharm = client.get("/api/todos", headers=setup["pharmacist"]).json()
    assert pharm["role"] == "pharmacist"
    types = [i["type"] for i in pharm["items"]]
    assert types == ["prescription_review"]
    assert pharm["items"][0]["count"] >= 1
    assert any("测试" in (p["review_comment"] or "") or p["diagnosis_name"] == "高血压"
               for p in pharm["items"][0]["list"])

    # 医师：待诊断申请 + 待确认危急值（M-5 整改）
    doc = client.get("/api/todos", headers=setup["doctor"]).json()
    assert [i["type"] for i in doc["items"]] == ["exam_diagnosis", "critical_ack"]
    assert doc["items"][0]["count"] >= 1

    # 管理员：全部预警聚合
    admin = client.get("/api/todos", headers=admin_headers).json()
    admin_types = {i["type"] for i in admin["items"]}
    assert admin_types == {
        "prescription_review",
        "exam_diagnosis",
        "stock_shortage",
        "critical_report",
    }
    assert admin["total"] >= 3

    # 经办人员：无待办类别
    op = client.get("/api/todos", headers=setup["operator"]).json()
    assert op["items"] == []
