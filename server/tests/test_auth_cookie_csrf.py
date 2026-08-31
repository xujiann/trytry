"""G3 令牌 HttpOnly Cookie 化 + CSRF 双提交防线（P1-23 收口）。

双模会话的行为契约，业务端与居民端各一套（Cookie 名独立）：

- **Header 模式**（既有对接方 / 迁移期前端兜底）：登录不带 X-Token-Transport
  头时**不下发任何 Cookie**，行为与升级前逐字节一致，写请求无 CSRF 要求——
  全部既有测试与对接方脚本的兼容性靠这一条；
- **Cookie 模式**（前端登录时带 `X-Token-Transport: cookie` 声明）：令牌进
  HttpOnly Cookie（JS 读不到），CSRF token 进非 HttpOnly Cookie；写请求
  （POST/PUT/PATCH/DELETE）必须带与 CSRF Cookie 一致的 X-CSRF-Token 头，
  否则 403（读请求不强制）；登出清 Cookie 并拉黑令牌；生产环境 Cookie 带 Secure。

非空洞性：把 deps.token_from_request 里的 CSRF 校验删掉，
test_cookie会话_写请求必须过CSRF双提交 的"缺头 403"断言必红。
"""
import time

import pytest
from starlette.websockets import WebSocketDisconnect

from app.config import settings
from app.models import SmsCode
from app.routers.auth import _reset_login_failures
from app.routers.portal import _reset_portal_failures
from app.security import (
    AUTH_COOKIE,
    COOKIE_MODE_HEADER,
    CSRF_COOKIE,
    CSRF_HEADER,
    PORTAL_AUTH_COOKIE,
    PORTAL_CSRF_COOKIE,
)

from conftest import login

COOKIE_MODE = {COOKIE_MODE_HEADER: "cookie"}


@pytest.fixture(autouse=True)
def clean_state(client):
    """每个用例前清空 Cookie 罐与锁定/限流状态：用例之间互不串会话。"""
    client.cookies.clear()
    _reset_login_failures()
    _reset_portal_failures()
    yield
    client.cookies.clear()


@pytest.fixture(scope="module")
def admin_headers(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def patient(client, admin_headers):
    """供实名绑定的患者档案。电话与登录手机号不同，避免登录时自动绑定。"""
    resp = client.post(
        "/api/patients",
        json={"name": "居民甲", "id_card": "330782199201014321", "phone": "13711112222"},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    return resp.json()


def _set_cookie_header(resp, name):
    """从响应里取指定 Cookie 的 Set-Cookie 原始头（无则 None）。"""
    for header in resp.headers.get_list("set-cookie"):
        if header.startswith(f"{name}="):
            return header
    return None


def _admin_cookie_login(client):
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers=COOKIE_MODE,
    )
    assert resp.status_code == 200
    return resp


def _portal_cookie_login(client, phone="13700009999"):
    """居民端 Cookie 模式登录（短信通道，console 桩回显验证码）。"""
    from app.database import SessionLocal

    with SessionLocal() as db:  # 绕过 60 秒下发冷却（与 test_portal_auth 同一套路）
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": phone, "purpose": "login"}
    ).json()["debug_code"]
    resp = client.post(
        "/api/portal/auth/sms/login",
        json={"phone": phone, "code": code},
        headers=COOKIE_MODE,
    )
    assert resp.status_code == 200
    return resp


# ---------------------------------------------------------------- 业务端


def test_cookie登录_下发HttpOnly令牌Cookie_响应体不变(client):
    resp = _admin_cookie_login(client)
    body = resp.json()
    # 响应体与 Header 模式完全一致：access_token 照常返回
    assert body["access_token"] and body["role"] == "admin"
    token_cookie = _set_cookie_header(resp, AUTH_COOKIE)
    csrf_cookie = _set_cookie_header(resp, CSRF_COOKIE)
    assert token_cookie is not None and csrf_cookie is not None
    low = token_cookie.lower()
    assert "httponly" in low and "samesite=lax" in low and "path=/" in low
    assert "secure" not in low  # 开发环境不加 Secure
    # CSRF Cookie 必须非 HttpOnly：前端 JS 要读出来放进 X-CSRF-Token 头
    assert "httponly" not in csrf_cookie.lower()
    # Cookie 里的令牌就是响应体里的那一枚（同一会话、同一 jti）
    assert client.cookies.get(AUTH_COOKIE) == body["access_token"]


