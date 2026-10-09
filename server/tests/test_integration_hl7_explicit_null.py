"""HL7 的显式空值 `""` 不当字面值落库（P2-1760，第五十二批扫描 AP2-3 可判的一半）。

HL7 v2 里字段为空表示不变、为 `""`（两个双引号）表示删除接收方已有的值。入站原先把 `""` 当字面值：修前实测——
A04 带 PID-13 为 `""` 建档，库里电话是 `'""'`；先 A08 改成 13812345678，再推 PID-13 为 `""` 的 A08，201「患者信息已更新」、
电话变回 `'""'`；A08 的 PID-5 为 `""` 时姓名变成 `'""'`；FHIR 出站照样导出 `name.text='""'`、`telecom.value='""'`；A01 的
DG1-3 为 `""` 时住院就诊的诊断编码与名称都是 `'""'`。同一条 PID 里 PID-7 / PID-8 的 `""` 早就当「没给」。ESB 的
hl7v2_patient 共用同一解析。

修法：`""` 一律不作为字面值入库——解析时按「没给」处理（`_hl7_null`，PID-5 / PID-13 / PV1 / DG1 / OBR-4 / OBX 取字段处共用）；
A08 因此对 `""` 保持原值（「非空字段覆盖更新」），PID-5 为 `""` 与 PID-5 为空同样 422「姓名缺失」。A08 遇 `""` 要不要按 HL7
本义清空接收方已有的值另待裁定，这里不做。
"""
import pytest

from app.database import SessionLocal
from app.models import Admission, Encounter, ExamReport, Patient

_WEIGHTS = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]


def _id_card(body17: str) -> str:
    return body17 + "10X98765432"[sum(int(c) * w for c, w in zip(body17, _WEIGHTS)) % 11]


ID_CARD = _id_card("33010219860101176")
WARD = "P21760病区"


def _adt(client, admin, event, control_id, pid, *extra):
    msh = f"MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20261009090000||ADT^{event}|{control_id}|P|2.4"
    return client.post("/api/integration/hl7v2/adt", headers=admin, json={"message": "\r".join([msh, pid, *extra])})


def _row(id_card=ID_CARD):
    with SessionLocal() as db:
        patients = db.query(Patient).filter(Patient.name.like("P21760%")).all()
        patient = next(p for p in patients if p.id_card == id_card)
        return {"name": patient.name, "birth": patient.birth_date, "gender": patient.gender, "phone": patient.phone,
                "ehc_no": patient.ehc_no}


def test_A04带空值电话_电话为空(client, admin):
    resp = _adt(client, admin, "A04", "P21760A", f'PID|1||{ID_CARD}^^^CN^ID||P21760张三||19860101|M|||杭州市||""')
    assert resp.status_code == 201, resp.text
    assert _row()["phone"] == ""   # 修前 '""'
    export = client.get(f"/api/integration/fhir/Patient/{_row()['ehc_no']}", headers=admin).json()
    assert "telecom" not in export   # 修前导出 telecom.value='""'


def test_A08带空值电话_保持原值(client, admin):
    assert _adt(client, admin, "A08", "P21760B",
                f"PID|1||{ID_CARD}^^^CN^ID||P21760张三||19860101|M|||杭州市||13812345678").status_code == 201
    resp = _adt(client, admin, "A08", "P21760C", f'PID|1||{ID_CARD}^^^CN^ID||P21760张三||""|""|||杭州市||""')
    assert resp.status_code == 201, resp.text
    assert {k: v for k, v in _row().items() if k != "ehc_no"} == {
        "name": "P21760张三", "birth": "1986-01-01", "gender": "男", "phone": "13812345678"}   # 修前电话被覆盖成 '""'


def test_A08带空值姓名_档案姓名不变(client, admin):
    resp = _adt(client, admin, "A08", "P21760D", f'PID|1||{ID_CARD}^^^CN^ID||""||19860101|M|||||13812345678')
    assert (resp.status_code, resp.json()) == (422, {"detail": "PID-5 患者姓名缺失"})   # 修前 201，姓名被覆盖成 '""'
    assert _row()["name"] == "P21760张三"


