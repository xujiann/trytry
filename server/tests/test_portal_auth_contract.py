"""居民端 `auth` 组 + 两个公开列表的**响应契约**（10 个端点）。

本组的关键不在字段多，在**两个条件键**：

- `auth/sms/code` 的 `debug_code`：仅 console 通道 + 显式开关 + 非生产三者同时满足
  才回显（P0 整改专门收紧过它——那是登录验证码的回显口子）；
- `auth/wechat/authorize` 的 `mock_code`：仅 Mock provider 才有。

用 Pydantic 声明可选字段并给默认值，会给**每一个**响应注入 `"debug_code": null`：
既改响应字节，又等于在生产响应与 OpenAPI 里公告这个字段的存在。
故两个端点带 `response_model_exclude_unset=True`，本文件把**两条分支**都钉住——
开的时候有、关的时候**整个键不出现**（不是 `null`）。

另有一个 Money 陷阱：`price-list.price` 是 `Money` 列，整数价格读回来是 int，
声明成 float 会把 `50` 变成 `50.0`。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.database import SessionLocal
from app.main import app
from app.models import ChargeItem, HealthArticle


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def seeded(client):
    with SessionLocal() as db:
        db.add(HealthArticle(title="高血压防治", category="chronic", content="少盐多动",
                             status="published"))
        db.add(HealthArticle(title="未发布稿", category="chronic", content="草稿",
                             status="draft"))
        db.add(ChargeItem(code="PC-INT", name="整数价项目", category="treatment",
                          price=50, active=True))
        db.add(ChargeItem(code="PC-DEC", name="小数价项目", category="treatment",
                          price=12.5, active=True))
        db.commit()
    return True


def _login(client, phone):
    code = client.post("/api/portal/auth/sms/code",
                       json={"phone": phone, "purpose": "login"}).json()["debug_code"]
    body = client.post("/api/portal/auth/sms/login",
                       json={"phone": phone, "code": code}).json()
    return body, {"Authorization": f"Bearer {body['access_token']}"}


# ------------------------------------------------- 条件键：debug_code
def test_回显开时debug_code在响应里(client):
    body = client.post("/api/portal/auth/sms/code",
                       json={"phone": "13910001001", "purpose": "login"}).json()
    assert set(body) == {"sent", "expires_in", "cooldown_seconds", "debug_code"}
    assert isinstance(body["debug_code"], str) and len(body["debug_code"]) == 6


def test_回显关时debug_code整个键不出现而不是null(client):
    """这条是本批最要紧的守卫。

    契约若不带 `response_model_exclude_unset=True`，可选字段会被序列化成
    `"debug_code": null` 出现在**每一个**响应里——包括生产。那既改字节，
    又等于在响应体与 OpenAPI 里公告"这里本来可以回显验证码"。
    """
    old = settings.sms_debug_echo
    settings.sms_debug_echo = False
    try:
        body = client.post("/api/portal/auth/sms/code",
                           json={"phone": "13910001002", "purpose": "login"}).json()
    finally:
        settings.sms_debug_echo = old
    assert set(body) == {"sent", "expires_in", "cooldown_seconds"}
    assert "debug_code" not in body, (
        f"回显关闭时 debug_code 仍出现在响应里：{body}——"
        "契约把它注进来了（应当 response_model_exclude_unset=True）"
    )


# ------------------------------------------------- 条件键：mock_code
def test_mock模式下authorize带mock_code(client):
    body = client.get("/api/portal/auth/wechat/authorize?redirect_uri=https://x.test/cb").json()
    assert set(body) == {"provider", "state", "authorize_url", "mock_code"}
    assert all(isinstance(v, str) and v for v in body.values())


def test_authorize的mock_code同样靠exclude_unset而非默认null(client):
    """与 debug_code 同一形状的条件键，建模方式必须一致。

    没法在用例里换掉 provider（它由配置决定），所以退一步钉住**契约的写法**：
    该端点必须带 `response_model_exclude_unset=True`，否则非 mock 部署会平白
    多出一个 `"mock_code": null`。
    """
    from app.main import app as fastapi_app

    route = next(
        r for r in _iter_routes(fastapi_app)
        if r.path == "/api/portal/auth/wechat/authorize"
    )
    assert route.response_model_exclude_unset is True, (
        "authorize 没开 exclude_unset——非 mock 部署会多出 mock_code: null"
    )


def _iter_routes(app):
    from fastapi.routing import APIRoute, APIRouter

    def walk(routes):
        for route in routes:
            if isinstance(route, APIRoute):
                yield route
            original = getattr(route, "original_router", None)
            if original is not None and getattr(original, "routes", None):
                yield from walk(original.routes)
            elif isinstance(route, APIRouter) and getattr(route, "routes", None):
                yield from walk(route.routes)

    yield from walk(app.routes)


def test_sms_code也开着exclude_unset(client):
    from app.main import app as fastapi_app

    route = next(r for r in _iter_routes(fastapi_app)
                 if r.path == "/api/portal/auth/sms/code")
    assert route.response_model_exclude_unset is True


# ------------------------------------------------------------ 登录与绑定
LOGIN_KEYS = {"access_token", "token_type", "expires_in", "bound", "name", "nickname"}


def test_短信登录键集合与类型(client):
    body, _ = _login(client, "13910002001")
    assert set(body) == LOGIN_KEYS
    assert isinstance(body["expires_in"], int) and isinstance(body["bound"], bool)
    assert all(isinstance(body[k], str) for k in ("access_token", "token_type", "name", "nickname"))


def test_微信登录与短信登录同形(client):
    """两处共用一个模型的依据：键集合必须一致，否则合并建模就是错的。"""
    sms, _ = _login(client, "13910002002")
    au = client.get("/api/portal/auth/wechat/authorize?redirect_uri=https://x.test/cb").json()
    wx = client.post("/api/portal/auth/wechat/login", json={"code": au["mock_code"]}).json()
    assert set(wx) == set(sms) == LOGIN_KEYS


def test_登出键集合(client):
    _, h = _login(client, "13910002003")
    body = client.post("/api/portal/auth/logout", headers=h).json()
    assert body == {"logged_out": True}


def test_实名未命中时的键集合(client):
    _, h = _login(client, "13910002004")
    resp = client.post("/api/portal/auth/realname",
                       json={"name": "查无此人", "id_card": "330281199901017777"}, headers=h)
    # 未命中走 4xx；命中的成功分支由 test_portal_auth.py 覆盖，这里只钉形状不重复造档案
    assert resp.status_code >= 400 and set(resp.json()) == {"detail"}


# ------------------------------------------------------------ 两个公开列表
def test_健康宣教只出已发布且键集合固定(client, seeded):
    rows = client.get("/api/portal/health-articles").json()
    assert rows, "seeded 发布了一篇"
    for row in rows:
        assert set(row) == {"id", "title", "category", "content"}
        assert isinstance(row["id"], int)
    assert all(r["title"] != "未发布稿" for r in rows), "草稿被公示出去了"


PRICE_KEYS = {"code", "name", "category", "category_name", "price",
              "last_adjusted_at", "effective_date"}


def test_价格公示键集合与类型(client, seeded):
    rows = client.get("/api/portal/price-list").json()
    assert rows
    for row in rows:
        assert set(row) == PRICE_KEYS
        assert isinstance(row["price"], (int, float)) and not isinstance(row["price"], bool)
        # 无调价记录时是空串而非 null
        assert isinstance(row["last_adjusted_at"], str)
        assert isinstance(row["effective_date"], str)


def test_价格是Money列_整数价必须仍是int(client, seeded):
    """`price` 建成 `int | float` 的依据：seeded 特意造了 50 与 12.5 两种。

    声明成 float 会把 `50` 变成 `50.0`——公示页上"50 元"变"50.0 元"，
    而且那是改响应字节。
    """
    rows = {r["code"]: r["price"] for r in client.get("/api/portal/price-list").json()}
    assert rows["PC-INT"] == 50 and isinstance(rows["PC-INT"], int), (
        f"整数价被转成了 {rows['PC-INT']!r}"
    )
    assert isinstance(rows["PC-DEC"], float)
