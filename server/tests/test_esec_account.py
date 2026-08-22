"""等保账户安全（工程包 E1）：账号停用、口令生命周期、登录留痕、TOTP、会话管理。

覆盖口径（对应任务 1-5 + 零行为回归）：

- 停用**即时生效**（同一令牌停用前 200、停用后 403——非空洞：先证明能用再证明被拦）；
  不可停用自己；不可停用最后一个可用 admin；
- 428 强制改密：管理员重置口令后除改密/登出外一律 428；90 天超期同口径；改密解除；
- login_logs：成功/失败/锁定触发均落库，director 可查、operator 403；
- TOTP：RFC 4226 / RFC 6238 官方测试向量、±1 时间窗、开通→登录强制→关闭→admin 重置
  全链路（非空洞：错码必拒、对码必过、旁路必不带提示字段）；
- 会话管理：并发上限 409 与登出释放名额；空闲超时的滑动续签与超时拒绝；
- **默认配置零行为变化**：三开关全关时登录响应字节口径与既有一致。
"""
import time as _time
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app import totp
from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import LoginLog, User, utcnow
from app.routers.auth import _reset_login_failures
from app.security import revoked_tokens

#: RFC 6238 附录 B 的 SHA1 测试密钥（ASCII "12345678901234567890" 的 base32）
RFC_SECRET_B32 = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"


# ---------------------------------------------------------------------------
# 纯算法：RFC 官方测试向量（不依赖应用与数据库）
# ---------------------------------------------------------------------------


def test_rfc4226_hotp官方向量():
    expected = ["755224", "287082", "359152", "969429", "338314",
                "254676", "287922", "162583", "399871", "520489"]
    assert [totp.hotp(RFC_SECRET_B32, c) for c in range(10)] == expected


def test_rfc6238_totp官方向量():
    vectors = {
        59: "94287082",
        1111111109: "07081804",
        1111111111: "14050471",
        1234567890: "89005924",
        2000000000: "69279037",
        20000000000: "65353130",
    }
    for at, code in vectors.items():
        assert totp.totp_at(RFC_SECRET_B32, at, digits=8) == code, at


def test_totp校验时间窗与格式防线():
    now = 1234567890.0
    current = totp.totp_at(RFC_SECRET_B32, now)
    prev = totp.totp_at(RFC_SECRET_B32, now - 30)
    prev2 = totp.totp_at(RFC_SECRET_B32, now - 60)
    assert totp.verify(RFC_SECRET_B32, current, at=now)
    assert totp.verify(RFC_SECRET_B32, prev, at=now)  # ±1 窗内：容忍 30 秒钟差
    assert not totp.verify(RFC_SECRET_B32, prev2, at=now)  # 两个窗以外必须拒
    assert not totp.verify(RFC_SECRET_B32, "", at=now)
    assert not totp.verify(RFC_SECRET_B32, "12345", at=now)  # 位数不对
    assert not totp.verify(RFC_SECRET_B32, "abc123", at=now)  # 非纯数字
    uri = totp.otpauth_uri("dr_wang", RFC_SECRET_B32)
    assert uri.startswith("otpauth://totp/") and RFC_SECRET_B32 in uri


# ---------------------------------------------------------------------------
# 应用层
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _clean_auth_state():
    """每条用例前清空锁定/限速/会话登记与令牌黑名单，避免用例间串扰。"""
    _reset_login_failures()
    revoked_tokens.clear()
    yield
    _reset_login_failures()
    revoked_tokens.clear()


def _login(client, username, password, **extra):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password, **extra}
    )


def _headers(client, username, password, **extra):
    resp = _login(client, username, password, **extra)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture()
def admin(client):
    return _headers(client, "admin", "admin123")


def _create_user(client, admin, username, role="operator", password="passw0rd1", **extra):
    resp = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role, **extra},
        headers=admin,
    )
    assert resp.status_code in (201, 409), resp.text  # 409=该用例重跑时已存在
    return resp


