"""㉓老年健康业务协同：自理能力评估（ADL自动分级）、认知筛查、体质辨识。"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..visibility import scope_patient_list
from ..database import get_db
from ..datetypes import OptionalDateStr
from ..deps import get_current_user, paginate, require_roles
from ..models import ElderlyAssessment, Patient, User

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


def _latest_by_patient(rows: list[ElderlyAssessment]) -> dict[int, ElderlyAssessment]:
    """每位老人最近一次评估：按评估日期取最晚的（没填日期的按录入那天），同一天取后录的。

    原先按编号取最后录的那条（P2-125）：补录一张更早的纸质评估表，它就成了「最新一次」——失能清单、重度失能提醒、
    统计都按那张旧表算，年度复评提醒也按它的日期报「已超一年」。键的先后照 rows 里各人第一次出现的顺序（与原先一致）。
    """
    latest: dict[int, ElderlyAssessment] = {}
    for row in rows:
        current = latest.get(row.patient_id)
        if current is None or _assessed_on(row) >= _assessed_on(current):
            latest[row.patient_id] = row
    return latest


def _assessed_on(row: ElderlyAssessment) -> str:
    return row.assessed_date or row.created_at.date().isoformat()


class AssessmentCreate(BaseModel):
    patient_id: int
    adl_score: int = Field(ge=0, le=100)
    cognitive_score: int = Field(default=0, ge=0, le=30)
    tcm_constitution: str = Field(default="", max_length=32)
    # 复评提醒按字符串比 `assessed_date <= 一年前`：`2025/01/15`、`20250115` 这类写法
    # 在同一年份里比出来是反的，这个人就永远不进年度复评提醒（P1-61，实测）
    assessed_date: OptionalDateStr = ""


class AssessmentOut(AssessmentCreate):
    id: int
    care_level: str
    # 出参不带入参的日历校验（P1-63）：库里的存量坏日期要原样读出来，而不是让响应 500
    assessed_date: str = ""

    model_config = {"from_attributes": True}


@router.post(
    "/assessments",
    response_model=AssessmentOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2/L5: 老年健康评估
)
def create_assessment(
    body: AssessmentCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    assessment = ElderlyAssessment(
        **body.model_dump(), care_level=grade_adl(body.adl_score), org_id=user.org_id
    )
    db.add(assessment)
    db.commit()
    db.refresh(assessment)
    return assessment


@router.get("/assessments", response_model=list[AssessmentOut])
def list_assessments(
    response: Response,
    patient_id: int | None = None,
    care_level: str | None = None,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(ElderlyAssessment)
    query = scope_patient_list(db, user, query, ElderlyAssessment, patient_id, "eldercare")
    if care_level:
        query = query.filter(ElderlyAssessment.care_level == care_level)
    return paginate(query.order_by(ElderlyAssessment.id.desc()), response, offset, limit)


class DisabledElderOut(BaseModel):
    """失能老人清单行（每人取最新一次评估）。"""

    patient_id: int
    care_level: str
    adl_score: int


class EldercareAlertOut(BaseModel):
    patient_id: int
    alert_type: str
    message: str
    assessed_date: str


class EldercareAlertsOut(BaseModel):
    total: int
    alerts: list[EldercareAlertOut]


class EldercareCognitiveOut(BaseModel):
    """认知筛查小结。`avg_score` 是真除法派生：有筛查必 float、无人筛查为 null
    （键恒在值可空，非条件键）。"""

    screened: int
    unscreened: int
    avg_score: float | None


class EldercareTcmOut(BaseModel):
    done: int
    not_done: int


class EldercareStatsOut(BaseModel):
    """老年健康统计。`by_care_level` 键是失能等级中文名、随数据变 → 宽 dict；
    `disabled_rate_pct` 真除法派生：有人必 float、无人为 null。"""

    assessed_people: int
    assessment_records: int
    by_care_level: dict[str, int]
    disabled_count: int
    disabled_rate_pct: float | None
    cognitive: EldercareCognitiveOut
    tcm_constitution: EldercareTcmOut
    caliber: str


@router.get("/disabled", response_model=list[DisabledElderOut])
def disabled_elderly(db: Session = Depends(get_db)):
    """失能老人清单（每人取最新一次评估），供上门服务与家庭病床对接。"""
    latest = _latest_by_patient(db.query(ElderlyAssessment).order_by(ElderlyAssessment.id).all())
    return [
        {"patient_id": a.patient_id, "care_level": a.care_level, "adl_score": a.adl_score}
        for a in latest.values()
        if a.care_level != "能力完好"
    ]


@router.get("/alerts", response_model=EldercareAlertsOut)
def eldercare_alerts(today: str | None = None, db: Session = Depends(get_db)):
    """㉓老年健康预警/智能提醒：重度失能专案提示 + 年度评估到期复评提醒。"""
    from datetime import timedelta

    from ..deps import resolve_business_date

    current = resolve_business_date(today)
    reassess_before = (current - timedelta(days=365)).isoformat()
    latest = _latest_by_patient(db.query(ElderlyAssessment).order_by(ElderlyAssessment.id).all())
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
        # 与「最近一次评估」同一个日期口径：没填评估日期的按录入那天算（P2-212）。原先只看 assessed_date，
        # 日期那一格是选填的，没填日期的老人永远不出复评提醒
        if _assessed_on(a) <= reassess_before:
            alerts.append(
                {
                    "patient_id": a.patient_id,
                    "alert_type": "reassess_due",
                    "message": "距上次健康评估已超一年，应安排复评",
                    "assessed_date": a.assessed_date,
                }
            )
    return {"total": len(alerts), "alerts": alerts}


@router.get("/stats", response_model=EldercareStatsOut)
def eldercare_stats(db: Session = Depends(get_db)):
    """老年健康统计（指引㉓"统计分析"）。

    以**每位老人最近一次评估**为准，不是评估条数——同一人评三次不该在
    失能构成里占三个坑，那会让复评频繁的机构看起来失能率特别高。

    体质辨识与认知筛查未做的单列：这两项本就不是每次评估必做，
    按 0 分并入统计会把"没做"读成"分数为 0"。
    """
    rows = (
        db.query(ElderlyAssessment)
        .order_by(ElderlyAssessment.patient_id, ElderlyAssessment.id)
        .all()
    )
    latest = _latest_by_patient(rows)

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
