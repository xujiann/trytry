"""全域慢专病 · 路径与任务域：路径实例、节点流转、统一任务中心。

对应招标文件：全程管理中心端 #13/#16、专家端 #7、服务团队专家端 #2、
成员端 #17/#18、医生移动端 #1/#6/#7。

任务中心的一条原则：**任务的状态只能沿一条链走**
`pending → claimed → doing → submitted → done`，外加 `overdue` / `rejected` / `cancelled`
三个旁支。所有动作接口都只在这条链上移动一格，没有"直接置为任意状态"的口子——
有那个口子，前端一定会用它来绕过审核。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import add_amount
from ...database import get_db
from ...datetypes import OptionalDateStr
from ...deps import get_current_user, paginate, require_roles, resolve_business_date
from ..platform import Patient, User, evidence_urls, notify_user, valid_task_evidence
from ..models import (
    SpdEnrollment,
    SpdPathInstance,
    SpdPathNode,
    SpdPathTemplate,
    SpdTask,
)
from ..service import advance_path, award_points, node_enter_allowed, spawn_task, sweep_overdue
from ...visibility import assert_org_writable, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·路径与任务"],
    dependencies=[Depends(get_current_user)],
)

SERVICE_ROLES = ("doctor", "public_health", "director")
OPEN_STATUSES = ("pending", "claimed", "doing", "submitted", "overdue")


# ============================================================ 路径实例


class StartPathIn(BaseModel):
    enrollment_id: int
    template_id: int
    overrides: dict = Field(default_factory=dict)


def _instance_out(db: Session, i: SpdPathInstance) -> dict:
    template = db.get(SpdPathTemplate, i.template_id)
    enrollment = db.get(SpdEnrollment, i.enrollment_id)
    patient = db.get(Patient, enrollment.patient_id) if enrollment else None
    return {
        "id": i.id, "enrollment_id": i.enrollment_id, "template_id": i.template_id,
        "template_code": i.template_code,
        "template_name": template.name if template else "",
        "scene": template.scene if template else "",
        "program_code": enrollment.program_code if enrollment else "",
        "patient_id": enrollment.patient_id if enrollment else None,
        "patient_name": patient.name if patient else "",
        "current_node_key": i.current_node_key, "current_stage": i.current_stage,
        "status": i.status, "progress": i.progress, "overrides": i.overrides or {},
        "owner_user_id": i.owner_user_id,
        "started_at": i.started_at.isoformat(),
        "finished_at": i.finished_at.isoformat() if i.finished_at else "",
    }


@router.post("/path-instances", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def start_path_instance(
    body: StartPathIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """为患者启动路径实例并生成首节点任务。

    同一纳管档案下**同一模板**只允许有一个在跑的实例：重复启动会生成两份
    并行任务，办完一份另一份还挂着，基层只会当成系统出错。
    """
    from ..service import start_path

    enrollment = db.get(SpdEnrollment, body.enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    if enrollment.status != "active":
        raise HTTPException(status_code=409, detail="非在管患者不能启动路径")
    template = db.get(SpdPathTemplate, body.template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    running = (
        db.query(SpdPathInstance)
        .filter(
            SpdPathInstance.enrollment_id == body.enrollment_id,
            SpdPathInstance.template_id == body.template_id,
            SpdPathInstance.status == "running",
        )
        .first()
    )
    if running is not None:
        raise HTTPException(status_code=409, detail="该路径已在执行中")
    try:
        instance = start_path(db, enrollment, template, user.id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    instance.overrides = body.overrides
    db.commit()
    return _instance_out(db, instance)


@router.get("/path-instances")
def list_path_instances(
    response: Response,
    enrollment_id: int | None = None,
    program_code: str | None = None,
    status: str | None = None,
    scene: str | None = None,
    stage: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """按病种、场景、阶段和状态查询患者路径实例（中心端 #16）。"""
    query = db.query(SpdPathInstance)
    if enrollment_id is not None:
        query = query.filter(SpdPathInstance.enrollment_id == enrollment_id)
    if status:
        query = query.filter(SpdPathInstance.status == status)
    if stage:
        query = query.filter(SpdPathInstance.current_stage == stage)
    orgs = visible_org_ids(db, user)
    if orgs is not None or program_code:
        enroll_query = db.query(SpdEnrollment.id)
        if orgs is not None:
            enroll_query = enroll_query.filter(SpdEnrollment.org_id.in_(orgs))
        if program_code:
            enroll_query = enroll_query.filter(SpdEnrollment.program_code == program_code)
        query = query.filter(
            SpdPathInstance.enrollment_id.in_([eid for (eid,) in enroll_query.all()] or [0])
        )
    if scene:
        template_ids = [
            t.id for t in db.query(SpdPathTemplate).filter(SpdPathTemplate.scene == scene).all()
        ]
        query = query.filter(SpdPathInstance.template_id.in_(template_ids or [0]))
    rows = paginate(query.order_by(SpdPathInstance.id.desc()), response, offset, limit)
    return [_instance_out(db, i) for i in rows]


