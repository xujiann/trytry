"""居民端账号体系：手机号验证码登录、微信登录、实名绑定与登录态访问。"""
import pytest

from app.models import SmsCode
from app.routers.portal import SEND_COOLDOWN_SECONDS, _reset_portal_failures
from app.sms import ConsoleSmsProvider, set_sms_provider


@pytest.fixture(autouse=True)
def clean_state():
    """每个用例前清空限流/锁定状态与短信通道桩，用例之间互不干扰。"""
    _reset_portal_failures()
    set_sms_provider(None)
    yield
    _reset_portal_failures()
    set_sms_provider(None)


@pytest.fixture(scope="module")
def patient(client, admin):
    """带联系电话的患者档案：用于手机号唯一命中自动绑定。"""
    return client.post(
        "/api/patients",
        json={"name": "登录居民", "id_card": "330782199001011234", "phone": "13800001111"},
        headers=admin,
    ).json()


def _clear_cooldown(phone: str) -> None:
    """绕过 60 秒下发冷却：删掉该号码的历史验证码记录（等价于冷却已过）。"""
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.query(SmsCode).filter(SmsCode.phone == phone).delete()
        db.commit()


def send_code(client, phone: str, purpose: str = "login") -> str:
    _clear_cooldown(phone)
    resp = client.post("/api/portal/auth/sms/code", json={"phone": phone, "purpose": purpose})
    assert resp.status_code == 200, resp.text
    return resp.json()["debug_code"]


# ---------------------------------------------------------------- 验证码下发


def test_send_code_validates_phone(client):
    resp = client.post("/api/portal/auth/sms/code", json={"phone": "12345678901"})
    assert resp.status_code == 422


def test_send_code_returns_debug_code_only_in_console_dev(client):
    body = client.post("/api/portal/auth/sms/code", json={"phone": "13900002222"}).json()
    assert body["sent"] is True
    assert len(body["debug_code"]) == 6 and body["debug_code"].isdigit()
    assert body["cooldown_seconds"] == SEND_COOLDOWN_SECONDS


def test_debug_code_suppressed_when_switch_off(client, monkeypatch):
    """回显收紧：sms_debug_echo 关闭时（即默认值），console 通道也不再泄露验证码。"""
    from app.config import settings

    monkeypatch.setattr(settings, "sms_debug_echo", False)
    body = client.post("/api/portal/auth/sms/code", json={"phone": "13900002200"}).json()
    assert body["sent"] is True
    assert "debug_code" not in body


def test_debug_code_never_echoed_in_production(client, monkeypatch):
    """双重门：即便误开开关，生产环境（env=prod）也永不回显。"""
    from app.config import settings

    monkeypatch.setattr(settings, "sms_debug_echo", True)
    monkeypatch.setattr(settings, "env", "prod")
    body = client.post("/api/portal/auth/sms/code", json={"phone": "13900002201"}).json()
    assert body["sent"] is True
    assert "debug_code" not in body


def test_send_code_cooldown_blocks_second_request(client):
    _clear_cooldown("13900003333")
    assert client.post("/api/portal/auth/sms/code", json={"phone": "13900003333"}).status_code == 200
    again = client.post("/api/portal/auth/sms/code", json={"phone": "13900003333"})
    assert again.status_code == 429
    assert "秒后再获取" in again.json()["detail"]


def test_send_code_per_phone_window_limit(client):
    """冷却之外还有窗口配额：单号 10 分钟最多 5 条，第 6 条被拒。"""
    phone = "13900004444"
    for _ in range(5):
        assert send_code(client, phone)
    _clear_cooldown(phone)
    resp = client.post("/api/portal/auth/sms/code", json={"phone": phone})
    assert resp.status_code == 429
    assert "过于频繁" in resp.json()["detail"]


