"""出站 FHIR 资源里没有值的元素整个省掉（第十六批「导出 / 打印 vs 页面」扫描 T1-4）。

FHIR R4 的 JSON 表示不许出现空串、空数组、空对象与 null——没有值的元素就不写。原先照抄库里的默认值，实测（修前）：
没登记出生日期、没电话的老档案（`consents.is_minor` 的注释说老档案常缺出生日期），单条导出与批量导出都给
`"birthDate": ""`、`"telecom": []`；只有诊断编码的就诊，内联诊断带 `"text": ""`；没写所见的检查报告
`"presentedForm": []`。做校验的前置机整条拒收，这些档案到不了省平台。
"""
import json
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models import Encounter, ExamReport, Patient
from app.routers.integration import (
    _fhir_compact,
    fhir_diagnostic_report_resource,
    fhir_encounter_resource,
    fhir_patient_resource,
)

FHIR_OUT = Path(settings.upload_dir) / "fhir_out"
MOMENT = datetime(2026, 9, 28, 1, 0)


def _empty_paths(value, path="$"):
    """资源里所有空值的位置；空列表 = 合规。"""
    if value in ("", None, [], {}):
        return [path]
    if isinstance(value, dict):
        return [p for k, v in value.items() for p in _empty_paths(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [p for i, v in enumerate(value) for p in _empty_paths(v, f"{path}[{i}]")]
    return []


def _manifest():
    path = FHIR_OUT / "manifest.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def test_单条导出_没登记出生日期与电话的档案不带这两个元素(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "T1-4 单条老档案", "id_card": "33028119520303201X"}).json()
    body = client.get(f"/api/integration/fhir/Patient/{patient['ehc_no']}", headers=admin).json()
    assert "birthDate" not in body and "telecom" not in body, body   # 修前 "birthDate": "" 与 "telecom": []
    assert _empty_paths(body) == []
    assert body["name"] == [{"text": "T1-4 单条老档案"}] and body["gender"] == "unknown"


def test_三类资源的组装_没值的元素省掉_有值的与布尔假照写():
    legacy = Patient(ehc_no="EHC-T14", id_card="330281196305052016", name="T1-4", gender="未知",
                     birth_date="", phone="")
    resource = fhir_patient_resource(legacy)
    assert "birthDate" not in resource and "telecom" not in resource   # 修前 "" 与 []

    def encounter(code, name):
        return Encounter(id=1, encounter_type="outpatient", org_id=1, created_at=MOMENT, doctor_name="",
                         diagnosis_code=code, diagnosis_name=name)

    only_code = fhir_encounter_resource(encounter("I10", ""), "EHC-T14")["contained"][0]["code"]
    assert only_code == {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "I10"}]}  # 修前多 "text": ""
    only_name = fhir_encounter_resource(encounter("", "原发性高血压"), "EHC-T14")["contained"][0]["code"]
    assert only_name == {"text": "原发性高血压"}                                                   # 修前多 "coding": []

    report = ExamReport(id=1, reported_at=MOMENT, conclusion="未见异常", finding="", critical=False)
    dr = fhir_diagnostic_report_resource(report, 1, "EHC-T14")
    assert "presentedForm" not in dr                                                              # 修前 []
    assert dr["extension"] == [{"url": "urn:medplat:critical", "valueBoolean": False}]            # 假也是值
    assert _fhir_compact({"a": 0, "b": False, "c": [{}, ""], "d": {"e": None}}) == {"a": 0, "b": False}


def test_批量导出的每一行都没有空值(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "T1-4 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "T1-4 批量老档案", "id_card": "330281194511112017"}).json()
    enc = client.post("/api/encounters", headers=admin, json={
        "patient_id": patient["id"], "org_id": org, "diagnosis_code": "I10"})
    assert enc.status_code in (200, 201), enc.text
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": patient["id"], "from_org_id": org, "center_type": "imaging",
        "item_code": "CT-T14", "item_name": "头颅CT(T1-4)"}).json()["id"]
    assert client.post(f"/api/exams/{req}/claim", headers=admin).status_code == 200
    rep = client.post(f"/api/exams/{req}/report", headers=admin, json={
        "finding": "", "conclusion": "T1-4 未见异常", "critical": False, "reported_by": "影像科"})
    assert rep.status_code in (200, 201), rep.text

    from app.jobs import fhir_batch_export

    before = len(_manifest())
    with SessionLocal() as db:   # 每类每轮至多 1000 条：导到没有增量为止，本用例建的几条才一定在里面
        for _ in range(100):
            if not fhir_batch_export(db)[0]:
                break
    files = [json.loads(line)["file"] for line in _manifest()[before:]]
    rows = [json.loads(line) for name in files
            for line in (FHIR_OUT / name).read_text(encoding="utf-8").splitlines() if line.strip()]
    mine = [r for r in rows if patient["ehc_no"] in json.dumps(r, ensure_ascii=False)]
    assert {r["resourceType"] for r in mine} == {"Patient", "Encounter", "DiagnosticReport"}, mine
    offenders = {f"{r['resourceType']}/{r['id']}": _empty_paths(r) for r in rows if _empty_paths(r)}
    assert offenders == {}, offenders   # 修前：$.birthDate、$.telecom、$.contained[0].code.text、$.presentedForm
