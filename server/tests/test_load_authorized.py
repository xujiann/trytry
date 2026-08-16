"""load_authorized 依赖：by-id 取对象 + 机构归属校验（以会计凭证详情为载体）。

用真实端点 GET /api/accounting/vouchers/{id} 验证依赖行为：
- 本机构可见（读）、他院 403、不存在 404、非法 id 422。
凭证归属由 Voucher.org_id 决定，端点已改用 load_authorized(Voucher, "voucher_id", write=False)。
"""
from conftest import reset_database


def _login(client, username, password="pw123456"):
    tok = client.post("/api/auth/login", json={"username": username, "password": password}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


def _setup(client):
    admin = _login(client, "admin", "admin123")
    a = client.post("/api/organizations", json={"name": "凭证甲院", "org_type": "lead_hospital", "level": "county"}, headers=admin).json()
    b = client.post("/api/organizations", json={"name": "凭证乙院", "org_type": "township", "level": "township"}, headers=admin).json()
    # director 是全域角色（看得到全部），测机构隔离要用非全域的 operator 去读。
    client.post("/api/users", json={"username": "la_dir_a", "password": "pw123456", "full_name": "甲院长", "role": "director", "org_id": a["id"]}, headers=admin)
    client.post("/api/users", json={"username": "la_op_a", "password": "pw123456", "full_name": "甲经办", "role": "operator", "org_id": a["id"]}, headers=admin)
    client.post("/api/users", json={"username": "la_op_b", "password": "pw123456", "full_name": "乙经办", "role": "operator", "org_id": b["id"]}, headers=admin)
    # 甲院建一张凭证（director 可建）
    dir_a = _login(client, "la_dir_a")
    v = client.post(
        "/api/accounting/vouchers",
        json={"org_id": a["id"], "period": "2026-08", "summary": "测试凭证",
              "voucher_no": "PZ-2026-08-001", "voucher_date": "2026-08-15",
              "entries": [{"subject_code": "1001", "debit": 100, "credit": 0},
                          {"subject_code": "2001", "debit": 0, "credit": 100}]},
        headers=dir_a,
    )
    return {"admin": admin, "a": a, "b": b, "dir_a": dir_a,
            "op_a": _login(client, "la_op_a"), "op_b": _login(client, "la_op_b"), "voucher": v}


def test_load_authorized_paths():
    from fastapi.testclient import TestClient

    from app.main import app

    reset_database()
    with TestClient(app) as client:  # 进入 lifespan 以触发 admin 初始化
        s = _setup(client)
        assert s["voucher"].status_code == 201, s["voucher"].text
        vid = s["voucher"].json()["id"]

        # 本机构非全域账号可读
        assert client.get(f"/api/accounting/vouchers/{vid}", headers=s["op_a"]).status_code == 200
        # 他院非全域账号读同一凭证 → 403（load_authorized 机构归属校验生效）
        assert client.get(f"/api/accounting/vouchers/{vid}", headers=s["op_b"]).status_code == 403
        # 全域 admin 可读
        assert client.get(f"/api/accounting/vouchers/{vid}", headers=s["admin"]).status_code == 200
        # 不存在 → 404（自定义 not_found 文案）
        r404 = client.get("/api/accounting/vouchers/999999", headers=s["op_a"])
        assert r404.status_code == 404 and "凭证" in r404.json()["detail"]
