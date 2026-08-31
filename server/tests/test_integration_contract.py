"""对接适配层 `/api/integration` 5 个未治理端点的**特征化网 + 响应契约**。

套路同 `test_esb_contract.py`：先钉住**当前**响应的完整 JSON（dict 相等）与键序
→ 再加 `response_model` → 加完逐字节不变（CLAUDE.md §7/§11）。
已治理的 4 个端点（hl7v2/adt、hl7v2/oru、fhir/DiagnosticReport、fhir/Encounter）
不在此列。

本簇的建模判断（都以此处的精确断言为依据）：

- **出站 FHIR Patient 是外部标准形状 → 宽 dict 透传**（workflows.nodes 先例）：
  FHIR R4 资源的字段面由国际标准定义，identifier/name/telecom 都是嵌套数组，
  给它建窄模型等于替 HL7 组织另立规格；此处逐键钉死当前导出的 7 键资源
  （含 telecom 空数组分支），契约用 dict 原样透传。
- **入站回执的 patient 复用 `PatientOut` 并按角色脱敏**（H1 口径）——与已治理的
  `AdtInboundOut.patient` 同一先例；掩码后的身份证号/电话逐字符钉住。
- **`values` 是 `dict[str, float]`**：三条产地（component、顶层 valueQuantity）
  全部经 `float(quantity)`，整数入参 172 恒以 `172.0` 出参。
- **统计里的 int/float 之别**：`total`/`failed`/`count` 恒 int（COUNT 与
  `int(x or 0)`），`failure_rate_pct` 恒 float（`round(x*100.0/n, 2)` 与兜底
  字面量 `0.0` 两条产地都是浮点）——声明成 float 会把 `5` 变 `5.0`，即改字节。
- `exchange-logs` 的明细过滤参数只作用于 `logs`，`total/failed/by_type`
  始终是全量口径——此处专门钉一遍，防契约化时被"顺手统一"。
"""
import re

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app

HL7_PATIENT_KEYS = ["created", "ack", "patient"]
FHIR_PATIENT_KEYS = ["created", "patient"]
OBSERVATION_KEYS = ["followup_id", "chronic_id", "disease", "values", "level"]
PATIENT_KEYS = ["name", "id_card", "gender", "birth_date", "phone", "id", "ehc_no"]
FHIR_EXPORT_KEYS = ["resourceType", "id", "identifier", "name", "gender", "birthDate", "telecom"]
LOGS_KEYS = ["total", "failed", "failure_rate_pct", "by_type", "logs"]
LOG_TYPE_KEYS = ["message_type", "count", "failed", "failure_rate_pct"]
LOG_ROW_KEYS = ["id", "source_system", "message_type", "direction", "success", "error_detail", "at"]

ID_CARD_SYSTEM = "urn:oid:2.16.156.10011.1.3"
EHC_SYSTEM = "urn:medplat:ehc"

