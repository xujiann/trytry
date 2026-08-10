"""综合决策可视化驾驶舱：汇聚各业务数据，指标口径对齐《监测指标体系（2024版）》。"""
from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import (
    ChronicPatient,
    DrugStock,
    Encounter,
    ExamReport,
    ExamRequest,
    Organization,
    Patient,
    Prescription,
    Referral,
)

router = APIRouter(prefix="/api/metrics", tags=["决策驾驶舱"], dependencies=[Depends(get_current_user)])


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    org_total = db.query(func.count(Organization.id)).scalar() or 0
    patient_total = db.query(func.count(Patient.id)).scalar() or 0

    # 促分工：县域内基层医疗卫生机构诊疗人次占比（监测指标7）
    encounter_total = db.query(func.count(Encounter.id)).scalar() or 0
    grassroots_encounters = (
        db.query(func.count(Encounter.id))
        .join(Organization, Encounter.org_id == Organization.id)
        .filter(Organization.level.in_(["township", "village"]))
        .scalar()
        or 0
    )

    # 同质化：远程诊断服务量（监测指标5），互认情况
    reported = db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "reported").scalar() or 0
    recognized = (
        db.query(func.count(ExamRequest.id)).filter(ExamRequest.status == "recognized").scalar() or 0
    )
    critical = db.query(func.count(ExamReport.id)).filter(ExamReport.critical.is_(True)).scalar() or 0

    # 双向转诊
    ref_up = db.query(func.count(Referral.id)).filter(Referral.direction == "up").scalar() or 0
    ref_down = db.query(func.count(Referral.id)).filter(Referral.direction == "down").scalar() or 0
    ref_completed = db.query(func.count(Referral.id)).filter(Referral.status == "completed").scalar() or 0

    # 集中审方
    rx_total = db.query(func.count(Prescription.id)).scalar() or 0
    rx_auto = db.query(func.count(Prescription.id)).filter(Prescription.status == "auto_passed").scalar() or 0
    rx_rejected = db.query(func.count(Prescription.id)).filter(Prescription.status == "rejected").scalar() or 0

    # 保健康：慢病管理（监测指标13的过程指标）
    chronic_by_level = dict(
        db.query(ChronicPatient.level, func.count(ChronicPatient.id)).group_by(ChronicPatient.level).all()
    )

    stock_alerts = (
        db.query(func.count(DrugStock.id)).filter(DrugStock.quantity < DrugStock.threshold).scalar() or 0
    )

    def pct(part: int, total: int) -> float:
        return round(part * 100.0 / total, 2) if total else 0.0

    return {
        "resources": {"organizations": org_total, "patients": patient_total},
        "service_division": {
            "encounters_total": encounter_total,
            "grassroots_encounter_ratio_pct": pct(grassroots_encounters, encounter_total),
        },
        "remote_diagnosis": {
            "reported_total": reported,
            "recognized_total": recognized,
            "recognition_ratio_pct": pct(recognized, reported + recognized),
            "critical_values": critical,
        },
        "referrals": {"up": ref_up, "down": ref_down, "completed": ref_completed},
        "prescription_review": {
            "total": rx_total,
            "auto_pass_ratio_pct": pct(rx_auto, rx_total),
            "rejected": rx_rejected,
        },
        "chronic_management": {
            "total": sum(chronic_by_level.values()),
            "by_level": {str(k): v for k, v in sorted(chronic_by_level.items())},
        },
        "pharmacy": {"stock_alerts": stock_alerts},
    }
