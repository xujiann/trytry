"""医共体绩效考核：按机构自动汇算业务数据，生成绩效评分。

维度（与规划第35项功能对应）：
- 转诊结案率（服务协同）
- 远程诊断服务量（资源下沉）
- 慢病随访覆盖（医防融合）
- 处方合格率（合理用药）
- 家医签约履约量（签约服务）
"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import (
    ChronicPatient,
    ContractService,
    ExamRequest,
    FamilyDoctorContract,
    FollowUp,
    Organization,
    Prescription,
    Referral,
)

router = APIRouter(prefix="/api/performance", tags=["绩效考核"], dependencies=[Depends(get_current_user)])

# 各维度满分权重，总分100
WEIGHTS = {"referral": 20, "remote_exam": 20, "chronic": 25, "rx": 20, "contract": 15}


@router.get("/orgs")
def org_scorecards(db: Session = Depends(get_db)):
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

        score = round(
            ratio(ref_completed, ref_total) * WEIGHTS["referral"]
            + volume_score(exam_count) * WEIGHTS["remote_exam"]
            + ratio(chronic_followed, chronic_total) * WEIGHTS["chronic"]
            + ratio(rx_ok, rx_total) * WEIGHTS["rx"]
            + volume_score(contract_services) * WEIGHTS["contract"],
            1,
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
    return {"weights": WEIGHTS, "scorecards": results}
