"""综合决策可视化驾驶舱：汇聚各业务数据，指标口径对齐《监测指标体系（2024版）》。

块2（指引㉙联动下钻）：指标卡/预警横幅 → 明细下钻。
口径唯一来源是本模块的 METRIC_QUERIES 查询构造函数——
/overview、/alerts、/drilldown 三处一律复用同一个函数，杜绝口径漂移。
"""
from typing import Any

from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user, row_dict
from ..models import (
    ChronicPatient,
    DrugStock,
    Encounter,
    ExamReport,
    ExamRequest,
    InfectiousCase,
    MedicalWaste,
    Organization,
    Patient,
    Prescription,
    Referral,
)

router = APIRouter(prefix="/api/metrics", tags=["决策驾驶舱"], dependencies=[Depends(get_current_user)])

# 未闭环危急值口径（M-5 整改）：notified/acknowledged 与存量空串，resolved 不计入
OPEN_CRITICAL_STATUSES = ["notified", "acknowledged", ""]
# 医废滞留判定：收集后超过 2 天仍未交接
MEDWASTE_OVERDUE_DAYS = 2
# 传染病"近期"窗口：近 7 日
INFECTIOUS_WINDOW_DAYS = 7


# ---------------------------------------------------------------------------
# 指标查询构造函数（口径唯一来源）
# ---------------------------------------------------------------------------


def q_critical_values(db: Session):
    """未闭环危急值报告。"""
    return db.query(ExamReport).filter(
        ExamReport.critical.is_(True),
        ExamReport.critical_status.in_(OPEN_CRITICAL_STATUSES),
    )


def q_stock_alerts(db: Session):
    """低于阈值的药品库存（缺药预警）。"""
    return db.query(DrugStock).filter(DrugStock.quantity < DrugStock.threshold)


def q_chronic_overdue(db: Session):
    """随访到期日已过的在管慢病患者。"""
    today = date.today().isoformat()
    return db.query(ChronicPatient).filter(
        ChronicPatient.next_due != "", ChronicPatient.next_due < today
    )


def q_medwaste_overdue(db: Session):
    """超期未交接的医疗废物批次。"""
    cutoff = (date.today() - timedelta(days=MEDWASTE_OVERDUE_DAYS)).isoformat()
    return db.query(MedicalWaste).filter(
        MedicalWaste.status != "handed_over", MedicalWaste.collected_date <= cutoff
    )


def q_infectious_recent(db: Session):
    """近 7 日发病的传染病个案报告。"""
    window_start = (date.today() - timedelta(days=INFECTIOUS_WINDOW_DAYS)).isoformat()
    return db.query(InfectiousCase).filter(InfectiousCase.onset_date >= window_start)


def q_referrals_up(db: Session):
    return db.query(Referral).filter(Referral.direction == "up")


def q_referrals_down(db: Session):
    return db.query(Referral).filter(Referral.direction == "down")


def q_referrals_completed(db: Session):
    return db.query(Referral).filter(Referral.status == "completed")


def q_grassroots_encounters(db: Session):
    """基层（乡级/村级）机构诊疗人次。"""
    return (
        db.query(Encounter)
        .join(Organization, Encounter.org_id == Organization.id)
        .filter(Organization.level.in_(["township", "village"]))
    )


def q_reported_exams(db: Session):
    return db.query(ExamRequest).filter(ExamRequest.status == "reported")


def q_recognized_exams(db: Session):
    return db.query(ExamRequest).filter(ExamRequest.status == "recognized")


def q_pending_reviews(db: Session):
    """待药师审核处方。"""
    return db.query(Prescription).filter(Prescription.status == "pending_review")


def q_rejected_prescriptions(db: Session):
    return db.query(Prescription).filter(Prescription.status == "rejected")


# ---------------------------------------------------------------------------
# 明细行渲染（含跳转所需的业务 id 与关键字段）
# ---------------------------------------------------------------------------


def _row_critical(r: ExamReport) -> dict:
    return {
        "id": r.id,
        "request_id": r.request_id,
        "conclusion": r.conclusion,
        "critical_status": r.critical_status or "pending",
        "reported_by": r.reported_by,
        "reported_at": r.reported_at.isoformat(),
    }


