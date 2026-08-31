"""阶段十一 权限模型重构与安全加固。

自定义角色与权限点、审计防篡改哈希链、国密算法适配层、CSP 响应头。
"""


from app import gmcrypto


# ================================================================ 国密适配层


def test_SM3对得上国标测试向量():
    """GB/T 32905-2016 附录 A 的两个标准向量。对不上就是实现错了，
    而一个"能跑但算错"的杂凑比没有更危险。"""
    assert gmcrypto.sm3_hash(b"abc").hex() == (
        "66c7f0f462eeedd9d1f2d46bdc10e4e24167c4875cf2f7a2297da02b8f4ba8e0"
    )
    assert gmcrypto.sm3_hash(b"abcd" * 16).hex() == (
        "debe9ff92275b8a138604889c18e5a4d6fdb70e5387e5765293dcba39c0c5732"
    )


def test_切换算法后老口令仍可校验(monkeypatch):
    """不带算法前缀就只能强制全员改密，那是没人能接受的升级方式。"""
    from app.config import settings

    monkeypatch.setattr(settings, "crypto_suite", "general")
    general_hash = gmcrypto.hash_password("passw0rd1")
    assert not general_hash.startswith("sm3$")

    monkeypatch.setattr(settings, "crypto_suite", "sm")
    sm_hash = gmcrypto.hash_password("passw0rd1")
    assert sm_hash.startswith("sm3$")
    # 切到国密之后，用通用算法存的老口令照样能登录
    assert gmcrypto.verify_password("passw0rd1", general_hash) is True
    assert gmcrypto.verify_password("passw0rd1", sm_hash) is True
    assert gmcrypto.verify_password("wrongpass1", sm_hash) is False

    monkeypatch.setattr(settings, "crypto_suite", "general")
    # 切回来之后国密存的口令也仍然可用
    assert gmcrypto.verify_password("passw0rd1", sm_hash) is True


def test_令牌头部标注算法(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "crypto_suite", "general")
    assert gmcrypto.mac_alg() == "HS256"
    monkeypatch.setattr(settings, "crypto_suite", "sm")
    assert gmcrypto.mac_alg() == "HS-SM3"
    monkeypatch.setattr(settings, "crypto_suite", "general")


# ================================================================ CSP


def test_CSP响应头挡住外部来源(client):
    """管理端是 build-free 内联脚本，必须放行 unsafe-inline；
    真正要挡的是外部来源——那才是 XSS 被利用时的外泄通道。"""
    csp = client.get("/api/health").headers["Content-Security-Policy"]
    assert "default-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "form-action 'self'" in csp
    assert "object-src 'none'" in csp
    # 内联脚本确实放行了——不放行管理端直接白屏
    assert "script-src 'self' 'unsafe-inline'" in csp


# ================================================================ 审计哈希链


def test_审计哈希链可校验(client, admin):
    client.post(
        "/api/organizations",
        json={"name": "阶段十一机构", "org_type": "township", "level": "township"},
        headers=admin,
    )
    result = client.get("/api/audit/verify", headers=admin)
    assert result.status_code == 200, result.text
    body = result.json()
    assert body["valid"] is True
    assert body["checked"] >= 1
    assert "不可抵赖需外部存证" in body["caliber"]


def test_改动历史记录会被链校验发现(client, admin):
    """这是哈希链唯一真正的能力：改过就看得出来。"""
    from app.database import SessionLocal
    from app.models import AuditLog

    client.post(
        "/api/organizations",
        json={"name": "阶段十一机构二", "org_type": "township", "level": "township"},
        headers=admin,
    )
    db = SessionLocal()
    try:
        target = db.query(AuditLog).filter(AuditLog.entry_hash != "").order_by(
            AuditLog.id
        ).first()
        original_path = target.path
        target.path = "/api/被篡改的路径"
        db.commit()
        broken_id = target.id
    finally:
        db.close()

    verdict = client.get("/api/audit/verify", headers=admin).json()
    assert verdict["valid"] is False
    assert verdict["broken_at"] == broken_id

    db = SessionLocal()
    try:
        row = db.get(AuditLog, broken_id)
        row.path = original_path
        db.commit()
    finally:
        db.close()
    assert client.get("/api/audit/verify", headers=admin).json()["valid"] is True


# ================================================================ 角色与权限点


def test_权限点从路由表自动登记(client, admin):
    """手工维护的清单与真实接口的偏差，是这类系统最难查的问题。"""
    perms = client.get("/api/rbac/permissions", params={"module": "fund"}, headers=admin).json()
    assert perms, "基金模块的写接口应当已被登记为权限点"
    codes = {p["code"] for p in perms}
    assert "POST:/api/fund/pools" in codes
    entry = [p for p in perms if p["code"] == "POST:/api/fund/pools"][0]
    # 代码里 require_roles("director") 的声明被记下来，供"复制内置角色"用
    assert "director" in entry["builtin_roles"]


