"""审计留痕的操作主体：Cookie 会话下必须记成真实操作人，不得记成 anonymous。

G3 把三套浏览器前端切成 Cookie 会话后，浏览器不再发 Authorization 头。
`_write_audit` 当时只读该头，于是**浏览器发起的全部写操作都留痕成 anonymous**
——审计表照常长，但"谁干的"这一列全是空的，等保留痕形同虚设。

三条网：

1. **Cookie 模式（业务端）**：写请求落痕的 username/user_id 是真实操作人；
2. **Header 模式**：既有对接方口径回归，行为不变；
3. **居民端 Cookie 会话**：记成可辨识的 `resident:{account_id}`（令牌 sub 本身
   就是这个值），**不得把手机号一类 PII 抄进审计表**，也不得撞进 users 表。

非空洞性：把 `_write_audit` 的取令牌改回只读 `request.headers["authorization"]`，
用例 1、3 的"不是 anonymous"断言必红。
"""
import pytest

from app.database import SessionLocal
from app.models import AuditLog, SmsCode, User
from app.security import (
    COOKIE_MODE_HEADER,
    CSRF_COOKIE,
    CSRF_HEADER,
    PORTAL_CSRF_COOKIE,
    decode_token,
)

COOKIE_MODE = {COOKIE_MODE_HEADER: "cookie"}
RESIDENT_PHONE = "13700008888"


@pytest.fixture(autouse=True)
def clean_cookies(client):
    client.cookies.clear()
    yield
    client.cookies.clear()


def _audit_for(path: str) -> AuditLog:
    """取该路径最近一条审计（每个用例用独立探针路径，不会串）。"""
    with SessionLocal() as db:
        entry = (
            db.query(AuditLog)
            .filter(AuditLog.path == path)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None, f"写操作 {path} 完全没有留痕"
        return entry


def _admin_user_id() -> int:
    with SessionLocal() as db:
        return db.query(User).filter(User.username == "admin").first().id


def test_cookie会话写操作留痕真实操作人(client):
    """Cookie 模式建机构：审计的 username 必须是 admin，不是 anonymous。"""
    resp = client.post(
        "/api/auth/login",
        json={"username": "admin", "password": "admin123"},
        headers=COOKIE_MODE,
    )
    assert resp.status_code == 200
    csrf = client.cookies.get(CSRF_COOKIE)
    assert csrf
    resp = client.post(
        "/api/organizations",
        json={"name": "留痕主体测试卫生院", "org_type": "township", "level": "township"},
        headers={CSRF_HEADER: csrf},  # 注意：无 Authorization 头，全靠 Cookie
    )
    assert resp.status_code == 201, resp.text

    entry = _audit_for("/api/organizations")
    assert entry.username != "anonymous", "Cookie 会话的写操作留痕成了匿名（审计失去追责能力）"
    assert entry.username == "admin"
    assert entry.user_id == _admin_user_id()


def test_header会话写操作留痕回归(client):
    """Header 模式（既有对接方）口径不变：照旧记真实操作人。"""
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin123"}
    ).json()["access_token"]
    resp = client.post(
        "/api/audit-subject-header-probe",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 404  # 无此路由，但写操作尝试必须留痕

    entry = _audit_for("/api/audit-subject-header-probe")
    assert entry.username == "admin"
    assert entry.user_id == _admin_user_id()


def test_居民端Cookie会话留痕账户而非手机号(client):
    """居民端 Cookie 会话：记成 resident:{account_id}，不得写入手机号。"""
    with SessionLocal() as db:  # 绕过 60 秒下发冷却（与 test_auth_cookie_csrf 同一套路）
        db.query(SmsCode).filter(SmsCode.phone == RESIDENT_PHONE).delete()
        db.commit()
    code = client.post(
        "/api/portal/auth/sms/code", json={"phone": RESIDENT_PHONE, "purpose": "login"}
    ).json()["debug_code"]
    resp = client.post(
        "/api/portal/auth/sms/login",
        json={"phone": RESIDENT_PHONE, "code": code},
        headers=COOKIE_MODE,
    )
    assert resp.status_code == 200, resp.text
    account_id = decode_token(resp.json()["access_token"])["account_id"]

    csrf = client.cookies.get(PORTAL_CSRF_COOKIE)
    assert csrf
    resp = client.post("/api/portal/audit-subject-probe", headers={CSRF_HEADER: csrf})
    assert resp.status_code == 404  # 无此路由，但写操作尝试必须留痕

    entry = _audit_for("/api/portal/audit-subject-probe")
    assert entry.username == f"resident:{account_id}"
    assert entry.user_id is None, "居民账户不在 users 表内，不得挂到某个业务用户上"
    assert RESIDENT_PHONE not in entry.username, "审计表不得落手机号一类 PII"
