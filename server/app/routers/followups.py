"""统一随访中心（T2.4）。

此前只有慢病随访有载体（`/api/chronic/{id}/followups`），出院随访、术后随访
无处安放——而这两类恰恰是最容易漏的。这里把四类随访收敛到同一个任务模型：
慢病、出院、术后、妇幼。

任务由业务动作自动派生（出院时、术中记录结案时），也可人工补建。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..visibility import assert_obj_org_writable, assert_org_writable, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_roles, resolve_business_date, row_dict
from ..models import FollowupTask, Organization, Patient, User, utcnow

router = APIRouter(prefix="/api/followups", tags=["随访中心"], dependencies=[Depends(get_current_user)])

# 出院随访默认间隔
DISCHARGE_FOLLOWUP_DAYS = 7

CATEGORY_TITLES = {
    "chronic": "慢病随访",
    "discharge": "出院随访",
    "surgery": "术后随访",
    "maternal": "妇幼访视",
}


def create_task(
    db: Session,
    patient_id: int,
    org_id: int,
    category: str,
    source_id: int,
    title: str,
    due_days: int,
) -> FollowupTask:
    """供业务模块调用的派生入口（出院、手术结案等）。

    同来源同类别已有待随访任务时不重复派生——出院流程可能因故重跑，
    重复派生会让随访护士看到两条一模一样的任务。
    """
    existing = (
        db.query(FollowupTask)
        .filter(
            FollowupTask.category == category,
            FollowupTask.source_id == source_id,
            FollowupTask.status == "pending",
        )
        .first()
    )
    if existing is not None:
        return existing
    task = FollowupTask(
        patient_id=patient_id,
        org_id=org_id,
        category=category,
        source_id=source_id,
        title=title,
        due_date=(date.today() + timedelta(days=due_days)).isoformat(),
    )
    db.add(task)
    return task


class FollowupIn(BaseModel):
    patient_id: int
    org_id: int
    category: str = Field(pattern="^(chronic|discharge|surgery|maternal)$")
    source_id: int = 0
    title: str = ""
    due_date: str = Field(min_length=10, max_length=10)
    assigned_to: str = ""


def _out(t: FollowupTask, patient_names: dict, org_names: dict) -> dict:
    return {
        "id": t.id,
        "patient_id": t.patient_id,
        "patient_name": patient_names.get(t.patient_id, ""),
        "org_id": t.org_id,
        "org_name": org_names.get(t.org_id, ""),
        "category": t.category,
        "category_name": CATEGORY_TITLES.get(t.category, t.category),
        "source_id": t.source_id,
        "title": t.title,
        "due_date": t.due_date,
        "assigned_to": t.assigned_to,
        "status": t.status,
        "result": t.result,
        "completed_at": t.completed_at.isoformat() if t.completed_at else "",
    }


# ---- 响应契约（字段精确镜像 `_out`/回执/统计行的现输出，勿改字节）----
# 取证与建模判断见 tests/test_followups_contract.py 的 docstring。


class FollowupTaskOut(BaseModel):
    """任务回执与列表行/超期行同形（`_out` 唯一产地）。

    completed_at 未完成时是**空串**不是 null（isoformat() if ... else ""），
    故声明 str 而非 str | None。
    """

    id: int
    patient_id: int
    patient_name: str
    org_id: int
    org_name: str
    category: str
    category_name: str
    source_id: int
    title: str
    due_date: str
    assigned_to: str
    status: str
    result: str
    completed_at: str


class FollowupActionReceiptOut(BaseModel):
    """完成/取消回执只有两键，不与 14 键任务行互相注入。"""

    id: int
    status: str


class FollowupCategoryStatsOut(BaseModel):
    # overdue 在 completion_rate_pct 之前——后者是循环后补进 dict 的尾键；
    # 完成率是真除法/兜底 0.0 的浮点产地，恒 float。
    category: str
    category_name: str
    pending: int
    done: int
    cancelled: int
    overdue: int
    completion_rate_pct: float


def _name_maps(db: Session, tasks: list[FollowupTask]) -> tuple[dict, dict]:
    patient_ids = {t.patient_id for t in tasks}
    patients = (
        row_dict(db.query(Patient.id, Patient.name).filter(Patient.id.in_(patient_ids)).all())
        if patient_ids
        else {}
    )
    orgs = row_dict(db.query(Organization.id, Organization.name).all())
    return patients, orgs


@router.post(
    "",
    status_code=201,
    response_model=FollowupTaskOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def create_followup(body: FollowupIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """人工补建随访任务（随访计划外的临时安排）。"""
    assert_org_writable(db, user, body.org_id)
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    payload = body.model_dump()
    payload["title"] = body.title or CATEGORY_TITLES.get(body.category, "随访")
    task = FollowupTask(**payload)
    db.add(task)
    db.commit()
    db.refresh(task)
    names, orgs = _name_maps(db, [task])
    return _out(task, names, orgs)


@router.get("", response_model=list[FollowupTaskOut])
def list_followups(
    response: Response,
    category: str | None = None,
    status: str | None = None,
    org_id: int | None = None,
    patient_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(FollowupTask)
    if category:
        query = query.filter(FollowupTask.category == category)
    if status:
        query = query.filter(FollowupTask.status == status)
    if org_id is not None:
        query = query.filter(FollowupTask.org_id == org_id)
    query = scope_patient_list(db, user, query, FollowupTask, patient_id, "followup")
    rows = paginate(query.order_by(FollowupTask.due_date, FollowupTask.id), response, offset, limit)
    names, orgs = _name_maps(db, rows)
    return [_out(t, names, orgs) for t in rows]


@router.get("/overdue", response_model=list[FollowupTaskOut])
def overdue_followups(today: str | None = None, db: Session = Depends(get_db)):
    """超期未随访清单。today 覆盖参数仅供测试/排查（与其他预警接口同一约定）。"""
    cutoff = resolve_business_date(today).isoformat()
    rows = (
        db.query(FollowupTask)
        .filter(FollowupTask.status == "pending", FollowupTask.due_date < cutoff)
        .order_by(FollowupTask.due_date)
        .limit(500)
        .all()
    )
    names, orgs = _name_maps(db, rows)
    return [_out(t, names, orgs) for t in rows]


class CompleteIn(BaseModel):
    result: str = Field(min_length=1, max_length=1024)


@router.post(
    "/{task_id}/complete",
    response_model=FollowupActionReceiptOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def complete_followup(task_id: int, body: CompleteIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.get(FollowupTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="随访任务不存在")
    assert_obj_org_writable(db, user, task)
    if task.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态 {task.status} 不可完成")
    task.status = "done"
    task.result = body.result
    task.completed_at = utcnow()
    db.commit()
    return {"id": task.id, "status": task.status}


@router.post(
    "/{task_id}/cancel",
    response_model=FollowupActionReceiptOut,
    dependencies=[Depends(require_roles("doctor", "public_health"))],
)
def cancel_followup(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.get(FollowupTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="随访任务不存在")
    assert_obj_org_writable(db, user, task)
    if task.status != "pending":
        raise HTTPException(status_code=409, detail=f"当前状态 {task.status} 不可取消")
    task.status = "cancelled"
    db.commit()
    return {"id": task.id, "status": task.status}


@router.get("/stats", response_model=list[FollowupCategoryStatsOut])
def followup_stats(today: str | None = None, db: Session = Depends(get_db)):
    """随访完成情况：按类别统计待随访/已完成/超期与完成率。"""
    cutoff = resolve_business_date(today).isoformat()
    rows = db.query(FollowupTask).all()
    stats: dict[str, dict] = {}
    for t in rows:
        entry = stats.setdefault(
            t.category,
            {
                "category": t.category,
                "category_name": CATEGORY_TITLES.get(t.category, t.category),
                "pending": 0,
                "done": 0,
                "cancelled": 0,
                "overdue": 0,
            },
        )
        entry[t.status] += 1
        if t.status == "pending" and t.due_date < cutoff:
            entry["overdue"] += 1
    for entry in stats.values():
        # 完成率分母排除已取消：取消的任务不该拉低随访绩效
        denominator = entry["pending"] + entry["done"]
        entry["completion_rate_pct"] = (
            round(entry["done"] * 100 / denominator, 2) if denominator else 0.0
        )
    return sorted(stats.values(), key=lambda x: x["category"])
