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
from typing import Any, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import and_, or_, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..numtypes import INT4_MAX, INT4_MIN
from ..texttypes import NON_BLANK
from ..visibility import (
    assert_obj_org_writable,
    assert_org_visible,
    assert_org_writable,
    assert_patient_visible,
    visible_org_ids,
    visible_patient_ids,
)
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

# 状态文案（措辞照抄模型列注释；报错文案用它，别把英文码直接拼给窗口人员看——P2-74）
INSTANCE_STATUS_NAMES = {"running": "流转中", "completed": "已完成", "cancelled": "已终止"}


# ============================================================================
# 流程定义
# ============================================================================


class NodeIn(BaseModel):
    key: str = Field(min_length=1, max_length=48, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    # 可推进该节点的角色；空串表示任何已登录用户
    role: str = ""
    # 下一节点 key；空串表示终态
    next: str = ""


class DefinitionIn(BaseModel):
    key: str = Field(min_length=1, max_length=48, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=128, pattern=NON_BLANK)
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


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class WorkflowDefinitionCreatedOut(BaseModel):
    """新建流程定义只回四个键（没有 active），与列表的五个键不同形。"""

    id: int
    key: str
    name: str
    # 节点定义（JSON 列）：每个节点的字段随节点类型而变，宽字典如实反映
    nodes: list[dict[str, Any]]


class WorkflowDefinitionOut(WorkflowDefinitionCreatedOut):
    active: bool


class WorkflowInstanceOut(BaseModel):
    id: int
    definition_key: str
    business_type: str
    business_id: int
    title: str
    org_id: int | None
    current_node: str
    # 定义被删/节点不存在时折成空串，不是 null
    current_node_name: str
    current_node_role: str
    status: str
    updated_at: str


class WorkflowInstanceStatusOut(BaseModel):
    id: int
    status: str


class WorkflowTransitionOut(BaseModel):
    id: int
    from_node: str
    to_node: str
    action: str
    comment: str
    actor: str
    created_at: str


class MyTasksOut(BaseModel):
    count: int
    tasks: list[WorkflowInstanceOut]


class UnifiedRequestOut(BaseModel):
    """统一申请单：五类单据折成同一形状。`raw_status` 与 `status` 都出——
    统一口径便于筛选，原生状态便于回到原单据核对，缺一个都要再查一次。

    `patient_name`/`org_name` **不在 `_unified()` 里**——它们是 handler 拿到
    列表之后统一回填的（一次查全部姓名，避免每条一次查询）。写这个模型时只照着
    `_unified()` 建，漏了这两个键，契约把它们静默丢掉，被
    `test_unified_requests_aggregates_five_types` 当场抓住。
    教训：**只读 helper 不够，要读完整个 handler**——返回值在 return 之前还会被改。
    """

    request_type: str
    request_type_name: str
    id: int
    patient_id: int | None
    org_id: int | None
    title: str
    raw_status: str
    status: str
    created_at: str
    # 由 handler 在 _unified() 之后统一回填（见上）
    patient_name: str
    org_name: str


class UnifiedRequestsOut(BaseModel):
    """`truncated` 明说截断了没有——只回 `items` 的话，看的人会把截断后的
    列表当成全部。"""

    total: int
    returned: int
    truncated: bool
    by_status: dict[str, int]
    by_type: dict[str, int]
    items: list[UnifiedRequestOut]


@router.post("/definitions", response_model=WorkflowDefinitionCreatedOut, status_code=201,
             dependencies=[Depends(require_admin)])
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


@router.get("/definitions", response_model=list[WorkflowDefinitionOut])
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
    business_type: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    business_id: int = Field(default=0, ge=INT4_MIN, le=INT4_MAX)
    title: str = Field(default="", max_length=256)
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


def _scope_instances(db: Session, user: User, query):
    """实例读侧与写侧同一口径（P0-38）：非全域角色只看本机构的与不挂机构的（全县流程）。

    推进 / 终止按 `assert_obj_org_writable(instance)` 判——别家的实例办不了，
    清单与待办里也就不该出现；不挂机构的实例谁都办得了，照旧人人可见。
    """
    allowed = visible_org_ids(db, user)
    if allowed is None:
        return query
    return query.filter(or_(WorkflowInstance.org_id.in_(allowed), WorkflowInstance.org_id.is_(None)))


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


@router.post("/instances", response_model=WorkflowInstanceOut, status_code=201)
def start_instance(
    body: StartIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """发起流程实例，落在首节点。"""
    assert_org_writable(db, user, body.org_id)
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
    comment: str = Field(default="", max_length=512)


def _move_instance(db: Session, instance_id: int, from_node: str, **values: Any) -> bool:
    """实例的一次状态跃迁：判定与写入压在**同一条带状态条件的 UPDATE** 里，返回这次是否跃迁到。

    旧写法是"`db.get` 读实例 → 判 `status == 'running'` → `db.add` 流转行 → 赋值
    `current_node`/`status` → commit"，典型的 check-then-act：两个人同时点"推进"
    （或一人推进、一人终止），都读到 running 且都读到同一节点，于是**两条流转行**都落库，
    实例的终态以最后提交的为准——留痕上看是同一个节点被批了两次，业务上是一次审批
    被另一次悄悄盖掉。`WHERE status = 'running' AND current_node = 读到的节点` 让后到的
    那几路 rowcount 为 0，与顺序请求一样拿 409：一个实例离开某个节点恰好一次，
    流转行也就恰好一条。

    唯一性长在**实例行**上，所以刻意**不**在 workflow_transitions 上建
    `(instance_id, from_node)` 唯一索引：`_validate_nodes` 不拒环（a→b→a 只要另有终态
    就存得下），环形定义里合法的第二圈会撞上索引，单子从此既推不动也终止不了——
    把一条多余的留痕换成一份卡死的单子。条件 UPDATE 认的是"当前位置"而不是历史，
    对环形定义同样成立，也不需要迁移。

    `from_node` 必须由调用方在 UPDATE **之前**读好再传进来：SQLAlchemy 2.0 的
    ORM `update()` 会顺手把 session 里的同一个对象同步成新值，UPDATE 之后再读
    `instance.current_node` 拿到的是推进后的节点，留痕里的 from_node 会等于 to_node。
    """
    moved = cast(CursorResult, db.execute(
        update(WorkflowInstance)
        .where(
            WorkflowInstance.id == instance_id,
            WorkflowInstance.status == "running",
            WorkflowInstance.current_node == from_node,
        )
        .values(updated_at=utcnow(), **values)
    ))
    return bool(moved.rowcount)


def _stale_move_409(db: Session, instance: WorkflowInstance, verb: str) -> HTTPException:
    """跃迁没命中时的措辞：按**库里此刻的样子**说话，而不是锁外读到的旧值。

    先 `rollback`（哪怕一行没命中，ORM `update()` 也可能已把内存里的对象同步成新值），
    再 `refresh` 读赢家提交后的实况，然后分两种：

    - 状态被改走了（对方终止了、或对方把最后一步推成了 completed）——复用顺序请求
      那句 409，调用方分辨不出"撞了车"与"本来就晚了一步"；
    - 状态还是 running、只是节点被推走了——才用这句新增的。顺序路径下这一刻根本不会
      409（晚到的人看到的是下一个节点，去推它或撞角色 403），没有可照抄的文案；
      而自动跟着推下一个节点等于替人做了另一道审批（下一节点的角色多半也不是他），
      所以宁可让人刷新后重来。

    连带的行为变化：同一个人双击"推进"，从前是两个 200 加一条多余留痕，现在是
    一个 200 一个 409。前端（pages-mgmt.js）对非 2xx 一律展示 `detail`，无需改动。
    """
    db.rollback()
    db.refresh(instance)
    if instance.status != "running":
        return HTTPException(status_code=409, detail=f"当前状态 {INSTANCE_STATUS_NAMES.get(instance.status, instance.status)} 不可{verb}")
    return HTTPException(status_code=409, detail="当前节点刚被其他人推进，请刷新后重试")


@router.post("/instances/{instance_id}/advance", response_model=WorkflowInstanceOut)
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
        raise HTTPException(status_code=409, detail=f"当前状态 {INSTANCE_STATUS_NAMES.get(instance.status, instance.status)} 不可推进")
    definition = _definition_or_404(db, instance.definition_key)
    current = _node(definition, instance.current_node)
    if current is None:
        # 定义被改过、当前节点已不存在：不猜下一步，交人工处理
        raise HTTPException(status_code=409, detail="当前节点已不在流程定义中，请检查流程定义")
    required_role = current.get("role", "")
    if required_role and user.role not in (required_role, "admin"):
        raise HTTPException(status_code=403, detail=f"该节点需 {required_role} 角色推进")

    # 先把当前位置读进本地变量：UPDATE 之后再读对象拿到的是新值（见 _move_instance）
    from_node = instance.current_node
    next_key = current.get("next", "")
    values: dict[str, Any] = {"current_node": next_key} if next_key else {"status": "completed"}
    if not _move_instance(db, instance.id, from_node, **values):
        raise _stale_move_409(db, instance, "推进")
    # 留痕只在跃迁命中后追加，且与它同一个事务：UPDATE 拿到的行锁一直持到 commit，
    # 中间没人能把实例挪走，from_node 记的就是这次推进真正的起点。
    db.add(
        WorkflowTransition(
            instance_id=instance.id,
            from_node=from_node,
            to_node=next_key,
            action="advance",
            comment=body.comment,
            actor_id=user.id,
        )
    )
    db.commit()
    db.refresh(instance)
    return _instance_out(instance, _node(definition, instance.current_node))


@router.post("/instances/{instance_id}/cancel",
             response_model=WorkflowInstanceStatusOut)
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
        raise HTTPException(status_code=409, detail=f"当前状态 {INSTANCE_STATUS_NAMES.get(instance.status, instance.status)} 不可终止")
    from_node = instance.current_node
    # 终止的条件里也带上 current_node：终止撞上一次推进时宁可让终止方拿 409 重来，
    # 也不要在一个自己没读到过的节点上落一条"从这里终止"的留痕——那条留痕会说谎。
    if not _move_instance(db, instance.id, from_node, status="cancelled"):
        raise _stale_move_409(db, instance, "终止")
    db.add(
        WorkflowTransition(
            instance_id=instance.id,
            from_node=from_node,
            to_node="",
            action="cancel",
            comment=body.comment,
            actor_id=user.id,
        )
    )
    db.commit()
    db.refresh(instance)
    return {"id": instance.id, "status": instance.status}


@router.get("/instances", response_model=list[WorkflowInstanceOut])
def list_instances(
    response: Response,
    definition_key: str | None = None,
    status: str | None = None,
    business_type: str | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = _scope_instances(db, user, db.query(WorkflowInstance))
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


@router.get("/instances/{instance_id}/history",
            response_model=list[WorkflowTransitionOut])
def instance_history(instance_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    instance = db.get(WorkflowInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="流程实例不存在")
    assert_org_visible(db, user, instance.org_id)  # 不挂机构的全县流程照旧放行
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


@router.get("/my-tasks", response_model=MyTasksOut)
def my_tasks(
    response: Response,
    offset: int = 0,
    limit: int = 200,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """待办联动：当前用户角色可推进的流转中实例。

    admin 看全部——管理员本来就是兜底处理卡单的人。

    「可推进」两个条件缺一不可：节点角色对得上，**且**实例的机构办得了（P0-38）。
    原先只按角色筛，别家机构的单子也算进待办，点「推进」才吃 403。

    角色这一筛在库里做（P1-82）：原先先取最新 200 条在办实例、再在 Python 里按角色筛，
    `count` 数的是筛剩的——最新 200 条都是别的角色的单子时，医生的待办是空的、计数是 0，
    而他名下的单子一张没少。判定照旧：节点要求别的角色的不算；定义或节点对不上号的
    （定义被改过、节点已不在）照旧算，那是要有人来终止的卡单。
    """
    definitions = {d.key: d for d in db.query(WorkflowDefinition).all()}
    query = _scope_instances(db, user, db.query(WorkflowInstance)).filter(WorkflowInstance.status == "running")
    if user.role != "admin":
        others_nodes = []
        for key, definition in definitions.items():
            nodes = []
            for n in definition.nodes:
                required_role = (_node(definition, n["key"]) or {}).get("role", "")
                if required_role and required_role != user.role:
                    nodes.append(n["key"])
            if nodes:
                others_nodes.append(and_(WorkflowInstance.definition_key == key,
                                         WorkflowInstance.current_node.in_(nodes)))
        if others_nodes:
            query = query.filter(~or_(*others_nodes))
    rows = paginate(query.order_by(WorkflowInstance.id.desc()), response, offset, limit)
    tasks = []
    for instance in rows:
        found = definitions.get(instance.definition_key)
        tasks.append(_instance_out(instance, _node(found, instance.current_node) if found else None))
    # 计数与响应头同一个数：paginate 刚按同一个查询数过
    return {"count": int(response.headers["X-Total-Count"]), "tasks": tasks}


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


@service_router.get("", response_model=UnifiedRequestsOut)
def unified_requests(
    patient_id: int | None = None,
    status: str | None = None,
    request_type: str | None = None,
    limit: int = Query(default=200, ge=0, le=INT4_MAX),  # 下界 0、上界只到列容量（P2-54）：原先负数在 PG 上 LIMIT 报错、SQLite 上回出 returned=-1、整个请求 500
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
        visible = None
    else:
        # P0-33：上面那句守卫原先只包在 `if patient_id` 里——不带患者号（页面默认就不带）时，
        # 五类单据按全域吐出来，连患者姓名。与各单据自己的清单同一口径（如预约清单的
        # scope_patient_list）：只见本机构服务过的患者；全域角色为 None，不过滤。
        # 本机构经手的单据（申请方 / 受邀方 / 领取方 / 执行机构）本身就构成服务关系，不会被滤掉。
        visible = visible_patient_ids(db, user)

    def scoped(query, model):
        if patient_id is not None:
            return query.filter(model.patient_id == patient_id)
        if visible is not None:
            return query.filter(model.patient_id.in_(visible))
        return query

    items: list[dict] = []

    appt_q = db.query(Appointment, AppointmentSlot).join(
        AppointmentSlot, Appointment.slot_id == AppointmentSlot.id
    )
    appt_q = scoped(appt_q, Appointment)
    for appt, slot in appt_q.order_by(Appointment.id.desc()).limit(limit).all():
        items.append(
            _unified("appointment", appt.id, appt.patient_id, slot.org_id,
                     f"{slot.resource_name} {slot.slot_date} {slot.slot_time}",
                     appt.status, appt.created_at)
        )

    exam_q = db.query(ExamRequest)
    exam_q = scoped(exam_q, ExamRequest)
    for exam in exam_q.order_by(ExamRequest.id.desc()).limit(limit).all():
        items.append(
            _unified("exam", exam.id, exam.patient_id, exam.from_org_id,
                     exam.item_name, exam.status, exam.created_at)
        )

    cons_q = db.query(Consultation)
    cons_q = scoped(cons_q, Consultation)
    for cons in cons_q.order_by(Consultation.id.desc()).limit(limit).all():
        items.append(
            _unified("consultation", cons.id, cons.patient_id, cons.from_org_id,
                     cons.question[:64], cons.status, cons.created_at)
        )

    blood_q = db.query(TransfusionRequest)
    blood_q = scoped(blood_q, TransfusionRequest)
    for req in blood_q.order_by(TransfusionRequest.id.desc()).limit(limit).all():
        items.append(
            _unified("blood", req.id, req.patient_id, req.org_id,
                     f"{req.blood_type} {req.component} {req.quantity_ml}ml", req.status, req.created_at)
        )

    surg_q = db.query(SurgeryRequest)
    surg_q = scoped(surg_q, SurgeryRequest)
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
