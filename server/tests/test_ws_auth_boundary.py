"""WebSocket 通道的认证边界：与 HTTP 侧同一份准入判定。

修复前 `ws._token_valid()` 只验「签名 + 过期 + 登出黑名单」，不查 `users.status`、
不判改密基线、不拒居民端令牌——同一枚令牌 HTTP 侧 403/401、WS 侧却能建连并收到
本机构的危急值 / 缺药**定向广播**。本文件按"先证明能用、再证明被拦"的口径钉住：

- 每条拒绝用例都带 **HTTP 侧对照断言**，证明拒的是同一枚令牌、同一个理由；
- 停用/改密类用例先跑通一次正常建连，排除"根本连不上"这种空洞通过；
- 三条握手路径（query token / 首帧 / Cookie 兜底）逐条覆盖；
- 长连接存活期：已建连的连接在账号被停用后，下一次广播投递前即被踢下线。
"""
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from conftest import reset_database

import app.ws as ws_mod
from app.main import app
from app.security import AUTH_COOKIE, COOKIE_MODE_HEADER


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(autouse=True)
def _no_admission_cache(monkeypatch):
    """存活期复核默认缓存 30 秒；用例里的停用/改密是"刚刚发生"的，
    把窗口设为 0 让每次复核都现查，否则测的是缓存而不是判定。"""
    monkeypatch.setattr(ws_mod, "_REVALIDATE_TTL_SECONDS", 0.0)
    ws_mod._admission_cache.clear()
    yield
    ws_mod._admission_cache.clear()


