"""㉓老年健康业务协同：自理能力评估（ADL自动分级）、认知筛查、体质辨识。"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import ElderlyAssessment, Patient

router = APIRouter(prefix="/api/eldercare", tags=["老年健康"], dependencies=[Depends(get_current_user)])


def grade_adl(score: int) -> str:
    """Barthel 指数分级。"""
    if score >= 95:
        return "能力完好"
    if score >= 60:
        return "轻度失能"
    if score >= 40:
        return "中度失能"
    return "重度失能"


class AssessmentCreate(BaseModel):
    patient_id: int
    adl_score: int = Field(ge=0, le=100)
    cognitive_score: int = Field(default=0, ge=0, le=30)
    tcm_constitution: str = ""
    assessed_date: str = ""


class AssessmentOut(AssessmentCreate):
    id: int
    care_level: str

    model_config = {"from_attributes": True}


@router.post(
    "/assessments",
    response_model=AssessmentOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 老年健康评估
)
def create_assessment(body: AssessmentCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assessment = ElderlyAssessment(**body.model_dump(), care_level=grade_adl(body.adl_score))
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment


@router.get("/assessments", response_model=list[AssessmentOut])
def list_assessments(patient_id: int | None = None, care_level: str | None = None, db: Session = Depends(get_db)):
    query = db.query(ElderlyAssessment)
    if patient_id is not None:
        query = query.filter(ElderlyAssessment.patient_id == patient_id)
    if care_level:
        query = query.filter(ElderlyAssessment.care_level == care_level)
    return query.order_by(ElderlyAssessment.id.desc()).limit(200).all()


@router.get("/disabled")
def disabled_elderly(db: Session = Depends(get_db)):
    """失能老人清单（每人取最新一次评估），供上门服务与家庭病床对接。"""
    latest: dict[int, ElderlyAssessment] = {}
    for a in db.query(ElderlyAssessment).order_by(ElderlyAssessment.id).all():
        latest[a.patient_id] = a
    return [
        {"patient_id": a.patient_id, "care_level": a.care_level, "adl_score": a.adl_score}
        for a in latest.values()
        if a.care_level != "能力完好"
    ]
