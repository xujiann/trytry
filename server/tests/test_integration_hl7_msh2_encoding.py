"""MSH-2 编码字符不是缺省的 `^~\\&` 时三个 HL7 入口一律 422 写明原因，不落库（P2-1762，第五十二批扫描 AP2-12）。

平台按缺省编码字符拆字段（组件 `^`、重复 `~`、转义 `\\`、子组件 `&`），原先不读 MSH-2。修前实测：
- MSH-2 为 `^#/&`（重复符换成 `#`）的 A04 回 201，姓名存成「张三#ZHANGSAN」、电话「0571-88886666#13812345678」；
- MSH-2 为 `$~\\&`（组件符换成 `$`）的 A04 回 422「不支持的消息类型 ADT$A04」，错因说错。

修法：MSH-2 不是 `^~\\&`（v2.7 带截断符的 `^~\\&#` 照收）时 422「MSH-2 编码字符须为 ^~\\&（收到 …）」，在判消息类型之前；
不做通用的按 MSH-2 解析。简化建档 / ADT / ORU 三个入口与 ESB 的 hl7v2_patient 共用。
"""
import pytest

from app.database import SessionLocal
from app.models import ExamRequest, Patient

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


REPEAT_HASH = "^#/&"     # 重复符 / 转义符换了，组件符还是 ^
COMPONENT_DOLLAR = "$~\\&"   # 组件符换成 $


def _adt(msh2, event, control_id, id_card):
    sep = msh2[0]
    return "\r".join([f"MSH|{msh2}|HIS|XZYY|MEDPLAT|COUNTY|20261009090000||ADT{sep}{event}|{control_id}|P|2.4",
                      f"PID|1||{id_card}{sep}{sep}{sep}CN{sep}ID||P21762张三#ZHANG{sep}SAN||19920304|M|||杭州||"
                      "0571-88886666#13812345678"])


def _patients(id_card):
    with SessionLocal() as db:
        return [(p.name, p.phone) for p in db.query(Patient).filter(Patient.name.like("P21762%")).all()
                if p.id_card == id_card]


@pytest.mark.parametrize(("path", "msh2", "body17"), [
    ("/api/integration/hl7v2/adt", REPEAT_HASH, "33010219920304176"),        # 修前 201，「#」当数据落库
    ("/api/integration/hl7v2/adt", COMPONENT_DOLLAR, "33010219920305176"),   # 修前 422「不支持的消息类型 ADT$A04」
    ("/api/integration/hl7v2/patient", REPEAT_HASH, "33010219920306176"),    # 修前 201
    ("/api/integration/hl7v2/patient", COMPONENT_DOLLAR, "33010219920307176"),
], ids=["ADT重复符换了", "ADT组件符换了", "简化建档重复符换了", "简化建档组件符换了"])
def test_非缺省编码字符_422写明原因_不落库(client, admin, path, msh2, body17):
    id_card = _id_card(body17)
    resp = client.post(path, headers=admin, json={"message": _adt(msh2, "A04", f"P21762-{body17[-3:]}", id_card)})
    assert (resp.status_code, resp.json()) == (422, {"detail": f"MSH-2 编码字符须为 ^~\\&（收到 {msh2}）"})
    assert _patients(id_card) == []


def test_ORU非缺省编码字符_422_申请单不动(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21762 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient_id = client.post("/api/patients", headers=admin, json={
        "name": "P21762 检验患者", "id_card": _id_card("33010219920308176")}).json()["id"]
    request_id = client.post("/api/exams", headers=admin, json={
        "patient_id": patient_id, "from_org_id": org, "center_type": "lab", "item_code": "K", "item_name": "电解质"}
    ).json()["id"]
    message = "\r".join([f"MSH|{REPEAT_HASH}|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|P21762O|P|2.4",
                         f"OBR|1|{request_id}||K^电解质", "OBX|1|NM|K^血钾||4.1|mmol/L|3.5-5.3|N#W"])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": message})
    assert (resp.status_code, resp.json()) == (422, {"detail": f"MSH-2 编码字符须为 ^~\\&（收到 {REPEAT_HASH}）"})
    with SessionLocal() as db:
        assert db.get(ExamRequest, request_id).status == "pending"


def test_ESB的hl7v2_patient同一判据_记失败不建档(client, admin):
    endpoint = client.post("/api/esb/endpoints", headers=admin, json={
        "code": "HIS_P21762", "name": "接入方P21762", "system_type": "his", "direction": "inbound"}).json()
    id_card = _id_card("33010219920309176")
    queued = client.post("/api/esb/messages", json={
        "msg_type": "hl7v2_patient", "payload": {"message": _adt(REPEAT_HASH, "A04", "P21762-ESB", id_card)},
        "max_retries": 2}, headers={"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]})
    assert queued.status_code == 201, queued.text
    done = client.post(f"/api/esb/messages/{queued.json()['id']}/process", headers=admin)
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "failed" and "MSH-2 编码字符须为" in done.json()["last_error"], done.json()
    assert _patients(id_card) == []


@pytest.mark.parametrize(("msh2", "body17"), [("^~\\&", "33010219920310176"), ("^~\\&#", "33010219920311176")],
                         ids=["缺省", "v2.7带截断符"])
def test_缺省编码字符照旧(client, admin, msh2, body17):
    id_card = _id_card(body17)
    message = "\r".join([f"MSH|{msh2}|HIS|XZYY|MEDPLAT|COUNTY|20261009090000||ADT^A04|P21762-{body17[-3:]}|P|2.7",
                         f"PID|1||{id_card}^^^CN^ID||P21762李^四~LI^SI||19920304|M|||杭州||0571-88886666~13812345678"])
    resp = client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    assert _patients(id_card) == [("P21762李四", "13812345678")]