def test_header登录_不下发任何Cookie(client):
    """不带 X-Token-Transport 声明的登录（全部既有对接方）行为与升级前一致。"""
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200
    assert resp.headers.get_list("set-cookie") == []


def test_cookie会话_读接口无需header(client):
    _admin_cookie_login(client)
    # 无 Authorization 头，仅凭 Cookie（TestClient 自动回带）
    assert client.get("/api/organizations").status_code == 200


def test_cookie会话_写请求必须过CSRF双提交(client):
    _admin_cookie_login(client)
    payload = {"name": "CSRF测试卫生院", "org_type": "township", "level": "township"}
    # 缺 X-CSRF-Token → 403（非空洞锚点：删掉 CSRF 校验此断言必红）
    resp = client.post("/api/organizations", json=payload)
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]
    # 带错的 → 403
    resp = client.post(
        "/api/organizations", json=payload, headers={CSRF_HEADER: "not-the-right-token"}
    )
    assert resp.status_code == 403
    # 带对的（与 CSRF Cookie 一致）→ 正常业务响应
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    resp = client.post("/api/organizations", json=payload, headers={CSRF_HEADER: csrf})
    assert resp.status_code == 201


def test_header模式_写请求无CSRF要求(client, admin_headers):
    """Header 模式没有"浏览器自动携带"问题，完全不做 CSRF 校验。"""
    resp = client.post(
        "/api/organizations",
        json={"name": "Header模式卫生院", "org_type": "township", "level": "township"},
        headers=admin_headers,
    )
    assert resp.status_code == 201


def test_cookie登出_清Cookie并吊销令牌(client):
    _admin_cookie_login(client)
    assert client.get("/api/organizations").status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE)
    resp = client.post("/api/auth/logout", headers={CSRF_HEADER: csrf})
    assert resp.status_code == 200
    # 登出响应清掉两个会话 Cookie（Max-Age=0 / 过期）
    cleared = _set_cookie_header(resp, AUTH_COOKIE)
    assert cleared is not None and ("max-age=0" in cleared.lower() or "expires=" in cleared.lower())
    assert _set_cookie_header(resp, CSRF_COOKIE) is not None
    # 令牌已进黑名单：即使 Cookie 残留也不再可用
    assert client.get("/api/organizations").status_code == 401


def test_生产环境_SetCookie带Secure(client, monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    resp = _admin_cookie_login(client)
    for name in (AUTH_COOKIE, CSRF_COOKIE):
        assert "secure" in _set_cookie_header(resp, name).lower()


# ---------------------------------------------------------------- 居民端（同套断言）


def test_portal_cookie登录_下发独立命名的Cookie(client, patient):
    resp = _portal_cookie_login(client)
    assert resp.json()["access_token"]  # 响应体保持原样
    token_cookie = _set_cookie_header(resp, PORTAL_AUTH_COOKIE)
    csrf_cookie = _set_cookie_header(resp, PORTAL_CSRF_COOKIE)
    assert token_cookie is not None and "httponly" in token_cookie.lower()
    assert csrf_cookie is not None and "httponly" not in csrf_cookie.lower()


def test_portal_header登录_不下发Cookie(client, patient):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == "13700009999").delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13700009999", "purpose": "login"}
    ).json()["debug_code"]
    resp = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13700009999", "code": code}
    )
    assert resp.status_code == 200
    assert resp.headers.get_list("set-cookie") == []


