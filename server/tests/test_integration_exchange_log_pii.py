"""集成平台入站在进处理函数之前被拒（422）时，交换日志把整条报文连同证件号、姓名、电话写进 `error_detail`（P2-1143，
第三十三批「隐私出口」扫描 A2-1）。

P2-521 给这类拒绝补了留痕：`_InboundRoute` 落的是 `f"422: 请求体校验失败 {exc.errors()!r}"`，而 pydantic 的 errors() 每条都带
`input`——对接方把字段名写错（`{"hl7": "MSH…PID…"}`）时 input 是整条 HL7（PID 的证件号、姓名、电话，ORU 还有 OBX 检验结果），
FHIR 资源发成数组时 input 是整个 Patient。交换日志没有保留期，读取端（`GET /api/integration/exchange-logs`）只挂了路由级的
经办角色、不按机构筛，任一机构的经办都读得到；与 PII 加密开关无关。同样的原始报文在 ESB 那一侧只给管理员看（P0-49）。

修法：落日志时每条错误只取 type / loc / msg，丢掉 input 与 ctx；回给对接方的 422 响应照旧（那是它自己发来的报文）。
"""
import pytest

from conftest import login

ID_CARD, PHONE = "330102199001011234", "13812345678"
SOURCE = "HIS-P21143"
HL7_ADT = ("MSH|^~\\&|HIS|H1|MEDPLAT|P|20260930101010||ADT^A04|MSG1143|P|2.5\r"
           f"PID|1||{ID_CARD}^^^^ID||张三||19900101|M|||某县某村||{PHONE}\r")
FHIR_PATIENT = {"resourceType": "Patient", "identifier": [{"value": ID_CARD}], "name": [{"text": "李四"}],
                "telecom": [{"system": "phone", "value": PHONE}], "birthDate": "1990-01-01"}


@pytest.fixture(scope="module")
def operators(client, admin):
    """两家机构各一名经办：甲院推报文，乙院读交换日志。"""
    heads = {}
    for tag in ("a", "b"):
        org = client.post("/api/organizations", headers=admin, json={
            "name": f"P21143 {tag} 卫生院", "org_type": "township", "level": "township"})
        assert org.status_code == 201, org.text
        user = client.post("/api/users", headers=admin, json={
            "username": f"p21143_op_{tag}", "password": "passw0rd1", "role": "operator", "org_id": org.json()["id"]})
        assert user.status_code == 201, user.text
        heads[tag] = login(client, f"p21143_op_{tag}", "passw0rd1")
    return heads


def _details(client, head) -> dict[str, str]:
    got = client.get("/api/integration/exchange-logs", headers=head, params={"source_system": SOURCE})
    assert got.status_code == 200, got.text
    return {row["message_type"]: row["error_detail"] for row in got.json()["logs"]}


def test_请求体校验失败落日志只留位置与说明_不带报文原文(client, operators):
    push = {**operators["a"], "X-Source-System": SOURCE}
    wrong_field = client.post("/api/integration/hl7v2/adt", headers=push, json={"hl7": HL7_ADT})
    assert wrong_field.status_code == 422, wrong_field.text
    as_array = client.post("/api/integration/fhir/Patient", headers=push, json=[FHIR_PATIENT])
    assert as_array.status_code == 422, as_array.text

    details = _details(client, operators["b"])   # 另一家机构的经办
    hl7, fhir = details["hl7v2_adt"], details["fhir_patient"]
    # 错在哪、错成什么样照旧查得到：对接方据此改报文，监控页据此数失败
    assert hl7.startswith("422: 请求体校验失败") and fhir.startswith("422: 请求体校验失败")
    assert "'type': 'missing'" in hl7 and "'loc': ('body', 'message')" in hl7 and "'msg': 'Field required'" in hl7
    assert "'type': 'dict_type'" in fhir and "'loc': ('body',)" in fhir
    assert "'msg': 'Input should be a valid dictionary'" in fhir
    for detail in (hl7, fhir):
        for pii in (ID_CARD, PHONE, "张三", "李四"):
            assert pii not in detail, detail[:300]   # 修前：整条 HL7 / 整个 Patient 连同证件号、电话、姓名
        assert "'input'" not in detail and "'ctx'" not in detail, detail[:300]


def test_回给对接方的422照旧带原因(client, operators):
    """对接方拿到的 422 响应不变：它据此定位自己报文的错处（响应里的 input 是它自己发来的内容）。"""
    got = client.post("/api/integration/hl7v2/adt", headers={**operators["a"], "X-Source-System": SOURCE},
                      json={"hl7": HL7_ADT})
    assert got.status_code == 422
    [error] = got.json()["detail"]
    assert error["loc"] == ["body", "message"] and error["type"] == "missing"