@router.get("/path-instances/{instance_id}")
def get_path_instance(instance_id: int, db: Session = Depends(get_db)):
    """路径执行明细：节点清单 + 每个节点的任务状态与责任人。"""
    instance = db.get(SpdPathInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="路径实例不存在")
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == instance.template_id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    tasks = db.query(SpdTask).filter(SpdTask.instance_id == instance_id).all()
    by_node: dict[str, list[SpdTask]] = {}
    for task in tasks:
        by_node.setdefault(task.node_key, []).append(task)
    out = _instance_out(db, instance)
    out["nodes"] = [
        {
            "key": n.key, "name": n.name, "stage": n.stage, "seq": n.seq, "dept": n.dept,
            "exec_role": n.exec_role, "service_type": n.service_type,
            "due_days": n.due_days, "timeout_action": n.timeout_action,
            "require_form": n.require_form, "require_evidence": n.require_evidence,
            "is_current": n.key == instance.current_node_key,
            "tasks": [
                {"id": t.id, "status": t.status, "assignee_id": t.assignee_id,
                 "due_date": t.due_date, "finished_at":
                     t.finished_at.isoformat() if t.finished_at else ""}
                for t in by_node.get(n.key, [])
            ],
        }
        for n in nodes
    ]
    return out


class InstanceAdjustIn(BaseModel):
    overrides: dict | None = None
    status: str | None = Field(default=None, pattern="^(running|paused|cancelled)$")
    owner_user_id: int | None = None