def _make_user(client, admin, username, password="Passw0rd1", role="doctor", org_id=None):
    body = {"username": username, "password": password, "role": role}
    if org_id is not None:
        body["org_id"] = org_id
    resp = client.post("/api/users", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _login_token(client, username, password="Passw0rd1") -> str:
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _ws_admitted(client, token: str) -> bool:
    """握手是否被采信：建连后广播一条，收到即放行，1008 断开即被拒。

    直接看 `manager.active` 会有竞态（登记发生在服务端协程里），所以用"能不能
    收到广播"这个业务可观测的结果判定——这正是缺陷本身的危害面。
    """
    try:
        with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
            deadline = time.time() + 5
            while not ws_mod.manager.active and time.time() < deadline:
                time.sleep(0.02)
            ws_mod.manager.broadcast({"type": "ws_auth_probe"})
            return ws.receive_json().get("type") == "ws_auth_probe"
    except WebSocketDisconnect:
        return False


# ---------------------------------------------------------------------------
# 1. 停用账号
# ---------------------------------------------------------------------------


def test_停用账号_WS被拒且与HTTP侧同一口径(client, admin):
    user = _make_user(client, admin, "ws_disabled")
    token = _login_token(client, "ws_disabled")

    # 先证明这枚令牌本来两侧都能用（非空洞：排除"它本来就连不上"）
    assert client.get("/api/users/roles", headers={"Authorization": f"Bearer {token}"}).status_code == 200
    assert _ws_admitted(client, token) is True

    assert client.patch(
        f"/api/users/{user['id']}/status", json={"status": "disabled"}, headers=admin
    ).status_code == 200

    # HTTP 侧对照：同一枚令牌 403「账号已停用」
    http = client.get("/api/users/roles", headers={"Authorization": f"Bearer {token}"})
    assert http.status_code == 403 and "停用" in http.json()["detail"]
    # WS 侧必须同样拒绝，且连接不进登记表（不参与任何定向广播）
    assert _ws_admitted(client, token) is False
    assert ws_mod.manager.active == {}


def test_停用账号_首帧鉴权与Cookie兜底路径同样被拒(client, admin):
    """三条握手路径口径一致：query token 之外的两条也不能成为绕过口。"""
    user = _make_user(client, admin, "ws_disabled2")

    # Cookie 兜底路径：先证明启用状态下 Cookie 能建连
    cookie_client = TestClient(app)
    with cookie_client:
        resp = cookie_client.post(
            "/api/auth/login",
            json={"username": "ws_disabled2", "password": "Passw0rd1"},
            headers={COOKIE_MODE_HEADER: "cookie"},
        )
        assert resp.status_code == 200 and cookie_client.cookies.get(AUTH_COOKIE)
        with cookie_client.websocket_connect("/ws/notifications") as ws:
            deadline = time.time() + 5
            while not ws_mod.manager.active and time.time() < deadline:
                time.sleep(0.02)
            assert ws_mod.manager.active, "Cookie 兜底在启用状态下本应建连成功"
            ws_mod.manager.broadcast({"type": "cookie_ok"})
            assert ws.receive_json()["type"] == "cookie_ok"

        assert client.patch(
            f"/api/users/{user['id']}/status", json={"status": "disabled"}, headers=admin
        ).status_code == 200

        # 停用后 Cookie 不再被采信 → 回退首帧鉴权 → 首帧同样是停用令牌 → 1008
        # 收一条广播来判定：被拒则 receive 抛断开，被误采信则收到 probe（而不是
        # 干等到超时——回归时要的是一条明确的红，不是挂住的用例）
        token = cookie_client.cookies.get(AUTH_COOKIE)
        with pytest.raises(WebSocketDisconnect):
            with cookie_client.websocket_connect("/ws/notifications") as ws:
                ws.send_text(token)  # 首帧鉴权路径
                ws_mod.manager.broadcast({"type": "should_not_arrive"})
                assert ws.receive_json() is None, "停用账号经 Cookie/首帧路径仍收到了广播"
        assert ws_mod.manager.active == {}


def test_存活期复核_已建连的连接在账号停用后被踢下线(client, admin):
    """握手时校验挡不住"沉默的长连接"：客户端可以一条心跳都不发。
    投递前复核保证停用之后的下一条广播不会送到这个连接上。"""
    user = _make_user(client, admin, "ws_live_kick")
    token = _login_token(client, "ws_live_kick")

    with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
        deadline = time.time() + 5
        while not ws_mod.manager.active and time.time() < deadline:
            time.sleep(0.02)
        # 连接活着，广播收得到
        ws_mod.manager.broadcast({"type": "before_disable"})
        assert ws.receive_json()["type"] == "before_disable"

        client.patch(
            f"/api/users/{user['id']}/status", json={"status": "disabled"}, headers=admin
        )
        # 这里不发任何心跳，直接广播：连接应在投递前被复核踢掉
        ws_mod.manager.broadcast({"type": "after_disable"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    assert ws_mod.manager.active == {}


# ---------------------------------------------------------------------------
# 2. 居民端令牌（scope=portal）
# ---------------------------------------------------------------------------


def test_居民端令牌_WS被拒且与HTTP侧同一口径(client, admin):
    from app.database import SessionLocal
    from app.models import SmsCode

    phone = "13900008888"
    with SessionLocal() as db:  # 绕过 60 秒下发冷却
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    portal_token = client.post(
        "/api/portal/auth/sms/login", json={"phone": phone, "code": code}
    ).json()["access_token"]

    # 居民端令牌自己的接口是通的（非空洞：证明令牌本身有效，被拒的是"作用域"）
    assert client.get(
        "/api/portal/me", headers={"Authorization": f"Bearer {portal_token}"}
    ).status_code == 200
    # HTTP 侧对照：401「居民端令牌不可用于业务接口」
    http = client.get("/api/users/roles", headers={"Authorization": f"Bearer {portal_token}"})
    assert http.status_code == 401 and "居民端令牌" in http.json()["detail"]
    # WS 侧同样拒绝——此前它能建连，且因 org_id 为空而收到全部无定向广播
    assert _ws_admitted(client, portal_token) is False
    assert ws_mod.manager.active == {}


# ---------------------------------------------------------------------------
# 3. 改密基线
# ---------------------------------------------------------------------------


def test_改密后_旧令牌WS被拒且与HTTP侧同一口径(client, admin):
    _make_user(client, admin, "ws_changepw")
    token = _login_token(client, "ws_changepw")
    headers = {"Authorization": f"Bearer {token}"}

    assert _ws_admitted(client, token) is True  # 改密前建连正常

    assert client.post(
        "/api/auth/change-password",
        json={"current_password": "Passw0rd1", "new_password": "Passw0rd2"},
        headers=headers,
    ).status_code == 200

    http = client.get("/api/users/roles", headers=headers)
    assert http.status_code == 401 and "密码已修改" in http.json()["detail"]
    assert _ws_admitted(client, token) is False
    assert ws_mod.manager.active == {}

    # 新令牌照常可用：拒的是旧基线，不是把这个账号一起关在门外
    assert _ws_admitted(client, _login_token(client, "ws_changepw", "Passw0rd2")) is True


def test_心跳复核_账号停用后下一次心跳即断开(client, admin):
    """心跳路径**现查**，不吃投递前复核的那份缓存——`_authorize(cached=False)`。
    与 test_p0_fixes 里"登出后下一次心跳即断开"同一条语义，这里换成停用。"""
    user = _make_user(client, admin, "ws_heartbeat")
    token = _login_token(client, "ws_heartbeat")
    with client.websocket_connect(f"/ws/notifications?token={token}") as ws:
        deadline = time.time() + 5
        while not ws_mod.manager.active and time.time() < deadline:
            time.sleep(0.02)
        ws.send_text("ping")  # 停用前的心跳不该断开
        ws_mod.manager.broadcast({"type": "still_alive"})
        assert ws.receive_json()["type"] == "still_alive"

        client.patch(
            f"/api/users/{user['id']}/status", json={"status": "disabled"}, headers=admin
        )
        ws.send_text("ping")
        with pytest.raises(WebSocketDisconnect):
            ws.receive_text()


def test_登出令牌WS被拒_原有黑名单口径未回退(client, admin):
    """回归：改用共享判定后，原先唯一生效的那条（登出黑名单）不能丢。"""
    _make_user(client, admin, "ws_logout")
    token = _login_token(client, "ws_logout")
    assert _ws_admitted(client, token) is True
    assert client.post(
        "/api/auth/logout", headers={"Authorization": f"Bearer {token}"}
    ).status_code == 200
    assert _ws_admitted(client, token) is False
