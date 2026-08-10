"""M2 安全合规：登录锁定、密码复杂度、脱敏、登出黑名单、安全头、审计导出。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app
from app.routers.auth import _reset_login_failures


@pytest.fixture(scope="module")
def client():
    reset_database()
    _reset_login_failures()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin_headers(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def operator_headers(client, admin_headers):
    client.post(
        "/api/users",
        json={"username": "op_sec", "password": "secpass123", "role": "operator"},
        headers=admin_headers,
    )
    resp = client.post("/api/auth/login", json={"username": "op_sec", "password": "secpass123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_login_lockout_after_five_failures(client):
    """同一用户名连续5次失败 → 锁定10分钟，正确密码也被拒。"""
    for i in range(4):
        resp = client.post("/api/auth/login", json={"username": "locked_user", "password": "bad"})
        assert resp.status_code == 401
    fifth = client.post("/api/auth/login", json={"username": "locked_user", "password": "bad"})
    assert fifth.status_code == 423

    again = client.post("/api/auth/login", json={"username": "locked_user", "password": "whatever"})
    assert again.status_code == 423
    _reset_login_failures()


def test_login_failure_counter_resets_on_success(client):
    for _ in range(3):
        client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
    ok = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert ok.status_code == 200
    # 成功后计数清零：再失败4次仍未锁定
    for _ in range(4):
        resp = client.post("/api/auth/login", json={"username": "admin", "password": "bad"})
        assert resp.status_code == 401
    _reset_login_failures()


def test_password_complexity_enforced(client, admin_headers):
    weak_cases = ["short1", "onlyletters", "12345678"]
    for weak in weak_cases:
        resp = client.post(
            "/api/users",
            json={"username": "weak_user", "password": weak, "role": "operator"},
            headers=admin_headers,
        )
        assert resp.status_code == 422, weak

    change = client.post(
        "/api/auth/change-password",
        json={"current_password": "admin123", "new_password": "abcdefgh"},
        headers=admin_headers,
    )
    assert change.status_code == 422


def test_patient_masking_for_non_admin(client, admin_headers, operator_headers):
    client.post(
        "/api/patients",
        json={"name": "脱敏测试", "id_card": "320981199001015678", "phone": "13812345678"},
        headers=admin_headers,
    )
    # admin 看全量
    full = client.get("/api/patients?keyword=脱敏测试", headers=admin_headers).json()[0]
    assert full["id_card"] == "320981199001015678"
    assert full["phone"] == "13812345678"

    # 非 admin 看掩码：身份证保留前4后4、电话保留前3后2
    masked = client.get("/api/patients?keyword=脱敏测试", headers=operator_headers).json()[0]
    assert masked["id_card"] == "3209**********5678"
    assert masked["phone"] == "138******78"

    detail = client.get(f"/api/patients/{full['ehc_no']}", headers=operator_headers).json()
    assert detail["id_card"] == "3209**********5678"
    assert detail["phone"] == "138******78"


def test_logout_blacklists_token(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert client.get("/api/users", headers=headers).status_code == 200

    out = client.post("/api/auth/logout", headers=headers)
    assert out.status_code == 200
    assert out.json()["logged_out"] is True

    # 令牌已失效
    assert client.get("/api/users", headers=headers).status_code == 401


def test_security_response_headers(client):
    resp = client.get("/api/health")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["x-frame-options"] == "DENY"
    assert resp.headers["referrer-policy"] == "no-referrer"


def test_audit_export_admin_only(client, admin_headers, operator_headers):
    assert client.get("/api/audit/export", headers=operator_headers).status_code == 403

    resp = client.get("/api/audit/export", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == len(data["logs"])
    assert data["total"] > 0
    assert {"username", "method", "path", "status_code", "at"} <= set(data["logs"][0].keys())