def test_A01的DG1与主治医师为空值_诊断与医师为空(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21760 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    ward = client.post("/api/inpatient/wards", headers=admin, json={"org_id": org, "name": WARD}).json()
    bed = client.post("/api/inpatient/beds", headers=admin, json={"ward_id": ward["id"], "bed_no": "1"})
    assert bed.status_code == 201, bed.text
    id_card = _id_card("33010219860202176")
    pv1_null = _adt(client, admin, "A01", "P21760E", f"PID|1||{id_card}^^^CN^ID||P21760住院||19860202|F",
                    'PV1|1|I|""^^1', "DG1|1||I10^高血压")
    assert (pv1_null.status_code, pv1_null.json()) == (422, {"detail": "PV1-3 须为 病区^房间^床号"})   # 修前 404 病区 "" 不存在
    resp = _adt(client, admin, "A01", "P21760F", f"PID|1||{id_card}^^^CN^ID||P21760住院||19860202|F",
                f'PV1|1|I|{WARD}^^1||||""', 'DG1|1||""|""')
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        admission = db.get(Admission, resp.json()["admission_id"])
        encounter = db.get(Encounter, resp.json()["encounter_id"])
        assert (admission.doctor_name, admission.diagnosis_name) == ("", "")   # 修前 ('""', '""')
        assert (encounter.doctor_name, encounter.diagnosis_code, encounter.diagnosis_name) == ("", "", "")


def test_ORU的结果项空值不印双引号_不当认不得的标志(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21760 检验县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient_id = client.post("/api/patients", headers=admin, json={
        "name": "P21760 检验患者", "id_card": _id_card("33010219860303176")}).json()["id"]
    request_id = client.post("/api/exams", headers=admin, json={
        "patient_id": patient_id, "from_org_id": org, "center_type": "lab", "item_code": "K", "item_name": "电解质"}
    ).json()["id"]
    message = "\r".join(["MSH|^~\\&|LIS|XZYY|MEDPLAT|COUNTY|20261009100000||ORU^R01|P21760G|P|2.4",
                         f'OBR|1|{request_id}||K^""',
                         'OBX|1|NM|K^血钾||4.1|""|""|""',
                         'OBX|2|CE|HBsAg^乙肝表面抗原||""^阴性||""|N'])
    resp = client.post("/api/integration/hl7v2/oru", headers=admin, json={"message": message})
    assert resp.status_code == 201, resp.text
    with SessionLocal() as db:
        report = db.get(ExamReport, resp.json()["report_id"])
        assert report.finding.split("\n") == ["血钾：4.1", "乙肝表面抗原：阴性 [N]"]   # 修前「血钾：4.1 ""（参考 ""） [""]」
        assert report.conclusion == "电解质：共 2 项，异常 0 项"   # 修前「电解质：…，另 1 项的异常标志平台不认得（""）…」


def test_ESB的hl7v2_patient同样不落字面值(client, admin):
    endpoint = client.post("/api/esb/endpoints", headers=admin, json={
        "code": "HIS_P21760", "name": "接入方P21760", "system_type": "his", "direction": "inbound"}).json()
    id_card = _id_card("33010219860404176")
    message = "\r".join(["MSH|^~\\&|HIS|XZYY|MEDPLAT|COUNTY|20261009090000||ADT^A04|P21760H|P|2.4",
                         f'PID|1||{id_card}^^^CN^ID||P21760总线||19860404|F|||||""'])
    queued = client.post("/api/esb/messages", json={"msg_type": "hl7v2_patient", "payload": {"message": message}},
                         headers={"X-Esb-Endpoint": endpoint["code"], "X-Esb-Token": endpoint["auth_token"]})
    assert queued.status_code == 201, queued.text
    done = client.post(f"/api/esb/messages/{queued.json()['id']}/process", headers=admin)
    assert done.status_code == 200 and done.json()["status"] == "succeeded", done.text
    assert _row(id_card)["phone"] == ""   # 修前 '""'
