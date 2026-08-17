"""E9 互操作标准化：FHIR 资源扩展 + CDA 文档导出 + 省平台上报连接器骨架。

出站导出沿用 integration.py 的既定姿态：调用方是区域平台/接口引擎侧的对接账号
（operator 角色），按设计要能跨机构导出，故不做机构阻断，而是每次导出 log_patient_access
留痕（可问责）。省平台上报单是机构自有数据，按 A案 做机构隔离。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..clock import now_naive
from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import (
    Admission,
    Encounter,
    ExamReport,
    ExamRequest,
    Organization,
    Patient,
    Prescription,
    ProvincialReport,
    Referral,
    ReferralClinicalRef,
    User,
)
from ..routers.integration import (
    EHC_SYSTEM,
    ID_CARD_SYSTEM,
    _GENDER_TO_FHIR,
    _log_exchange,
    parse_fhir_patient,
)
from ..routers.patients import create_patient_idempotent
from ..visibility import (
    assert_obj_org_writable,
    assert_org_writable,
    log_patient_access,
    scope_org_list,
)

router = APIRouter(prefix="/api/integration", tags=["互操作标准化"],
                   dependencies=[Depends(require_roles("operator"))])


def _esc_xml(s: str) -> str:
    return (str(s or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _cda_header(db: Session, user: User, org_id: int | None, doc_ext: str) -> str:
    """CDA R2 头部块：文档 id / effectiveTime / author（导出操作员）/ custodian（保管机构）。

    时间统一走平台唯一时间入口 now_naive（禁裸 datetime.now），格式化为 CDA
    HL7 TS（YYYYMMDDHHMMSS）。custodian 按文档归属机构 org_id 查 Organization。
    """
    ts = now_naive().strftime("%Y%m%d%H%M%S")
    author_name = _esc_xml((user.full_name or user.username) if user else "")
    org = db.get(Organization, org_id) if org_id else None
    org_name = _esc_xml(org.name if org else "")
    return f"""  <id root="2.16.156.10011.x" extension="{_esc_xml(doc_ext)}"/>
  <effectiveTime value="{ts}"/>
  <author>
    <time value="{ts}"/>
    <assignedAuthor>
      <assignedPerson><name>{author_name}</name></assignedPerson>
    </assignedAuthor>
  </author>
  <custodian>
    <assignedCustodian>
      <representedCustodianOrganization><name>{org_name}</name></representedCustodianOrganization>
    </assignedCustodian>
  </custodian>"""


# ================================================================ FHIR 资源扩展（出站）
@router.get("/fhir/Encounter/{encounter_id}")
def fhir_encounter(encounter_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    enc = db.get(Encounter, encounter_id)
    if enc is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    log_patient_access(db, user, enc.patient_id, "fhir_encounter", "export")
    patient = db.get(Patient, enc.patient_id)
    return {
        "resourceType": "Encounter",
        "id": str(enc.id),
        "status": "finished",
        "class": {"code": "AMB" if enc.encounter_type == "outpatient" else "IMP"},
        "subject": {"reference": f"Patient/{patient.ehc_no if patient else enc.patient_id}"},
        "serviceProvider": {"reference": f"Organization/{enc.org_id}"},
        "period": {"start": enc.created_at.isoformat() if enc.created_at else None},
        "reasonCode": ([{"text": enc.diagnosis_name}] if enc.diagnosis_name else []),
    }


@router.get("/fhir/Condition/encounter/{encounter_id}")
def fhir_condition(encounter_id: int, db: Session = Depends(get_db),
                   user: User = Depends(get_current_user)):
    enc = db.get(Encounter, encounter_id)
    if enc is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    if not enc.diagnosis_name:
        raise HTTPException(status_code=404, detail="该就诊无诊断可映射为 Condition")
    log_patient_access(db, user, enc.patient_id, "fhir_condition", "export")
    patient = db.get(Patient, enc.patient_id)
    return {
        "resourceType": "Condition",
        "id": f"enc-{enc.id}",
        "code": {"coding": ([{"code": enc.diagnosis_code}] if enc.diagnosis_code else []),
                 "text": enc.diagnosis_name},
        "subject": {"reference": f"Patient/{patient.ehc_no if patient else enc.patient_id}"},
        "encounter": {"reference": f"Encounter/{enc.id}"},
    }


@router.get("/fhir/MedicationRequest/{prescription_id}")
def fhir_medication_request(prescription_id: int, db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    pres = db.get(Prescription, prescription_id)
    if pres is None:
        raise HTTPException(status_code=404, detail="处方不存在")
    log_patient_access(db, user, pres.patient_id, "fhir_medication", "export")
    patient = db.get(Patient, pres.patient_id)
    return {
        "resourceType": "MedicationRequest",
        "id": str(pres.id),
        "status": "active" if pres.status in ("auto_passed", "approved") else "unknown",
        "intent": "order",
        "subject": {"reference": f"Patient/{patient.ehc_no if patient else pres.patient_id}"},
        "reasonCode": ([{"text": pres.diagnosis_name}] if pres.diagnosis_name else []),
        "authoredOn": pres.created_at.isoformat() if pres.created_at else None,
    }


@router.get("/fhir/DiagnosticReport/{report_id}")
def fhir_diagnostic_report(report_id: int, db: Session = Depends(get_db),
                           user: User = Depends(get_current_user)):
    rep = db.get(ExamReport, report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail="检查报告不存在")
    req = db.get(ExamRequest, rep.request_id)
    if req is None:
        raise HTTPException(status_code=404, detail="检查申请不存在")
    log_patient_access(db, user, req.patient_id, "fhir_diagnostic", "export")
    patient = db.get(Patient, req.patient_id)
    return {
        "resourceType": "DiagnosticReport",
        "id": str(rep.id),
        "status": "final",
        "category": [{"text": req.center_type}],
        "code": {"text": req.item_name},
        "subject": {"reference": f"Patient/{patient.ehc_no if patient else req.patient_id}"},
        "conclusion": rep.conclusion,
        "effectiveDateTime": rep.reported_at.isoformat() if rep.reported_at else None,
    }


@router.get("/fhir/Bundle/patient/{ehc_no}")
def fhir_bundle(ehc_no: str, db: Session = Depends(get_db),
                user: User = Depends(get_current_user)):
    """患者 FHIR 事务 Bundle：Patient + 近期 Encounter/Condition 打包出站。"""
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    log_patient_access(db, user, patient.id, "fhir_bundle", "export")
    entries = [{
        "resource": {
            "resourceType": "Patient", "id": patient.ehc_no,
            "identifier": [{"system": EHC_SYSTEM, "value": patient.ehc_no},
                           {"system": ID_CARD_SYSTEM, "value": patient.id_card if user.role == "admin" else "***"}],
            "name": [{"text": patient.name}],
            "gender": _GENDER_TO_FHIR.get(patient.gender, "unknown"),
            "birthDate": patient.birth_date,
        }
    }]
    encs = db.query(Encounter).filter(Encounter.patient_id == patient.id).order_by(
        Encounter.id.desc()).limit(20).all()
    for enc in encs:
        entries.append({"resource": {
            "resourceType": "Encounter", "id": str(enc.id), "status": "finished",
            "subject": {"reference": f"Patient/{patient.ehc_no}"},
            "period": {"start": enc.created_at.isoformat() if enc.created_at else None},
            "reasonCode": ([{"text": enc.diagnosis_name}] if enc.diagnosis_name else []),
        }})
        if enc.diagnosis_name:
            entries.append({"resource": {
                "resourceType": "Condition", "id": f"enc-{enc.id}",
                "code": {"text": enc.diagnosis_name},
                "subject": {"reference": f"Patient/{patient.ehc_no}"},
                "encounter": {"reference": f"Encounter/{enc.id}"},
            }})
    return {"resourceType": "Bundle", "type": "collection", "total": len(entries),
            "entry": entries}


# ================================================================ FHIR Bundle 入站
class BundleIn(BaseModel):
    resourceType: str = Field(pattern="^Bundle$")
    entry: list[dict] = []


@router.post("/fhir/Bundle")
def fhir_bundle_inbound(body: BundleIn, db: Session = Depends(get_db),
                        user: User = Depends(get_current_user)):
    """入站事务 Bundle：逐条处理 Patient 资源（EMPI 幂等），返回每条结果。"""
    results = []
    for i, ent in enumerate(body.entry):
        res = ent.get("resource", {}) if isinstance(ent, dict) else {}
        rtype = res.get("resourceType")
        try:
            if rtype == "Patient":
                fields = parse_fhir_patient(res)
                patient, _created = create_patient_idempotent(db, fields)
                results.append({"index": i, "resourceType": "Patient",
                                "status": "ok", "ehc_no": patient.ehc_no})
            elif rtype in ("Encounter", "Condition"):
                # 无干净的机构映射，不落库，但确认接收（不再当作不支持而丢弃），
                # 单独计入 accepted，与 Patient 的 ok 区分开。
                results.append({"index": i, "resourceType": rtype,
                                "status": "accepted",
                                "reason": "已接收确认（临床上下文，未落库）"})
            else:
                results.append({"index": i, "resourceType": rtype or "unknown",
                                "status": "skipped", "reason": "仅支持 Patient/Encounter/Condition 条目入站"})
        except HTTPException as exc:
            results.append({"index": i, "resourceType": rtype, "status": "error",
                            "reason": exc.detail})
    ok = sum(1 for r in results if r["status"] == "ok")
    accepted = sum(1 for r in results if r["status"] == "accepted")
    _log_exchange("fhir_bundle", ok > 0 or accepted > 0 or not body.entry,
                  error_detail="" if (ok or accepted) else "无成功条目", source_system="")
    return {"total": len(results), "succeeded": ok, "accepted": accepted,
            "results": results}


# ================================================================ CDA 文档导出
@router.get("/cda/discharge/{admission_id}")
def cda_discharge(admission_id: int, db: Session = Depends(get_db),
                  user: User = Depends(get_current_user)):
    """出院小结导出为 CDA R2 结构（简化 ClinicalDocument）。"""
    adm = db.get(Admission, admission_id)
    if adm is None:
        raise HTTPException(status_code=404, detail="住院记录不存在")
    log_patient_access(db, user, adm.patient_id, "cda_discharge", "export")
    patient = db.get(Patient, adm.patient_id)
    header = _cda_header(db, user, adm.org_id, f"discharge-{adm.id}")
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <typeId root="2.16.840.1.113883.1.3"/>
  <code code="34105-7" displayName="出院小结"/>
  <title>出院小结</title>
{header}
  <recordTarget><patientRole>
    <id extension="{_esc_xml(patient.ehc_no if patient else '')}"/>
    <patient><name>{_esc_xml(patient.name if patient else '')}</name></patient>
  </patientRole></recordTarget>
  <component><structuredBody>
    <component><section>
      <code code="11535-2" displayName="出院诊断"/>
      <text>{_esc_xml(adm.diagnosis_name)}</text>
    </section></component>
    <component><section>
      <code code="8648-8" displayName="住院经过"/>
      <text>入院 {_esc_xml(adm.admitted_at.isoformat() if adm.admitted_at else '')}；主管医师 {_esc_xml(adm.doctor_name)}</text>
    </section></component>
  </structuredBody></component>
</ClinicalDocument>"""
    return Response(content=xml, media_type="application/xml")


