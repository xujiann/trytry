"""出站 FHIR 资源带上 R4 的必填元素：DiagnosticReport.code、内联 Condition.subject（P2-1079，第三十一批「编码体系与术语」
扫描 C4-7）。

R4 里 DiagnosticReport.code、Condition.subject 都是 1..1。原先检查报告导出只有 basedOn（指向从不导出的
ServiceRequest）、subject、结论与所见——省平台看不出这是哪项检查；就诊内联的诊断 Condition 没有 subject。做校验的
前置机整条拒收（P2-679 为空值修过同一类后果）。修后报告带 `code`（本地系统 `urn:medplat:exam-item` + 申请单的
item_code，名称进 text，对接规范映射表 item_code→code），内联 Condition 带 `subject`。
"""
import json
from datetime import datetime
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models import Encounter, ExamReport
from app.routers.integration import EXAM_ITEM_SYSTEM, fhir_diagnostic_report_resource, fhir_encounter_resource

FHIR_OUT = Path(settings.upload_dir) / "fhir_out"
MOMENT = datetime(2026, 9, 30, 1, 0)


def test_组装_报告带检查项目_内联诊断带患者():
    report = ExamReport(id=1, reported_at=MOMENT, conclusion="未见异常", finding="", critical=False)
    dr = fhir_diagnostic_report_resource(report, 1, "EHC-P21079", item_code="CT-HEAD", item_name="头颅CT平扫")
    assert dr["code"] == {"coding": [{"system": EXAM_ITEM_SYSTEM, "code": "CT-HEAD"}], "text": "头颅CT平扫"}   # 修前没有
    encounter = Encounter(id=1, encounter_type="outpatient", org_id=1, created_at=MOMENT, doctor_name="",
                          diagnosis_code="I10", diagnosis_name="原发性高血压")
    (condition,) = fhir_encounter_resource(encounter, "EHC-P21079")["contained"]
    assert condition["subject"] == {"reference": "Patient/EHC-P21079"}   # 修前没有


def test_批量导出的报告带检查项目(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21079 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P21079 患者", "id_card": "330281195501012079"}).json()
    req = client.post("/api/exams", headers=admin, json={
        "patient_id": patient["id"], "from_org_id": org, "center_type": "imaging",
        "item_code": "DR-P21079", "item_name": "胸部DR(P21079)"}).json()["id"]
    assert client.post(f"/api/exams/{req}/claim", headers=admin).status_code == 200
    rep = client.post(f"/api/exams/{req}/report", headers=admin, json={
        "finding": "双肺纹理清晰", "conclusion": "P21079 未见异常", "critical": False, "reported_by": "影像科"})
    assert rep.status_code in (200, 201), rep.text

    from app.jobs import fhir_batch_export

    manifest = FHIR_OUT / "manifest.jsonl"
    before = len(manifest.read_text(encoding="utf-8").splitlines()) if manifest.exists() else 0
    with SessionLocal() as db:
        for _ in range(100):
            if not fhir_batch_export(db)[0]:
                break
    files = [json.loads(line)["file"] for line in manifest.read_text(encoding="utf-8").splitlines()[before:]]
    mine = [row for name in files for line in (FHIR_OUT / name).read_text(encoding="utf-8").splitlines()
            if line.strip() and (row := json.loads(line))["resourceType"] == "DiagnosticReport"
            and row.get("conclusion") == "P21079 未见异常"]
    assert [r["code"] for r in mine] == [
        {"coding": [{"system": EXAM_ITEM_SYSTEM, "code": "DR-P21079"}], "text": "胸部DR(P21079)"}]
