"""慢病一体化管理：病种目录、建档、智能分级分组、随访、超期预警。

块1 起病种不再硬编码，统一由 ChronicDiseaseType 目录驱动（指引㉒㉗）：
- 分级规则（指标名 + 阈值区间）、指导要点、随访周期均从目录读取；
- 原 hypertension/diabetes/copd 三个编码与阈值原样迁入种子数据，行为不变；
- 随访后按病种周期自动建议下次到期日；非血压血糖类指标存入 FollowUp.metrics。

膳食运动指导要点依据国卫办基层函〔2025〕121号要求嵌入系统，
在接诊和随访时同步返回。
"""
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..concurrency import insert_if_absent, insert_or_conflict
from ..database import get_db
from ..deps import (
    get_current_user,
    paginate,
    require_admin,
    require_roles,
    resolve_business_date,
)
from ..models import ChronicDiseaseType, ChronicPatient, FollowUp, Organization, Patient, User
from ..visibility import assert_patient_visible
from ..schemas import ChronicCreate, ChronicOut, FollowUpCreate, FollowUpOut

router = APIRouter(prefix="/api/chronic", tags=["慢病管理"], dependencies=[Depends(get_current_user)])

# 基层膳食运动指导要点兜底表（121号文）：目录缺失病种（如体重/血脂管理）仍可给出要点
GUIDANCE_POINTS = {
    "hypertension": "限盐（每日<5g）、控制体重、戒烟限酒、每周≥150分钟中等强度运动、规律服药并自测血压",
    "diabetes": "控制总能量摄入、主食粗细搭配、规律三餐、餐后适量运动、监测血糖、遵医嘱用药",
    "copd": "戒烟、防寒保暖避免感染、腹式呼吸与缩唇呼吸锻炼、适度有氧运动、规范吸入用药",
    "obesity": "膳食总量控制、减少高油高糖食品、每周≥150分钟运动并逐步增量、行为记录与定期测量",
    "hyperlipidemia": "限制饱和脂肪与反式脂肪摄入、增加膳食纤维、控制体重、规律运动、定期复查血脂",
}

# 随访周期兜底值（天）：目录缺失时按季度随访
DEFAULT_FOLLOWUP_INTERVAL_DAYS = 90


def get_disease_type(db: Session, code: str) -> ChronicDiseaseType | None:
    """按编码取病种目录条目（含停用项，停用校验由调用方决定）。"""
    return db.query(ChronicDiseaseType).filter(ChronicDiseaseType.code == code).first()


def guidance_for(db: Session, code: str) -> str:
    """指导要点：优先取目录，目录缺失回落到 121 号文兜底表。"""
    dt = get_disease_type(db, code)
    if dt is not None and dt.guidance:
        return dt.guidance
    return GUIDANCE_POINTS.get(code, "")


def _metric_value(body: FollowUpCreate, key: str) -> float | None:
    """指标取值：优先取 FollowUp 同名列（sbp/dbp/glucose），其次取通用 metrics JSON。"""
    value = getattr(body, key, None)
    if value is None:
        value = (body.metrics or {}).get(key)
    return None if value is None else float(value)


def _metric_level(metric: dict, value: float) -> int:
    """单指标定级：direction=high 时越高越危（≥阈值），low 时越低越危（≤阈值）。"""
    level3, level2 = metric.get("level3"), metric.get("level2")
    if metric.get("direction") == "low":
        if level3 is not None and value <= level3:
            return 3
        if level2 is not None and value <= level2:
            return 2
        return 1
    if level3 is not None and value >= level3:
        return 3
    if level2 is not None and value >= level2:
        return 2
    return 1


def _evaluate_level(db: Session, disease: str, body: FollowUpCreate) -> int | None:
    """智能分级：3=高危需转诊评估, 2=需干预, 1=控制良好。

    规则来自病种目录 level_rules：取各指标定级的最高档；
    require_all=true（默认）时指标缺项则不评级，返回 None（维持原级）。
    """
    disease_type = get_disease_type(db, disease)
    rules = (disease_type.level_rules if disease_type else None) or {}
    metrics = rules.get("metrics") or []
    if not metrics:
        return None
    require_all = rules.get("require_all", True)
    levels: list[int] = []
    for metric in metrics:
        value = _metric_value(body, metric.get("key", ""))
        if value is None:
            if require_all:
                return None
            continue
        levels.append(_metric_level(metric, value))
    return max(levels) if levels else None