def test_内置角色齐备且不可删不可停(client, admin):
    roles = client.get("/api/rbac/roles", headers=admin).json()
    builtin = {r["key"] for r in roles if r["builtin"]}
    assert builtin == {"admin", "director", "doctor", "pharmacist", "public_health", "operator"}

    doctor = [r for r in roles if r["key"] == "doctor"][0]
    assert client.delete(f"/api/rbac/roles/{doctor['id']}", headers=admin).status_code == 422
    assert client.patch(f"/api/rbac/roles/{doctor['id']}", json={"active": False},
                        headers=admin).status_code == 422
    # 内置角色的权限来自代码声明，明说而不是显示 0
    detail = client.get(f"/api/rbac/roles/{doctor['id']}/permissions", headers=admin).json()
    assert "代码内 require_roles 声明" in detail["role"]["permission_source"]


def test_自定义角色按权限点授权后生效(client, admin):
    """这是本阶段真正要给的能力：内置六角色之外能配出新角色。"""
    role = client.post(
        "/api/rbac/roles",
        json={"key": "ward_clerk", "name": "病区文员", "description": "只做医废收集"},
        headers=admin,
    )
    assert role.status_code == 201, role.text
    role_id = role.json()["id"]

    client.post(
        "/api/users",
        json={"username": "s11_clerk", "password": "passw0rd1", "role": "ward_clerk"},
        headers=admin,
    )
    clerk = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "s11_clerk", "password": "passw0rd1"}
    ).json()["access_token"]}

    org = client.get("/api/organizations", headers=admin).json()[0]
    body = {"org_id": org["id"], "waste_type": "infectious", "weight_kg": 1.0,
            "collected_date": "2026-08-12"}
    # 未授权：403
    assert client.post("/api/medwaste", json=body, headers=clerk).status_code == 403

    perms = client.get(
        "/api/rbac/permissions", params={"keyword": "/api/medwaste"}, headers=admin
    ).json()
    target = [p for p in perms if p["code"] == "POST:/api/medwaste"][0]
    granted = client.post(
        f"/api/rbac/roles/{role_id}/permissions",
        json={"permission_ids": [target["id"]]}, headers=admin,
    )
    assert granted.json()["granted"] == 1

    # 授权后：放行
    assert client.post("/api/medwaste", json=body, headers=clerk).status_code == 201
    # 没授的接口仍然拦
    assert client.post(
        "/api/medwaste/locations",
        json={"org_id": org["id"], "name": "点位", "location_type": "source"},
        headers=clerk,
    ).status_code == 403


def test_按模块批量授权与撤销(client, admin):
    role = client.get("/api/rbac/roles", headers=admin).json()
    clerk_role = [r for r in role if r["key"] == "ward_clerk"][0]
    granted = client.post(
        f"/api/rbac/roles/{clerk_role['id']}/permissions",
        json={"modules": ["medwaste"]}, headers=admin,
    ).json()
    assert granted["granted"] >= 1
    assert granted["already_had"] >= 1  # 上一条用例已授的那个

    detail = client.get(
        f"/api/rbac/roles/{clerk_role['id']}/permissions", headers=admin
    ).json()
    ids = {p["id"] for p in detail["permissions"]}
    assert len(ids) == granted["total"]

    one = detail["permissions"][0]["id"]
    assert client.delete(
        f"/api/rbac/roles/{clerk_role['id']}/permissions/{one}", headers=admin
    ).status_code == 200
    assert client.delete(
        f"/api/rbac/roles/{clerk_role['id']}/permissions/{one}", headers=admin
    ).status_code == 404


def test_不存在的权限点id单列报出而不是静默忽略(client, admin):
    """静默忽略会让人以为授成功了。"""
    role = [r for r in client.get("/api/rbac/roles", headers=admin).json()
            if r["key"] == "ward_clerk"][0]
    resp = client.post(
        f"/api/rbac/roles/{role['id']}/permissions",
        json={"permission_ids": [99999999]}, headers=admin,
    ).json()
    assert resp["unknown_permission_ids"] == [99999999]
    assert resp["granted"] == 0


def test_内置角色不接受授权表配置(client, admin):
    doctor = [r for r in client.get("/api/rbac/roles", headers=admin).json()
              if r["key"] == "doctor"][0]
    resp = client.post(
        f"/api/rbac/roles/{doctor['id']}/permissions",
        json={"modules": ["medwaste"]}, headers=admin,
    )
    assert resp.status_code == 422


def test_仍有用户在用的自定义角色不可删(client, admin):
    role = [r for r in client.get("/api/rbac/roles", headers=admin).json()
            if r["key"] == "ward_clerk"][0]
    assert client.delete(f"/api/rbac/roles/{role['id']}", headers=admin).status_code == 409


def test_内置角色判定完全照旧(client, admin):
    """零感知升级：这一阶段不应改变任何内置角色的行为。"""
    client.post(
        "/api/users",
        json={"username": "s11_doc", "password": "passw0rd1", "role": "doctor"},
        headers=admin,
    )
    doc = {"Authorization": "Bearer " + client.post(
        "/api/auth/login", json={"username": "s11_doc", "password": "passw0rd1"}
    ).json()["access_token"]}
    org = client.get("/api/organizations", headers=admin).json()[0]
    # 医师不是经办：医废收集仍然 403（走内存比较，不查权限表）
    assert client.post(
        "/api/medwaste",
        json={"org_id": org["id"], "waste_type": "sharp", "weight_kg": 1.0,
              "collected_date": "2026-08-12"},
        headers=doc,
    ).status_code == 403