def test_portal_cookie会话_读接口无需header(client, patient):
    _portal_cookie_login(client)
    assert client.get("/api/portal/me").status_code == 200


def test_portal_cookie会话_写请求必须过CSRF双提交(client, patient):
    _portal_cookie_login(client)
    payload = {"name": "居民甲", "id_card": "330782199201014321"}
    resp = client.post("/api/portal/auth/realname", json=payload)
    assert resp.status_code == 403
    assert "CSRF" in resp.json()["detail"]
    resp = client.post(
        "/api/portal/auth/realname", json=payload, headers={CSRF_HEADER: "wrong-token"}
    )
    assert resp.status_code == 403
    csrf = client.cookies.get(PORTAL_CSRF_COOKIE)
    assert csrf
    resp = client.post("/api/portal/auth/realname", json=payload, headers={CSRF_HEADER: csrf})
    assert resp.status_code == 200 and resp.json()["bound"] is True


def test_portal_cookie登出_清Cookie并吊销(client, patient):
    _portal_cookie_login(client)
    assert client.get("/api/portal/me").status_code == 200
    csrf = client.cookies.get(PORTAL_CSRF_COOKIE)
    resp = client.post("/api/portal/auth/logout", headers={CSRF_HEADER: csrf})
    assert resp.status_code == 200
    assert _set_cookie_header(resp, PORTAL_AUTH_COOKIE) is not None
    assert client.get("/api/portal/me").status_code == 401


def test_portal_生产环境_SetCookie带Secure(client, patient, monkeypatch):
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == "13700009999").delete()
        db.commit()
    # 先在开发态拿验证码（生产不回显 debug_code），再切生产态走登录
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": "13700009999", "purpose": "login"}
    ).json()["debug_code"]
    monkeypatch.setattr(settings, "environment", "prod")
    resp = client.post(
        "/api/portal/auth/sms/login",
        json={"phone": "13700009999", "code": code},
        headers=COOKIE_MODE,
    )
    assert resp.status_code == 200
    for name in (PORTAL_AUTH_COOKIE, PORTAL_CSRF_COOKIE):
        assert "secure" in _set_cookie_header(resp, name).lower()


# ---------------------------------------------------------------- 两套 Cookie 互不越界


def test_业务Cookie不可用于居民端接口(client):
    """current_resident 只读 medplat_portal_token，业务 Cookie 不被采信。"""
    _admin_cookie_login(client)
    assert client.get("/api/portal/me").status_code == 401


def test_居民Cookie不可用于业务接口(client, patient):
    """get_current_user 只读 medplat_token；即使把居民令牌塞进业务 Cookie，
    scope=portal 也会被拒（与 Header 模式同一道防线）。"""
    _portal_cookie_login(client)
    assert client.get("/api/organizations").status_code == 401
    client.cookies.set(AUTH_COOKIE, client.cookies.get(PORTAL_AUTH_COOKIE))
    assert client.get("/api/organizations").status_code == 401


# ---------------------------------------------------------------- WebSocket Cookie 兜底


def test_ws_cookie兜底_握手随附Cookie即可鉴权(client):
    from app.ws import manager

    _admin_cookie_login(client)
    with client.websocket_connect("/ws/notifications") as ws:
        deadline = time.time() + 5
        while not manager.active and time.time() < deadline:
            time.sleep(0.05)
        assert manager.active, "Cookie 未被采信：连接没有完成鉴权登记"
        ws.send_text("ping")  # Cookie 模式下这是心跳帧而非令牌帧
        assert manager.broadcast({"type": "cookie_ws_ok"}) is True
        assert ws.receive_json()["type"] == "cookie_ws_ok"


def test_ws_无效Cookie不采信_仍回退首帧鉴权(client):
    client.cookies.set(AUTH_COOKIE, "not-a-token")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/ws/notifications") as ws:
            ws.send_text("also-not-a-token")  # 首帧被当作令牌 → 无效 → 1008 断开
            ws.receive_json()