def _suggest_next_due(db: Session, disease: str, today: str | None = None) -> str:
    """按病种随访周期建议下次到期日（业务当天 + 周期天数）。"""
    disease_type = get_disease_type(db, disease)
    days = (
        disease_type.followup_interval_days
        if disease_type and disease_type.followup_interval_days > 0
        else DEFAULT_FOLLOWUP_INTERVAL_DAYS
    )
    return (resolve_business_date(today) + timedelta(days=days)).isoformat()


# ---------- 病种目录 ----------


class DiseaseTypeCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    level_rules: dict = Field(default_factory=dict)
    guidance: str = ""
    followup_interval_days: int = Field(default=90, gt=0)
    active: bool = True


class DiseaseTypeUpdate(BaseModel):
    name: str | None = None
    level_rules: dict | None = None
    guidance: str | None = None
    followup_interval_days: int | None = Field(default=None, gt=0)
    active: bool | None = None


def _disease_type_out(dt: ChronicDiseaseType) -> dict:
    return {
        "id": dt.id,
        "code": dt.code,
        "name": dt.name,
        "level_rules": dt.level_rules or {},
        "guidance": dt.guidance,
        "followup_interval_days": dt.followup_interval_days,
        "active": dt.active,
    }


@router.get("/disease-types")
def list_disease_types(active: bool | None = None, db: Session = Depends(get_db)):
    """慢病病种目录：前端病种下拉与分级规则展示的唯一数据源。"""
    query = db.query(ChronicDiseaseType)
    if active is not None:
        query = query.filter(ChronicDiseaseType.active.is_(active))
    return [_disease_type_out(dt) for dt in query.order_by(ChronicDiseaseType.id).all()]


@router.post("/disease-types", status_code=201, dependencies=[Depends(require_admin)])
def create_disease_type(body: DiseaseTypeCreate, db: Session = Depends(get_db)):
    if get_disease_type(db, body.code) is not None:
        raise HTTPException(status_code=409, detail="病种编码已存在")
    disease_type = insert_or_conflict(
        db, ChronicDiseaseType(**body.model_dump()), "病种编码已存在"
    )
    return _disease_type_out(disease_type)


