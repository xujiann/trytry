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
from sqlalchemy import true
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import get_current_user
from ..models import DrugStock, ExamReport, ExamRequest, Organization, Prescription, User
from ..visibility import visible_org_ids
from .dispense import q_dispensable_shortage

router = APIRouter(prefix="/api/todos", tags=["待办中心"])


class TodoSectionOut(BaseModel):
    """待办分节。`list` 的行形随 `type` 换（审方 3 键/待诊断 4 键/缺药 6 键/
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


#: 每节预览的行数上限。网页铃铛每节只显示前 5 条、医生移动端前 20 条，预览就是预览；
#: 可 `count` 必须是全部（P1-85）——原先 `count = len(预览)`，积压过 100 条就显示 100，
#: 积压越严重显示得越像「还好」。`total` 是各节计数之和，跟着对。
PREVIEW = 100


def _pending_prescriptions(db: Session) -> dict:
    query = db.query(Prescription).filter(Prescription.status == "pending_review")
    rows = query.order_by(Prescription.id.desc()).limit(PREVIEW).all()
    return {
        "type": "prescription_review",
        "title": "待药师审处方",
        "count": query.count(),
        "list": [
            {"id": p.id, "diagnosis_name": p.diagnosis_name, "review_comment": p.review_comment}
            for p in rows
        ],
    }


def _pending_exams(db: Session) -> dict:
    query = db.query(ExamRequest).filter(ExamRequest.status.in_(["pending", "diagnosing"]))
    rows = query.order_by(ExamRequest.id.desc()).limit(PREVIEW).all()
    return {
        "type": "exam_diagnosis",
        "title": "待诊断申请",
        "count": query.count(),
        "list": [
            {"id": r.id, "center_type": r.center_type, "item_name": r.item_name, "status": r.status}
            for r in rows
        ],
    }


def _stock_alerts(db: Session) -> dict:
    # 按可发量判，与缺药预警页、驾驶舱同一个构造（P2-1250）：原先比汇总，批次过了效期汇总一片不少，发药 409、待办照旧不报。
    # 每行 `(库存, 可发量)`；`quantity` 照旧是汇总，可发量另给 `dispensable`
    query = q_dispensable_shortage(db)
    # 同一机构内按库存行编号排（P2-971）：原先只按机构，同一家谁先谁后由库决定，PG 上每次入库、发药都可能换一批
    rows = query.order_by(DrugStock.org_id, DrugStock.id).limit(PREVIEW).all()
    # 带上机构名称（P2-371）：缺药是哪家的，医生移动端的待办卡片原先只能打出「机构 3」
    names = {oid: name for oid, name in db.query(Organization.id, Organization.name)
             .filter(Organization.id.in_({s.org_id for s, _ in rows}))} if rows else {}
    return {
        "type": "stock_shortage",
        "title": "缺药预警",
        "count": query.count(),
        "list": [
            {"org_id": s.org_id, "org_name": names.get(s.org_id, ""), "drug_name": s.drug_name,
             "quantity": s.quantity, "threshold": s.threshold, "dispensable": int(dispensable)}
            for s, dispensable in rows
        ],
    }


def _critical_reports(db: Session) -> dict:
    # 未闭环危急值：notified/acknowledged（含存量迁移前空串），resolved 不再计入。
    # `== true()` 而不是 `.is_(True)`：只有这么写才用得上危急值部分索引（P2-1156，见 `ExamReport` 的 __table_args__）
    query = db.query(ExamReport).filter(
        ExamReport.critical == true(),
        ExamReport.critical_status.in_(["notified", "acknowledged", ""]),
    )
    rows = query.order_by(ExamReport.id.desc()).limit(PREVIEW).all()
    return {
        "type": "critical_report",
        "title": "未闭环危急值",
        "count": query.count(),
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


def _unacknowledged_critical(db: Session, user: User) -> dict:
    """医师待办：待确认接收的危急值（notified，含存量空串）。

    只列**本机构申请单**上的（P0-40）：危急值只通知申请机构的医生（`exams.submit_report` 的
    `notify_staff(org_id=申请机构)`），确认接收也按申请单的患者判可见性（P0-27）——
    别家的危急值确认不了，列进待办只会把别家患者的检验结论推到每个医生的铃铛里。
    """
    query = (
        db.query(ExamReport)
        .join(ExamRequest, ExamRequest.id == ExamReport.request_id)
        .filter(
            ExamReport.critical == true(), ExamReport.critical_status.in_(["notified", ""])   # 写法同上（P2-1156）
        )
    )
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(ExamRequest.from_org_id.in_(orgs))
    rows = query.order_by(ExamReport.id.desc()).limit(PREVIEW).all()
    return {
        "type": "critical_ack",
        "title": "待确认危急值",
        "count": query.count(),
        "list": [{"id": r.id, "request_id": r.request_id, "conclusion": r.conclusion} for r in rows],
    }


@router.get("", response_model=TodosOut)
def my_todos(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    if user.role == "pharmacist":
        items = [_pending_prescriptions(db)]
    elif user.role == "doctor":
        items = [_pending_exams(db), _unacknowledged_critical(db, user)]
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