def _row_stock(s: DrugStock) -> dict:
    return {
        "id": s.id,
        "org_id": s.org_id,
        "drug_code": s.drug_code,
        "drug_name": s.drug_name,
        "quantity": s.quantity,
        "threshold": s.threshold,
    }


def _row_chronic(c: ChronicPatient) -> dict:
    return {
        "id": c.id,
        "patient_id": c.patient_id,
        "disease": c.disease,
        "level": c.level,
        "next_due": c.next_due,
        "managed_by_org_id": c.managed_by_org_id,
    }


def _row_medwaste(w: MedicalWaste) -> dict:
    return {
        "id": w.id,
        "org_id": w.org_id,
        "waste_type": w.waste_type,
        "weight_kg": w.weight_kg,
        "status": w.status,
        "collected_date": w.collected_date,
    }


def _row_infectious(c: InfectiousCase) -> dict:
    return {
        "id": c.id,
        "org_id": c.org_id,
        "disease_code": c.disease_code,
        "disease_name": c.disease_name,
        "category": c.category,
        "onset_date": c.onset_date,
    }


def _row_referral(r: Referral) -> dict:
    return {
        "id": r.id,
        "patient_id": r.patient_id,
        "from_org_id": r.from_org_id,
        "to_org_id": r.to_org_id,
        "direction": r.direction,
        "status": r.status,
        "reason": r.reason,
    }


def _row_encounter(e: Encounter) -> dict:
    return {
        "id": e.id,
        "patient_id": e.patient_id,
        "org_id": e.org_id,
        "encounter_type": e.encounter_type,
        "diagnosis_name": e.diagnosis_name,
        "created_at": e.created_at.isoformat(),
    }


def _row_exam_request(r: ExamRequest) -> dict:
    return {
        "id": r.id,
        "patient_id": r.patient_id,
        "from_org_id": r.from_org_id,
        "center_type": r.center_type,
        "item_name": r.item_name,
        "status": r.status,
    }


def _row_prescription(p: Prescription) -> dict:
    return {
        "id": p.id,
        "patient_id": p.patient_id,
        "org_id": p.org_id,
        "diagnosis_name": p.diagnosis_name,
        "status": p.status,
        "review_comment": p.review_comment,
    }


