"""居民端服务门户：电子健康档案向本人开放（121号文第五条）。

身份核验：电子健康卡号 + 身份证号双因子匹配，仅返回本人档案。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..models import (
    ChronicPatient,
    Encounter,
    ExamReport,
    ExamRequest,
    HealthArticle,
    Patient,
    SatisfactionSurvey,
)
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


class PortalSurveyCreate(BaseModel):
    ehc_no: str
    id_card: str
    target_type: str = Field(pattern="^(contract|encounter|consultation)$")
    target_id: int = 0
    score: int = Field(ge=1, le=5)
    comment: str = ""


@router.post("/surveys", status_code=201)
def portal_submit_survey(body: PortalSurveyCreate, db: Session = Depends(get_db)):
    """居民端满意度提交：电子健康卡号+身份证号双因子核验后，以本人身份评价。"""
    patient = (
        db.query(Patient)
        .filter(Patient.ehc_no == body.ehc_no, Patient.id_card == body.id_card)
        .first()
    )
    if patient is None:
        raise HTTPException(status_code=403, detail="身份核验失败")
    survey = SatisfactionSurvey(
        target_type=body.target_type,
        target_id=body.target_id,
        patient_id=patient.id,
        score=body.score,
        comment=body.comment,
    )
    db.add(survey)
    db.commit()
    return {"id": survey.id, "submitted": True}


@router.get("/health-articles")
def published_articles(category: str | None = None, db: Session = Depends(get_db)):
    """健康宣教：居民端展示已发布文章（无需登录）。"""
    q = db.query(HealthArticle).filter(HealthArticle.status == "published")
    if category:
        q = q.filter(HealthArticle.category == category)
    return [
        {"id": a.id, "title": a.title, "category": a.category, "content": a.content}
        for a in q.order_by(HealthArticle.id.desc()).limit(50).all()
    ]
