"""会话生命周期的"解困通道"：推令牌基线之后，用户必须还能重新进来。

平台有两处会把用户彻底锁死，两处的根因都是"令牌作废了，可它占的东西没跟着放掉"：

1. **并发会话上限 × 强制改密**：`change_password` / `admin_reset_password` /
   `set_user_status(disabled)` 推 `token_valid_from` 让旧令牌失效，却不清
   `SessionRegistry` 里的活跃令牌登记。上限为 1 时用户改完密码登不进来（409
   会话已达上限），旧令牌又过不了基线校验所以也登不出去（401）——名额要占满令牌
   自然寿命（8 小时）才由空闲淘汰释放，中间没有任何自助出口。
2. **Cookie 模式改密后的死 Cookie**：令牌在 HttpOnly Cookie 里，前端读不到也删
   不掉；改密后浏览器一直回带一枚废令牌，业务接口 401，连 logout 都 401。

两组用例都按"先复现死局的前提、再证明出口存在"写，不只断言最后一次登录 200。
"""
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.main import app
from app.routers.auth import _reset_login_failures
from app.security import AUTH_COOKIE, COOKIE_MODE_HEADER, CSRF_COOKIE, CSRF_HEADER


@pytest.fixture(autouse=True)
def _clean_sessions():
    _reset_login_failures()
    yield
    _reset_login_failures()


def _make_user(client, admin, username, password="Passw0rd1", role="doctor"):
    resp = client.post(
        "/api/users",
        json={"username": username, "password": password, "role": role},
        headers=admin,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login(client, username, password):
    return client.post("/api/auth/login", json={"username": username, "password": password})


# ---------------------------------------------------------------------------
# 应修 A：并发会话上限 × 推基线
# ---------------------------------------------------------------------------


def test_并发上限为1时_改密后可立即重新登录(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "session_max_concurrent", 1)
    _make_user(client, admin, "sr_changepw")

    token = _login(client, "sr_changepw", "Passw0rd1").json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    # 名额确实被占满（非空洞：证明这不是"上限没生效所以随便登"）
    assert _login(client, "sr_changepw", "Passw0rd1").status_code == 409

    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "Passw0rd1", "new_password": "Passw0rd2"},
        headers=headers,
    ).status_code == 200
    # 旧令牌确实已废：登出这条自助出口在改密后是走不通的（死局的另一半）
    assert client.post("/api/auth/logout", headers=headers).status_code == 401

    # 修复点：改密即释放名额，用户拿新口令直接进得来
    resp = _login(client, "sr_changepw", "Passw0rd2")
    assert resp.status_code == 200, resp.text
    new_headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    assert client.get("/api/users/roles", headers=new_headers).status_code == 200
    # 新会话仍然按 1 个名额计——释放不等于把上限一起放掉
    assert _login(client, "sr_changepw", "Passw0rd2").status_code == 409
    assert client.post("/api/auth/logout", headers=new_headers).status_code == 200


def test_并发上限为1时_管理员重置口令后用户可登录(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "session_max_concurrent", 1)
    user = _make_user(client, admin, "sr_reset")

    _login(client, "sr_reset", "Passw0rd1")  # 占满名额
    assert _login(client, "sr_reset", "Passw0rd1").status_code == 409

    assert client.post(
        f"/api/users/{user['id']}/reset-password",
        json={"new_password": "Passw0rd9"},
        headers=admin,
    ).status_code == 200
    # 重置的常见场景是"号可能已失陷"，用户必须能拿临时口令立刻进来改密
    assert _login(client, "sr_reset", "Passw0rd9").status_code == 200


def test_并发上限为1时_停用再启用后用户可登录(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "session_max_concurrent", 1)
    user = _make_user(client, admin, "sr_status")

    _login(client, "sr_status", "Passw0rd1")
    assert _login(client, "sr_status", "Passw0rd1").status_code == 409

    assert client.patch(
        f"/api/users/{user['id']}/status", json={"status": "disabled"}, headers=admin
    ).status_code == 200
    assert _login(client, "sr_status", "Passw0rd1").status_code == 403  # 停用期间当然进不来
    assert client.patch(
        f"/api/users/{user['id']}/status", json={"status": "active"}, headers=admin
    ).status_code == 200
    # 重新启用后不该还要先撞一轮 409
    assert _login(client, "sr_status", "Passw0rd1").status_code == 200