@router.patch("/path-instances/{instance_id}",
              dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def adjust_path_instance(
    instance_id: int, body: InstanceAdjustIn, db: Session = Depends(get_db)
):
    """个性化调整：改的是**实例**不是模板（服务团队专家端 #2）。"""
    instance = db.get(SpdPathInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="路径实例不存在")
    if instance.status == "completed":
        raise HTTPException(status_code=409, detail="已完成的路径不可调整")
    data = body.model_dump(exclude_unset=True)
    for key, value in data.items():
        setattr(instance, key, value)
    if data.get("status") == "cancelled":
        instance.finished_at = now_naive()
        for task in (
            db.query(SpdTask)
            .filter(SpdTask.instance_id == instance_id, SpdTask.status.in_(OPEN_STATUSES))
            .all()
        ):
            task.status = "cancelled"
            task.review_note = "路径取消"
    db.commit()
    return _instance_out(db, instance)


@router.post("/path-instances/{instance_id}/advance",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def advance_instance(
    instance_id: int, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """手工推进到下一节点。

    正常流转由任务完成时自动触发（见 `complete_task`），这个接口是给
    "线下已经做了、系统里补一步"的场景用的，会校验当前节点没有未完成任务。
    """
    instance = db.get(SpdPathInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="路径实例不存在")
    # 实例本身不带机构，归属看它服务的纳管档案——别家机构不能替人推进路径
    owner = db.get(SpdEnrollment, instance.enrollment_id)
    assert_org_writable(db, user, owner.org_id if owner else None)
    if instance.status == "paused":
        # 因进入条件暂停的实例：重判当前节点条件，满足即恢复并派任务
        node = (
            db.query(SpdPathNode)
            .filter(
                SpdPathNode.template_id == instance.template_id,
                SpdPathNode.key == instance.current_node_key,
            )
            .first()
        )
        if node is None:
            raise HTTPException(status_code=409, detail="暂停节点已不存在，请调整路径实例")
        allowed, matched = node_enter_allowed(db, instance, node)
        if not allowed:
            raise HTTPException(
                status_code=409,
                detail=f"进入「{node.name}」的条件仍未满足，无法恢复",
            )
        enrollment = owner
        template = db.get(SpdPathTemplate, instance.template_id)
        instance.status = "running"
        if node.stage and enrollment is not None:
            enrollment.stage = node.stage
        if enrollment is not None:
            spawn_task(
                db, patient_id=enrollment.patient_id,
                title=f"{template.name if template else '路径'}·{node.name}",
                task_type="path", enrollment=enrollment, instance=instance,
                node=node, due_days=node.due_days, source="path",
            )
        db.commit()
        return {"instance": _instance_out(db, instance), "status": "running",
                "resumed": True, "matched": matched}
    if instance.status != "running":
        raise HTTPException(status_code=409, detail="路径不在执行中")
    open_tasks = (
        db.query(SpdTask)
        .filter(
            SpdTask.instance_id == instance_id,
            SpdTask.node_key == instance.current_node_key,
            SpdTask.status.in_(OPEN_STATUSES),
        )
        .count()
    )
    if open_tasks:
        raise HTTPException(status_code=409, detail="当前节点仍有未完成任务，不能推进")
    result = advance_path(db, instance)
    db.commit()
    return {"instance": _instance_out(db, instance), **result}


@router.get("/path-nodes/{node_id}/enter-check")
def check_node_enter(node_id: int, instance_id: int, db: Session = Depends(get_db)):
    """校验患者是否满足节点进入条件，供前端在办理前给出提示。"""
    node = db.get(SpdPathNode, node_id)
    instance = db.get(SpdPathInstance, instance_id)
    if node is None or instance is None:
        raise HTTPException(status_code=404, detail="节点或路径实例不存在")
    allowed, matched = node_enter_allowed(db, instance, node)
    return {"allowed": allowed, "matched": matched, "conditions": node.enter_condition or []}


# ============================================================ 统一任务中心


class TaskIn(BaseModel):
    patient_id: int
    title: str = Field(min_length=1, max_length=128)
    task_type: str = Field(
        default="followup",
        pattern="^(path|followup|intervention|assess|revisit|referral|report|recall|edu|screen)$",
    )
    program_code: str = Field(default="", max_length=32)
    enrollment_id: int | None = None
    assignee_id: int | None = None
    team_id: int | None = None
    org_id: int | None = None
    due_days: int = Field(default=7, ge=0, le=3650)
    priority: int = Field(default=1, ge=1, le=3)
    form_code: str = Field(default="", max_length=32)
    require_evidence: bool = False


def _task_out(t: SpdTask, brief: dict | None = None) -> dict:
    out = {
        "id": t.id, "program_code": t.program_code, "patient_id": t.patient_id,
        "enrollment_id": t.enrollment_id, "instance_id": t.instance_id,
        "node_key": t.node_key, "task_type": t.task_type, "title": t.title,
        "org_id": t.org_id, "team_id": t.team_id, "assignee_id": t.assignee_id,
        "exec_role": t.exec_role, "status": t.status, "priority": t.priority,
        "due_date": t.due_date, "form_code": t.form_code,
        "require_evidence": t.require_evidence, "form": t.form or {},
        "result": t.result or {}, "evidence": t.evidence or [],
        "evidence_urls": evidence_urls(t.evidence),
        "urged_count": t.urged_count, "escalated": t.escalated,
        "review_note": t.review_note, "source": t.source,
        "created_at": t.created_at.isoformat(),
        "finished_at": t.finished_at.isoformat() if t.finished_at else "",
    }
    if brief:
        out.update({"patient_name": brief.get("name", ""), "phone": brief.get("phone", "")})
    return out


@router.post("/tasks", status_code=201, dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_task(
    body: TaskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_patient_visible(db, user, body.patient_id, resource="spd_task")
    enrollment = (
        db.get(SpdEnrollment, body.enrollment_id) if body.enrollment_id is not None else None
    )
    if body.enrollment_id is not None and enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    task = spawn_task(
        db,
        patient_id=body.patient_id,
        title=body.title,
        task_type=body.task_type,
        program_code=body.program_code,
        enrollment=enrollment,
        assignee_id=body.assignee_id,
        org_id=body.org_id if body.org_id is not None else user.org_id,
        team_id=body.team_id,
        due_days=body.due_days,
        priority=body.priority,
        source="manual",
        form_code=body.form_code,
        require_evidence=body.require_evidence,
    )
    db.commit()
    return _task_out(task)


@router.get("/tasks")
def list_tasks(
    response: Response,
    task_type: str | None = None,
    status: str | None = None,
    open_only: bool = False,
    program_code: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    assignee_id: int | None = None,
    mine: bool = False,
    patient_id: int | None = None,
    priority: int | None = None,
    due_before: str = "",
    escalated: bool | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """任务清单：中心端 #13 要求的六个筛选维度 + "我的待办"。"""
    query = db.query(SpdTask)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_task")
        query = query.filter(SpdTask.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(SpdTask.org_id.in_(orgs))
    if mine:
        query = query.filter(SpdTask.assignee_id == user.id)
    for column, value in (
        (SpdTask.task_type, task_type), (SpdTask.status, status),
        (SpdTask.program_code, program_code), (SpdTask.org_id, org_id),
        (SpdTask.team_id, team_id), (SpdTask.assignee_id, assignee_id),
        (SpdTask.priority, priority),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if open_only:
        query = query.filter(SpdTask.status.in_(OPEN_STATUSES))
    if escalated is not None:
        query = query.filter(SpdTask.escalated.is_(escalated))
    if due_before:
        query = query.filter(SpdTask.due_date != "", SpdTask.due_date <= due_before)
    rows = paginate(
        query.order_by(SpdTask.priority.desc(), SpdTask.due_date, SpdTask.id.desc()),
        response, offset, limit,
    )
    briefs = {
        p.id: {"name": p.name, "phone": p.phone}
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return [_task_out(r, briefs.get(r.patient_id)) for r in rows]


@router.get("/tasks/summary")
def task_summary(
    program_code: str | None = None,
    org_id: int | None = None,
    mine: bool = False,
    today: str | None = None,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待办统计：按类型与状态汇总，供各端工作台顶部的数字卡片使用。

    进这个接口时顺手扫一次超期（`sweep_overdue`）——工作台是各端的第一屏，
    在这里刷新可以保证"没开定时任务的环境也能看到真实的超期数"。
    """
    business_day = resolve_business_date(today)
    swept = sweep_overdue(db, business_day)
    db.commit()

    query = db.query(SpdTask)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdTask.org_id.in_(orgs))
    if org_id is not None:
        query = query.filter(SpdTask.org_id == org_id)
    if program_code:
        query = query.filter(SpdTask.program_code == program_code)
    if mine:
        query = query.filter(SpdTask.assignee_id == user.id)

    by_status = dict(
        query.with_entities(SpdTask.status, func.count(SpdTask.id))
        .group_by(SpdTask.status).all()
    )
    by_type = dict(
        query.filter(SpdTask.status.in_(OPEN_STATUSES))
        .with_entities(SpdTask.task_type, func.count(SpdTask.id))
        .group_by(SpdTask.task_type).all()
    )
    today_str = business_day.isoformat()
    return {
        "by_status": by_status,
        "open_by_type": by_type,
        "open_total": sum(by_status.get(s, 0) for s in OPEN_STATUSES),
        "overdue": by_status.get("overdue", 0),
        "escalated": query.filter(SpdTask.escalated.is_(True)).count(),
        "due_today": query.filter(
            SpdTask.due_date == today_str, SpdTask.status.in_(OPEN_STATUSES)
        ).count(),
        "swept": swept,
    }


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    task = db.get(SpdTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    assert_patient_visible(db, user, task.patient_id, resource="spd_task")
    patient = db.get(Patient, task.patient_id)
    return _task_out(task, {"name": patient.name, "phone": patient.phone} if patient else None)


def _load_task(db: Session, task_id: int, user: User) -> SpdTask:
    """取任务并校验机构归属（SpdTask 有 org_id）。

    任务处理（claim/submit/review/complete 等）原经此 helper 取对象、无机构守卫——
    别家服务角色可领取/填报/审核办结他院任务，`review`/`complete` 还联动 award_points
    与 advance_path 跨机构改积分与路径。收进取件函数：一律须本机构可写。
    """
    task = db.get(SpdTask, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    assert_org_writable(db, user, task.org_id)
    return task


@router.post("/tasks/{task_id}/claim", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def claim_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """接收任务。已被别人接收的返回 409——静默改责任人会让原责任人白干一场。"""
    task = _load_task(db, task_id, user)
    if task.status not in ("pending", "overdue"):
        raise HTTPException(status_code=409, detail="该任务不处于可接收状态")
    if task.assignee_id not in (None, user.id):
        raise HTTPException(status_code=409, detail="该任务已由其他人员接收")
    task.assignee_id = user.id
    task.status = "claimed"
    db.commit()
    return _task_out(task)


class AssignIn(BaseModel):
    assignee_id: int
    note: str = Field(default="", max_length=256)


@router.post("/tasks/{task_id}/assign", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def assign_task(
    task_id: int,
    body: AssignIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """分配/转派任务。转派保留 `transferred_from`，方便追"这活是从谁那儿转来的"。"""
    task = _load_task(db, task_id, user)
    if task.status in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="已结束的任务不可再分配")
    if db.get(User, body.assignee_id) is None:
        raise HTTPException(status_code=404, detail="责任人不存在")
    if task.assignee_id is not None and task.assignee_id != body.assignee_id:
        task.transferred_from = task.assignee_id
    task.assignee_id = body.assignee_id
    if task.status == "pending":
        task.status = "claimed"
    if body.note:
        task.review_note = body.note
    db.commit()
    return _task_out(task)


@router.post("/tasks/{task_id}/urge", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def urge_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """催办：计数 +1 并给责任人发站内消息。催办不改状态——催过还是待办。

    走适配层的 `notify_user` 而不是平台的 `notify.notify_staff`：后者按机构+角色
    群发，发不到**具体某个人**（任务的责任人）。
    """
    task = _load_task(db, task_id, user)
    if task.status not in OPEN_STATUSES:
        raise HTTPException(status_code=409, detail="该任务已结束，无需催办")
    # 催办计数走原子 UPDATE：两个人同时点催办，读-改-写只会记成一次
    add_amount(db, SpdTask, task.id, "urged_count", 1)
    db.flush()
    db.refresh(task)
    if task.assignee_id is not None:
        notify_user(
            db, task.assignee_id, category="spd_task", title="慢专病任务催办",
            body=f"任务「{task.title}」已被催办（第{task.urged_count}次），请尽快处理",
            link_type="spd_task", link_id=task.id,
        )
    db.commit()
    return _task_out(task)


@router.post("/tasks/{task_id}/escalate", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def escalate_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """超时升级：置紧急并标记升级，由上级机构接手督办。"""
    task = _load_task(db, task_id, user)
    if task.status not in OPEN_STATUSES:
        raise HTTPException(status_code=409, detail="该任务已结束，无需升级")
    task.escalated = True
    task.priority = max(task.priority, 2)
    db.commit()
    return _task_out(task)


class SubmitIn(BaseModel):
    result: dict = Field(default_factory=dict)
    # 附件 id 列表。曾是自由字符串——那能让 require_evidence 被一串乱码糊弄过去
    evidence: list[int | str] = Field(default_factory=list)
    draft: bool = False
    note: str = Field(default="", max_length=512)


@router.post("/tasks/{task_id}/submit", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def submit_task(
    task_id: int, body: SubmitIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """填写表单并保存草稿 / 提交审核（成员端 #17/#18）。

    `require_evidence` 的任务没传佐证材料时拒绝提交——这是节点配置里
    勾选过的硬要求，提交时不校验等于配置形同虚设。
    """
    task = _load_task(db, task_id, user)
    if task.status in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="该任务已结束")
    task.result = body.result
    if body.evidence:
        problems = valid_task_evidence(db, task.id, body.evidence)
        if problems:
            raise HTTPException(status_code=422, detail="；".join(problems))
        task.evidence = body.evidence
    if body.draft:
        task.status = "doing"
        db.commit()
        return _task_out(task)
    if task.require_evidence and not (task.evidence or []):
        raise HTTPException(status_code=422, detail="该任务要求上传佐证材料后才能提交")
    task.status = "submitted"
    task.assignee_id = task.assignee_id or user.id
    if body.note:
        task.review_note = body.note
    db.commit()
    return _task_out(task)


class ReviewTaskIn(BaseModel):
    approved: bool = True
    note: str = Field(default="", max_length=256)


@router.post("/tasks/{task_id}/review", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def review_task(
    task_id: int, body: ReviewTaskIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """审核任务：通过即完成并推进路径，退回则回到办理中。"""
    task = _load_task(db, task_id, user)
    if task.status != "submitted":
        raise HTTPException(status_code=409, detail="只有待审核的任务可以审核")
    task.reviewer_id = user.id
    task.review_note = body.note
    if not body.approved:
        task.status = "rejected"
        db.commit()
        return _task_out(task)
    return _finish_task(db, task, user)


@router.post("/tasks/{task_id}/complete", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def complete_task(
    task_id: int, body: SubmitIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """直接办结（不走审核的任务类型）。表单与佐证要求同 submit。"""
    task = _load_task(db, task_id, user)
    if task.status in ("done", "cancelled"):
        raise HTTPException(status_code=409, detail="该任务已结束")
    if body.result:
        task.result = body.result
    if body.evidence:
        problems = valid_task_evidence(db, task.id, body.evidence)
        if problems:
            raise HTTPException(status_code=422, detail="；".join(problems))
        task.evidence = body.evidence
    if task.require_evidence and not (task.evidence or []):
        raise HTTPException(status_code=422, detail="该任务要求上传佐证材料后才能办结")
    return _finish_task(db, task, user)


def _finish_task(db: Session, task: SpdTask, user: User) -> dict:
    """任务办结的收尾动作：置完成、推进路径、村医计分、更新随访日期。"""
    task.status = "done"
    task.finished_at = now_naive()
    task.assignee_id = task.assignee_id or user.id
    # 会话是 autoflush=False 的：不显式 flush，下面数"还有几条没办完"时
    # 会把刚办完的这条也数进去，路径就永远推不动。
    db.flush()

    advanced = None
    if task.instance_id is not None:
        instance = db.get(SpdPathInstance, task.instance_id)
        if instance is not None and instance.status == "running":
            siblings = (
                db.query(SpdTask)
                .filter(
                    SpdTask.instance_id == instance.id,
                    SpdTask.node_key == task.node_key,
                    SpdTask.id != task.id,
                    SpdTask.status.in_(OPEN_STATUSES),
                )
                .count()
            )
            if siblings == 0 and task.node_key == instance.current_node_key:
                advanced = advance_path(db, instance)

    if task.task_type == "followup" and task.enrollment_id is not None:
        enrollment = db.get(SpdEnrollment, task.enrollment_id)
        if enrollment is not None:
            enrollment.last_followup_at = date.today().isoformat()
            interval = _followup_interval(db, enrollment)
            enrollment.next_followup_at = (
                date.today() + timedelta(days=interval)
            ).isoformat()
            award_points(
                db, enrollment.village_doctor_id, "followup",
                ref_type="task", ref_id=task.id, note="随访完成",
                org_id=enrollment.org_id,
            )
    db.commit()
    out = _task_out(task)
    if advanced:
        out["advanced"] = advanced
    return out


def _followup_interval(db: Session, enrollment: SpdEnrollment) -> int:
    """随访周期取该病种当前阶段的管理目标配置，没配就按 90 天。"""
    from ..service import target_for

    for metric in ("bp_sys", "glucose_fasting", "spo2", "egfr", "ldl", "bmi"):
        target = target_for(db, enrollment.program_code, enrollment.stage, metric)
        if target is not None:
            return target.followup_interval_days
    return 90


class BatchTaskIn(BaseModel):
    task_ids: list[int] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(claim|urge|escalate|cancel|assign)$")
    assignee_id: int | None = None
    note: str = Field(default="", max_length=256)


@router.post("/tasks/batch", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def batch_tasks(
    body: BatchTaskIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """批量处理（中心端 #13）。逐条判定，不满足条件的跳过并在返回里说明。

    整批要么全成要么全败在这里是错的口径：一次勾 200 条，有 3 条已经被别人接了，
    不该让另外 197 条也办不成。
    """
    # 机构收口：非全域只处理本机构任务，越权 id 直接不纳入本批（不泄露其存在）
    tasks_q = db.query(SpdTask).filter(SpdTask.id.in_(body.task_ids))
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        tasks_q = tasks_q.filter(SpdTask.org_id.in_(orgs))
    tasks = tasks_q.all()
    done, skipped = 0, []
    for task in tasks:
        if body.action in ("claim", "urge", "escalate", "cancel") and task.status not in OPEN_STATUSES:
            skipped.append({"id": task.id, "reason": "任务已结束"})
            continue
        if body.action == "claim":
            if task.assignee_id not in (None, user.id):
                skipped.append({"id": task.id, "reason": "已被他人接收"})
                continue
            task.assignee_id, task.status = user.id, "claimed"
        elif body.action == "urge":
            add_amount(db, SpdTask, task.id, "urged_count", 1)
        elif body.action == "escalate":
            task.escalated, task.priority = True, max(task.priority, 2)
        elif body.action == "cancel":
            task.status, task.review_note = "cancelled", body.note or "批量取消"
            task.finished_at = now_naive()
        elif body.action == "assign":
            if body.assignee_id is None:
                raise HTTPException(status_code=422, detail="批量分配须指定责任人")
            if task.status in ("done", "cancelled"):
                skipped.append({"id": task.id, "reason": "任务已结束"})
                continue
            if task.assignee_id is not None and task.assignee_id != body.assignee_id:
                task.transferred_from = task.assignee_id
            task.assignee_id = body.assignee_id
        done += 1
    db.commit()
    return {"processed": done, "skipped": skipped}


@router.get("/tasks-export")
def export_tasks(
    program_code: str | None = None,
    status: str | None = None,
    org_id: int | None = None,
    task_type: str | None = None,
    limit: int = 2000,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """任务导出：返回行数据 + 表头，前端拼 CSV。

    不在服务端生成文件：平台既有的导出（绩效、考核）都走这个形状，
    多一种导出方式就多一份要维护的编码/换行/BOM 处理。
    """
    query = db.query(SpdTask)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdTask.org_id.in_(orgs))
    for column, value in (
        (SpdTask.program_code, program_code), (SpdTask.status, status),
        (SpdTask.org_id, org_id), (SpdTask.task_type, task_type),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    rows = query.order_by(SpdTask.id.desc()).limit(min(max(limit, 1), 5000)).all()
    briefs = {
        p.id: p.name
        for p in db.query(Patient).filter(Patient.id.in_([r.patient_id for r in rows] or [0]))
    }
    return {
        "columns": ["任务ID", "患者", "病种", "任务类型", "标题", "状态", "优先级",
                    "截止日期", "责任人ID", "催办次数", "创建时间"],
        "rows": [
            [r.id, briefs.get(r.patient_id, ""), r.program_code, r.task_type, r.title,
             r.status, r.priority, r.due_date, r.assignee_id or "", r.urged_count,
             r.created_at.strftime("%Y-%m-%d %H:%M")]
            for r in rows
        ],
        "total": len(rows),
    }
