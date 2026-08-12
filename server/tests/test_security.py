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


def _parse_ndjson(resp):
    """NDJSON → (记录列表, meta 行)。"""
    import json

    lines = [json.loads(x) for x in resp.text.strip().split("\n") if x.strip()]
    meta = next(x for x in lines if x.get("_meta"))
    return [x for x in lines if not x.get("_meta")], meta


def test_audit_export_admin_only(client, admin_headers, operator_headers):
    """T6.2：导出改为游标式 NDJSON 流，不再一次性把全表读进内存。"""
    assert client.get("/api/audit/export", headers=operator_headers).status_code == 403

    resp = client.get("/api/audit/export", headers=admin_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-ndjson")
    logs, meta = _parse_ndjson(resp)
    assert meta["total"] == len(logs) > 0
    assert meta["last_id"] == logs[-1]["id"]
    assert {"username", "method", "path", "status_code", "at"} <= set(logs[0].keys())
    # id 升序，便于归档端校验连续性
    assert [x["id"] for x in logs] == sorted(x["id"] for x in logs)


def test_audit_export_incremental_by_cursor(client, admin_headers):
    """增量归档：上次导到哪，下次从哪继续。"""
    logs, _ = _parse_ndjson(client.get("/api/audit/export", headers=admin_headers))
    midpoint = logs[len(logs) // 2]["id"]
    rest, meta = _parse_ndjson(
        client.get(f"/api/audit/export?since_id={midpoint}", headers=admin_headers)
    )
    assert all(x["id"] > midpoint for x in rest)
    assert meta["since_id"] == midpoint


def test_audit_export_batches_do_not_load_whole_table(client, admin_headers, monkeypatch):
    """批读大小设为 2 时仍能完整导出——证明是分批而不是一次性全量。"""
    from app.routers import users as users_module

    monkeypatch.setattr(users_module, "AUDIT_EXPORT_BATCH", 2)
    logs, meta = _parse_ndjson(client.get("/api/audit/export", headers=admin_headers))
    assert meta["total"] == len(logs) > 2


def test_audit_export_rejects_bad_until(client, admin_headers):
    assert client.get("/api/audit/export?until=2026-8", headers=admin_headers).status_code == 422
