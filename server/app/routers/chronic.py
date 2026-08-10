"""慢病一体化管理：建档、智能分级分组、随访、超期预警。

膳食运动指导要点依据国卫办基层函〔2025〕121号要求嵌入系统，
在接诊和随访时同步返回。
"""
from datetime import date

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import ChronicPatient, FollowUp, Organization, Patient
from ..schemas import ChronicCreate, ChronicOut, FollowUpCreate, FollowUpOut

router = APIRouter(prefix="/api/chronic", tags=["慢病管理"], dependencies=[Depends(get_current_user)])

# 基层膳食运动指导要点（121号文要求嵌入医共体信息系统）
GUIDANCE_POINTS = {
    "hypertension": "限盐（每日<5g）、控制体重、戒烟限酒、每周≥150分钟中等强度运动、规律服药并自测血压",
    "diabetes": "控制总能量摄入、主食粗细搭配、规律三餐、餐后适量运动、监测血糖、遵医嘱用药",
    "copd": "戒烟、防寒保暖避免感染、腹式呼吸与缩唇呼吸锻炼、适度有氧运动、规范吸入用药",
    "obesity": "膳食总量控制、减少高油高糖食品、每周≥150分钟运动并逐步增量、行为记录与定期测量",
    "hyperlipidemia": "限制饱和脂肪与反式脂肪摄入、增加膳食纤维、控制体重、规律运动、定期复查血脂",
}


def _evaluate_level(disease: str, body: FollowUpCreate) -> int | None:
    """智能分级：3=高危需转诊评估, 2=需干预, 1=控制良好。无对应指标时返回 None（维持原级）。"""
    if disease == "hypertension" and body.sbp is not None and body.dbp is not None:
        if body.sbp >= 160 or body.dbp >= 100:
            return 3
        if body.sbp >= 140 or body.dbp >= 90:
            return 2
        return 1
    if disease == "diabetes" and body.glucose is not None:
        if body.glucose >= 10.0:
            return 3
        if body.glucose >= 7.0:
            return 2
        return 1
    return None


@router.post("", response_model=ChronicOut, status_code=201)
def register_chronic(body: ChronicCreate, db: Session = Depends(get_db)):
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.managed_by_org_id) is None:
        raise HTTPException(status_code=404, detail="管理机构不存在")
    existing = (
        db.query(ChronicPatient)
        .filter(ChronicPatient.patient_id == body.patient_id, ChronicPatient.disease == body.disease)
        .first()
    )
    if existing:
        return existing
    chronic = ChronicPatient(**body.model_dump())
    db.add(chronic)
    db.commit()
    db.refresh(chronic)
    return chronic


@router.get("", response_model=list[ChronicOut])
def list_chronic(disease: str | None = None, level: int | None = None, db: Session = Depends(get_db)):
    query = db.query(ChronicPatient)
    if disease:
        query = query.filter(ChronicPatient.disease == disease)
    if level is not None:
        query = query.filter(ChronicPatient.level == level)
    return query.order_by(ChronicPatient.level.desc(), ChronicPatient.id).limit(500).all()


@router.get("/overdue", response_model=list[ChronicOut])
def list_overdue(today: str | None = None, db: Session = Depends(get_db)):
    """随访超期名单：next_due 早于今天的建档患者，推送全科医生任务。"""
    cutoff = today or date.today().isoformat()
    return (
        db.query(ChronicPatient)
        .filter(ChronicPatient.next_due != "", ChronicPatient.next_due < cutoff)
        .order_by(ChronicPatient.next_due)
        .all()
    )


@router.post("/{chronic_id}/followups", status_code=201)
def add_followup(chronic_id: int, body: FollowUpCreate, db: Session = Depends(get_db)):
    chronic = db.get(ChronicPatient, chronic_id)
    if chronic is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")
    followup = FollowUp(chronic_id=chronic_id, **body.model_dump())
    new_level = _evaluate_level(chronic.disease, body)
    if new_level is not None:
        chronic.level = new_level
    if body.next_due:
        chronic.next_due = body.next_due
    db.add(followup)
    db.commit()
    db.refresh(followup)
    return {
        "followup": FollowUpOut.model_validate(followup).model_dump(),
        "level": chronic.level,
        "guidance_points": GUIDANCE_POINTS.get(chronic.disease, ""),
        "refer_up_suggested": chronic.level == 3,
    }


# 风险评分：病种对应的关键随访指标
_RISK_METRICS = {"hypertension": "sbp", "diabetes": "glucose"}
# 当前分级基础分
_LEVEL_BASE = {1: 20, 2: 50, 3: 80}


@router.get("/{chronic_id}/risk")
def risk_score(chronic_id: int, db: Session = Depends(get_db)):
    """简单风险评分：最近3次随访关键指标趋势 + 当前分级加权。

    score = 分级基础分（1级20 / 2级50 / 3级80）
            + 趋势修正（上升 +15，下降 -10，平稳 0）
    风险档位：≥70 高危，≥40 中危，其余低危。
    """
    chronic = db.get(ChronicPatient, chronic_id)
    if chronic is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")

    metric = _RISK_METRICS.get(chronic.disease)
    recent = (
        db.query(FollowUp)
        .filter(FollowUp.chronic_id == chronic_id)
        .order_by(FollowUp.id.desc())
        .limit(3)
        .all()
    )
    values: list[float] = []
    if metric:
        # 按时间正序排列的最近3次有效数值
        values = [
            v for v in (getattr(f, metric) for f in reversed(recent)) if v is not None
        ]

    trend = "insufficient_data"
    adjust = 0
    if len(values) >= 2:
        if values[-1] > values[0]:
            trend, adjust = "rising", 15
        elif values[-1] < values[0]:
            trend, adjust = "falling", -10
        else:
            trend, adjust = "stable", 0

    score = max(0, min(100, _LEVEL_BASE.get(chronic.level, 20) + adjust))
    risk_level = "high" if score >= 70 else "medium" if score >= 40 else "low"
    return {
        "chronic_id": chronic_id,
        "disease": chronic.disease,
        "level": chronic.level,
        "metric": metric or "",
        "recent_values": values,
        "trend": trend,
        "score": score,
        "risk_level": risk_level,
        "refer_up_suggested": risk_level == "high",
    }


@router.get("/{chronic_id}/followups", response_model=list[FollowUpOut])
def list_followups(chronic_id: int, db: Session = Depends(get_db)):
    if db.get(ChronicPatient, chronic_id) is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")
    return (
        db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).order_by(FollowUp.id.desc()).all()
    )
