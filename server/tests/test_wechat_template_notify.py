"""工程包 I2：微信模板消息 provider 与站内信旁路接线。

- provider 层：mock 落日志返回 True；official 经 access_token 缓存调公众号
  接口（httpx 打桩，验证缓存命中与 40001 强刷重试）；
- 接线层：notify_patient 在「账户绑定 openid + 系统参数配置了该类目模板」时
  旁路发送，缺任一条件即静默跳过；发送失败/桩件抛异常都不影响站内信落库。
"""
import httpx
import pytest

from conftest import reset_database

from app.notify import notify_patient
from app.models import Notification, Patient, ResidentAccount, SystemParam
from app.wechat import MockWeChatProvider, OfficialWeChatProvider, set_wechat_provider


class DummyResp:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload


# ---------------------------------------------------------------------------
# provider 层
# ---------------------------------------------------------------------------


def test_mock通道只落日志返回True(caplog):
    with caplog.at_level("INFO", logger="medplat.wechat"):
        assert MockWeChatProvider().send_template_message("o1", "TMPL", {"title": "t"}, "/m/") is True
    assert "WECHAT-MOCK" in caplog.text and "TMPL" in caplog.text


def test_official通道token缓存与发送(monkeypatch):
    provider = OfficialWeChatProvider("appid", "secret", "https://cb.example/")
    token_calls, send_calls = [], []

    def fake_get(url, params=None, timeout=None, **kwargs):
        token_calls.append(params)
        return DummyResp({"access_token": f"tok{len(token_calls)}", "expires_in": 7200})

    def fake_post(url, params=None, json=None, timeout=None, **kwargs):
        send_calls.append({"params": params, "json": json})
        return DummyResp({"errcode": 0, "errmsg": "ok"})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    assert provider.send_template_message("openid-1", "TMPL1", {"title": "报告", "body": "已出"}, "/m/") is True
    assert provider.send_template_message("openid-2", "TMPL1", {"title": "又一条"}) is True
    # access_token 缓存：两次发送只取一次 token
    assert len(token_calls) == 1 and token_calls[0]["grant_type"] == "client_credential"
    # 数据按微信要求包成 {"value": ...}
    assert send_calls[0]["json"]["touser"] == "openid-1"
    assert send_calls[0]["json"]["data"] == {"title": {"value": "报告"}, "body": {"value": "已出"}}
    assert send_calls[0]["params"] == {"access_token": "tok1"}


def test_official通道token失效强刷重试一次(monkeypatch):
    provider = OfficialWeChatProvider("appid", "secret", "")
    provider._access_token, provider._token_expires_at = "stale", 9e12  # 缓存里是被吊销的 token
    results = [{"errcode": 40001, "errmsg": "invalid credential"}, {"errcode": 0}]
    tokens = []
    monkeypatch.setattr(
        httpx, "get",
        lambda url, params=None, timeout=None, **k: (tokens.append(1), DummyResp({"access_token": "fresh", "expires_in": 7200}))[1],
    )
    monkeypatch.setattr(
        httpx, "post", lambda url, params=None, json=None, timeout=None, **k: DummyResp(results.pop(0))
    )
    assert provider.send_template_message("o1", "T", {"a": 1}) is True
    assert tokens == [1]  # 只强刷了一次


def test_official通道失败不抛异常(monkeypatch):
    provider = OfficialWeChatProvider("appid", "secret", "")
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: DummyResp({"access_token": "tok", "expires_in": 7200})
    )
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: DummyResp({"errcode": 43004, "errmsg": "require subscribe"})
    )
    assert provider.send_template_message("o1", "T", {}) is False
    # token 都拿不到时同样只返回 False
    def boom(*a, **k):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "get", boom)
    provider2 = OfficialWeChatProvider("appid", "secret", "")
    assert provider2.send_template_message("o1", "T", {}) is False


# ---------------------------------------------------------------------------
# notify_patient 旁路接线
# ---------------------------------------------------------------------------


class CaptureProvider:
    name = "capture"

    def __init__(self, ok=True, explode=False):
        self.ok = ok
        self.explode = explode
        self.sent = []

    def authorize_url(self, state):
        return "/"

    def exchange_code(self, code):
        return None

    def send_template_message(self, openid, template_id, data, url=""):
        if self.explode:
            raise RuntimeError("boom")
        self.sent.append({"openid": openid, "template_id": template_id, "data": data})
        return self.ok


@pytest.fixture()
def db():
    reset_database()
    from app.database import SessionLocal

    with SessionLocal() as session:
        yield session
    set_wechat_provider(None)


def _seed(db, *, openid="oBIND01", template_key="wechat_template_exam_report", template_id="TPLX"):
    patient = Patient(ehc_no="EHCWX0001", name="旁路患者", id_card="330981199001010011")
    db.add(patient)
    db.flush()
    db.add(ResidentAccount(patient_id=patient.id, wechat_openid=openid, status="active"))
    if template_key:
        db.add(SystemParam(key=template_key, value=template_id, description="模板消息测试"))
    db.commit()
    return patient


def test_绑定openid且配置模板时旁路发送(db):
    patient = _seed(db)
    provider = CaptureProvider()
    set_wechat_provider(provider)
    n = notify_patient(db, patient.id, category="exam_report", title="报告已出", body="请查看")
    db.commit()
    assert n == 1
    assert provider.sent == [
        {"openid": "oBIND01", "template_id": "TPLX", "data": {"title": "报告已出", "body": "请查看"}}
    ]
    assert db.query(Notification).count() == 1  # 站内信照常落库


def test_未配置模板参数时不外呼(db):
    patient = _seed(db, template_key=None)
    provider = CaptureProvider()
    set_wechat_provider(provider)
    notify_patient(db, patient.id, category="exam_report", title="t", body="b")
    assert provider.sent == []


def test_类目不匹配的模板不误发(db):
    patient = _seed(db, template_key="wechat_template_followup")
    provider = CaptureProvider()
    set_wechat_provider(provider)
    notify_patient(db, patient.id, category="exam_report", title="t", body="b")
    assert provider.sent == []


def test_发送失败与桩件异常都不阻断站内信(db, caplog):
    patient = _seed(db)
    set_wechat_provider(CaptureProvider(ok=False))
    with caplog.at_level("WARNING", logger="medplat.notify"):
        assert notify_patient(db, patient.id, category="exam_report", title="t", body="b") == 1
    assert "模板消息发送失败" in caplog.text

    set_wechat_provider(CaptureProvider(explode=True))
    assert notify_patient(db, patient.id, category="exam_report", title="t2", body="b2") == 1
    db.commit()
    assert db.query(Notification).count() == 2


def test_旧桩件缺方法时静默跳过(db):
    patient = _seed(db)

    class LegacyStub:
        name = "legacy"

        def authorize_url(self, state):
            return "/"

        def exchange_code(self, code):
            return None

    set_wechat_provider(LegacyStub())
    assert notify_patient(db, patient.id, category="exam_report", title="t", body="b") == 1