@router.patch("/disease-types/{type_id}", dependencies=[Depends(require_admin)])
def update_disease_type(type_id: int, body: DiseaseTypeUpdate, db: Session = Depends(get_db)):
    disease_type = db.get(ChronicDiseaseType, type_id)
    if disease_type is None:
        raise HTTPException(status_code=404, detail="病种不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(disease_type, field, value)
    db.commit()
    db.refresh(disease_type)
    return _disease_type_out(disease_type)


# ---------- 建档与随访 ----------


@router.post(
    "",
    response_model=ChronicOut,
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 慢病建档
)
def register_chronic(body: ChronicCreate, db: Session = Depends(get_db)):
    disease_type = get_disease_type(db, body.disease)
    if disease_type is None or not disease_type.active:
        raise HTTPException(status_code=422, detail="病种编码不在慢病病种目录内")
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
    payload = body.model_dump()
    # 建档即安排首次随访：未指定到期日时按病种周期自动建议
    if not payload.get("next_due"):
        payload["next_due"] = _suggest_next_due(db, body.disease)
    # 建档是幂等的（上面查到既有档案就直接返回）。并发下两个请求都查不到
    # 就都会走到这里，撞 (patient_id, disease) 唯一约束——那也该返回既有档案，
    # 不是 500。同一个人同一病种建两份档，随访会各走各的。
    chronic = ChronicPatient(**payload)
    if not insert_if_absent(db, chronic):
        db.rollback()  # 同 maternal：提前返回前必须结束外层写事务
        return (
            db.query(ChronicPatient)
            .filter(
                ChronicPatient.patient_id == body.patient_id,
                ChronicPatient.disease == body.disease,
            )
            .first()
        )
    db.commit()
    db.refresh(chronic)
    return chronic


@router.get("", response_model=list[ChronicOut])
def list_chronic(
    response: Response,
    disease: str | None = None,
    level: int | None = None,
    offset: int = 0,
    limit: int = 500,
    db: Session = Depends(get_db),
):
    """慢病档案列表（L-3 分页：offset/limit，总数见 X-Total-Count 响应头）。"""
    query = db.query(ChronicPatient)
    if disease:
        query = query.filter(ChronicPatient.disease == disease)
    if level is not None:
        query = query.filter(ChronicPatient.level == level)
    return paginate(query.order_by(ChronicPatient.level.desc(), ChronicPatient.id), response, offset, limit)


@router.get("/overdue", response_model=list[ChronicOut])
def list_overdue(today: str | None = None, db: Session = Depends(get_db)):
    """随访超期名单：next_due 早于今天的建档患者，推送全科医生任务。

    L-2：默认取服务端当前日期；today 覆盖参数仅限测试/管理排查用途（YYYY-MM-DD）。
    """
    cutoff = resolve_business_date(today).isoformat()
    return (
        db.query(ChronicPatient)
        .filter(ChronicPatient.next_due != "", ChronicPatient.next_due < cutoff)
        .order_by(ChronicPatient.next_due)
        .all()
    )


@router.post(
    "/{chronic_id}/followups",
    status_code=201,
    dependencies=[Depends(require_roles("doctor", "public_health"))],  # H2: 随访属诊疗/公卫岗
)
def add_followup(chronic_id: int, body: FollowUpCreate, db: Session = Depends(get_db)):
    chronic = db.get(ChronicPatient, chronic_id)
    if chronic is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")
    payload = body.model_dump()
    # 未填下次到期日时按病种随访周期自动建议
    suggested = "" if body.next_due else _suggest_next_due(db, chronic.disease)
    payload["next_due"] = body.next_due or suggested
    followup = FollowUp(chronic_id=chronic_id, **payload)
    new_level = _evaluate_level(db, chronic.disease, body)
    if new_level is not None:
        chronic.level = new_level
    chronic.next_due = payload["next_due"]
    db.add(followup)
    db.commit()
    db.refresh(followup)
    return {
        "followup": FollowUpOut.model_validate(followup).model_dump(),
        "level": chronic.level,
        "guidance_points": guidance_for(db, chronic.disease),
        "next_due": chronic.next_due,
        "next_due_suggested": bool(suggested),
        "refer_up_suggested": chronic.level == 3,
    }


# 风险评分兜底指标（目录无规则时沿用）
_RISK_METRICS = {"hypertension": "sbp", "diabetes": "glucose"}
# 当前分级基础分
_LEVEL_BASE = {1: 20, 2: 50, 3: 80}


def _risk_metric(db: Session, disease: str) -> str:
    """风险趋势指标：取病种目录第一个分级指标，目录缺失时回落到兜底表。"""
    disease_type = get_disease_type(db, disease)
    metrics = ((disease_type.level_rules if disease_type else None) or {}).get("metrics") or []
    if metrics and metrics[0].get("key"):
        return str(metrics[0]["key"])
    return _RISK_METRICS.get(disease, "")


@router.get("/{chronic_id}/risk")
def risk_score(
    chronic_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """简单风险评分：最近3次随访关键指标趋势 + 当前分级加权。

    score = 分级基础分（1级20 / 2级50 / 3级80）
            + 趋势修正（上升 +15，下降 -10，平稳 0）
    风险档位：≥70 高危，≥40 中危，其余低危。
    """
    chronic = db.get(ChronicPatient, chronic_id)
    if chronic is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")
    # 慢病档案按 id 直取同样是患者维度数据：可见性判定 + 留痕
    assert_patient_visible(db, user, chronic.patient_id, resource="chronic")

    metric = _risk_metric(db, chronic.disease)
    recent = (
        db.query(FollowUp)
        .filter(FollowUp.chronic_id == chronic_id)
        .order_by(FollowUp.id.desc())
        .limit(3)
        .all()
    )
    values: list[float] = []
    if metric:
        # 按时间正序排列的最近3次有效数值（列指标优先，其次通用 metrics JSON）
        raw = (
            getattr(f, metric, None) if getattr(f, metric, None) is not None else (f.metrics or {}).get(metric)
            for f in reversed(recent)
        )
        values = [v for v in raw if v is not None]

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
def list_followups(
    chronic_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    chronic = db.get(ChronicPatient, chronic_id)
    if chronic is None:
        raise HTTPException(status_code=404, detail="慢病档案不存在")
    assert_patient_visible(db, user, chronic.patient_id, resource="chronic")
    return (
        db.query(FollowUp).filter(FollowUp.chronic_id == chronic_id).order_by(FollowUp.id.desc()).all()
    )
