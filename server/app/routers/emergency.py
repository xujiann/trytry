"""⑦县域智慧医疗急救：呼救调度→转运（生命体征实时回传）→到院→收治，"上车即入院"。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, require_roles
from ..models import EmergencyCase, EmergencyMilestone, EmergencyVital, Organization
from ..schemas import PatientOut  # noqa: F401  (保持 schemas 导入路径一致性)

router = APIRouter(prefix="/api/emergency", tags=["智慧急救"], dependencies=[Depends(get_current_user)])

_FLOW = {"dispatched": "en_route", "en_route": "arrived", "arrived": "admitted"}

# 绿道时间节点固定序列（时效分析口径）
MILESTONE_SEQUENCE = ["onset", "call", "depart", "arrive_scene", "arrive_hospital", "treatment"]
MILESTONE_NAMES = {
    "onset": "发病",
    "call": "呼救",
    "depart": "出车",
    "arrive_scene": "到达现场",
    "arrive_hospital": "到达医院",
    "treatment": "开始救治",
}


class CaseCreate(BaseModel):
    caller_phone: str = ""
    location: str = Field(min_length=1)
    symptom: str = ""
    ambulance_no: str = ""
    dest_org_id: int | None = None
    patient_id: int | None = None
    # 急救绿色通道：""=普通, chest_pain=胸痛, stroke=卒中, trauma=创伤
    channel_type: str = Field(default="", pattern="^(|chest_pain|stroke|trauma)$")


class CaseOut(CaseCreate):
    id: int
    status: str
    rescue_outcome: str = ""

    model_config = {"from_attributes": True}


class RescueOutcomeIn(BaseModel):
    # 只有 success / failed 两种结论；"没救过来"和"还没下结论"必须分开，
    # 后者留空即可，写进来会把抢救成功率算低。
    rescue_outcome: str = Field(pattern="^(success|failed)$")


class MilestoneCreate(BaseModel):
    milestone: str = Field(pattern="^(onset|call|depart|arrive_scene|arrive_hospital|treatment)$")
    occurred_at: str = Field(min_length=1, max_length=32)

    @field_validator("occurred_at")
    @classmethod
    def _check_time_format(cls, value: str) -> str:
        """L-12 整改：绿道节点时间须为 ISO 格式（如 2026-08-10 14:02），保证时效可计算。"""
        try:
            datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("occurred_at 须为 ISO 格式时间，如 2026-08-10 14:02") from None
        return value


class MilestoneOut(MilestoneCreate):
    id: int
    case_id: int

    model_config = {"from_attributes": True}


class VitalCreate(BaseModel):
    heart_rate: float | None = None
    sbp: float | None = None
    dbp: float | None = None
    spo2: float | None = None
    note: str = ""


class VitalOut(VitalCreate):
    id: int
    case_id: int

    model_config = {"from_attributes": True}


@router.post(
    "/cases",
    response_model=CaseOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 急救调度
)
def dispatch(body: CaseCreate, db: Session = Depends(get_db)):
    if body.dest_org_id is not None and db.get(Organization, body.dest_org_id) is None:
        raise HTTPException(status_code=404, detail="目标医院不存在")
    case = EmergencyCase(**body.model_dump())
    db.add(case)
    db.commit()
    db.refresh(case)
    return case


@router.get("/cases", response_model=list[CaseOut])
def list_cases(status: str | None = None, db: Session = Depends(get_db)):
    query = db.query(EmergencyCase)
    if status:
        query = query.filter(EmergencyCase.status == status)
    return query.order_by(EmergencyCase.id.desc()).limit(200).all()


@router.post(
    "/cases/{case_id}/rescue-outcome",
    response_model=CaseOut,
    dependencies=[Depends(require_roles("doctor"))],
)
def set_rescue_outcome(case_id: int, body: RescueOutcomeIn, db: Session = Depends(get_db)):
    """判定抢救转归（限医师）：抢救成功率的唯一数据来源。

    只允许对已到院的病例判定——车还在路上就写"抢救成功"，这个指标就没有
    可信度了。判定后可更正（抢救结论本来就可能随病情变化）。
    """
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    if case.status not in ("arrived", "admitted"):
        raise HTTPException(status_code=409, detail="患者尚未到院，不可判定抢救转归")
    case.rescue_outcome = body.rescue_outcome
    db.commit()
    db.refresh(case)
    return case


@router.post(
    "/cases/{case_id}/advance",
    response_model=CaseOut,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 急救流程推进
)
def advance(case_id: int, db: Session = Depends(get_db)):
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    next_status = _FLOW.get(case.status)
    if next_status is None:
        raise HTTPException(status_code=409, detail=f"状态 {case.status} 已是终态")
    case.status = next_status
    db.commit()
    db.refresh(case)
    return case


@router.post(
    "/cases/{case_id}/vitals",
    response_model=VitalOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 体征回传
)
def report_vitals(case_id: int, body: VitalCreate, db: Session = Depends(get_db)):
    """车载终端回传生命体征——院内可实时调阅，实现院前院内无缝对接。"""
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    if case.status == "admitted":
        raise HTTPException(status_code=409, detail="已收治，转由院内记录")
    vital = EmergencyVital(case_id=case_id, **body.model_dump())
    db.add(vital)
    db.commit()
    db.refresh(vital)
    return vital


@router.get("/cases/{case_id}/vitals", response_model=list[VitalOut])
def list_vitals(case_id: int, db: Session = Depends(get_db)):
    if db.get(EmergencyCase, case_id) is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    return db.query(EmergencyVital).filter(EmergencyVital.case_id == case_id).order_by(EmergencyVital.id).all()


# ---------- 急救绿道时间节点 ----------


@router.post(
    "/cases/{case_id}/milestones",
    response_model=MilestoneOut,
    status_code=201,
    dependencies=[Depends(require_roles("operator", "doctor"))],  # H2: 急救绿道记录
)
def record_milestone(case_id: int, body: MilestoneCreate, db: Session = Depends(get_db)):
    """记录绿道时间节点（每个节点每例仅记录一次）。"""
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    existing = (
        db.query(EmergencyMilestone)
        .filter(
            EmergencyMilestone.case_id == case_id,
            EmergencyMilestone.milestone == body.milestone,
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=f"节点「{MILESTONE_NAMES[body.milestone]}」已记录（{existing.occurred_at}）",
        )
    # L-12 整改：节点时间须与固定序列单调一致（不得"先救治后发病"），时效计算方可靠
    new_index = MILESTONE_SEQUENCE.index(body.milestone)
    new_time = datetime.fromisoformat(body.occurred_at)
    for other in (
        db.query(EmergencyMilestone).filter(EmergencyMilestone.case_id == case_id).all()
    ):
        try:
            other_time = datetime.fromisoformat(other.occurred_at)
        except ValueError:  # pragma: no cover - 兼容历史脏数据
            continue
        other_index = MILESTONE_SEQUENCE.index(other.milestone)
        if (other_index < new_index and other_time > new_time) or (
            other_index > new_index and other_time < new_time
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    f"节点「{MILESTONE_NAMES[body.milestone]}」时间与已记录节点"
                    f"「{MILESTONE_NAMES[other.milestone]}」（{other.occurred_at}）时序矛盾"
                ),
            )
    milestone = EmergencyMilestone(case_id=case_id, **body.model_dump())
    db.add(milestone)
    db.commit()
    db.refresh(milestone)
    return milestone


@router.get("/cases/{case_id}/timeline")
def case_timeline(case_id: int, db: Session = Depends(get_db)):
    """绿道时间轴：按固定节点序列返回已记录/缺失情况。"""
    case = db.get(EmergencyCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="急救事件不存在")
    recorded = {
        m.milestone: m
        for m in db.query(EmergencyMilestone).filter(EmergencyMilestone.case_id == case_id).all()
    }
    timeline = [
        {
            "milestone": key,
            "name": MILESTONE_NAMES[key],
            "occurred_at": recorded[key].occurred_at if key in recorded else None,
            "recorded": key in recorded,
        }
        for key in MILESTONE_SEQUENCE
    ]
    return {
        "case_id": case_id,
        "channel_type": case.channel_type,
        "status": case.status,
        "timeline": timeline,
        "recorded_count": len(recorded),
    }