def test_code_not_stored_in_plaintext(client):
    from app.database import SessionLocal

    phone = "13900005555"
    code = send_code(client, phone)
    with SessionLocal() as db:
        record = db.query(SmsCode).filter(SmsCode.phone == phone).order_by(SmsCode.id.desc()).first()
        assert code not in record.code_hash
        assert "$" in record.code_hash  # PBKDF2 salt$digest


def test_send_code_502_when_channel_down(client):
    class DownProvider(ConsoleSmsProvider):
        def send(self, phone, content):
            return False

    set_sms_provider(DownProvider())
    resp = client.post("/api/portal/auth/sms/code", json={"phone": "13900006666"})
    assert resp.status_code == 502


# ---------------------------------------------------------------- 验证码登录


def test_sms_login_creates_account_and_autobinds_unique_phone(client, patient):
    """登录手机号在患者主索引中唯一命中 → 自动完成实名绑定。"""
    code = send_code(client, "13800001111")
    resp = client.post("/api/portal/auth/sms/login", json={"phone": "13800001111", "code": code})
    assert resp.status_code == 200
    body = resp.json()
    assert body["bound"] is True
    assert body["name"] == "登录居民"
    assert body["expires_in"] > 0

    me = client.get("/api/portal/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["phone"] == "138******11"  # 前3后2脱敏
    assert me.json()["ehc_no"] == patient["ehc_no"]


def test_sms_login_wrong_code_rejected(client):
    send_code(client, "13900007777")
    resp = client.post("/api/portal/auth/sms/login", json={"phone": "13900007777", "code": "000000"})
    assert resp.status_code == 400


def test_code_is_single_use(client):
    code = send_code(client, "13900008888")
    first = client.post("/api/portal/auth/sms/login", json={"phone": "13900008888", "code": code})
    assert first.status_code == 200
    replay = client.post("/api/portal/auth/sms/login", json={"phone": "13900008888", "code": code})
    assert replay.status_code == 400


def test_code_for_other_phone_rejected(client):
    """A 号码的验证码不能用来登录 B 号码。"""
    code = send_code(client, "13900009999")
    send_code(client, "13911110000")
    resp = client.post("/api/portal/auth/sms/login", json={"phone": "13911110000", "code": code})
    assert resp.status_code == 400


def test_code_bruteforce_locks_phone(client):
    phone = "13911112222"
    send_code(client, phone)
    for _ in range(5):
        client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": "111111"})
    blocked = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": "111111"})
    assert blocked.status_code == 429


def test_login_purpose_isolated(client):
    """bind 用途的验证码不能用于登录。"""
    phone = "13911113333"
    code = send_code(client, phone, purpose="bind")
    resp = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    assert resp.status_code == 400


# ---------------------------------------------------------------- 实名绑定


_fresh_seq = iter(range(1000))


@pytest.fixture()
def fresh_login(client):
    """一个未实名绑定的新账户。

    每个用例用一个全新手机号：实名绑定会把账户手机号回填到患者档案，
    复用同一号码会让下个用例被"手机号唯一命中"自动绑定，掩盖真实行为。
    """
    phone = f"1392222{next(_fresh_seq):04d}"
    code = send_code(client, phone)
    body = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    assert body["bound"] is False
    return {"headers": {"Authorization": f"Bearer {body['access_token']}"}, "phone": phone}


def test_unbound_account_cannot_read_archive(client, fresh_login):
    resp = client.get("/api/portal/me/archive", headers=fresh_login["headers"])
    assert resp.status_code == 403
    assert "实名绑定" in resp.json()["detail"]


def test_realname_bind_then_archive(client, admin, fresh_login):
    client.post(
        "/api/patients",
        json={"name": "待绑定居民", "id_card": "330782199202022345"},
        headers=admin,
    )
    bind = client.post(
        "/api/portal/auth/realname",
        json={"name": "待绑定居民", "id_card": "330782199202022345"},
        headers=fresh_login["headers"],
    )
    assert bind.status_code == 200 and bind.json()["bound"] is True

    archive = client.get("/api/portal/me/archive", headers=fresh_login["headers"])
    assert archive.status_code == 200
    assert archive.json()["name"] == "待绑定居民"
    # 绑定时回填联系电话
    listed = client.get("/api/patients?keyword=待绑定居民", headers=admin).json()
    assert listed[0]["phone"] == fresh_login["phone"]


def test_realname_bind_unknown_identity_404_and_locks(client, fresh_login):
    for _ in range(5):
        resp = client.post(
            "/api/portal/auth/realname",
            json={"name": "查无此人", "id_card": "330782190001019999"},
            headers=fresh_login["headers"],
        )
        assert resp.status_code == 404
    locked = client.post(
        "/api/portal/auth/realname",
        json={"name": "查无此人", "id_card": "330782190001019999"},
        headers=fresh_login["headers"],
    )
    assert locked.status_code == 429


def test_realname_bind_rejects_archive_taken_by_other_account(client, admin, fresh_login):
    """同一份档案不允许被第二个账户绑定。"""
    client.post(
        "/api/patients",
        json={"name": "独占居民", "id_card": "330782199303033456"},
        headers=admin,
    )
    assert (
        client.post(
            "/api/portal/auth/realname",
            json={"name": "独占居民", "id_card": "330782199303033456"},
            headers=fresh_login["headers"],
        ).status_code
        == 200
    )
    other_phone = "13933330000"
    code = send_code(client, other_phone)
    other = client.post(
        "/api/portal/auth/sms/login", json={"phone": other_phone, "code": code}
    ).json()
    conflict = client.post(
        "/api/portal/auth/realname",
        json={"name": "独占居民", "id_card": "330782199303033456"},
        headers={"Authorization": f"Bearer {other['access_token']}"},
    )
    assert conflict.status_code == 409


def test_shared_phone_not_autobound(client, admin):
    """一个手机号登记在多份档案上（全家共用）时不自动绑定，交给实名绑定。"""
    shared = "13944440000"
    for name, idc in [("共用甲", "330782199404044567"), ("共用乙", "330782199505055678")]:
        client.post(
            "/api/patients", json={"name": name, "id_card": idc, "phone": shared}, headers=admin
        )
    code = send_code(client, shared)
    body = client.post("/api/portal/auth/sms/login", json={"phone": shared, "code": code}).json()
    assert body["bound"] is False


# ---------------------------------------------------------------- 微信登录


def test_wechat_authorize_returns_mock_code_in_mock_mode(client):
    body = client.get("/api/portal/auth/wechat/authorize").json()
    assert body["provider"] == "mock"
    assert body["mock_code"].startswith("mock-")
    assert body["state"] in body["authorize_url"]


def test_wechat_login_creates_account(client):
    auth = client.get("/api/portal/auth/wechat/authorize").json()
    resp = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["bound"] is False  # 微信登录后仍需实名绑定
    me = client.get("/api/portal/me", headers={"Authorization": f"Bearer {body['access_token']}"})
    assert me.json()["wechat_bound"] is True


def test_wechat_same_openid_returns_same_account(client):
    auth = client.get("/api/portal/auth/wechat/authorize").json()
    first = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]}).json()
    second = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]}).json()
    h1 = {"Authorization": f"Bearer {first['access_token']}"}
    h2 = {"Authorization": f"Bearer {second['access_token']}"}
    assert client.get("/api/portal/me", headers=h1).json()["account_id"] == (
        client.get("/api/portal/me", headers=h2).json()["account_id"]
    )


