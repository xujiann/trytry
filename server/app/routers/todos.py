"""任务待办中心：按当前用户角色聚合待处理事项，统一收件箱。

- 药师      → 待药师审处方（数量 + 列表）
- 医师      → 待诊断的共享中心申请 + 待确认危急值
- 管理员    → 全部预警（待审处方、待诊断申请、缺药预警、未闭环危急值）

M-5 整改：危急值口径随闭环状态更新——仅 notified/acknowledged（含存量空串）
计入待办与预警，已处置(resolved)不再累积；医师待办补"待确认危急值"。
"""
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import DrugStock, ExamReport, ExamRequest, Prescription, User

router = APIRouter(prefix="/api/todos", tags=["待办中心"])


class TodoSectionOut(BaseModel):
    """待办分节。`list` 的行形随 `type` 换（审方 3 键/待诊断 4 键/缺药 4 键/
    危急值 4 键/待确认 3 键）——真多态而非条件键：逐字段并模会把五种行的键互相
    注入 null，且待确认行（id/request_id/conclusion）是危急值行的真子集，smart
    union 会静默吞掉 critical_status。照 metrics/drilldown 的先例用宽字典透传，
    行形由同一行的 `type` 自描述，五种行形在 test_todos_contract.py 各钉一遍。"""

    type: str
    title: str
    count: int
    list: list[dict[str, Any]]


class TodosOut(BaseModel):
    role: str
    total: int
    items: list[TodoSectionOut]


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
    # 未闭环危急值：notified/acknowledged（含存量迁移前空串），resolved 不再计入
    rows = (
        db.query(ExamReport)
        .filter(
            ExamReport.critical.is_(True),
            ExamReport.critical_status.in_(["notified", "acknowledged", ""]),
        )
        .order_by(ExamReport.id.desc())
        .limit(100)
        .all()
    )
    return {
        "type": "critical_report",
        "title": "未闭环危急值",
        "count": len(rows),
        "list": [
            {
                "id": r.id,
                "request_id": r.request_id,
                "conclusion": r.conclusion,
                "critical_status": r.critical_status,
            }
            for r in rows
        ],
    }


def _unacknowledged_critical(db: Session) -> dict:
    """医师待办：待确认接收的危急值（notified，含存量空串）。"""
    rows = (
        db.query(ExamReport)
        .filter(
            ExamReport.critical.is_(True), ExamReport.critical_status.in_(["notified", ""])
        )
        .order_by(ExamReport.id.desc())
        .limit(100)
        .all()
    )
    return {
        "type": "critical_ack",
        "title": "待确认危急值",
        "count": len(rows),
        "list": [{"id": r.id, "request_id": r.request_id, "conclusion": r.conclusion} for r in rows],
    }


@router.get("", response_model=TodosOut)
def my_todos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "pharmacist":
        items = [_pending_prescriptions(db)]
    elif user.role == "doctor":
        items = [_pending_exams(db), _unacknowledged_critical(db)]
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
