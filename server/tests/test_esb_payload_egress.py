"""集成平台的消息载荷原样出给任意登录账号：村医翻得到全县过总线的患者身份证号与手机号（P0-49）。

`GET /api/esb/messages` 只挂了「登录即可」，`payload` 是接入方投进来的原始报文（HIS 挂号的 HL7 PID 段、FHIR Patient
资源……），证件号、手机号都是明文，一页 500 条、带总数；同一个人在 `/api/patients` 上对非管理员是掩码的（CLAUDE.md §4：
出口一律经 privacy.py 脱敏）。页面只对管理员开放（`app.js` 的集成平台页 `roles: ["admin"]`），接口却没跟上；载荷是宽
dict，出口脱敏闸门（按响应模型里的 id_card / phone 字段认）看不见它。「消费 / 重试」允许经办调，回执里同样整段回显载荷。

修法：消息清单与编排执行记录（逐步结果里有健康卡号）只给管理员——与唯一的调用方（管理员才看得到的集成平台页）同口径；
经办消费回执里的载荷不再回显（键在、值为空对象），管理员照旧看得到全文。
"""
import pytest

from conftest import login
from test_esb import FHIR_PATIENT, HL7_MESSAGE, enqueue, register_endpoint


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P049 总线卫生院", "org_type": "township", "level": "township"}).json()["id"]
    heads = {}
    for name, role in (("p049_doc", "doctor"), ("p049_op", "operator")):
        created = client.post("/api/users", headers=admin, json={
            "username": name, "password": "pass123456", "role": role, "org_id": org})
        assert created.status_code == 201, created.text
        heads[role] = login(client, name, "pass123456")
    endpoint = register_endpoint(client, admin, "P049_HIS", system_type="his")
    return {"endpoint": endpoint, **heads}


def _message(client, world, payload):
    got = enqueue(client, world["endpoint"], "generic", payload)
    assert got.status_code == 201, got.text
    return got.json()["id"]


@pytest.mark.parametrize("role", ["doctor", "operator"])
def test_非管理员查不了消息清单与执行记录(client, world, role):
    _message(client, world, {"hl7": HL7_MESSAGE})
    got = client.get("/api/esb/messages", headers=world[role])
    assert got.status_code == 403, got.text[:200]   # 修前 200：身份证号、手机号明文
    assert client.get("/api/esb/flow-runs", headers=world[role]).status_code == 403


def test_管理员照旧看得到载荷全文(client, admin, world):
    mid = _message(client, world, FHIR_PATIENT)
    rows = client.get("/api/esb/messages", headers=admin).json()
    assert next(r for r in rows if r["id"] == mid)["payload"] == FHIR_PATIENT
    assert client.get("/api/esb/flow-runs", headers=admin).status_code == 200


def test_经办消费回执不回显载荷_管理员的照旧(client, admin, world):
    by_operator = client.post(f"/api/esb/messages/{_message(client, world, FHIR_PATIENT)}/process",
                              headers=world["operator"])
    assert by_operator.status_code == 200, by_operator.text
    assert by_operator.json()["payload"] == {}   # 修前：证件号、手机号整段回显
    assert "330281198802027015" not in by_operator.text and "13900002222" not in by_operator.text
    by_admin = client.post(f"/api/esb/messages/{_message(client, world, FHIR_PATIENT)}/process", headers=admin)
    assert by_admin.status_code == 200 and by_admin.json()["payload"] == FHIR_PATIENT
