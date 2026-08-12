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


@router.get("/alerts")
def eldercare_alerts(today: str | None = None, db: Session = Depends(get_db)):
    """㉓老年健康预警/智能提醒：重度失能专案提示 + 年度评估到期复评提醒。"""
    from datetime import timedelta

    from ..deps import resolve_business_date

    current = resolve_business_date(today)
    reassess_before = (current - timedelta(days=365)).isoformat()
    latest: dict[int, ElderlyAssessment] = {}
    for a in db.query(ElderlyAssessment).order_by(ElderlyAssessment.id).all():
        latest[a.patient_id] = a
    alerts = []
    for a in latest.values():
        if a.care_level == "重度失能":
            alerts.append(
                {
                    "patient_id": a.patient_id,
                    "alert_type": "severe_disability",
                    "message": "重度失能，建议纳入家庭病床/上门服务专案",
                    "assessed_date": a.assessed_date,
                }
            )
        if a.assessed_date and a.assessed_date <= reassess_before:
            alerts.append(
                {
                    "patient_id": a.patient_id,
                    "alert_type": "reassess_due",
                    "message": "距上次健康评估已超一年，应安排复评",
                    "assessed_date": a.assessed_date,
                }
            )
    return {"total": len(alerts), "alerts": alerts}


@router.get("/stats")
def eldercare_stats(db: Session = Depends(get_db)):
    """老年健康统计（指引㉓"统计分析"）。

    以**每位老人最近一次评估**为准，不是评估条数——同一人评三次不该在
    失能构成里占三个坑，那会让复评频繁的机构看起来失能率特别高。

    体质辨识与认知筛查未做的单列：这两项本就不是每次评估必做，
    按 0 分并入统计会把"没做"读成"分数为 0"。
    """
    rows = (
        db.query(ElderlyAssessment)
        .order_by(ElderlyAssessment.patient_id, ElderlyAssessment.id.desc())
        .all()
    )
    latest: dict[int, ElderlyAssessment] = {}
    for r in rows:
        latest.setdefault(r.patient_id, r)

    by_level: dict[str, int] = {}
    cognitive_scores, tcm_done = [], 0
    for r in latest.values():
        by_level[r.care_level] = by_level.get(r.care_level, 0) + 1
        if r.cognitive_score > 0:
            cognitive_scores.append(r.cognitive_score)
        if r.tcm_constitution:
            tcm_done += 1
    people = len(latest)
    disabled = people - by_level.get("能力完好", 0)
    return {
        "assessed_people": people,
        "assessment_records": len(rows),
        "by_care_level": by_level,
        "disabled_count": disabled,
        "disabled_rate_pct": round(disabled * 100 / people, 2) if people else None,
        "cognitive": {
            "screened": len(cognitive_scores),
            # 未筛查单列，不按 0 分并入
            "unscreened": people - len(cognitive_scores),
            "avg_score": (
                round(sum(cognitive_scores) / len(cognitive_scores), 1)
                if cognitive_scores else None
            ),
        },
        "tcm_constitution": {"done": tcm_done, "not_done": people - tcm_done},
        "caliber": "按每位老人最近一次评估统计（非评估条数）；"
                   "认知筛查与体质辨识未做的单列，不按 0 分并入",
    }
