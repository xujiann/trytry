"""门（急）诊文书（浙江省指南 #3）：知情告知书、治疗处置记录、门急诊护理记录。

住院文书在 `clinical_docs.py`，那边挂 `admission_id`；这边挂 `encounter_id`。
两套分开而不是合并成"就诊上下文"，理由写在 `NursingRecord` 的模型注释里。

知情告知这块有两个刻意的设计，值得在这里重复一遍：

1. **签署时冻结正文**。模板会随法规修订，但患者签的是签署当时那份措辞。
   所以签署实例把 body 整段拷进 content，不留模板外键。
2. **拒签是一等状态**，不是"没签"。患者有权拒绝，而机构恰恰需要证明
   "告知过、对方拒绝了"——把拒签折叠进 pending，最需要证据的情况反而没了记录。
"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles
from ..models import (
    ConsentTemplate,
    Encounter,
    InformedConsent,
    NursingRecord,
    Organization,
    Patient,
    TreatmentRecord,
    User,
    utcnow,
)

router = APIRouter(prefix="/api/outpatient", tags=["门急诊文书"],
                   dependencies=[Depends(get_current_user)])

CONSENT_TYPE_NAMES = {
    "surgery": "手术", "anesthesia": "麻醉", "transfusion": "输血",
    "exam": "特殊检查", "treatment": "特殊治疗", "other": "其他",
}
CONSENT_STATUS_NAMES = {"pending": "待签署", "signed": "已签署", "refused": "拒绝签署"}
RELATION_NAMES = {
    "self": "本人", "spouse": "配偶", "parent": "父母", "child": "子女", "other": "委托人",
}


# ============================================================ 知情告知书模板


class TemplateIn(BaseModel):
    consent_type: str = Field(pattern="^(surgery|anesthesia|transfusion|exam|treatment|other)$")
    title: str = Field(min_length=1, max_length=128)
    body: str = Field(min_length=1, max_length=8192)
    version: str = Field(default="v1", max_length=16)


class TemplateUpdate(BaseModel):
    title: str | None = None
    body: str | None = None
    version: str | None = None
    active: bool | None = None


def _template_out(t: ConsentTemplate) -> dict:
    return {
        "id": t.id, "consent_type": t.consent_type,
        "consent_type_name": CONSENT_TYPE_NAMES.get(t.consent_type, t.consent_type),
        "title": t.title, "body": t.body, "version": t.version, "active": t.active,
    }


@router.post("/consent-templates", status_code=201, dependencies=[Depends(require_admin)])
def create_template(body: TemplateIn, db: Session = Depends(get_db)):
    template = ConsentTemplate(**body.model_dump())
    db.add(template)
    db.commit()
    return _template_out(template)


@router.get("/consent-templates")
def list_templates(
    consent_type: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    query = db.query(ConsentTemplate)
    if consent_type:
        query = query.filter(ConsentTemplate.consent_type == consent_type)
    if active is not None:
        query = query.filter(ConsentTemplate.active.is_(active))
    return [_template_out(t) for t in query.order_by(ConsentTemplate.id.desc()).limit(200).all()]


@router.patch("/consent-templates/{template_id}", dependencies=[Depends(require_admin)])
def update_template(template_id: int, body: TemplateUpdate, db: Session = Depends(get_db)):
    """改模板只影响**之后**签署的告知书，已签的不受影响（正文已冻结快照）。"""
    template = db.get(ConsentTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="模板不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(template, field, value)
    db.commit()
    return _template_out(template)


# ============================================================ 知情告知书签署


class ConsentIn(BaseModel):
    patient_id: int
    org_id: int
    consent_type: str = Field(pattern="^(surgery|anesthesia|transfusion|exam|treatment|other)$")
    # 用模板生成时给 template_id；不给则须自带 title + content
    template_id: int | None = None
    title: str = Field(default="", max_length=128)
    content: str = Field(default="", max_length=8192)
    related_type: str = Field(default="", max_length=32)
    related_id: int = 0
    doctor_name: str = Field(default="", max_length=64)


class SignIn(BaseModel):
    signer_name: str = Field(min_length=1, max_length=64)
    signer_relation: str = Field(default="self", pattern="^(self|spouse|parent|child|other)$")


class RefuseIn(BaseModel):
    signer_name: str = Field(min_length=1, max_length=64)
    signer_relation: str = Field(default="self", pattern="^(self|spouse|parent|child|other)$")
    refuse_reason: str = Field(min_length=1, max_length=256)


def _consent_out(c: InformedConsent) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "org_id": c.org_id,
        "consent_type": c.consent_type,
        "consent_type_name": CONSENT_TYPE_NAMES.get(c.consent_type, c.consent_type),
        "title": c.title,
        "content": c.content,
        "template_version": c.template_version,
        "related_type": c.related_type,
        "related_id": c.related_id,
        "doctor_name": c.doctor_name,
        "status": c.status,
        "status_name": CONSENT_STATUS_NAMES.get(c.status, c.status),
        "signer_name": c.signer_name,
        "signer_relation": c.signer_relation,
        "signer_relation_name": RELATION_NAMES.get(c.signer_relation, c.signer_relation),
        "signed_at": c.signed_at.isoformat() if c.signed_at else None,
        "refuse_reason": c.refuse_reason,
        "created_at": c.created_at.isoformat(),
    }


@router.post("/consents", status_code=201, dependencies=[Depends(require_roles("doctor"))])
def create_consent(
    body: ConsentIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """生成待签告知书（限医师——告知义务在医师，不能由窗口代劳）。"""
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")

    title, content, version = body.title, body.content, ""
    if body.template_id is not None:
        template = db.get(ConsentTemplate, body.template_id)
        if template is None:
            raise HTTPException(status_code=404, detail="模板不存在")
        if not template.active:
            raise HTTPException(status_code=409, detail="该模板已停用，请选用现行版本")
        if template.consent_type != body.consent_type:
            raise HTTPException(status_code=422, detail="模板类型与告知类型不一致")
        # 拷贝而非引用：患者签的是此刻这份措辞
        title = title or template.title
        content = template.body
        version = template.version
    if not title or not content:
        raise HTTPException(status_code=422, detail="未选用模板时须自带标题与正文")

    consent = InformedConsent(
        patient_id=body.patient_id,
        org_id=body.org_id,
        consent_type=body.consent_type,
        title=title,
        content=content,
        template_version=version,
        related_type=body.related_type,
        related_id=body.related_id,
        doctor_name=body.doctor_name or user.full_name or user.username,
        created_by=user.id,
    )
    db.add(consent)
    db.commit()
    return _consent_out(consent)


@router.get("/consents")
def list_consents(
    response: Response,
    patient_id: int | None = None,
    status: str | None = None,
    related_type: str | None = None,
    related_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(InformedConsent)
    if patient_id is not None:
        query = query.filter(InformedConsent.patient_id == patient_id)
    if status:
        query = query.filter(InformedConsent.status == status)
    if related_type:
        query = query.filter(InformedConsent.related_type == related_type)
    if related_id is not None:
        query = query.filter(InformedConsent.related_id == related_id)
    rows = paginate(query.order_by(InformedConsent.id.desc()), response, offset, limit)
    return [_consent_out(c) for c in rows]


@router.post("/consents/{consent_id}/sign", dependencies=[Depends(require_roles("doctor"))])
def sign_consent(consent_id: int, body: SignIn, db: Session = Depends(get_db)):
    """记录患方签署。已有结论的不可改写——告知书是证据，不是可编辑的表单。"""
    consent = _pending(db, consent_id)
    consent.status = "signed"
    consent.signer_name = body.signer_name
    consent.signer_relation = body.signer_relation
    consent.signed_at = utcnow()
    db.commit()
    return _consent_out(consent)


@router.post("/consents/{consent_id}/refuse", dependencies=[Depends(require_roles("doctor"))])
def refuse_consent(consent_id: int, body: RefuseIn, db: Session = Depends(get_db)):
    """记录拒绝签署。这是一等状态，机构据此证明"告知过、对方拒绝了"。"""
    consent = _pending(db, consent_id)
    consent.status = "refused"
    consent.signer_name = body.signer_name
    consent.signer_relation = body.signer_relation
    consent.refuse_reason = body.refuse_reason
    consent.signed_at = utcnow()
    db.commit()
    return _consent_out(consent)


def _pending(db: Session, consent_id: int) -> InformedConsent:
    consent = db.get(InformedConsent, consent_id)
    if consent is None:
        raise HTTPException(status_code=404, detail="告知书不存在")
    if consent.status != "pending":
        raise HTTPException(
            status_code=409,
            detail=f"该告知书已{CONSENT_STATUS_NAMES.get(consent.status)}，不可重复处理",
        )
    return consent


# ============================================================ 治疗处置记录


class TreatmentIn(BaseModel):
    treatment_name: str = Field(min_length=1, max_length=128)
    treatment_code: str = Field(default="", max_length=64)
    site: str = Field(default="", max_length=64)
    dose: str = Field(default="", max_length=64)
    executor_name: str = Field(default="", max_length=64)
    performed_at: str = Field(default="", max_length=16)
    # 留空表示"未记录"，不等于"无不适"——两者在纠纷里的分量完全不同
    reaction: str = Field(default="", max_length=256)
    note: str = Field(default="", max_length=512)


def _treatment_out(t: TreatmentRecord) -> dict:
    return {
        "id": t.id, "encounter_id": t.encounter_id, "patient_id": t.patient_id,
        "org_id": t.org_id, "treatment_name": t.treatment_name,
        "treatment_code": t.treatment_code, "site": t.site, "dose": t.dose,
        "executor_name": t.executor_name, "performed_at": t.performed_at,
        "reaction": t.reaction, "note": t.note,
        "created_at": t.created_at.isoformat(),
    }


@router.post(
    "/encounters/{encounter_id}/treatments",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def create_treatment(
    encounter_id: int,
    body: TreatmentIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    record = TreatmentRecord(
        encounter_id=encounter_id,
        patient_id=encounter.patient_id,
        org_id=encounter.org_id,
        created_by=user.id,
        **body.model_dump(),
    )
    db.add(record)
    db.commit()
    return _treatment_out(record)


@router.get("/encounters/{encounter_id}/treatments")
def list_treatments(encounter_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(TreatmentRecord)
        .filter(TreatmentRecord.encounter_id == encounter_id)
        .order_by(TreatmentRecord.id.desc())
        .limit(200)
        .all()
    )
    return [_treatment_out(t) for t in rows]


@router.get("/treatments")
def list_treatments_by_patient(
    response: Response,
    patient_id: int,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """按患者查处置史：换药、雾化这类连续处置需要跨就诊看一条线。"""
    query = (
        db.query(TreatmentRecord)
        .filter(TreatmentRecord.patient_id == patient_id)
        .order_by(TreatmentRecord.id.desc())
    )
    return [_treatment_out(t) for t in paginate(query, response, offset, limit)]


# ============================================================ 门急诊护理记录


class OutpatientNursingIn(BaseModel):
    nursing_level: str = Field(default="level3", pattern="^(special|level1|level2|level3)$")
    content: str = Field(min_length=1, max_length=2048)
    nurse_name: str = Field(default="", max_length=64)
    recorded_at: str = Field(default="", max_length=16)


@router.post(
    "/encounters/{encounter_id}/nursing-records",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def create_outpatient_nursing(
    encounter_id: int,
    body: OutpatientNursingIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """门急诊护理记录：输液观察、留观、清创换药。"""
    if db.get(Encounter, encounter_id) is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    record = NursingRecord(
        encounter_id=encounter_id,
        nursing_level=body.nursing_level,
        content=body.content,
        nurse_name=body.nurse_name,
        recorded_at=body.recorded_at or datetime.now().strftime("%Y-%m-%d %H:%M"),
        created_by=user.id,
    )
    db.add(record)
    db.commit()
    return {
        "id": record.id,
        "encounter_id": record.encounter_id,
        "nursing_level": record.nursing_level,
        "content": record.content,
        "nurse_name": record.nurse_name,
        "recorded_at": record.recorded_at,
    }


@router.get("/encounters/{encounter_id}/nursing-records")
def list_outpatient_nursing(encounter_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(NursingRecord)
        .filter(NursingRecord.encounter_id == encounter_id)
        .order_by(NursingRecord.id.desc())
        .limit(200)
        .all()
    )
    return [
        {
            "id": r.id, "nursing_level": r.nursing_level, "content": r.content,
            "nurse_name": r.nurse_name, "recorded_at": r.recorded_at,
        }
        for r in rows
    ]


@router.get("/encounters/{encounter_id}/completeness")
def encounter_completeness(encounter_id: int, db: Session = Depends(get_db)):
    """门急诊文书完整性自查：哪些该有而没有。

    与住院文书的完整性检查同一思路，但**不给"合格/不合格"结论**——门急诊
    并非每次就诊都需要处置记录或告知书，感冒开药就该只有病历。只列事实，
    由质控按科室规则判断。
    """
    encounter = db.get(Encounter, encounter_id)
    if encounter is None:
        raise HTTPException(status_code=404, detail="就诊记录不存在")
    treatments = (
        db.query(TreatmentRecord).filter(TreatmentRecord.encounter_id == encounter_id).count()
    )
    nursing = db.query(NursingRecord).filter(NursingRecord.encounter_id == encounter_id).count()
    consents = (
        db.query(InformedConsent)
        .filter(
            InformedConsent.related_type == "encounter",
            InformedConsent.related_id == encounter_id,
        )
        .all()
    )
    return {
        "encounter_id": encounter_id,
        "patient_id": encounter.patient_id,
        "treatment_records": treatments,
        "nursing_records": nursing,
        "consents_total": len(consents),
        "consents_pending": sum(1 for c in consents if c.status == "pending"),
        "consents_refused": sum(1 for c in consents if c.status == "refused"),
        # 只报事实：待签的告知书是真正该被追的那一项，拒签不是缺陷
        "note": "门急诊并非每次就诊都需处置与告知，本表只列事实，不判合格与否；"
                "待签署的告知书应在就诊结束前处理完毕",
    }
