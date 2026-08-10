"""医共体绩效考核：按机构自动汇算业务数据，生成绩效评分。

维度（与规划第35项功能对应）：
- 转诊结案率（服务协同）
- 远程诊断服务量（资源下沉）
- 慢病随访覆盖（医防融合）
- 处方合格率（合理用药）
- 家医签约履约量（签约服务）
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import require_roles
from ..models import (
    ChronicPatient,
    ContractService,
    ExamRequest,
    FamilyDoctorContract,
    FollowUp,
    Organization,
    PerformanceIndicator,
    Prescription,
    Referral,
)

router = APIRouter(
    prefix="/api/performance", tags=["绩效考核"], dependencies=[Depends(require_roles("director"))]
)

# 指标目录种子（启动时写入 performance_indicators 表；权重和不必为100，计分时按比例归一化）
DEFAULT_INDICATORS = {
    "referral": {"name": "转诊结案率", "weight": 20},
    "remote_exam": {"name": "远程诊断服务量", "weight": 20},
    "chronic": {"name": "慢病随访覆盖", "weight": 25},
    "rx": {"name": "处方合格率", "weight": 20},
    "contract": {"name": "家医签约履约量", "weight": 15},
}


def _normalized_weights(db: Session) -> dict[str, float]:
    """从指标表读取 active 指标权重，按比例归一化到总分100。表空时退回默认。"""
    rows = (
        db.query(PerformanceIndicator)
        .filter(PerformanceIndicator.active.is_(True), PerformanceIndicator.weight > 0)
        .all()
    )
    raw = (
        {r.key: r.weight for r in rows}
        if rows
        else {k: v["weight"] for k, v in DEFAULT_INDICATORS.items()}
    )
    total = sum(raw.values())
    return {k: round(w / total * 100, 2) for k, w in raw.items()}


class IndicatorPatch(BaseModel):
    weight: float | None = Field(default=None, ge=0)
    name: str | None = None
    active: bool | None = None


class IndicatorOut(BaseModel):
    id: int
    key: str
    name: str
    weight: float
    active: bool

    model_config = {"from_attributes": True}


@router.get("/indicators", response_model=list[IndicatorOut])
def list_indicators(db: Session = Depends(get_db)):
    return db.query(PerformanceIndicator).order_by(PerformanceIndicator.id).all()


@router.patch("/indicators/{key}", response_model=IndicatorOut)
def update_indicator(key: str, body: IndicatorPatch, db: Session = Depends(get_db)):
    """管理层调节指标权重/启停：调整立即反映到 /orgs 计分（按比例归一化）。"""
    indicator = db.query(PerformanceIndicator).filter(PerformanceIndicator.key == key).first()
    if indicator is None:
        raise HTTPException(status_code=404, detail="指标不存在")
    for field, value in body.model_dump(exclude_unset=True).items():
        if value is not None:
            setattr(indicator, field, value)
    db.flush()
    active_weight = (
        db.query(func.sum(PerformanceIndicator.weight))
        .filter(PerformanceIndicator.active.is_(True))
        .scalar()
        or 0
    )
    if active_weight <= 0:
        db.rollback()
        raise HTTPException(status_code=422, detail="启用指标的权重合计必须大于0")
    db.commit()
    db.refresh(indicator)
    return indicator


@router.get("/orgs")
def org_scorecards(db: Session = Depends(get_db)):
    weights = _normalized_weights(db)
    orgs = db.query(Organization).order_by(Organization.id).all()
    results = []
    for org in orgs:
        ref_total = db.query(func.count(Referral.id)).filter(Referral.from_org_id == org.id).scalar() or 0
        ref_completed = (
            db.query(func.count(Referral.id))
            .filter(Referral.from_org_id == org.id, Referral.status == "completed")
            .scalar()
            or 0
        )
        exam_count = (
            db.query(func.count(ExamRequest.id))
            .filter(ExamRequest.from_org_id == org.id, ExamRequest.status.in_(["reported", "recognized"]))
            .scalar()
            or 0
        )
        chronic_total = (
            db.query(func.count(ChronicPatient.id))
            .filter(ChronicPatient.managed_by_org_id == org.id)
            .scalar()
            or 0
        )
        chronic_followed = (
            db.query(func.count(func.distinct(FollowUp.chronic_id)))
            .join(ChronicPatient, FollowUp.chronic_id == ChronicPatient.id)
            .filter(ChronicPatient.managed_by_org_id == org.id)
            .scalar()
            or 0
        )
        rx_total = db.query(func.count(Prescription.id)).filter(Prescription.org_id == org.id).scalar() or 0
        rx_ok = (
            db.query(func.count(Prescription.id))
            .filter(Prescription.org_id == org.id, Prescription.status.in_(["auto_passed", "approved"]))
            .scalar()
            or 0
        )
        contract_services = (
            db.query(func.count(ContractService.id))
            .join(FamilyDoctorContract, ContractService.contract_id == FamilyDoctorContract.id)
            .filter(FamilyDoctorContract.org_id == org.id)
            .scalar()
            or 0
        )

        def ratio(part: int, total: int) -> float:
            return part / total if total else 0.0

        # 量类维度按封顶计分：达到5次即满分
        def volume_score(count: int, cap: int = 5) -> float:
            return min(count, cap) / cap

        # 各维度得分率（0-1）；仅对启用的指标计分
        dimension_ratios = {
            "referral": ratio(ref_completed, ref_total),
            "remote_exam": volume_score(exam_count),
            "chronic": ratio(chronic_followed, chronic_total),
            "rx": ratio(rx_ok, rx_total),
            "contract": volume_score(contract_services),
        }
        score = round(
            sum(dimension_ratios.get(key, 0.0) * weight for key, weight in weights.items()), 1
        )
        results.append(
            {
                "org_id": org.id,
                "org_name": org.name,
                "level": org.level,
                "score": score,
                "detail": {
                    "referral_completion": {"completed": ref_completed, "total": ref_total},
                    "remote_exams": exam_count,
                    "chronic_followup": {"followed": chronic_followed, "total": chronic_total},
                    "rx_pass": {"passed": rx_ok, "total": rx_total},
                    "contract_services": contract_services,
                },
            }
        )
    results.sort(key=lambda r: r["score"], reverse=True)
    return {"weights": weights, "scorecards": results}
