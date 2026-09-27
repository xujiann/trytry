"""写库的机构编号经局部变量转手、只过了机构写权限守卫，不查存在（P2-411）。

`org_id = body.org_id if body.org_id is not None else user.org_id`（发药是 `else prescription.org_id`）之后只做一句
`assert_org_writable`——它只管「能不能以这家机构的名义写」，全域角色直接放行、不查机构在不在（P2-169 讲过同一件事）。
全域角色填错一位机构编号：纳管建档撞外键，被翻成「该患者已纳管此病种」409（P1-90 文档里举的正是这句误报）；发药被
翻成「该处方已发药」409；筛查登记、按方案生成随访、生成报告撞外键 500；自动筛查、自动匹配查不到这家机构的患者，
回执「0 条」，看不出是编号填错了。P1-90 / P2-169 的零基线闸门只认 `列=body.字段` 与 `**body.model_dump()`，
经局部变量转手的一处也点不到。

修后七处在写权限守卫之后查机构存在，不存在 404「机构不存在」，一行不写。
"""
import pytest

from app.database import SessionLocal
from app.models import DispenseRecord, Prescription, PrescriptionItem, User
from app.spd.models import (
    SpdEnrollment,
    SpdFollowupRecord,
    SpdFollowupRule,
    SpdReportInstance,
    SpdReportTemplate,
    SpdScreening,
)

MISSING = 987654321


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2411 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2411 患者", "id_card": "330127197309092411"}).json()["id"]
    program = client.post("/api/spd/programs", headers=admin, json={
        "code": "p2411_prog", "name": "P2411 病种", "category": "chronic",
        "include_rules": [{"field": "age", "op": ">=", "value": 60}]})   # 自动筛查先查病种配没配纳入规则
    assert program.status_code == 201, program.text
    with SessionLocal() as db:
        admin_id = db.query(User).filter_by(username="admin").one().id
        rx = Prescription(patient_id=patient, org_id=org, status="auto_passed", created_by=admin_id)
        db.add(rx)
        db.flush()
        db.add(PrescriptionItem(prescription_id=rx.id, drug_code="P2411", drug_name="P2411 药", daily_dose=1, days=1))
        rule = SpdFollowupRule(code="p2411_rule", name="P2411 方案", scene="outpatient", points=[7], active=True)
        db.add(rule)
        template = db.query(SpdReportTemplate).first()
        assert template is not None, "启动种子里没有报告模板，前提变了"
        db.commit()
        return {"patient": patient, "rx": rx.id, "rule": rule.id, "template": template.code}


def _count(model, **where):
    with SessionLocal() as db:
        return db.query(model).filter_by(**where).count()


def _expect_404(got):
    assert (got.status_code, got.json()["detail"]) == (404, "机构不存在"), got.text


def test_发药填了不存在的机构_404_不再误报已发药(client, admin, world):
    _expect_404(client.post("/api/dispense", headers=admin, json={"prescription_id": world["rx"], "org_id": MISSING}))
    assert _count(DispenseRecord, prescription_id=world["rx"]) == 0   # 修前 409「该处方已发药」


def test_纳管建档填了不存在的机构_404_不再误报已纳管(client, admin, world):
    _expect_404(client.post("/api/spd/enrollments", headers=admin, json={
        "patient_id": world["patient"], "program_code": "p2411_prog", "org_id": MISSING}))
    assert _count(SpdEnrollment, patient_id=world["patient"]) == 0     # 修前 409「该患者已纳管此病种」


def test_筛查登记填了不存在的机构_404(client, admin, world):
    _expect_404(client.post("/api/spd/screenings", headers=admin, json={
        "patient_id": world["patient"], "program_code": "p2411_prog", "org_id": MISSING}))
    assert _count(SpdScreening, patient_id=world["patient"]) == 0


def test_自动筛查填了不存在的机构_404_不回0条(client, admin, world):
    _expect_404(client.post("/api/spd/screenings/auto-run", headers=admin, json={
        "program_code": "p2411_prog", "org_id": MISSING}))


def test_按方案生成随访填了不存在的机构_404(client, admin, world):
    _expect_404(client.post("/api/spd/followup-plans", headers=admin, json={
        "patient_id": world["patient"], "rule_id": world["rule"], "org_id": MISSING}))
    assert _count(SpdFollowupRecord, patient_id=world["patient"]) == 0


def test_自动匹配随访填了不存在的机构_404_不回0条(client, admin, world):
    _expect_404(client.post("/api/spd/followup-plans/auto-match", headers=admin, json={
        "scene": "outpatient", "org_id": MISSING}))


def test_生成报告填了不存在的机构_404(client, admin, world):
    _expect_404(client.post("/api/spd/report-instances", headers=admin, json={
        "template_code": world["template"], "org_id": MISSING, "period_label": "P2411"}))
    assert _count(SpdReportInstance, org_id=MISSING) == 0
