"""入站取证件号按标识类型与校验位挑，不再把别的标识当证件号另建一份主档（P2-723，第十九批「导入 / 入站对接 vs 界面录入」
扫描 K3-4）。

HL7 的 PID-3 是可重复字段（`证件号^^^CN^ID~病案号^^^HIS^MR`），原先不按 `~` 拆、只取第一个组件：整串「证件号~病案号」
当证件号另建一份档案；病案号排在前面时拿 16 位病案号建档、短病案号整条 422。FHIR Patient 的证件号 system 只认带
`urn:oid:` 前缀的写法，写成裸 OID 时落到最后一个 identifier（常是病案号）建档。主索引按证件号幂等，另建的那份就是
平行主数据（核心数据不可变定义），之后同样写法推来的就诊、住院都挂到那份上。ORU 的 PID 核对同一个取法。

修法：PID-3 先按 `~` 拆，优先标识类型（CX.5）为身份证的那一项，其次校验位对得上的 18 位号、再次 15 位纯数字老证号，
都没有时照旧取第一项（长度够不够照旧由 P1-61 管）；FHIR 的 system 认带不带前缀两种写法，也认 type 里的身份证类型码。
"""
import pytest

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


ID_CARD = _id_card("33010219700101723")


@pytest.fixture(scope="module")
def existing(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "P2723 张三", "id_card": ID_CARD})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _hl7_patient(client, admin, pid3, control_id):
    msg = "\r".join([f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20260929100000||ADT^A04|{control_id}|P|2.4",
                     f"PID|1||{pid3}||P2723 张三||19700101|M"])
    return client.post("/api/integration/hl7v2/patient", headers=admin, json={"message": msg})


@pytest.mark.parametrize("pid3", [
    f"{ID_CARD}~MR0001",                               # 修前：整串当证件号，另建一份
    f"0000123456789012~{ID_CARD}",                     # 修前：16 位病案号当证件号，另建一份
    f"MR01^^^HIS^MR~{ID_CARD}^^^CN^ID",                # 修前：短病案号在前，整条 422
    f"{ID_CARD}^^^CN^ID",                              # 原本就对的写法照旧
], ids=["证件号在前带病案号", "长病案号在前", "类型码标明", "单个证件号"])
def test_HL7_PID3重复_命中既有档案_不另建(client, admin, existing, pid3):
    resp = _hl7_patient(client, admin, pid3, "P2723")
    assert resp.status_code == 201, resp.text
    assert resp.json()["created"] is False and resp.json()["patient"]["id"] == existing, resp.json()


def test_FHIR证件号_system写成裸OID也认_不拿病案号建档(client, admin, existing):
    resource = {"resourceType": "Patient", "name": [{"text": "P2723 张三"}], "identifier": [
        {"system": "2.16.156.10011.1.3", "value": ID_CARD},
        {"system": "urn:hospital:mrn", "value": "MRN000012345678"}]}
    resp = client.post("/api/integration/fhir/Patient", headers=admin, json=resource)
    assert resp.status_code == 201, resp.text
    assert resp.json()["created"] is False and resp.json()["patient"]["id"] == existing   # 修前以病案号新建档案


def test_FHIR证件号用type标明也认(client, admin, existing):
    resource = {"resourceType": "Patient", "name": [{"text": "P2723 张三"}], "identifier": [
        {"type": {"coding": [{"code": "NNCHN"}]}, "value": ID_CARD},
        {"system": "urn:hospital:mrn", "value": "MRN000012345679"}]}
    resp = client.post("/api/integration/fhir/Patient", headers=admin, json=resource)
    assert resp.status_code == 201 and resp.json()["patient"]["id"] == existing, resp.text


def test_ORU的PID核对同一个取法(client, admin, existing):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2723 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    request = client.post("/api/exams", headers=admin, json={
        "patient_id": existing, "from_org_id": org, "center_type": "lab", "item_code": "GLU", "item_name": "血糖"}).json()
    msg = "\r".join(["MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20260929100000||ORU^R01|P2723O|P|2.4",
                     f"PID|1||MR01^^^HIS^MR~{ID_CARD}^^^CN^ID||P2723 张三",
                     f"OBR|1|{request['id']}||GLU^血糖", "OBX|1|NM|GLU^空腹血糖|1|5.6|mmol/L|3.9-6.1|N"])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": msg})
    assert resp.status_code == 201, resp.text   # 修前 422「PID 患者与申请单患者不一致」


def test_主索引里只有一份张三(client, admin, existing):
    from app.database import SessionLocal
    from app.models import Patient

    with SessionLocal() as db:
        names = [p.name for p in db.query(Patient).filter(Patient.name == "P2723 张三").all()]
    assert names == ["P2723 张三"], names