# metric → (中文名, 查询构造函数, 明细行渲染, 前端跳转页 hash, 明细列表头)
METRIC_QUERIES: dict[str, dict] = {
    "critical_values": {
        "label": "未闭环危急值",
        "query": q_critical_values,
        "row": _row_critical,
        "page": "critical",
        "columns": ["报告ID", "申请单", "结论", "闭环状态", "报告医师", "报告时间"],
        "fields": ["id", "request_id", "conclusion", "critical_status", "reported_by", "reported_at"],
    },
    "stock_alerts": {
        "label": "缺药预警",
        "query": q_stock_alerts,
        "row": _row_stock,
        "page": "pharmacy",
        "columns": ["库存ID", "机构", "药品编码", "药品名称", "结存", "阈值"],
        "fields": ["id", "org_id", "drug_code", "drug_name", "quantity", "threshold"],
    },
    "chronic_overdue": {
        "label": "慢病随访超期",
        "query": q_chronic_overdue,
        "row": _row_chronic,
        "page": "chronic",
        "columns": ["档案ID", "患者", "病种", "分级", "应随访日", "管理机构"],
        "fields": ["id", "patient_id", "disease", "level", "next_due", "managed_by_org_id"],
    },
    "medwaste_overdue": {
        "label": "医废滞留",
        "query": q_medwaste_overdue,
        "row": _row_medwaste,
        "page": "medwaste",
        "columns": ["批次ID", "机构", "类别", "重量(kg)", "状态", "收集日期"],
        "fields": ["id", "org_id", "waste_type", "weight_kg", "status", "collected_date"],
    },
    "infectious_recent": {
        "label": "近7日传染病报告",
        "query": q_infectious_recent,
        "row": _row_infectious,
        "page": "infectious",
        "columns": ["个案ID", "机构", "病种编码", "病种", "分类", "发病日期"],
        "fields": ["id", "org_id", "disease_code", "disease_name", "category", "onset_date"],
    },
    "referrals_up": {
        "label": "上转",
        "query": q_referrals_up,
        "row": _row_referral,
        "page": "referrals",
        "columns": ["转诊ID", "患者", "转出机构", "转入机构", "方向", "状态", "原因"],
        "fields": ["id", "patient_id", "from_org_id", "to_org_id", "direction", "status", "reason"],
    },
    "referrals_down": {
        "label": "下转",
        "query": q_referrals_down,
        "row": _row_referral,
        "page": "referrals",
        "columns": ["转诊ID", "患者", "转出机构", "转入机构", "方向", "状态", "原因"],
        "fields": ["id", "patient_id", "from_org_id", "to_org_id", "direction", "status", "reason"],
    },
    "referrals_completed": {
        "label": "转诊结案",
        "query": q_referrals_completed,
        "row": _row_referral,
        "page": "referrals",
        "columns": ["转诊ID", "患者", "转出机构", "转入机构", "方向", "状态", "原因"],
        "fields": ["id", "patient_id", "from_org_id", "to_org_id", "direction", "status", "reason"],
    },
    "grassroots_encounters": {
        "label": "基层诊疗人次",
        "query": q_grassroots_encounters,
        "row": _row_encounter,
        "page": "archive",
        "columns": ["就诊ID", "患者", "机构", "就诊类型", "诊断", "就诊时间"],
        "fields": ["id", "patient_id", "org_id", "encounter_type", "diagnosis_name", "created_at"],
    },
    "reported_exams": {
        "label": "远程诊断已报告",
        "query": q_reported_exams,
        "row": _row_exam_request,
        "page": "exams",
        "columns": ["申请ID", "患者", "申请机构", "中心", "项目", "状态"],
        "fields": ["id", "patient_id", "from_org_id", "center_type", "item_name", "status"],
    },
    "recognized_exams": {
        "label": "结果互认",
        "query": q_recognized_exams,
        "row": _row_exam_request,
        "page": "recognition",
        "columns": ["申请ID", "患者", "申请机构", "中心", "项目", "状态"],
        "fields": ["id", "patient_id", "from_org_id", "center_type", "item_name", "status"],
    },
    "pending_reviews": {
        "label": "待药师审核处方",
        "query": q_pending_reviews,
        "row": _row_prescription,
        "page": "rx",
        "columns": ["处方ID", "患者", "机构", "诊断", "状态", "审方意见"],
        "fields": ["id", "patient_id", "org_id", "diagnosis_name", "status", "review_comment"],
    },
    "rejected_prescriptions": {
        "label": "退回处方",
        "query": q_rejected_prescriptions,
        "row": _row_prescription,
        "page": "rx",
        "columns": ["处方ID", "患者", "机构", "诊断", "状态", "审方意见"],
        "fields": ["id", "patient_id", "org_id", "diagnosis_name", "status", "review_comment"],
    },
}

# 预警横幅口径：与 METRIC_QUERIES 同源，只是选取其中五类风险
ALERT_METRICS = [
    "critical_values",
    "stock_alerts",
    "chronic_overdue",
    "medwaste_overdue",
    "infectious_recent",
]
# 预警横幅沿用既有简称（对外契约不变）
ALERT_LABELS = {
    "critical_values": "危急值",
    "stock_alerts": "缺药预警",
    "chronic_overdue": "慢病随访超期",
    "medwaste_overdue": "医废滞留",
    "infectious_recent": "近7日传染病报告",
}


def metric_count(db: Session, metric: str) -> int:
    """指标计数：所有出口（overview/alerts/drilldown/前端）唯一的计数入口。"""
    return METRIC_QUERIES[metric]["query"](db).count()


@router.get("/trends")
def monthly_trends(months: int = 6, db: Session = Depends(get_db)):
    """近N月业务量趋势：就诊、远程诊断、转诊、处方（Python侧聚合，兼容SQLite/PostgreSQL）。"""
    from collections import Counter

    def month_key(dt) -> str:
        return f"{dt.year:04d}-{dt.month:02d}"

    def last_months(n: int) -> list[str]:
        today = date.today()
        keys = []
        year, month = today.year, today.month
        for _ in range(n):
            keys.append(f"{year:04d}-{month:02d}")
            month -= 1
            if month == 0:
                year, month = year - 1, 12
        return list(reversed(keys))

    keys = last_months(max(1, min(months, 24)))
    series = {}
    for name, column in (
        ("encounters", Encounter.created_at),
        ("exam_reports", ExamReport.reported_at),
        ("referrals", Referral.created_at),
        ("prescriptions", Prescription.created_at),
    ):
        counter = Counter(month_key(row[0]) for row in db.query(column).all() if row[0])
        series[name] = [counter.get(k, 0) for k in keys]
    return {"months": keys, "series": series}


