"""业务流程引擎与统一申请单中心（T5.2 / T5.3）。

## 流程引擎（T5.2）

流程定义 JSON 化，节点声明谁能推进，流转全程留痕，并按角色汇总待办。

既有的转诊、会诊、手术审批等状态机**保持不动**——它们的业务规则（床位释放、
号源回滚、费用校验）嵌在各自流程里，硬迁到通用引擎上只会把规则打散。引擎面向
**新增的审批类流程**，以及后续愿意迁移的流程。

## 统一申请单中心（T5.3）

这里刻意**不建第六张单据表**。平台已有预约、检查申请、会诊申请、用血申请、
手术申请五类单据，各自有必要的领域字段与状态机；再造一个通用单据表，结果只会是
一张没人写入的空表。真正缺的是"一个地方看全某位患者/某家机构所有在办事项"，
所以这里做的是**聚合视图**：把五类单据归一成统一字段返回，状态映射到统一口径。
"""
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..visibility import assert_obj_org_writable, assert_org_writable, assert_patient_visible
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, row_dict
from ..models import (
    Appointment,
    AppointmentSlot,
    TransfusionRequest,
    Consultation,
    ExamRequest,
    Organization,
    Patient,
    SurgeryRequest,
    User,
    WorkflowDefinition,
    WorkflowInstance,
    WorkflowTransition,
    utcnow,
)

router = APIRouter(prefix="/api/workflows", tags=["流程引擎"], dependencies=[Depends(get_current_user)])


# ============================================================================
# 流程定义
# ============================================================================


class NodeIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=64)
    # 可推进该节点的角色；空串表示任何已登录用户
    role: str = ""
    # 下一节点 key；空串表示终态
    next: str = ""


class DefinitionIn(BaseModel):
    key: str = Field(min_length=1, max_length=48)
    name: str = Field(min_length=1, max_length=128)
    nodes: list[NodeIn] = Field(min_length=1)


def _validate_nodes(nodes: list[NodeIn]) -> None:
    """定义期校验：节点键唯一、next 指向存在的节点、至少有一个终态。

    这些错误如果留到运行期，表现是单据卡死在某个节点上没人能推——
    比录入时报错难查得多。
    """
    keys = [n.key for n in nodes]
    if len(set(keys)) != len(keys):
        raise HTTPException(status_code=422, detail="节点 key 不得重复")
    known = set(keys)
    for node in nodes:
        if node.next and node.next not in known:
            raise HTTPException(status_code=422, detail=f"节点 {node.key} 的 next 指向不存在的节点")
    if all(n.next for n in nodes):
        raise HTTPException(status_code=422, detail="流程必须有终态节点（next 为空）")


@router.post("/definitions", status_code=201, dependencies=[Depends(require_admin)])
def create_definition(body: DefinitionIn, db: Session = Depends(get_db)):
    _validate_nodes(body.nodes)
    definition = WorkflowDefinition(
        key=body.key, name=body.name, nodes=[n.model_dump() for n in body.nodes]
    )
    db.add(definition)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="流程编码已存在") from None
    db.refresh(definition)
    return {"id": definition.id, "key": definition.key, "name": definition.name,
            "nodes": definition.nodes}


@router.get("/definitions")
def list_definitions(db: Session = Depends(get_db)):
    return [
        {"id": d.id, "key": d.key, "name": d.name, "nodes": d.nodes, "active": d.active}
        for d in db.query(WorkflowDefinition).order_by(WorkflowDefinition.key).all()
    ]


# ============================================================================
# 流程实例
# ============================================================================


class StartIn(BaseModel):
    definition_key: str
    business_type: str = Field(min_length=1, max_length=32)
    business_id: int = 0
    title: str = ""
    org_id: int | None = None


def _definition_or_404(db: Session, key: str) -> WorkflowDefinition:
    definition = (
        db.query(WorkflowDefinition)
        .filter(WorkflowDefinition.key == key, WorkflowDefinition.active.is_(True))
        .first()
    )
    if definition is None:
        raise HTTPException(status_code=404, detail="流程定义不存在或已停用")
    return definition


def _node(definition: WorkflowDefinition, key: str) -> dict | None:
    return next((n for n in definition.nodes if n["key"] == key), None)


def _instance_out(i: WorkflowInstance, node: dict | None = None) -> dict:
    return {
        "id": i.id,
        "definition_key": i.definition_key,
        "business_type": i.business_type,
        "business_id": i.business_id,
        "title": i.title,
        "org_id": i.org_id,
        "current_node": i.current_node,
        "current_node_name": (node or {}).get("name", ""),
        "current_node_role": (node or {}).get("role", ""),
        "status": i.status,
        "updated_at": i.updated_at.isoformat(),
    }