HL7_MSG = (
    "MSH|^~\\&|HIS|TOWN|MEDPLAT|COUNTY|20260101010101||ADT^A04|CTRLCT01|P|2.4\n"
    "PID|1||330881199001018821||契约^HL7患者||19900101|M|||||13800000001"
)


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def login(client, username, password):
    resp = client.post("/api/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def seed(client, admin):
    """一次种完全部场景，测试只做断言——交换日志统计要按这份调用清单精确对账。

    入站调用（也是 exchange_logs 的全部内容，id 升序）：
    ① hl7v2_patient 成功（建档，src CT-HIS）· ② hl7v2_patient 成功（幂等复投）·
    ③ fhir_patient 成功（无 X-Source-System）· ④ fhir_patient 失败
    （resourceType 不对，src CT-REG）· ⑤ fhir_observation 成功（src CT-HIS）。
    """
    data: dict = {}
    org = client.post(
        "/api/organizations",
        json={"name": "契约对接医院", "org_type": "lead_hospital", "level": "county"},
        headers=admin,
    ).json()
    data["org"] = org
    client.post(
        "/api/users",
        json={"username": "itct_op", "password": "pass123456", "role": "operator", "org_id": org["id"]},
        headers=admin,
    )
    data["operator"] = login(client, "itct_op", "pass123456")

    src = {"X-Source-System": "CT-HIS"}
    resp = client.post(
        "/api/integration/hl7v2/patient", json={"message": HL7_MSG},
        headers={**data["operator"], **src},
    )
    assert resp.status_code == 201, resp.text
    data["hl7_created"] = resp.json()
    data["hl7_again"] = client.post(
        "/api/integration/hl7v2/patient", json={"message": HL7_MSG},
        headers={**data["operator"], **src},
    ).json()

    data["fhir_p2"] = client.post(
        "/api/integration/fhir/Patient",
        json={
            "resourceType": "Patient",
            "identifier": [{"system": ID_CARD_SYSTEM, "value": "330881199001018822"}],
            "name": [{"text": "契约FHIR患者"}],
            "gender": "female",
            "birthDate": "1992-02-02",
        },
        headers=data["operator"],
    ).json()
    data["fhir_bad_resp"] = client.post(
        "/api/integration/fhir/Patient",
        json={"resourceType": "Bundle"},
        headers={**data["operator"], "X-Source-System": "CT-REG"},
    )

    # 血压观测归档需要慢病档案（患者① hypertension）
    p1 = data["hl7_created"]["patient"]
    data["chronic"] = client.post(
        "/api/chronic",
        json={"patient_id": p1["id"], "disease": "hypertension",
              "managed_by_org_id": org["id"]},
        headers=admin,
    ).json()
    resp = client.post(
        "/api/integration/fhir/Observation",
        json={
            "resourceType": "Observation",
            "subject": {"reference": f"Patient/{p1['ehc_no']}"},
            "component": [
                {"code": {"coding": [{"code": "8480-6"}]}, "valueQuantity": {"value": 172}},
                {"code": {"coding": [{"code": "8462-4"}]}, "valueQuantity": {"value": 96}},
            ],
        },
        headers={**data["operator"], **src},
    )
    assert resp.status_code == 201, resp.text
    data["observation"] = resp.json()
    return data


# ---------------------------------------------------------------- HL7 简化建档


def test_HL7建档回执精确_ACK与脱敏患者(seed):
    body = seed["hl7_created"]
    assert list(body.keys()) == HL7_PATIENT_KEYS
    assert list(body["patient"].keys()) == PATIENT_KEYS
    assert body == {
        "created": True,
        "ack": body["ack"],
        "patient": {
            "name": "契约HL7患者",
            "id_card": "3308**********8821",
            "gender": "男",
            "birth_date": "1990-01-01",
            "phone": "138******01",
            "id": body["patient"]["id"],
            "ehc_no": body["patient"]["ehc_no"],
        },
    }
    # ACK 应答：MSA|AA|原消息控制ID（时间戳 14 位为随机项，只钉形状）
    assert re.fullmatch(
        r"MSH\|\^~\\&\|MEDPLAT\|COUNTY\|\|\|\d{14}\|\|ACK\|CTRLCT01\|P\|2\.4\rMSA\|AA\|CTRLCT01",
        body["ack"],
    ), body["ack"]
    assert isinstance(body["patient"]["ehc_no"], str) and body["patient"]["ehc_no"]


def test_HL7重复投递幂等_created为false(seed):
    body = seed["hl7_again"]
    assert list(body.keys()) == HL7_PATIENT_KEYS
    assert body == {"created": False, "ack": body["ack"], "patient": seed["hl7_created"]["patient"]}


# ---------------------------------------------------------------- FHIR 入站


def test_FHIR建档回执精确_无电话为空串(seed):
    body = seed["fhir_p2"]
    assert list(body.keys()) == FHIR_PATIENT_KEYS
    assert body == {
        "created": True,
        "patient": {
            "name": "契约FHIR患者",
            "id_card": "3308**********8822",
            "gender": "女",
            "birth_date": "1992-02-02",
            "phone": "",
            "id": body["patient"]["id"],
            "ehc_no": body["patient"]["ehc_no"],
        },
    }


def test_观测入站回执精确_values恒float(seed):
    body = seed["observation"]
    assert list(body.keys()) == OBSERVATION_KEYS
    assert list(body["values"].keys()) == ["sbp", "dbp"]
    assert body == {
        "followup_id": body["followup_id"],
        "chronic_id": seed["chronic"]["id"],
        "disease": "hypertension",
        "values": {"sbp": 172.0, "dbp": 96.0},
        "level": 3,
    }
    # 三条产地都经 float(quantity)：整数入参 172 恒以 172.0 出参
    assert type(body["values"]["sbp"]) is float
    assert type(body["followup_id"]) is int and type(body["level"]) is int


# ---------------------------------------------------------------- FHIR 出站导出


def test_FHIR患者导出精确_角色脱敏与空telecom分支(client, admin, seed):
    ehc1 = seed["hl7_created"]["patient"]["ehc_no"]
    body = client.get(f"/api/integration/fhir/Patient/{ehc1}", headers=seed["operator"]).json()
    assert list(body.keys()) == FHIR_EXPORT_KEYS
    assert body == {
        "resourceType": "Patient",
        "id": ehc1,
        "identifier": [
            {"system": EHC_SYSTEM, "value": ehc1},
            {"system": ID_CARD_SYSTEM, "value": "3308**********8821"},
        ],
        "name": [{"text": "契约HL7患者"}],
        "gender": "male",
        "birthDate": "1990-01-01",
        "telecom": [{"system": "phone", "value": "138******01"}],
    }
    # admin 明文导出（审计留痕由 AccessLog 承担），字段面不变
    clear = client.get(f"/api/integration/fhir/Patient/{ehc1}", headers=admin).json()
    assert clear == {
        **body,
        "identifier": [
            {"system": EHC_SYSTEM, "value": ehc1},
            {"system": ID_CARD_SYSTEM, "value": "330881199001018821"},
        ],
        "telecom": [{"system": "phone", "value": "13800000001"}],
    }
    # 无电话患者：telecom 是空数组（不是缺键）
    ehc2 = seed["fhir_p2"]["patient"]["ehc_no"]
    body2 = client.get(f"/api/integration/fhir/Patient/{ehc2}", headers=seed["operator"]).json()
    assert list(body2.keys()) == FHIR_EXPORT_KEYS
    assert body2["telecom"] == [] and body2["gender"] == "female"


# ---------------------------------------------------------------- 交换监控


def test_交换日志精确_int与float之别与全量口径(client, seed):
    body = client.get("/api/integration/exchange-logs", headers=seed["operator"]).json()
    assert list(body.keys()) == LOGS_KEYS
    assert [list(r.keys()) for r in body["by_type"]] == [LOG_TYPE_KEYS] * 3
    assert [list(r.keys()) for r in body["logs"]] == [LOG_ROW_KEYS] * 5
    logs = body["logs"]
    assert body == {
        "total": 5,
        "failed": 1,
        "failure_rate_pct": 20.0,
        "by_type": [
            {"message_type": "fhir_observation", "count": 1, "failed": 0,
             "failure_rate_pct": 0.0},
            {"message_type": "fhir_patient", "count": 2, "failed": 1,
             "failure_rate_pct": 50.0},
            {"message_type": "hl7v2_patient", "count": 2, "failed": 0,
             "failure_rate_pct": 0.0},
        ],
        "logs": [
            {"id": logs[0]["id"], "source_system": "CT-HIS",
             "message_type": "fhir_observation", "direction": "inbound",
             "success": True, "error_detail": "", "at": logs[0]["at"]},
            {"id": logs[1]["id"], "source_system": "CT-REG",
             "message_type": "fhir_patient", "direction": "inbound",
             "success": False, "error_detail": "422: resourceType 必须为 Patient",
             "at": logs[1]["at"]},
            {"id": logs[2]["id"], "source_system": "",
             "message_type": "fhir_patient", "direction": "inbound",
             "success": True, "error_detail": "", "at": logs[2]["at"]},
            {"id": logs[3]["id"], "source_system": "CT-HIS",
             "message_type": "hl7v2_patient", "direction": "inbound",
             "success": True, "error_detail": "", "at": logs[3]["at"]},
            {"id": logs[4]["id"], "source_system": "CT-HIS",
             "message_type": "hl7v2_patient", "direction": "inbound",
             "success": True, "error_detail": "", "at": logs[4]["at"]},
        ],
    }
    # 计数恒 int（声明成 float 会把 5 变 5.0）；比率恒 float（0.0 兜底也是）
    assert type(body["total"]) is int and type(body["failed"]) is int
    assert isinstance(body["failure_rate_pct"], float)
    assert type(body["by_type"][0]["count"]) is int and type(body["by_type"][0]["failed"]) is int
    assert isinstance(body["by_type"][0]["failure_rate_pct"], float)
    assert all(isinstance(r["at"], str) for r in logs)


def test_交换日志过滤只作用于明细_统计仍是全量(client, seed):
    full = client.get("/api/integration/exchange-logs", headers=seed["operator"]).json()
    failed_only = client.get(
        "/api/integration/exchange-logs?success=false", headers=seed["operator"]
    ).json()
    assert failed_only == {**full, "logs": [full["logs"][1]]}
    by_type = client.get(
        "/api/integration/exchange-logs?message_type=hl7v2_patient", headers=seed["operator"]
    ).json()
    assert by_type["logs"] == full["logs"][3:]
    by_src = client.get(
        "/api/integration/exchange-logs?source_system=CT-HIS", headers=seed["operator"]
    ).json()
    assert by_src["logs"] == [full["logs"][0], full["logs"][3], full["logs"][4]]
    limited = client.get(
        "/api/integration/exchange-logs?limit=1", headers=seed["operator"]
    ).json()
    assert limited["logs"] == [full["logs"][0]]


# ---------------------------------------------------------------- 错误体


def test_各类错误体都只有detail(client, admin, seed):
    cases = [
        seed["fhir_bad_resp"],  # resourceType 不对 422（种子里先行触发）
        client.post("/api/integration/hl7v2/patient",
                    json={"message": "PID|1||330881199001018821"},
                    headers=seed["operator"]),  # 缺 MSH 422
        client.post("/api/integration/hl7v2/patient",
                    json={"message": "MSH|^~\\&|HIS|T|M|C|20260101||ADT^A04|C1|P|2.4"},
                    headers=seed["operator"]),  # 缺 PID 422
        client.post("/api/integration/fhir/Observation",
                    json={"resourceType": "Observation",
                          "subject": {"reference": f"Patient/{seed['fhir_p2']['patient']['ehc_no']}"},
                          "code": {"coding": [{"code": "2339-0"}]},
                          "valueQuantity": {"value": 6.1}},
                    headers=seed["operator"]),  # 无 diabetes 档案 404
        client.get("/api/integration/fhir/Patient/EHC-NOT-EXIST", headers=admin),  # 404
    ]
    assert [r.status_code for r in cases] == [422, 422, 422, 404, 404]
    for r in cases:
        assert set(r.json()) == {"detail"}
