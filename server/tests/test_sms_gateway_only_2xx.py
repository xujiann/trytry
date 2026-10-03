"""短信 HTTP 网关回 3xx 被当成「已发送」，也不留一行日志（P2-1245，第三十六批「接口的 HTTP 语义」扫描 U3-5）。

`sms.HttpGatewaySmsProvider.send` 原先只有 `status_code >= 400` 才算失败，而 httpx 默认不跟随跳转：网关地址写成 http 而
网关强制 https（301/308）、或接口路径挪了（302），`send()` 照样返回 True——居民获取验证码回 200 `sent: true`，短信根本没
投递、永远收不到；宣教短信记「已发送」；期间 WARNING 以上的日志一行没有。类注释自己写「网关返回 2xx 视为受理」，兄弟通道
（告警 webhook、审计锚点、ESB 投递、呼叫网关）都只认 2xx。

修法：改成 `resp.is_success`（只认 2xx），非 2xx 记 ERROR（号码打掩码，正文就是验证码、不进日志）；2xx 照旧算受理。
外网一律打桩：`httpx.post` 回真的 `httpx.Response`，不出网。
"""
import httpx
import pytest

from app.routers.portal import _reset_portal_failures
from app.sms import HttpGatewaySmsProvider, set_sms_provider

GATEWAY_URL = "http://8.8.8.8/sms/send"   # 公网 IP 直写，不走 DNS；外呼打了桩，不会真发
PHONE = "13800138001"
CODE = "481203"
CONTENT = f"【县域医共体】验证码 {CODE}，5分钟内有效，请勿转发。"


@pytest.fixture(autouse=True)
def clean_state():
    """限流 / 冷却状态与短信通道桩每条用例前后复位，互不干扰。"""
    _reset_portal_failures()
    set_sms_provider(None)
    yield
    set_sms_provider(None)
    _reset_portal_failures()


def _gateway_answers(monkeypatch, status: int) -> list[str]:
    """`httpx.post` 一律回 `status`（3xx 照网关真实的跳转带上 Location），记下每次投递的地址。"""
    posts: list[str] = []

    def fake_post(url, *args, **kwargs):
        posts.append(url)
        headers = {"Location": url.replace("http://", "https://")} if 300 <= status < 400 else {}
        return httpx.Response(status, headers=headers, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    return posts


@pytest.mark.parametrize("status", [301, 302, 307, 308, 400, 503])
def test_网关回非2xx_send返回False并记ERROR_日志不落号码与验证码(monkeypatch, caplog, status):
    posts = _gateway_answers(monkeypatch, status)
    provider = HttpGatewaySmsProvider(GATEWAY_URL, "sms-key", "县域医共体")
    with caplog.at_level("WARNING", logger="medplat"):
        assert provider.send(PHONE, CONTENT) is False, f"网关回 {status} 却算已发送"   # 修前 3xx 返回 True
    assert posts == [GATEWAY_URL]
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR" and "[SMS-HTTP]" in r.getMessage()]
    assert errors, f"网关回 {status} 没留 ERROR 日志"   # 修前 3xx 一行没有
    logged = "\n".join(errors)
    assert str(status) in logged, logged   # 状态码要在日志里，措辞不钉（4xx / 5xx 修前修后都是这一支）
    assert PHONE not in logged and CODE not in logged, f"号码或验证码明文进了日志：{logged}"


@pytest.mark.parametrize("status", [301, 302])
def test_网关回跳转_获取验证码502而不是回sent(client, monkeypatch, status):
    posts = _gateway_answers(monkeypatch, status)
    set_sms_provider(HttpGatewaySmsProvider(GATEWAY_URL, "", "县域医共体"))
    resp = client.post("/api/portal/auth/sms/code", json={"phone": f"1391245{status}0", "purpose": "login"})
    assert resp.status_code == 502, resp.text   # 修前 200 {"sent": true}：居民等一条永远不来的短信
    assert resp.json()["detail"] == "短信通道暂不可用，请稍后重试"
    assert posts == [GATEWAY_URL]   # 确实投递过，是网关没受理


@pytest.mark.parametrize("status", [200, 202, 204])
def test_网关回2xx照旧算受理(client, monkeypatch, status):
    posts = _gateway_answers(monkeypatch, status)
    provider = HttpGatewaySmsProvider(GATEWAY_URL, "", "县域医共体")
    assert provider.send(PHONE, CONTENT) is True
    set_sms_provider(provider)
    resp = client.post("/api/portal/auth/sms/code", json={"phone": f"1391246{status}0", "purpose": "login"})
    assert resp.status_code == 200 and resp.json()["sent"] is True, resp.text
    assert len(posts) == 2