def test_wechat_login_bad_code_rejected(client):
    assert client.post("/api/portal/auth/wechat/login", json={"code": "not-a-code"}).status_code == 400


def test_wechat_account_binds_phone_and_autobinds_archive(client, admin):
    """微信登录 → 补绑手机号 → 手机号唯一命中档案，一步到位完成实名。"""
    client.post(
        "/api/patients",
        json={"name": "微信居民", "id_card": "330782199606066789", "phone": "13955550000"},
        headers=admin,
    )
    auth = client.get("/api/portal/auth/wechat/authorize").json()
    token = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]}).json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    code = send_code(client, "13955550000", purpose="bind")
    resp = client.post(
        "/api/portal/auth/bind-phone", json={"phone": "13955550000", "code": code}, headers=headers
    )
    assert resp.status_code == 200 and resp.json()["bound_patient"] is True
    assert client.get("/api/portal/me/archive", headers=headers).json()["name"] == "微信居民"


def test_bind_wechat_to_phone_account(client):
    phone = "13966660000"
    code = send_code(client, phone)
    token = client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code}).json()
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    auth = client.get("/api/portal/auth/wechat/authorize").json()
    assert (
        client.post("/api/portal/auth/bind-wechat", json={"code": auth["mock_code"]}, headers=headers)
        .status_code
        == 200
    )
    # 之后用该微信登录，落到同一个账户
    again = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]}).json()
    h2 = {"Authorization": f"Bearer {again['access_token']}"}
    assert client.get("/api/portal/me", headers=h2).json()["account_id"] == (
        client.get("/api/portal/me", headers=headers).json()["account_id"]
    )