@router.get("/cda/referral/{referral_id}")
def cda_referral(referral_id: int, db: Session = Depends(get_db),
                 user: User = Depends(get_current_user)):
    """转诊单导出为 CDA 结构（承接 E3 病历随转）。"""
    ref = db.get(Referral, referral_id)
    if ref is None:
        raise HTTPException(status_code=404, detail="转诊记录不存在")
    log_patient_access(db, user, ref.patient_id, "cda_referral", "export")
    patient = db.get(Patient, ref.patient_id)
    header = _cda_header(db, user, ref.from_org_id, f"referral-{ref.id}")
    # 承接 E3 病历随转：转诊挂接的临床引用逐条随文档下传，作为独立 section。
    clinical_refs = db.query(ReferralClinicalRef).filter(
        ReferralClinicalRef.referral_id == ref.id).order_by(ReferralClinicalRef.id).all()
    ref_sections = "".join(
        f"""
    <component><section>
      <code code="55112-7" displayName="病历随转-{_esc_xml(cr.ref_type)}"/>
      <title>病历随转（{_esc_xml(cr.ref_type)}）</title>
      <text>{_esc_xml(cr.summary)}</text>
    </section></component>""" for cr in clinical_refs)
    xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<ClinicalDocument xmlns="urn:hl7-org:v3">
  <code code="57133-1" displayName="转诊单"/>
  <title>双向转诊单（{_esc_xml('上转' if ref.direction == 'up' else '下转')}）</title>
{header}
  <recordTarget><patientRole>
    <id extension="{_esc_xml(patient.ehc_no if patient else '')}"/>
    <patient><name>{_esc_xml(patient.name if patient else '')}</name></patient>
  </patientRole></recordTarget>
  <component><structuredBody><component><section>
    <code code="42349-1" displayName="转诊原因"/>
    <text>{_esc_xml(ref.reason)}</text>
    <text>转出机构 {ref.from_org_id} → 转入机构 {ref.to_org_id}</text>
  </section></component>{ref_sections}
  </structuredBody></component>