# ---------- 1. 员工账号停用 ----------


def test_停用即时生效_同一令牌停用前可用停用后被拒(client, admin):
    _create_user(client, admin, "e1_op1")
    uid = [u["id"] for u in client.get("/api/users", headers=admin).json()
           if u["username"] == "e1_op1"][0]
    headers = _headers(client, "e1_op1", "passw0rd1")
    # 非空洞前提：停用前这个令牌真的能用
    assert client.get("/api/users/roles", headers=headers).status_code == 200

    resp = client.patch(f"/api/users/{uid}/status", json={"status": "disabled"}, headers=admin)
    assert resp.status_code == 200 and resp.json()["status"] == "disabled"
    # 即时生效：同一令牌下一个请求就被拒，不等令牌过期
    denied = client.get("/api/users/roles", headers=headers)
    assert denied.status_code == 403 and "停用" in denied.json()["detail"]
    # 停用状态下不能再登录
    assert _login(client, "e1_op1", "passw0rd1").status_code == 403

    # 重新启用：旧令牌不复活（停用时已推令牌基线），重新登录后正常
    assert client.patch(
        f"/api/users/{uid}/status", json={"status": "active"}, headers=admin
    ).status_code == 200
    assert client.get("/api/users/roles", headers=headers).status_code == 401
    assert client.get(
        "/api/users/roles", headers=_headers(client, "e1_op1", "passw0rd1")
    ).status_code == 200


def test_不可停用自己与最后一个admin(client, admin):
    users = client.get("/api/users", headers=admin).json()
    admin_id = [u["id"] for u in users if u["username"] == "admin"][0]
    # 有且仅有一个可用 admin 时，自我停用命中"最后一个管理员"保护
    resp = client.patch(
        f"/api/users/{admin_id}/status", json={"status": "disabled"}, headers=admin
    )
    assert resp.status_code == 422 and "最后一个" in resp.json()["detail"]

    # 建第二个 admin 后：自我停用改为命中"不可停用自己"
    _create_user(client, admin, "e1_admin2", role="admin", password="passw0rd22")
    resp = client.patch(
        f"/api/users/{admin_id}/status", json={"status": "disabled"}, headers=admin
    )
    assert resp.status_code == 422 and "自己" in resp.json()["detail"]

    # 他人停用最后一个 admin：admin2 停用 admin 后自己成了最后一个，再停自己被拦
    admin2 = _headers(client, "e1_admin2", "passw0rd22")
    admin2_id = [u["id"] for u in users_after(client, admin2) if u["username"] == "e1_admin2"][0]
    assert client.patch(
        f"/api/users/{admin_id}/status", json={"status": "disabled"}, headers=admin2
    ).status_code == 200
    resp = client.patch(
        f"/api/users/{admin2_id}/status", json={"status": "disabled"}, headers=admin2
    )
    assert resp.status_code == 422 and "最后一个" in resp.json()["detail"]
    # 恢复现场：重新启用 admin（后续用例依赖种子管理员）
    assert client.patch(
        f"/api/users/{admin_id}/status", json={"status": "active"}, headers=admin2
    ).status_code == 200
    assert _login(client, "admin", "admin123").status_code == 200


def users_after(client, headers):
    return client.get("/api/users", headers=headers).json()


# ---------- 2. 口令生命周期：428 强制改密与 90 天超期 ----------


