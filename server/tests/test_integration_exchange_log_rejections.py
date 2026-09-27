"""集成平台入站在进处理函数之前就被拒的请求（422 / 401 / 403）不落交换日志（P2-521，第九批「留痕承诺」扫描 W3-8）。

`_log_exchange` 写着「失败也留痕」、`_run_inbound` 写着「成功/失败均落交换日志」、ORU 写着「全部入站落 ExchangeLog」；可请求体
校验（`Hl7Message.message` 非空、FHIR 资源须是对象）与路由级角色校验都在处理函数之前——空消息、缺字段、资源不是对象、
医生账号调入站，接口方收到的全是失败，交换日志一条都没有，监控页的失败率只数得到进了处理函数的那几条。

修法：集成路由换成 `_InboundRoute`，进处理函数之前的拒绝同样落日志；处理函数里已经记过的（`_run_inbound`）打标记，不重记。
"""
import pytest

from app.database import SessionLocal
from app.models import ExchangeLog

ORU = "/api/integration/hl7v2/oru"


def _logs():
    with SessionLocal() as db:
        return [(x.message_type, x.success, x.error_detail[:3], x.source_system)
                for x in db.query(ExchangeLog).order_by(ExchangeLog.id)]


@pytest.fixture(scope="module")
def doctor(client, admin):
    assert client.post("/api/users", headers=admin, json={
        "username": "p2521_doc", "password": "passw0rd1", "full_name": "P2521 医生", "role": "doctor"}).status_code in (200, 201)
    token = client.post("/api/auth/login", json={"username": "p2521_doc", "password": "passw0rd1"}).json()
    return {"Authorization": f"Bearer {token['access_token']}"}


def test_请求体校验与角色被拒同样落日志(client, admin, doctor):
    before = _logs()
    lis = {**admin, "X-Source-System": "LIS-P2521"}
    assert client.post(ORU, headers=lis, json={"message": "   "}).status_code == 422
    assert client.post(ORU, headers=lis, json={}).status_code == 422
    assert client.post("/api/integration/fhir/DiagnosticReport", headers=lis, json=["不是对象"]).status_code == 422
    assert client.post(ORU, headers={**doctor, "X-Source-System": "LIS-P2521"}, json={"message": "MSH|x"}).status_code == 403
    assert _logs()[len(before):] == [   # 修前一条都没有
        ("hl7v2_oru", False, "422", "LIS-P2521"),
        ("hl7v2_oru", False, "422", "LIS-P2521"),
        ("fhir_diagnostic_report", False, "422", "LIS-P2521"),
        ("hl7v2_oru", False, "403", "LIS-P2521"),
    ]


def test_处理函数里记过的不重记(client, admin):
    before = _logs()
    resp = client.post(ORU, headers=admin, json={"message": "MSH|^~\\&|LIS|X|MEDPLAT|Y|20260821100000||ORU^R01|C1|P|2.4"})
    assert resp.status_code in (404, 422), resp.text
    assert len(_logs()) == len(before) + 1   # 只有 _run_inbound 那一条
