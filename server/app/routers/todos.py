"""任务待办中心：按当前用户角色聚合待处理事项，统一收件箱。

- 药师      → 待药师审处方（数量 + 列表）
- 医师      → 待诊断的共享中心申请
- 管理员    → 全部预警（待审处方、待诊断申请、缺药预警、危急值）
"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import DrugStock, ExamReport, ExamRequest, Prescription, User

router = APIRouter(prefix="/api/todos", tags=["待办中心"])


def _pending_prescriptions(db: Session) -> dict:
    rows = (
        db.query(Prescription)
        .filter(Prescription.status == "pending_review")
        .order_by(Prescription.id.desc())
        .limit(100)
        .all()
    )
    return {
        "type": "prescription_review",
        "title": "待药师审处方",
        "count": len(rows),
        "list": [
            {"id": p.id, "diagnosis_name": p.diagnosis_name, "review_comment": p.review_comment}
            for p in rows
        ],
    }


def _pending_exams(db: Session) -> dict:
    rows = (
        db.query(ExamRequest)
        .filter(ExamRequest.status.in_(["pending", "diagnosing"]))
        .order_by(ExamRequest.id.desc())
        .limit(100)
        .all()
    )
    return {
        "type": "exam_diagnosis",
        "title": "待诊断申请",
        "count": len(rows),
        "list": [
            {"id": r.id, "center_type": r.center_type, "item_name": r.item_name, "status": r.status}
            for r in rows
        ],
    }


def _stock_alerts(db: Session) -> dict:
    rows = (
        db.query(DrugStock)
        .filter(DrugStock.quantity < DrugStock.threshold)
        .order_by(DrugStock.org_id)
        .limit(100)
        .all()
    )
    return {
        "type": "stock_shortage",
        "title": "缺药预警",
        "count": len(rows),
        "list": [
            {"org_id": s.org_id, "drug_name": s.drug_name, "quantity": s.quantity, "threshold": s.threshold}
            for s in rows
        ],
    }


def _critical_reports(db: Session) -> dict:
    rows = (
        db.query(ExamReport)
        .filter(ExamReport.critical.is_(True))
        .order_by(ExamReport.id.desc())
        .limit(100)
        .all()
    )
    return {
        "type": "critical_report",
        "title": "危急值报告",
        "count": len(rows),
        "list": [{"id": r.id, "request_id": r.request_id, "conclusion": r.conclusion} for r in rows],
    }


@router.get("")
def my_todos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "pharmacist":
        items = [_pending_prescriptions(db)]
    elif user.role == "doctor":
        items = [_pending_exams(db)]
    elif user.role in ("admin", "director"):
        items = [
            _pending_prescriptions(db),
            _pending_exams(db),
            _stock_alerts(db),
            _critical_reports(db),
        ]
    else:
        items = []
    return {"role": user.role, "total": sum(i["count"] for i in items), "items": items}