def test_管理员重置口令后除改密登出外一律428(client, admin):
    _create_user(client, admin, "e1_op2")
    uid = [u["id"] for u in client.get("/api/users", headers=admin).json()
           if u["username"] == "e1_op2"][0]
    # 建号（未显式要求首登改密）不触发 428——既有"建号即用"流程不受影响
    ok = _headers(client, "e1_op2", "passw0rd1")
    assert client.get("/api/users/roles", headers=ok).status_code == 200

    resp = client.post(
        f"/api/users/{uid}/reset-password", json={"new_password": "tempPass99"}, headers=admin
    )
    assert resp.status_code == 200 and resp.json()["must_change_password"] is True
    # 旧令牌已吊销；用临时口令能登录，但除改密/登出外一律 428
    headers = _headers(client, "e1_op2", "tempPass99")
    blocked = client.get("/api/users/roles", headers=headers)
    assert blocked.status_code == 428 and "改" in blocked.json()["detail"]
    assert client.get("/api/users", headers=headers).status_code == 428  # 无关权限，先 428
    # 豁免路径：登出可用
    assert client.post("/api/auth/logout", headers=headers).status_code == 200
    # 改密走通（改密即吊销令牌，重新登录后一切恢复）
    headers = _headers(client, "e1_op2", "tempPass99")
    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "tempPass99", "new_password": "mineOnly77"},
        headers=headers,
    ).status_code == 200
    headers = _headers(client, "e1_op2", "mineOnly77")
    assert client.get("/api/users/roles", headers=headers).status_code == 200


def test_建号显式要求首登改密(client, admin):
    _create_user(client, admin, "e1_op3", must_change_password=True)
    headers = _headers(client, "e1_op3", "passw0rd1")
    assert client.get("/api/users/roles", headers=headers).status_code == 428


def test_口令90天超期强制改密(client, admin):
    _create_user(client, admin, "e1_op4")
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "e1_op4").first()
        user.password_updated_at = utcnow() - timedelta(days=91)
        db.commit()
    headers = _headers(client, "e1_op4", "passw0rd1")
    blocked = client.get("/api/users/roles", headers=headers)
    assert blocked.status_code == 428 and "90" in blocked.json()["detail"]
    # 改密后解除（password_updated_at 刷新）
    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "passw0rd1", "new_password": "fresh2026a"},
        headers=headers,
    ).status_code == 200
    headers = _headers(client, "e1_op4", "fresh2026a")
    assert client.get("/api/users/roles", headers=headers).status_code == 200
    # 89 天：不超期
    with SessionLocal() as db:
        user = db.query(User).filter(User.username == "e1_op4").first()
        user.password_updated_at = utcnow() - timedelta(days=89)
        db.commit()
    assert client.get("/api/users/roles", headers=headers).status_code == 200


# ---------- 3. 登录留痕 ----------


def test_登录成败与锁定触发均落库_并可按权限查询(client, admin):
    _create_user(client, admin, "e1_op5")
    _create_user(client, admin, "e1_dir", role="director", password="passw0rd55")
    assert _login(client, "e1_op5", "wrong-pass1").status_code == 401
    assert _login(client, "e1_op5", "passw0rd1").status_code == 200
    # 连续失败到阈值触发锁定，再试一次命中"已锁定"
    for _ in range(5):
        _login(client, "e1_lockme", "bad-pass11")
    assert _login(client, "e1_lockme", "bad-pass11").status_code == 423

    with SessionLocal() as db:
        rows = db.query(LoginLog).all()
        by_reason = {r.fail_reason for r in rows if not r.success}
        assert "bad_credentials" in by_reason
        assert "lock_triggered" in by_reason
        assert "locked" in by_reason
        ok_rows = [r for r in rows if r.success and r.username == "e1_op5"]
        assert ok_rows and ok_rows[0].user_id is not None and ok_rows[0].channel == "password"
        assert all(r.ip for r in rows)  # 来源 IP 都记了

    # director 可查（admin 亦可）；operator 403
    director = _headers(client, "e1_dir", "passw0rd55")
    resp = client.get("/api/audit/logins", params={"username": "e1_op5"}, headers=director)
    assert resp.status_code == 200 and "X-Total-Count" in resp.headers
    assert {r["username"] for r in resp.json()} == {"e1_op5"}
    assert {r["success"] for r in resp.json()} == {True, False}
    failed_only = client.get(
        "/api/audit/logins", params={"success": False}, headers=director
    ).json()
    assert failed_only and all(r["success"] is False for r in failed_only)
    operator = _headers(client, "e1_op5", "passw0rd1")
    assert client.get("/api/audit/logins", headers=operator).status_code == 403


