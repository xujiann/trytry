"""就诊记录与患者360视图（健康档案汇聚）。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy.orm import Session

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
)
from ..schemas import EncounterCreate, EncounterOut

router = APIRouter(prefix="/api", tags=["就诊与健康档案"], dependencies=[Depends(get_current_user)])


@router.post(
    "/encounters",
    response_model=EncounterOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "operator"))],  # H2: 就诊记录=医疗岗
)
def create_encounter(body: EncounterCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    encounter = Encounter(**body.model_dump())
    db.add(encounter)
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
):
    """就诊记录列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(Encounter)
    if patient_id is not None:
        query = query.filter(Encounter.patient_id == patient_id)
    return paginate(query.order_by(Encounter.id.desc()), response, offset, limit)


# 360 视图每类记录的返回上限：与居民端 portal._build_archive 保持一致。
# 一位管了十年的慢病患者能攒出数百条就诊与上千条处方，全量返回体积不可控（T6.4）。
ARCHIVE_SECTION_LIMIT = 50


def _section(query, limit: int = ARCHIVE_SECTION_LIMIT) -> tuple[list, bool]:
    """取最近 limit 条，并判断是否还有更多（多取一条来判定，不额外做 count）。"""
    rows = query.limit(limit + 1).all()
    return rows[:limit], len(rows) > limit


@router.get("/archive/{ehc_no}")
def patient_360_view(ehc_no: str, db: Session = Depends(get_db)):
    """患者全景360视图：档案、就诊、检查检验报告、慢病、处方一屏汇聚。

    各段取最近 ARCHIVE_SECTION_LIMIT 条，`has_more` 标明是否被截断；
    需要完整清单时走各自的分页列表接口。
    """
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
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
    return {
        "section_limit": ARCHIVE_SECTION_LIMIT,
        "has_more": {
            "encounters": encounters_more,
            "exam_reports": reports_more,
            "prescriptions": prescriptions_more,
            "checkups": checkups_more,
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
