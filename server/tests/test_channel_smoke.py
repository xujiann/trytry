"""channel_smoke 脚本的「诚实形状」锁（真握手本身永远不进测试套件）。

脚本存在的意义是把 ROADMAP「四通道无一次真实握手记录」变成拿到厂商账号
当天就能出的验收记录。测试锁三件事：

1. **无凭据必 SKIP、退出码 2**——绝不把"没试"报成"通过"；
2. **拒绝擅发**：短信通道配了网关但没给收测号码，必须 SKIP 而不是发出去；
3. **不引 Mock**：脚本源码里出现 MockWeChatProvider/ConsoleSmsProvider
   即失败——Mock 绿了不算真连通，这正是被登记的缺口本身。

FAIL 语义（网关拒绝→FAIL 且退出码 1）用 monkeypatch 断网验证，不出真网。
"""
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

import channel_smoke  # noqa: E402
from app.config import settings  # noqa: E402


@pytest.fixture(autouse=True)
def _bare_settings(monkeypatch):
    """无凭据环境：四通道的配置全部清空（本容器/CI 的真实形态）。"""
    monkeypatch.setattr(settings, "sms_gateway_url", "")
    monkeypatch.setattr(settings, "wechat_provider", "mock")
    monkeypatch.setattr(settings, "wechat_appid", "")
    monkeypatch.setattr(settings, "payment_gateway_url", "")


def test_无凭据时全部SKIP且退出码2(capsys):
    assert channel_smoke.main([]) == 2
    out = capsys.readouterr().out
    assert out.count("[SKIP]") == 4
    assert "[PASS]" not in out and "[FAIL]" not in out


def test_短信配了网关但无收测号码必须拒发(monkeypatch):
    monkeypatch.setattr(settings, "sms_gateway_url", "https://sms.example.com/send")
    r = channel_smoke.smoke_sms(None)
    assert r.status == channel_smoke.SKIP
    assert "拒绝擅发" in r.evidence


def test_网关拒绝是FAIL且退出码1(monkeypatch, capsys):
    """FAIL 语义用断网验证：configured + 不可达 = FAIL，绝不降级成 SKIP/PASS。"""
    monkeypatch.setattr(settings, "payment_gateway_url", "https://pay.example.com")
    monkeypatch.setattr(settings, "payment_gateway_key", "k")

    import httpx

    def _refuse(*a, **kw):
        raise httpx.ConnectError("blocked")

    monkeypatch.setattr(httpx, "get", _refuse)
    assert channel_smoke.main(["--channel", "payment"]) == 1
    assert "[FAIL]" in capsys.readouterr().out


def test_微信错误码原样入档(monkeypatch):
    monkeypatch.setattr(settings, "wechat_provider", "official")
    monkeypatch.setattr(settings, "wechat_appid", "wxTEST")
    monkeypatch.setattr(settings, "wechat_secret", "s")

    import httpx

    class _Resp:
        def json(self):
            return {"errcode": 40013, "errmsg": "invalid appid"}

    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _Resp())
    r = channel_smoke.smoke_wechat()
    assert r.status == channel_smoke.FAIL
    assert "40013" in r.evidence


def test_脚本不引Mock通道():
    src = (SCRIPTS / "channel_smoke.py").read_text(encoding="utf-8")
    for banned in ("MockWeChatProvider", "ConsoleSmsProvider"):
        assert banned not in src, f"冒烟脚本引入了 {banned}——Mock 绿了不算真连通"


def test_电子证照如实报无代码路径():
    r = channel_smoke.smoke_ehcert()
    assert r.status == channel_smoke.SKIP and "尚无" in r.evidence