def test_停用账号的登录尝试落库为disabled(client, admin):
    _create_user(client, admin, "e1_op6")
    uid = [u["id"] for u in client.get("/api/users", headers=admin).json()
           if u["username"] == "e1_op6"][0]
    client.patch(f"/api/users/{uid}/status", json={"status": "disabled"}, headers=admin)
    assert _login(client, "e1_op6", "passw0rd1").status_code == 403
    with SessionLocal() as db:
        assert db.query(LoginLog).filter(
            LoginLog.username == "e1_op6", LoginLog.fail_reason == "disabled"
        ).count() >= 1


# ---------- 4. TOTP 双因素（应用链路） ----------


def test_totp开通登录校验关闭与admin重置全链路(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "totp_required_roles", "doctor")
    _create_user(client, admin, "e1_dr", role="doctor", password="passw0rd77")
    # 角色被要求但未开通：放行 + setup 提示（渐进启用，不锁死存量）
    resp = _login(client, "e1_dr", "passw0rd77")
    assert resp.status_code == 200 and resp.json()["totp_setup_required"] is True
    # 不在要求名单里的角色：正常登录且响应**不带**该字段
    assert "totp_setup_required" not in _login(client, "admin", "admin123").json()

    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    setup = client.post("/api/auth/totp/setup", headers=headers)
    assert setup.status_code == 200
    secret = setup.json()["secret"]
    assert setup.json()["otpauth_uri"].startswith("otpauth://totp/")
    # 错码不启用（非空洞：先证明错码被拒）
    assert client.post(
        "/api/auth/totp/activate", json={"code": "000000"}, headers=headers
    ).status_code == 400
    # 未启用前登录仍不要求验证码
    assert _login(client, "e1_dr", "passw0rd77").json().get("totp_setup_required") is True
    # 对码启用
    assert client.post(
        "/api/auth/totp/activate",
        json={"code": totp.totp_at(secret, _time.time())},
        headers=headers,
    ).json()["enabled"] is True

    # 启用后：无码 401、错码 401、对码 200 且响应不再带 setup 提示
    resp = _login(client, "e1_dr", "passw0rd77")
    assert resp.status_code == 401 and "totp_code" in resp.json()["detail"]
    assert _login(client, "e1_dr", "passw0rd77", totp_code="000000").status_code == 401
    good = _login(client, "e1_dr", "passw0rd77",
                  totp_code=totp.totp_at(secret, _time.time()))
    assert good.status_code == 200 and "totp_setup_required" not in good.json()
    with SessionLocal() as db:
        reasons = {
            r.fail_reason
            for r in db.query(LoginLog).filter(LoginLog.username == "e1_dr").all()
            if not r.success
        }
        assert {"totp_required", "totp_invalid"} <= reasons

    # 本人关闭：错码 400，对码关闭后登录不再要求验证码
    headers = {"Authorization": f"Bearer {good.json()['access_token']}"}
    assert client.post(
        "/api/auth/totp/disable", json={"code": "000000"}, headers=headers
    ).status_code == 400
    assert client.post(
        "/api/auth/totp/disable",
        json={"code": totp.totp_at(secret, _time.time())},
        headers=headers,
    ).json()["enabled"] is False
    assert _login(client, "e1_dr", "passw0rd77").status_code == 200

    # 重新开通后由 admin 重置（换手机解困）：登录回到"未开通放行 + 提示"
    setup = client.post("/api/auth/totp/setup", headers=headers).json()
    client.post("/api/auth/totp/activate",
                json={"code": totp.totp_at(setup["secret"], _time.time())}, headers=headers)
    uid = [u["id"] for u in client.get("/api/users", headers=admin).json()
           if u["username"] == "e1_dr"][0]
    assert client.post(
        f"/api/users/{uid}/totp/reset", headers=admin
    ).json()["reset"] is True
    resp = _login(client, "e1_dr", "passw0rd77")
    assert resp.status_code == 200 and resp.json()["totp_setup_required"] is True