@router.get("/alerts")
def alert_summary(db: Session = Depends(get_db)):
    """全局风险预警汇总：五类风险一屏聚合，供驾驶舱预警横幅使用。

    计数全部走 metric_count（与 /overview、/drilldown 同一查询构造函数）。
    """
    items: list[dict[str, Any]] = [
        {"type": metric, "label": ALERT_LABELS[metric], "count": metric_count(db, metric)}
        for metric in ALERT_METRICS
    ]
    return {"total": sum(i["count"] for i in items), "items": [i for i in items if i["count"] > 0]}


@router.get("/drilldown")
def drilldown(
    response: Response,
    metric: str,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    """指标下钻明细（指引㉙）：口径与 /overview、/alerts 完全一致。

    返回明细行含业务 id 与关键字段，`page` 为前端 location.hash 跳转目标页。
    """
    meta = METRIC_QUERIES.get(metric)
    if meta is None:
        raise HTTPException(
            status_code=422,
            detail=f"未知指标：{metric}（可选：{'、'.join(METRIC_QUERIES)}）",
        )
    query = meta["query"](db)
    total = query.count()
    limit = min(max(limit, 1), 500)
    entity = query.column_descriptions[0]["entity"]
    rows = query.order_by(entity.id.desc()).offset(max(offset, 0)).limit(limit).all()
    items = [meta["row"](obj) for obj in rows]
    response.headers["X-Total-Count"] = str(total)
    return {
        "metric": metric,
        "label": meta["label"],
        "page": meta["page"],
        "columns": meta["columns"],
        "fields": meta["fields"],
        "total": total,
        "offset": max(offset, 0),
        "limit": limit,
        "items": items,
    }


@router.get("/drilldown-metrics")
def drilldown_metrics(db: Session = Depends(get_db)):
    """可下钻指标目录（含当前计数），供前端指标卡绑定下钻入口。"""
    return [
        {
            "metric": metric,
            "label": meta["label"],
            "page": meta["page"],
            "count": metric_count(db, metric),
        }
        for metric, meta in METRIC_QUERIES.items()
    ]


@router.get("/overview")
def overview(db: Session = Depends(get_db)):
    org_total = db.query(func.count(Organization.id)).scalar() or 0
    patient_total = db.query(func.count(Patient.id)).scalar() or 0

    # 促分工：县域内基层医疗卫生机构诊疗人次占比（监测指标7）
    encounter_total = db.query(func.count(Encounter.id)).scalar() or 0
    grassroots_encounters = metric_count(db, "grassroots_encounters")

    # 同质化：远程诊断服务量（监测指标5），互认情况
    reported = metric_count(db, "reported_exams")
    recognized = metric_count(db, "recognized_exams")
    critical = metric_count(db, "critical_values")

    # 双向转诊
    ref_up = metric_count(db, "referrals_up")
    ref_down = metric_count(db, "referrals_down")
    ref_completed = metric_count(db, "referrals_completed")

    # 集中审方
    rx_total = db.query(func.count(Prescription.id)).scalar() or 0
    rx_auto = (
        db.query(func.count(Prescription.id)).filter(Prescription.status == "auto_passed").scalar()
        or 0
    )
    rx_rejected = metric_count(db, "rejected_prescriptions")
    rx_pending = metric_count(db, "pending_reviews")

    # 保健康：慢病管理（监测指标13的过程指标）
    chronic_by_level = row_dict(
        db.query(ChronicPatient.level, func.count(ChronicPatient.id))
        .group_by(ChronicPatient.level)
        .all()
    )

    stock_alerts = metric_count(db, "stock_alerts")

    def pct(part: int, total: int) -> float:
        return round(part * 100.0 / total, 2) if total else 0.0

    return {
        "resources": {"organizations": org_total, "patients": patient_total},
        "service_division": {
            "encounters_total": encounter_total,
            "grassroots_encounters": grassroots_encounters,
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
            "pending_review": rx_pending,
        },
        "chronic_management": {
            "total": sum(chronic_by_level.values()),
            "by_level": {str(k): v for k, v in sorted(chronic_by_level.items())},
        },
        "pharmacy": {"stock_alerts": stock_alerts},
    }
