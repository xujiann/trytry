"""㉞行政协同：项目管理。

指引第 34 条"具体功能"里点名了项目管理，此前平台一处都没有。

两条口径：

1. **进度由项目负责人直接报数，平台不按里程碑推算**。里程碑权重各院口径
   不一，平台算一个没人认的百分比，只会让人把精力花在争论算法上。
2. **逾期按日期现算**，不设定时任务改状态——与接种禁忌、疫苗效期同一条。
"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from ..visibility import assert_org_writable
from ..database import get_db
from ..datetypes import OptionalDateStr
from ..deps import get_current_user, require_roles, resolve_business_date, resolve_org_scope
from ..models import AdminProject, Organization, ProjectMilestone, User

router = APIRouter(
    prefix="/api/projects", tags=["项目管理"], dependencies=[Depends(get_current_user)]
)

PROJECT_STATUS = {
    "planning": "筹备",
    "ongoing": "进行中",
    "done": "已完成",
    "suspended": "已中止",
}


class ProjectIn(BaseModel):
    org_id: int
    name: str = Field(min_length=1, max_length=256)
    category: str = Field(default="general", max_length=32)
    owner_name: str = Field(default="", max_length=64)
    start_date: OptionalDateStr = ""
    due_date: OptionalDateStr = ""
    budget_amount: float = Field(default=0, ge=0)
    description: str = Field(default="", max_length=1024)


class ProjectUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(planning|ongoing|done|suspended)$")
    progress_pct: int | None = Field(default=None, ge=0, le=100)
    owner_name: str | None = None
    due_date: OptionalDateStr | None = None
    description: str | None = None


class MilestoneIn(BaseModel):
    name: str = Field(min_length=1, max_length=256)
    due_date: OptionalDateStr = ""
    note: str = Field(default="", max_length=512)


def _project(db: Session, project_id: int) -> AdminProject:
    project = db.get(AdminProject, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="项目不存在")
    return project


def _milestone_out(m: ProjectMilestone, today: str) -> dict:
    return {
        "id": m.id,
        "name": m.name,
        "due_date": m.due_date,
        "done": m.done,
        "done_date": m.done_date,
        # 逾期现算：已完成的不算逾期，无到期日的也不算
        "overdue": (not m.done) and bool(m.due_date) and m.due_date < today,
        "note": m.note,
    }


def _project_out(p: AdminProject, today: str, milestones: list[ProjectMilestone]) -> dict:
    ms = [_milestone_out(m, today) for m in milestones]
    return {
        "id": p.id,
        "org_id": p.org_id,
        "name": p.name,
        "category": p.category,
        "owner_name": p.owner_name,
        "start_date": p.start_date,
        "due_date": p.due_date,
        "status": p.status,
        "status_name": PROJECT_STATUS.get(p.status, p.status),
        # 负责人报的进度，不是平台按里程碑推算的
        "progress_pct": p.progress_pct,
        "budget_amount": round(p.budget_amount, 2),
        "description": p.description,
        "milestones": ms,
        "milestone_done": len([m for m in ms if m["done"]]),
        "milestone_total": len(ms),
        "milestone_overdue": len([m for m in ms if m["overdue"]]),
        "overdue": p.status not in ("done", "suspended") and bool(p.due_date) and p.due_date < today,
    }


@router.post("", status_code=201, dependencies=[Depends(require_roles("director", "operator"))])
def create_project(
    body: ProjectIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    if body.start_date and body.due_date and body.due_date < body.start_date:
        raise HTTPException(status_code=422, detail="计划完成日期不得早于开始日期")
    project = AdminProject(created_by=user.id, **body.model_dump())
    db.add(project)
    db.commit()
    db.refresh(project)
    return _project_out(project, resolve_business_date(None).isoformat(), [])


@router.get("")
def list_projects(
    org_id: int | None = None,
    status: str | None = None,
    group_id: int | None = None,
    overdue_only: bool = False,
    today: str | None = None,
    db: Session = Depends(get_db),
):
    today_str = resolve_business_date(today).isoformat()
    query = db.query(AdminProject)
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(AdminProject.org_id.in_(scope))
    if status:
        query = query.filter(AdminProject.status == status)
    projects = query.order_by(AdminProject.id.desc()).limit(200).all()
    # 一次取回全部里程碑按 project_id 分组，不在循环里逐条查（P0-1 的教训）
    by_project: dict[int, list[ProjectMilestone]] = {}
    if projects:
        for m in (
            db.query(ProjectMilestone)
            .filter(ProjectMilestone.project_id.in_([p.id for p in projects]))
            .all()
        ):
            by_project.setdefault(m.project_id, []).append(m)
    rows = [_project_out(p, today_str, by_project.get(p.id, [])) for p in projects]
    return [r for r in rows if r["overdue"]] if overdue_only else rows


@router.get("/{project_id}")
def get_project(project_id: int, today: str | None = None, db: Session = Depends(get_db)):
    project = _project(db, project_id)
    milestones = (
        db.query(ProjectMilestone)
        .filter(ProjectMilestone.project_id == project_id)
        .order_by(ProjectMilestone.due_date, ProjectMilestone.id)
        .all()
    )
    return _project_out(project, resolve_business_date(today).isoformat(), milestones)


@router.patch("/{project_id}", dependencies=[Depends(require_roles("director", "operator"))])
def update_project(project_id: int, body: ProjectUpdate, db: Session = Depends(get_db)):
    """更新进度与状态。

    结项要求进度报到 100：允许"已完成但进度 60%"，那份进度数就再也没人信了。
    确实做不完的项目走 suspended（中止），那是另一回事，不要求进度满格。
    """
    project = _project(db, project_id)
    data = body.model_dump(exclude_unset=True)
    if data.get("status") == "done":
        target = data.get("progress_pct", project.progress_pct)
        if target != 100:
            raise HTTPException(
                status_code=422,
                detail="结项须同时把进度报到 100%；确实做不完请用「中止」而非「完成」",
            )
    for field, value in data.items():
        if value is not None:
            setattr(project, field, value)
    db.commit()
    db.refresh(project)
    return _project_out(project, resolve_business_date(None).isoformat(), [])


@router.post(
    "/{project_id}/milestones",
    status_code=201,
    dependencies=[Depends(require_roles("director", "operator"))],
)
def add_milestone(project_id: int, body: MilestoneIn, db: Session = Depends(get_db)):
    _project(db, project_id)
    milestone = ProjectMilestone(project_id=project_id, **body.model_dump())
    db.add(milestone)
    db.commit()
    db.refresh(milestone)
    return _milestone_out(milestone, resolve_business_date(None).isoformat())


@router.post(
    "/milestones/{milestone_id}/done",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def complete_milestone(
    milestone_id: int, done_date: OptionalDateStr = "", db: Session = Depends(get_db)
):
    milestone = db.get(ProjectMilestone, milestone_id)
    if milestone is None:
        raise HTTPException(status_code=404, detail="里程碑不存在")
    if milestone.done:
        raise HTTPException(status_code=409, detail="该里程碑已完成")
    milestone.done = True
    milestone.done_date = done_date or resolve_business_date(None).isoformat()
    db.commit()
    db.refresh(milestone)
    return _milestone_out(milestone, resolve_business_date(None).isoformat())


@router.post(
    "/milestones/{milestone_id}/reopen",
    dependencies=[Depends(require_roles("director", "operator"))],
)
def reopen_milestone(milestone_id: int, db: Session = Depends(get_db)):
    """撤销完成。误点了要能改回来——凡是拦得住的都要放得开。"""
    milestone = db.get(ProjectMilestone, milestone_id)
    if milestone is None:
        raise HTTPException(status_code=404, detail="里程碑不存在")
    milestone.done = False
    milestone.done_date = ""
    db.commit()
    db.refresh(milestone)
    return _milestone_out(milestone, resolve_business_date(None).isoformat())


@router.get("/stats/overview")
def project_stats(
    group_id: int | None = None, today: str | None = None, db: Session = Depends(get_db)
):
    """项目总览：按状态、逾期与进度分布。

    平均进度只算**在办项目**（筹备+进行中）：把已完成的 100% 混进去，
    项目一多平均进度就永远好看，看不出在办的那批到底卡在哪。
    """
    today_str = resolve_business_date(today).isoformat()
    query = db.query(AdminProject)
    scope = resolve_org_scope(db, group_id, None)
    if scope is not None:
        query = query.filter(AdminProject.org_id.in_(scope))
    projects = query.all()
    by_status: dict[str, int] = {}
    active_progress = []
    overdue = 0
    for p in projects:
        by_status[p.status] = by_status.get(p.status, 0) + 1
        if p.status in ("planning", "ongoing"):
            active_progress.append(p.progress_pct)
            if p.due_date and p.due_date < today_str:
                overdue += 1
    return {
        "total": len(projects),
        "by_status": {
            k: {"count": v, "name": PROJECT_STATUS.get(k, k)} for k, v in by_status.items()
        },
        "active": len(active_progress),
        "overdue": overdue,
        "avg_progress_pct_active": (
            round(sum(active_progress) / len(active_progress), 1) if active_progress else None
        ),
        "total_budget": round(sum(p.budget_amount for p in projects), 2),
        "caliber": "平均进度只算在办项目（筹备+进行中），已完成与已中止不计入",
    }