def test_clear_user只清目标账号_不误伤他人名额(monkeypatch):
    """SessionRegistry.clear_user 的定点断言：别把整张表清了。"""
    from app.state_store import SessionRegistry

    registry = SessionRegistry(default_ttl_seconds=600)
    registry.register("alice", "jti-a1")
    registry.register("alice", "jti-a2")
    registry.register("bob", "jti-b1")
    registry.touch("jti-a1")
    registry.touch("jti-b1")
    assert registry.active_count("alice") == 2

    registry.clear_user("alice")
    assert registry.active_count("alice") == 0
    assert registry.last_seen("jti-a1") is None
    assert registry.active_count("bob") == 1
    assert registry.last_seen("jti-b1") is not None
    registry.clear_user("nobody")  # 不存在的账号是空操作，不抛


# ---------------------------------------------------------------------------
# 应修 B：Cookie 模式改密后的死 Cookie
# ---------------------------------------------------------------------------


def _cookie_login(cookie_client, username, password):
    return cookie_client.post(
        "/api/auth/login",
        json={"username": username, "password": password},
        headers={COOKIE_MODE_HEADER: "cookie"},
    )


def test_Cookie模式改密后会话续期且无残留死Cookie(client, admin):
    _make_user(client, admin, "sr_cookie")
    cookie_client = TestClient(app)
    with cookie_client:
        assert _cookie_login(cookie_client, "sr_cookie", "Passw0rd1").status_code == 200
        csrf = cookie_client.cookies.get(CSRF_COOKIE)
        old_token = cookie_client.cookies.get(AUTH_COOKIE)
        assert cookie_client.get("/api/users/roles").status_code == 200

        assert cookie_client.post(
            "/api/auth/change-password",
            json={"current_password": "Passw0rd1", "new_password": "Passw0rd2"},
            headers={CSRF_HEADER: csrf},
        ).status_code == 200

        # 令牌 Cookie 已被换成新签的一枚，会话没断
        assert cookie_client.cookies.get(AUTH_COOKIE) != old_token
        assert cookie_client.get("/api/users/roles").status_code == 200
        # CSRF 不轮换：改密前就持有的双提交值仍然可用（并发写请求不被误杀）
        assert cookie_client.cookies.get(CSRF_COOKIE) == csrf

        # 登出这条自助出口通了，且 Cookie 被真正清干净
        assert cookie_client.post(
            "/api/auth/logout", headers={CSRF_HEADER: csrf}
        ).status_code == 200
        assert cookie_client.cookies.get(AUTH_COOKIE) is None
        assert cookie_client.cookies.get(CSRF_COOKIE) is None
        assert cookie_client.get("/api/users/roles").status_code == 401

        # 用新口令重新登录一切正常
        assert _cookie_login(cookie_client, "sr_cookie", "Passw0rd2").status_code == 200


def test_Header模式改密不下发Cookie_行为不变(client, admin):
    """续期只针对 Cookie 会话：Header 对接方的改密响应不能凭空多出 Set-Cookie。"""
    _make_user(client, admin, "sr_header")
    token = _login(client, "sr_header", "Passw0rd1").json()["access_token"]
    resp = client.post(
        "/api/auth/change-password",
        json={"current_password": "Passw0rd1", "new_password": "Passw0rd2"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    assert resp.json() == {"changed": True, "tokens_revoked": True}
    assert "set-cookie" not in {k.lower() for k in resp.headers}


def test_Cookie模式并发上限下改密不占双份名额(client, admin, monkeypatch):
    """续期会重新登记一枚令牌——不登记则上限失守，登记两次则用户又被锁。"""
    monkeypatch.setattr(settings, "session_max_concurrent", 1)
    _make_user(client, admin, "sr_cookie_limit")
    cookie_client = TestClient(app)
    with cookie_client:
        assert _cookie_login(cookie_client, "sr_cookie_limit", "Passw0rd1").status_code == 200
        csrf = cookie_client.cookies.get(CSRF_COOKIE)
        assert cookie_client.post(
            "/api/auth/change-password",
            json={"current_password": "Passw0rd1", "new_password": "Passw0rd2"},
            headers={CSRF_HEADER: csrf},
        ).status_code == 200
        # 续期的会话占住了唯一的名额（上限没被绕过）
        assert _login(client, "sr_cookie_limit", "Passw0rd2").status_code == 409
        # 而它是活的、能登出，登出后名额立即回来（没被登记成两份）
        assert cookie_client.post(
            "/api/auth/logout", headers={CSRF_HEADER: csrf}
        ).status_code == 200
        assert _login(client, "sr_cookie_limit", "Passw0rd2").status_code == 200
