"""就诊记录与患者360视图（健康档案汇聚）。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

from .. import events
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles
from ..models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    Organization,
    Patient,
    PhysicalExam,
    Prescription,
    Settlement,
    User,
)
from ..visibility import assert_org_writable, assert_patient_visible, visible_org_ids
from ..schemas import EncounterCreate, EncounterOut

router = APIRouter(prefix="/api", tags=["就诊与健康档案"], dependencies=[Depends(get_current_user)])


@router.post(
    "/encounters",
    response_model=EncounterOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 就诊记录=医疗岗
)
def create_encounter(
    body: EncounterCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # 自授权闭链防护：就诊记录是 patient_basis 的最高优先级可见性依据，
    # 若不校验机构归属，任何人 POST 一条本不属于自己机构的就诊即可解锁该患者全档案。
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    encounter = Encounter(**body.model_dump())
    db.add(encounter)
    db.flush()
    events.publish(db, events.ENCOUNTER_CREATED, {
        "encounter_id": encounter.id,
        "patient_id": encounter.patient_id,
        "org_id": encounter.org_id,
        "encounter_type": encounter.encounter_type,
        "diagnosis_code": encounter.diagnosis_code or "",
        "diagnosis_name": encounter.diagnosis_name or "",
    })
    db.commit()
    db.refresh(encounter)
    return encounter


@router.get("/encounters", response_model=list[EncounterOut])
def list_encounters(
    response: Response,
    patient_id: int | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """就诊记录列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。

    第九轮：**不带 patient_id 时只返回本机构的就诊记录**。原先返回全表——
    任何一个登录账号翻一页就拿到全县的就诊记录，这是最直接的横向越权。
    带了 patient_id 则走患者可见性判定（并留痕）。
    """
    query = db.query(Encounter)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="encounter")
        query = query.filter(Encounter.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(Encounter.org_id.in_(orgs))
    return paginate(query.order_by(Encounter.id.desc()), response, offset, limit)


# 360 视图每类记录的返回上限：与居民端 portal._build_archive 保持一致。
# 一位管了十年的慢病患者能攒出数百条就诊与上千条处方，全量返回体积不可控（T6.4）。
ARCHIVE_SECTION_LIMIT = 50


def _section(query, limit: int = ARCHIVE_SECTION_LIMIT) -> tuple[list, bool]:
    """取最近 limit 条，并判断是否还有更多（多取一条来判定，不额外做 count）。"""
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


@router.get("/archive/{ehc_no}")
def patient_360_view(
    ehc_no: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """患者全景360视图：档案、就诊、检查检验报告、慢病、处方一屏汇聚。

    各段取最近 ARCHIVE_SECTION_LIMIT 条，`has_more` 标明是否被截断；
    需要完整清单时走各自的分页列表接口。

    第九轮：这是全平台聚合度最高的一个接口——一次调用拿到一个人的就诊、
    检查、慢病、处方。**它此前对任何登录账号开放**，实测乙镇卫生院的医生
    凭 ehc_no 就能看甲县医院患者的全部诊疗信息。现在须有业务关系并留痕。
    """
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assert_patient_visible(db, user, patient.id, resource="archive_360")
    encounters, encounters_more = _section(
        db.query(Encounter).filter(Encounter.patient_id == patient.id).order_by(Encounter.id.desc())
    )
    reports, reports_more = _section(
        db.query(ExamReport)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
    )
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient.id).all()
    prescriptions, prescriptions_more = _section(
        db.query(Prescription).filter(Prescription.patient_id == patient.id).order_by(Prescription.id.desc())
    )
    checkups, checkups_more = _section(
        db.query(PhysicalExam).filter(PhysicalExam.patient_id == patient.id).order_by(PhysicalExam.id.desc())
    )
    # 医疗费用记录（指南 #2 病历概要要求的第三类内容）。
    # 取结算单而非费用明细：明细是一次就诊几十上百条，塞进 360 视图会把真正
    # 该被看见的临床信息挤下去；要看明细走 /api/billing/details。
    settlements, settlements_more = _section(
        db.query(Settlement)
        .filter(Settlement.patient_id == patient.id)
        .order_by(Settlement.id.desc())
    )
    return {
        "section_limit": ARCHIVE_SECTION_LIMIT,
        "has_more": {
            "encounters": encounters_more,
            "exam_reports": reports_more,
            "prescriptions": prescriptions_more,
            "checkups": checkups_more,
            "settlements": settlements_more,
        },
        "patient": {
            "ehc_no": patient.ehc_no,
            "name": patient.name,
            "gender": patient.gender,
            "birth_date": patient.birth_date,
        },
        "encounters": [
            {
                "id": e.id,
                "org_id": e.org_id,
                "encounter_type": e.encounter_type,
                "diagnosis_name": e.diagnosis_name,
                "summary": e.summary,
            }
            for e in encounters
        ],
        "exam_reports": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "conclusion": r.conclusion,
                "critical": r.critical,
            }
            for r in reports
        ],
        "chronic_diseases": [
            {"id": c.id, "disease": c.disease, "level": c.level, "next_due": c.next_due} for c in chronic
        ],
        "prescriptions": [
            {"id": p.id, "diagnosis_name": p.diagnosis_name, "status": p.status} for p in prescriptions
        ],
        "settlements": [
            {
                "id": s.id,
                "bill_type": s.bill_type,
                "total_amount": s.total_amount,
                "insurance_pay": s.insurance_pay,
                "self_pay": s.self_pay,
                "created_at": s.created_at.isoformat(),
            }
            for s in settlements
        ],
        "physical_exams": [
            {
                "id": e.id,
                "exam_date": e.exam_date,
                "package_name": e.package_name,
                "has_abnormal": e.has_abnormal,
                "abnormal_items": e.abnormal_items,
            }
            for e in checkups
        ],
    }