def test_bind_phone_already_used_by_other_account(client):
    """手机号已属于别的账户时不允许被微信账户抢绑。"""
    phone = "13977770000"
    code = send_code(client, phone)
    client.post("/api/portal/auth/sms/login", json={"phone": phone, "code": code})
    auth = client.get("/api/portal/auth/wechat/authorize").json()
    token = client.post("/api/portal/auth/wechat/login", json={"code": auth["mock_code"]}).json()
    resp = client.post(
        "/api/portal/auth/bind-phone",
        json={"phone": phone, "code": "123456"},
        headers={"Authorization": f"Bearer {token['access_token']}"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------- 令牌隔离与登出


def test_portal_token_rejected_by_business_api(client, patient):
    """居民端令牌不得进入业务接口。"""
    code = send_code(client, "13800001111")
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800001111", "code": code}
    ).json()["access_token"]
    resp = client.get("/api/patients", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401
    assert "居民端令牌" in resp.json()["detail"]


def test_business_token_rejected_by_portal_api(client, admin):
    resp = client.get("/api/portal/me", headers=admin)
    assert resp.status_code == 401


def test_portal_requires_token(client):
    assert client.get("/api/portal/me/archive").status_code == 401


def test_portal_logout_revokes_token(client, patient):
    code = send_code(client, "13800001111")
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800001111", "code": code}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/portal/me", headers=headers).status_code == 200
    assert client.post("/api/portal/auth/logout", headers=headers).status_code == 200
    assert client.get("/api/portal/me", headers=headers).status_code == 401


def test_token_survey_submission(client, patient):
    code = send_code(client, "13800001111")
    token = client.post(
        "/api/portal/auth/sms/login", json={"phone": "13800001111", "code": code}
    ).json()["access_token"]
    resp = client.post(
        "/api/portal/me/surveys",
        json={"target_type": "encounter", "score": 5, "comment": "服务很好"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 201 and resp.json()["submitted"] is True


# ---------------------------------------------------------------- 旧接口开关


def test_legacy_endpoints_can_be_disabled(client, patient, monkeypatch):
    from app.routers import portal as portal_module

    monkeypatch.setattr(portal_module.settings, "portal_legacy_verify", False)
    resp = client.post(
        "/api/portal/my-archive",
        json={"ehc_no": patient["ehc_no"], "id_card": "330782199001011234"},
    )
    assert resp.status_code == 410
    assert client.get(
        f"/api/portal/my-archive?ehc_no={patient['ehc_no']}&id_card=330782199001011234"
    ).status_code == 410
    assert client.post(
        "/api/portal/surveys",
        json={
            "ehc_no": patient["ehc_no"],
            "id_card": "330782199001011234",
            "target_type": "encounter",
            "score": 5,
        },
    ).status_code == 410
