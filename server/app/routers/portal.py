"""居民端服务门户：电子健康档案向本人开放（121号文第五条）。

身份核验：电子健康卡号 + 身份证号双因子匹配，仅返回本人档案。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import ChronicPatient, Encounter, ExamReport, ExamRequest, Patient
from .chronic import GUIDANCE_POINTS

router = APIRouter(prefix="/api/portal", tags=["居民端"])


@router.get("/my-archive")
def my_archive(ehc_no: str, id_card: str, db: Session = Depends(get_db)):
    patient = (
        db.query(Patient).filter(Patient.ehc_no == ehc_no, Patient.id_card == id_card).first()
    )
    if patient is None:
        raise HTTPException(status_code=403, detail="身份核验失败")

    encounters = (
        db.query(Encounter)
        .filter(Encounter.patient_id == patient.id)
        .order_by(Encounter.id.desc())
        .limit(50)
        .all()
    )
    reports = (
        db.query(ExamReport)
        .join(ExamRequest, ExamReport.request_id == ExamRequest.id)
        .filter(ExamRequest.patient_id == patient.id)
        .order_by(ExamReport.id.desc())
        .limit(50)
        .all()
    )
    chronic = db.query(ChronicPatient).filter(ChronicPatient.patient_id == patient.id).all()

    return {
        "name": patient.name,
        "ehc_no": patient.ehc_no,
        "encounters": [
            {"diagnosis_name": e.diagnosis_name, "encounter_type": e.encounter_type, "summary": e.summary}
            for e in encounters
        ],
        "exam_reports": [{"conclusion": r.conclusion, "critical": r.critical} for r in reports],
        "chronic_care": [
            {
                "disease": c.disease,
                "level": c.level,
                "next_followup_due": c.next_due,
                "guidance_points": GUIDANCE_POINTS.get(c.disease, ""),
            }
            for c in chronic
        ],
    }