@router.post("/instances", status_code=201)
def start_instance(
    body: StartIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    """发起流程实例，落在首节点。"""
    definition = _definition_or_404(db, body.definition_key)
    if body.org_id is not None and db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    first = definition.nodes[0]
    instance = WorkflowInstance(
        definition_key=definition.key,
        business_type=body.business_type,
        business_id=body.business_id,
        title=body.title,
        org_id=body.org_id,
        current_node=first["key"],
        created_by=user.id,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)
    return _instance_out(instance, first)


class AdvanceIn(BaseModel):
    comment: str = ""


@router.post("/instances/{instance_id}/advance")
def advance_instance(
    instance_id: int,
    body: AdvanceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """推进到下一节点。当前节点声明的角色才有权推进（admin 全通）。"""
    instance = db.get(WorkflowInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="流程实例不存在")
    assert_obj_org_writable(db, user, instance)
    if instance.status != "running":
        raise HTTPException(status_code=409, detail=f"当前状态 {instance.status} 不可推进")
    definition = _definition_or_404(db, instance.definition_key)
    current = _node(definition, instance.current_node)
    if current is None:
        # 定义被改过、当前节点已不存在：不猜下一步，交人工处理
        raise HTTPException(status_code=409, detail="当前节点已不在流程定义中，请检查流程定义")
    required_role = current.get("role", "")
    if required_role and user.role not in (required_role, "admin"):
        raise HTTPException(status_code=403, detail=f"该节点需 {required_role} 角色推进")

    next_key = current.get("next", "")
    transition = WorkflowTransition(
        instance_id=instance.id,
        from_node=instance.current_node,
        to_node=next_key,
        action="advance",
        comment=body.comment,
        actor_id=user.id,
    )
    db.add(transition)
    if next_key:
        instance.current_node = next_key
    else:
        instance.status = "completed"
    instance.updated_at = utcnow()
    db.commit()
    db.refresh(instance)
    return _instance_out(instance, _node(definition, instance.current_node))


@router.post("/instances/{instance_id}/cancel")
def cancel_instance(
    instance_id: int,
    body: AdvanceIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    instance = db.get(WorkflowInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="流程实例不存在")
    assert_obj_org_writable(db, user, instance)
    if instance.status != "running":
        raise HTTPException(status_code=409, detail=f"当前状态 {instance.status} 不可终止")
    db.add(
        WorkflowTransition(
            instance_id=instance.id,
            from_node=instance.current_node,
            to_node="",
            action="cancel",
            comment=body.comment,
            actor_id=user.id,
        )
    )
    instance.status = "cancelled"
    instance.updated_at = utcnow()
    db.commit()
    return {"id": instance.id, "status": instance.status}


@router.get("/instances")
def list_instances(
    response: Response,
    definition_key: str | None = None,
    status: str | None = None,
    business_type: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
):
    query = db.query(WorkflowInstance)
    if definition_key:
        query = query.filter(WorkflowInstance.definition_key == definition_key)
    if status:
        query = query.filter(WorkflowInstance.status == status)
    if business_type:
        query = query.filter(WorkflowInstance.business_type == business_type)
    rows = paginate(query.order_by(WorkflowInstance.id.desc()), response, offset, limit)
    definitions = {d.key: d for d in db.query(WorkflowDefinition).all()}
    return [
        _instance_out(
            i, _node(definitions[i.definition_key], i.current_node)
            if i.definition_key in definitions else None
        )
        for i in rows
    ]


@router.get("/instances/{instance_id}/history")
def instance_history(instance_id: int, db: Session = Depends(get_db)):
    if db.get(WorkflowInstance, instance_id) is None:
        raise HTTPException(status_code=404, detail="流程实例不存在")
    rows = (
        db.query(WorkflowTransition)
        .filter(WorkflowTransition.instance_id == instance_id)
        .order_by(WorkflowTransition.id)
        .all()
    )
    actors = row_dict(db.query(User.id, User.full_name).all())
    return [
        {
            "id": t.id,
            "from_node": t.from_node,
            "to_node": t.to_node,
            "action": t.action,
            "comment": t.comment,
            "actor": actors.get(t.actor_id, ""),
            "created_at": t.created_at.isoformat(),
        }
        for t in rows
    ]


@router.get("/my-tasks")
def my_tasks(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """待办联动：当前用户角色可推进的流转中实例。

    admin 看全部——管理员本来就是兜底处理卡单的人。
    """
    definitions = {d.key: d for d in db.query(WorkflowDefinition).all()}
    rows = (
        db.query(WorkflowInstance)
        .filter(WorkflowInstance.status == "running")
        .order_by(WorkflowInstance.id.desc())
        .limit(200)
        .all()
    )
    tasks = []
    for instance in rows:
        definition = definitions.get(instance.definition_key)
        node = _node(definition, instance.current_node) if definition else None
        required_role = (node or {}).get("role", "")
        if user.role == "admin" or not required_role or required_role == user.role:
            tasks.append(_instance_out(instance, node))
    return {"count": len(tasks), "tasks": tasks}


# ============================================================================
# 统一申请单中心（T5.3）：五类既有单据的聚合视图
# ============================================================================

service_router = APIRouter(
    prefix="/api/service-requests", tags=["统一申请单中心"], dependencies=[Depends(get_current_user)]
)

# 各类单据的原生状态 → 统一口径（pending 待处理 / processing 处理中 /
# done 已完成 / cancelled 已取消）。映射写在一处，避免每个前端各译一遍。
STATUS_MAP = {
    "appointment": {"booked": "pending", "fulfilled": "done", "cancelled": "cancelled"},
    "exam": {"pending": "pending", "diagnosing": "processing", "reported": "done",
             "cancelled": "cancelled"},
    "consultation": {"applied": "pending", "accepted": "processing", "completed": "done",
                     "declined": "cancelled"},
    "blood": {"pending": "pending", "approved": "processing", "issued": "done",
              "rejected": "cancelled"},
    "surgery": {"requested": "pending", "approved": "processing", "scheduled": "processing",
                "completed": "done", "cancelled": "cancelled"},
}

TYPE_NAMES = {
    "appointment": "预约",
    "exam": "检查申请",
    "consultation": "会诊申请",
    "blood": "用血申请",
    "surgery": "手术申请",
}


def _unified(kind: str, obj_id: int, patient_id: int | None, org_id: int | None,
             title: str, raw_status: str, created_at) -> dict:
    return {
        "request_type": kind,
        "request_type_name": TYPE_NAMES[kind],
        "id": obj_id,
        "patient_id": patient_id,
        "org_id": org_id,
        "title": title,
        "raw_status": raw_status,
        "status": STATUS_MAP[kind].get(raw_status, raw_status),
        "created_at": created_at.isoformat() if created_at else "",
    }


@service_router.get("")
def unified_requests(
    patient_id: int | None = None,
    status: str | None = None,
    request_type: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """一个患者/一家机构所有在办事项的统一视图。

    刻意不建第六张单据表：五类单据各有必要的领域字段与状态机，再造一个通用表
    只会得到一张没人写入的空表。缺的是"一处看全"，所以这里做聚合而非替代。
    """
    # 聚合视图更要守：它一次把五类单据端出来，是最省事的一个越权入口
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="unified_requests")

    items: list[dict] = []

    appt_q = db.query(Appointment, AppointmentSlot).join(
        AppointmentSlot, Appointment.slot_id == AppointmentSlot.id
    )
    if patient_id is not None:
        appt_q = appt_q.filter(Appointment.patient_id == patient_id)
    for appt, slot in appt_q.order_by(Appointment.id.desc()).limit(limit).all():
        items.append(
            _unified("appointment", appt.id, appt.patient_id, slot.org_id,
                     f"{slot.resource_name} {slot.slot_date} {slot.slot_time}",
                     appt.status, appt.created_at)
        )

    exam_q = db.query(ExamRequest)
    if patient_id is not None:
        exam_q = exam_q.filter(ExamRequest.patient_id == patient_id)
    for exam in exam_q.order_by(ExamRequest.id.desc()).limit(limit).all():
        items.append(
            _unified("exam", exam.id, exam.patient_id, exam.from_org_id,
                     exam.item_name, exam.status, exam.created_at)
        )

    cons_q = db.query(Consultation)
    if patient_id is not None:
        cons_q = cons_q.filter(Consultation.patient_id == patient_id)
    for cons in cons_q.order_by(Consultation.id.desc()).limit(limit).all():
        items.append(
            _unified("consultation", cons.id, cons.patient_id, cons.from_org_id,
                     cons.question[:64], cons.status, cons.created_at)
        )

    blood_q = db.query(TransfusionRequest)
    if patient_id is not None:
        blood_q = blood_q.filter(TransfusionRequest.patient_id == patient_id)
    for req in blood_q.order_by(TransfusionRequest.id.desc()).limit(limit).all():
        items.append(
            _unified("blood", req.id, req.patient_id, req.org_id,
                     f"{req.blood_type} {req.component} {req.quantity_ml}ml", req.status, req.created_at)
        )

    surg_q = db.query(SurgeryRequest)
    if patient_id is not None:
        surg_q = surg_q.filter(SurgeryRequest.patient_id == patient_id)
    for surgery in surg_q.order_by(SurgeryRequest.id.desc()).limit(limit).all():
        items.append(
            _unified("surgery", surgery.id, surgery.patient_id, surgery.org_id,
                     surgery.surgery_name, surgery.status, surgery.created_at)
        )

    if request_type:
        items = [i for i in items if i["request_type"] == request_type]
    if status:
        items = [i for i in items if i["status"] == status]
    items.sort(key=lambda x: x["created_at"], reverse=True)

    patient_names = row_dict(db.query(Patient.id, Patient.name).all())
    org_names = row_dict(db.query(Organization.id, Organization.name).all())
    for item in items:
        item["patient_name"] = patient_names.get(item["patient_id"], "")
        item["org_name"] = org_names.get(item["org_id"], "")

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for item in items:
        by_status[item["status"]] = by_status.get(item["status"], 0) + 1
        by_type[item["request_type"]] = by_type.get(item["request_type"], 0) + 1
    # T6.7：total/统计口径覆盖全部命中项，items 只返回前 limit 条；
    # truncated 明确告知被截断，避免调用方拿 items 的长度当总数用。
    return {
        "total": len(items),
        "returned": min(len(items), limit),
        "truncated": len(items) > limit,
        "by_status": by_status,
        "by_type": by_type,
        "items": items[:limit],
    }
