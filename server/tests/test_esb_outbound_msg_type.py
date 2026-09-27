"""出站消息的类型编码含中文：入队照收，投递时放不进请求头，一路重试到死信、永远投不出去（P2-412）。

投递把类型编码放在请求头 `X-Esb-Msg-Type` 里，请求头只收 ASCII——httpx 对「入院通知」这样的值直接抛
UnicodeEncodeError（ValueError 的子类），按失败记一句看不懂的「'ascii' codec can't encode…」重试，重试多少次都一样，
最后进死信；入队那一刻却是 201，接入方以为投出去了。修后出站接入方入队时就 422 说清楚（入站消息不投递，照旧不限）；
投递时对编排转投的入站消息与存量行给出说得清的失败原因。
"""
import httpx

from app.database import SessionLocal
from app.models import EsbMessage
from app.routers import esb as esb_module
from test_esb_outbound import FakeHttpx, enqueue, register_endpoint


class StrictHttpx(FakeHttpx):
    """头值照真 httpx 的规矩编码（非 ASCII 抛 UnicodeEncodeError）——`FakeHttpx` 不管这个，拿它测等于测不到缺陷。"""

    def post(self, url, content=b"", headers=None, timeout=None):
        httpx.Headers(headers or {})
        return super().post(url, content=content, headers=headers, timeout=timeout)


def test_出站接入方入队中文类型编码_422说清楚(client, admin):
    ep = register_endpoint(client, admin, "P2412_OUT", direction="outbound", endpoint_url="https://province.example/p2412")
    got = client.post("/api/esb/messages", json={"msg_type": "入院通知", "payload": {"k": "v"}},
                      headers={"X-Esb-Endpoint": ep["code"], "X-Esb-Token": ep["auth_token"]})
    assert got.status_code == 422 and "ASCII" in got.json()["detail"], got.text   # 修前 201，之后永远投不出去
    assert enqueue(client, ep, "admission_notice", {"k": "v"})["status"] == "queued"   # 英文编码照常


def test_入站接入方的中文类型编码照收(client, admin):
    ep = register_endpoint(client, admin, "P2412_IN", system_type="his")
    assert enqueue(client, ep, "入院通知", {"k": "v"})["status"] == "queued"


def test_存量的中文类型出站消息_投递失败说得清(client, admin, monkeypatch):
    fake = StrictHttpx()
    monkeypatch.setattr(esb_module, "httpx", fake)
    ep = register_endpoint(client, admin, "P2412_OUT2", direction="outbound",
                           endpoint_url="https://province.example/p2412b")
    with SessionLocal() as db:   # 修之前入队的存量行
        row = EsbMessage(endpoint_id=ep["id"], msg_type="入院通知", payload={"k": "v"})
        db.add(row)
        db.commit()
        message_id = row.id
    got = client.post(f"/api/esb/messages/{message_id}/process", headers=admin)
    assert got.status_code == 200 and got.json()["status"] == "failed", got.text
    assert "放不进投递请求头" in got.json()["last_error"], got.text   # 修前「'ascii' codec can't encode…」
    assert fake.calls == []
