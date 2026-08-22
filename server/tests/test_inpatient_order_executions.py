"""医嘱执行记录（工程包 B1）：登记、按医嘱查询、停用后拒登记。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def order(client, admin):
    org = client.post(
        "/api/organizations",
        json={"name": "医嘱执行县医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    ward = client.post(
        "/api/inpatient/wards", json={"org_id": org["id"], "name": "执行一病区"}, headers=admin
    ).json()
    bed = client.post(
        "/api/inpatient/beds", json={"ward_id": ward["id"], "bed_no": "E-01"}, headers=admin
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "执行患者", "id_card": "330000199001015555"}, headers=admin
    ).json()
    admission = client.post(
        "/api/inpatient/admissions",
        json={"patient_id": patient["id"], "ward_id": ward["id"], "bed_id": bed["id"]},
        headers=admin,
    ).json()
    return client.post(
        "/api/inpatient/orders",
        json={"admission_id": admission["id"], "order_type": "long", "content": "青霉素 80万U bid（需皮试）"},
        headers=admin,
    ).json()


def test_执行登记与按医嘱查询(client, admin, order):
    first = client.post(
        f"/api/inpatient/orders/{order['id']}/executions",
        json={"note": "皮试后首剂执行", "skin_test_result": "negative"},
        headers=admin,
    )
    assert first.status_code == 201, first.text
    body = first.json()
    assert body["inpatient_order_id"] == order["id"]
    assert body["skin_test_result"] == "negative"
    assert body["executed_by_name"] != ""
    # 不需要皮试的执行：结果留空（None），不是"阴性"——两者不是一回事
    second = client.post(
        f"/api/inpatient/orders/{order['id']}/executions",
        json={"note": "第二剂执行"},
        headers=admin,
    )
    assert second.status_code == 201
    assert second.json()["skin_test_result"] is None

    rows = client.get(f"/api/inpatient/orders/{order['id']}/executions", headers=admin).json()
    assert len(rows) == 2
    assert rows[0]["note"] == "第二剂执行", "按登记先后倒序返回"


def test_皮试结果只认阴性阳性(client, admin, order):
    resp = client.post(
        f"/api/inpatient/orders/{order['id']}/executions",
        json={"note": "乱填皮试", "skin_test_result": "maybe"},
        headers=admin,
    )
    assert resp.status_code == 422


def test_停用医嘱不可再登记执行(client, admin, order):
    assert client.post(f"/api/inpatient/orders/{order['id']}/stop", headers=admin).status_code == 200
    resp = client.post(
        f"/api/inpatient/orders/{order['id']}/executions",
        json={"note": "停用后补记"},
        headers=admin,
    )
    assert resp.status_code == 409
    assert "已停止" in resp.json()["detail"]
    # 已有的执行记录仍可查——停用挡的是新增，不抹历史
    rows = client.get(f"/api/inpatient/orders/{order['id']}/executions", headers=admin).json()
    assert len(rows) == 2


def test_医嘱不存在404(client, admin):
    assert client.get("/api/inpatient/orders/999999/executions", headers=admin).status_code == 404
    assert client.post(
        "/api/inpatient/orders/999999/executions", json={"note": "x"}, headers=admin
    ).status_code == 404
