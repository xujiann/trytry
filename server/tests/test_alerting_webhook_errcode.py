"""运维告警 webhook：对端回 HTTP 200 但应答 JSON 的 errcode 非 0 也算没发出去（P2-1268，第三十七批「出站报文与上报的
标准符合度」扫描 AA3-8）。

`alerting.send_alert` 的 docstring 原先写「企业微信/钉钉群机器人、告警平台皆可承接」，可报文固定是平台自定的
`{service, kind, message, at}`，群机器人要的是它们自己的格式；格式不对时它们回 HTTP 200，只在应答体的 errcode 里说拒收。
send_alert 只看 HTTP 状态码（P2-266 只修了 4xx / 5xx）：记成已发出（返回 True）、一行错误日志也没有——按 docstring 配成
群机器人后，备份 / 归档失败、定时任务失败、病毒检出这些告警群里一条都收不到，同类告警还进了冷却期。

修后：应答体是带非 0 errcode 的 JSON 时返回 False、记一条 ERROR（带 errcode / errmsg，不带告警正文）；冷却照旧（发送
失败也占冷却）；errcode 为 0、应答体不是 JSON、空体照旧算送达；4xx / 5xx 照旧失败（`test_alerting.py` 里 P2-266 的用例）。
docstring 与运维手册不再说群机器人可直接承接。外呼一律打桩，不出网。
"""
import httpx
import pytest

import app.alerting as alerting
from app.alerting import send_alert
from app.config import settings

SECRET = "患者 330102197501011239 的检验报告归档失败"   # 告警正文里可能带的敏感信息，不得进日志


@pytest.fixture(autouse=True)
def _fresh_cooldowns():
    alerting.reset_cooldowns()
    yield
    alerting.reset_cooldowns()


def _answers(monkeypatch, response: httpx.Response) -> list[dict]:
    """开启告警通道，`httpx.post` 一律回 `response`，记下每次外呼的报文。"""
    calls: list[dict] = []

    def fake_post(url, json=None, timeout=None):
        calls.append(json)
        return response

    monkeypatch.setattr(settings, "alert_webhook_url", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=DUMMY")
    monkeypatch.setattr(alerting.httpx, "post", fake_post)
    return calls


def _errors(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelname == "ERROR" and r.name == "medplat.alerting"]


@pytest.mark.parametrize("body", [
    {"errcode": 40008, "errmsg": "invalid message type"},   # 企业微信群机器人：缺 msgtype
    {"errcode": 310000, "errmsg": "keywords not in content"},
    {"errcode": "40035", "errmsg": "missing parameter"},    # errcode 写成字符串的
], ids=["40008", "310000", "字符串errcode"])
def test_对端回200但errcode非0_算没发出去_记ERROR不带告警正文(monkeypatch, caplog, body):
    calls = _answers(monkeypatch, httpx.Response(200, json=body))
    with caplog.at_level("WARNING", logger="medplat.alerting"):
        assert send_alert("job_failed:audit_archive", SECRET) is False   # 修前 True：记成已发出
    assert len(calls) == 1
    errors = _errors(caplog)
    assert errors, "对端拒收却一行 ERROR 都没有"   # 修前没有
    logged = "\n".join(errors)
    assert str(body["errcode"]) in logged and body["errmsg"] in logged, logged
    assert "job_failed:audit_archive" in logged, logged
    assert SECRET not in logged and "330102197501011239" not in logged, f"告警正文进了日志：{logged}"


def test_errcode拒收照旧占冷却(monkeypatch):
    calls = _answers(monkeypatch, httpx.Response(200, json={"errcode": 40008, "errmsg": "invalid message type"}))
    assert send_alert("k_errcode", "第一条") is False
    assert send_alert("k_errcode", "冷却期内的第二条") is False
    assert len(calls) == 1   # docstring 约定：发送失败也占冷却，不因拒收而高频重呼


@pytest.mark.parametrize("response", [
    httpx.Response(200, json={"errcode": 0, "errmsg": "ok"}),
    httpx.Response(200, json={"errcode": "0", "errmsg": "ok"}),
    httpx.Response(200, json={"status": "accepted"}),   # 告警平台：JSON 里不带 errcode
    httpx.Response(200, json=["ok"]),                   # JSON 但不是对象
    httpx.Response(200, text="ok"),                     # 非 JSON
    httpx.Response(200),                                # 空体
    httpx.Response(204),
    httpx.Response(200, content=b"[" * 100_000),        # 畸形应答：解析出什么错都不能让旁路抛出去
], ids=["errcode0", "errcode字符串0", "无errcode", "JSON数组", "非JSON", "空体", "204", "畸形"])
def test_errcode为0或应答不是带errcode的JSON_照旧算送达(monkeypatch, caplog, response):
    calls = _answers(monkeypatch, response)
    with caplog.at_level("WARNING", logger="medplat.alerting"):
        assert send_alert("k_ok", "告警") is True
    assert len(calls) == 1 and not _errors(caplog)