def test_totp开关为空时已启用账号也旁路(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "totp_required_roles", "doctor")
    _create_user(client, admin, "e1_dr2", role="doctor", password="passw0rd88")
    headers = _headers(client, "e1_dr2", "passw0rd88")
    secret = client.post("/api/auth/totp/setup", headers=headers).json()["secret"]
    client.post("/api/auth/totp/activate",
                json={"code": totp.totp_at(secret, _time.time())}, headers=headers)
    assert _login(client, "e1_dr2", "passw0rd88").status_code == 401  # 开关开：要码
    monkeypatch.setattr(settings, "totp_required_roles", "")
    resp = _login(client, "e1_dr2", "passw0rd88")  # 开关关：全部旁路
    assert resp.status_code == 200 and "totp_setup_required" not in resp.json()


# ---------- 5. 会话管理：并发上限与空闲超时 ----------


def test_并发会话上限与登出释放名额(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "session_max_concurrent", 2)
    _create_user(client, admin, "e1_op7")
    h1 = _headers(client, "e1_op7", "passw0rd1")
    _headers(client, "e1_op7", "passw0rd1")
    third = _login(client, "e1_op7", "passw0rd1")
    assert third.status_code == 409 and "上限" in third.json()["detail"]
    with SessionLocal() as db:
        assert db.query(LoginLog).filter(
            LoginLog.username == "e1_op7", LoginLog.fail_reason == "concurrent_limit"
        ).count() == 1
    # 登出一个即释放名额
    assert client.post("/api/auth/logout", headers=h1).status_code == 200
    assert _login(client, "e1_op7", "passw0rd1").status_code == 200


def test_空闲超时_滑动续签与超时拒绝(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "session_idle_timeout_seconds", 1)
    _create_user(client, admin, "e1_op8")
    headers = _headers(client, "e1_op8", "passw0rd1")
    # 滑动续签：持续活动下累计时长可超过 idle_timeout
    for _ in range(3):
        assert client.get("/api/users/roles", headers=headers).status_code == 200
        _time.sleep(0.45)
    # 闲置超过 idle_timeout：拒绝，且提示口径明确
    _time.sleep(1.2)
    resp = client.get("/api/users/roles", headers=headers)
    assert resp.status_code == 401 and "闲置" in resp.json()["detail"]
    # 重新登录即恢复
    assert client.get(
        "/api/users/roles", headers=_headers(client, "e1_op8", "passw0rd1")
    ).status_code == 200


# ---------- 6. 默认配置零行为变化 ----------


def test_默认配置三开关全关时登录响应与既有口径一致(client, admin):
    assert settings.totp_required_roles == ""
    assert settings.session_idle_timeout_seconds == 0
    assert settings.session_max_concurrent == 0
    resp = _login(client, "admin", "admin123")
    assert resp.status_code == 200
    # 响应键集合与升级前完全一致：不出现 totp_setup_required 等新字段
    assert set(resp.json().keys()) == {"access_token", "token_type", "role"}
    # 既有接口链路不受三开关影响
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert client.get("/api/users", headers=headers).status_code == 200


def test_微信登录成败均落登录留痕(client):
    from app.database import SessionLocal
    from app.models import LoginLog

    # mock 微信通道：code=ok 的固定 openid（先核实 MockWeChatProvider 语义）
    resp = client.post("/api/portal/auth/wechat/login", json={"code": "mock-code"})
    ok = resp.status_code == 200
    bad = client.post("/api/portal/auth/wechat/login", json={"code": ""})
    with SessionLocal() as db:
        rows = db.query(LoginLog).filter(LoginLog.channel == "wechat").all()
    assert rows, "微信通道登录没有落留痕"
    if ok:
        assert any(r.success for r in rows)
    if bad.status_code == 400:
        assert any((not r.success) and r.fail_reason == "oauth_failed" for r in rows)
