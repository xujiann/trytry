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


@router.get("/archive/{ehc_no}")
def patient_360_view(ehc_no: str, db: Session = Depends(get_db)):
    """患者全景360视图：档案、就诊、检查检验报告、慢病、处方一屏汇聚。"""
    patient = db.query(Patient).filter(Patient.ehc_no == ehc_no).first()
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    encounters = (
        db.query(Encounter).filter(Encounter.patient_id == patient.id).order_by(Encounter.id.desc()).all()
    )
    reports = (
        db.query(ExamReport)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
        .all()
    )
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient.id).all()
    prescriptions = (
        db.query(Prescription).filter(Prescription.patient_id == patient.id).order_by(Prescription.id.desc()).all()
    )
    return {
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
    }