</ClinicalDocument>"""
    return Response(content=xml, media_type="application/xml")


# ================================================================ 省平台上报连接器骨架
class ProvincialReportIn(BaseModel):
    report_type: str = Field(min_length=1, max_length=32)
    org_id: int
    target: str = Field(default="provincial", pattern="^(provincial|municipal|national)$")
    payload: dict = {}


def _submit_adapter(report: ProvincialReport) -> None:
    """外部适配器占位：实盘投递属外部依赖，这里 mock 为"提交即回执"。

    真实实现从这里替换为对省平台/接口引擎的出站调用，其余流程不变。
    """
    report.status = "acked"
    report.ack = f"MOCK-ACK-{report.report_type}"


@router.post("/provincial/reports", status_code=201)
def submit_provincial_report(body: ProvincialReportIn, db: Session = Depends(get_db),
                             user: User = Depends(get_current_user)):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    report = ProvincialReport(report_type=body.report_type, org_id=body.org_id,
                              target=body.target, payload=body.payload, status="queued")
    db.add(report)
    db.flush()
    _submit_adapter(report)
    db.commit()
    db.refresh(report)
    return _report_out(report)


@router.get("/provincial/reports")
def list_provincial_reports(report_type: str | None = None, status: str | None = None,
                            org_id: int | None = None, db: Session = Depends(get_db),
                            user: User = Depends(get_current_user)):
    q = db.query(ProvincialReport)
    if report_type:
        q = q.filter(ProvincialReport.report_type == report_type)
    if status:
        q = q.filter(ProvincialReport.status == status)
    q = scope_org_list(db, user, q, ProvincialReport, org_id)
    return [_report_out(r) for r in q.order_by(ProvincialReport.id.desc()).limit(300).all()]


def _report_out(r: ProvincialReport) -> dict:
    return {"id": r.id, "report_type": r.report_type, "org_id": r.org_id, "target": r.target,
            "status": r.status, "ack": r.ack, "created_at": r.created_at.isoformat()}
