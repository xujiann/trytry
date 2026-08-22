"""全域慢专病 · 配置域：标准化指导路径与路径节点。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""


from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....database import get_db
from ....deps import get_current_user, paginate, require_roles
from ...platform import User
from ...models import (
    SpdPathNode,
    SpdPathTemplate,
    SpdProgram,
)
from ....visibility import assert_org_writable
from ._base import CONFIG_ROLES, _bump_version, _conditions, router


# ============================================================ 标准化指导路径


class PathTemplateIn(BaseModel):
    program_id: int
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    scene: str = Field(default="outpatient", pattern="^(outpatient|inpatient|home|followup)$")
    risk_level: str = Field(default="", max_length=16)
    version: str = Field(default="v1", max_length=16)
    scope: str = Field(default="region", pattern="^(region|org|team)$")
    org_id: int | None = None
    team_id: int | None = None
    description: str = Field(default="", max_length=512)


class PathNodeIn(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    stage: str = Field(default="", max_length=32)
    seq: int = 0
    dept: str = Field(default="", max_length=64)
    exec_role: str = Field(default="doctor", max_length=32)
    service_type: str = Field(
        default="followup",
        pattern="^(followup|revisit|edu|scale|exam|intervention|referral|monitor)$",
    )
    enter_condition: list[dict] = Field(default_factory=list)
    complete_condition: list[dict] = Field(default_factory=list)
    next_key: str = Field(default="", max_length=32)
    due_days: int = Field(default=7, ge=0, le=3650)
    timeout_action: str = Field(default="remind", pattern="^(remind|escalate|auto_complete)$")
    require_form: bool = False
    require_evidence: bool = False
    form_code: str = Field(default="", max_length=32)
    note: str = Field(default="", max_length=256)


def _template_out(t: SpdPathTemplate, nodes: list[SpdPathNode] | None = None) -> dict:
    out = {
        "id": t.id, "program_id": t.program_id, "code": t.code, "name": t.name,
        "scene": t.scene, "risk_level": t.risk_level, "version": t.version,
        "status": t.status, "scope": t.scope, "org_id": t.org_id, "team_id": t.team_id,
        "description": t.description, "copied_from_id": t.copied_from_id,
        "created_by": t.created_by,
    }
    if nodes is not None:
        out["nodes"] = [_node_out(n) for n in nodes]
        out["node_count"] = len(nodes)
    return out


def _node_out(n: SpdPathNode) -> dict:
    return {
        "id": n.id, "template_id": n.template_id, "key": n.key, "name": n.name,
        "stage": n.stage, "seq": n.seq, "dept": n.dept, "exec_role": n.exec_role,
        "service_type": n.service_type, "enter_condition": n.enter_condition or [],
        "complete_condition": n.complete_condition or [], "next_key": n.next_key,
        "due_days": n.due_days, "timeout_action": n.timeout_action,
        "require_form": n.require_form, "require_evidence": n.require_evidence,
        "form_code": n.form_code, "note": n.note,
    }


@router.post("/path-templates", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_path_template(
    body: PathTemplateIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(SpdProgram, body.program_id) is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    if body.org_id is not None:
        assert_org_writable(db, user, body.org_id)
    template = SpdPathTemplate(**body.model_dump(), created_by=user.username, status="draft")
    db.add(template)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该路径编码与版本已存在") from None
    return _template_out(template, [])


@router.get("/path-templates")
def list_path_templates(
    response: Response,
    program_id: int | None = None,
    scene: str | None = None,
    status: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdPathTemplate)
    if program_id is not None:
        query = query.filter(SpdPathTemplate.program_id == program_id)
    if scene:
        query = query.filter(SpdPathTemplate.scene == scene)
    if status:
        query = query.filter(SpdPathTemplate.status == status)
    if keyword:
        query = query.filter(SpdPathTemplate.name.contains(keyword))
    rows = paginate(query.order_by(SpdPathTemplate.id.desc()), response, offset, limit)
    counts: dict[int, int] = {}
    if rows:
        ids = [t.id for t in rows]
        for node in db.query(SpdPathNode).filter(SpdPathNode.template_id.in_(ids)).all():
            counts[node.template_id] = counts.get(node.template_id, 0) + 1
    return [{**_template_out(t), "node_count": counts.get(t.id, 0)} for t in rows]


@router.get("/path-templates/{template_id}")
def get_path_template(template_id: int, db: Session = Depends(get_db)):
    template = db.get(SpdPathTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == template_id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    return _template_out(template, nodes)


@router.post("/path-templates/{template_id}/nodes", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def add_path_node(
    template_id: int,
    body: PathNodeIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    template = db.get(SpdPathTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    assert_org_writable(db, user, template.org_id)
    if template.status == "published":
        # 已发布的模板不允许直接加节点：在跑的实例会突然多出一个没人知道的任务。
        # 要改就复制一版新的，这也是"版本"存在的意义。
        raise HTTPException(status_code=409, detail="已发布路径不可直接改节点，请复制新版本后修改")
    node = SpdPathNode(
        template_id=template_id,
        **body.model_dump(exclude={"enter_condition", "complete_condition"}),
        enter_condition=_conditions(body.enter_condition),
        complete_condition=_conditions(body.complete_condition),
    )
    db.add(node)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="节点 key 在该路径内重复") from None
    return _node_out(node)


@router.patch("/path-nodes/{node_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_path_node(
    node_id: int,
    body: dict,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    node = db.get(SpdPathNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="路径节点不存在")
    template = db.get(SpdPathTemplate, node.template_id)
    assert_org_writable(db, user, template.org_id if template else None)
    if template is not None and template.status == "published":
        raise HTTPException(status_code=409, detail="已发布路径不可直接改节点，请复制新版本后修改")
    allowed = {
        "name", "stage", "seq", "dept", "exec_role", "service_type", "next_key",
        "due_days", "timeout_action", "require_form", "require_evidence", "form_code", "note",
    }
    for key, value in body.items():
        if key in allowed:
            setattr(node, key, value)
    for key in ("enter_condition", "complete_condition"):
        if key in body:
            setattr(node, key, _conditions(body[key]))
    db.commit()
    return _node_out(node)


@router.delete("/path-nodes/{node_id}", status_code=204,
               dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def delete_path_node(
    node_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    node = db.get(SpdPathNode, node_id)
    if node is None:
        raise HTTPException(status_code=404, detail="路径节点不存在")
    template = db.get(SpdPathTemplate, node.template_id)
    assert_org_writable(db, user, template.org_id if template else None)
    if template is not None and template.status == "published":
        raise HTTPException(status_code=409, detail="已发布路径不可直接删节点，请复制新版本后修改")
    db.delete(node)
    db.commit()
    return Response(status_code=204)


class CopyIn(BaseModel):
    code: str = Field(default="", max_length=32)
    name: str = Field(default="", max_length=64)
    version: str = Field(default="", max_length=16)


@router.post("/path-templates/{template_id}/copy", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def copy_path_template(
    template_id: int,
    body: CopyIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """复制路径：连节点一起复制，落成 draft。这是修改已发布路径的正规途径。"""
    src = db.get(SpdPathTemplate, template_id)
    if src is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    assert_org_writable(db, user, src.org_id)
    copy = SpdPathTemplate(
        program_id=src.program_id,
        code=body.code or src.code,
        name=body.name or f"{src.name}(副本)",
        scene=src.scene, risk_level=src.risk_level,
        version=body.version or _bump_version(src.version),
        status="draft", copied_from_id=src.id, scope=src.scope, org_id=src.org_id,
        team_id=src.team_id, description=src.description, created_by=user.username,
    )
    db.add(copy)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="目标编码与版本已存在") from None
    for node in db.query(SpdPathNode).filter(SpdPathNode.template_id == src.id).all():
        db.add(
            SpdPathNode(
                template_id=copy.id, key=node.key, name=node.name, stage=node.stage,
                seq=node.seq, dept=node.dept, exec_role=node.exec_role,
                service_type=node.service_type, enter_condition=node.enter_condition,
                complete_condition=node.complete_condition, next_key=node.next_key,
                due_days=node.due_days, timeout_action=node.timeout_action,
                require_form=node.require_form, require_evidence=node.require_evidence,
                form_code=node.form_code, note=node.note,
            )
        )
    db.commit()
    return _template_out(copy)


class StatusIn(BaseModel):
    status: str = Field(pattern="^(draft|published|disabled)$")


@router.post("/path-templates/{template_id}/status",
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def set_path_status(
    template_id: int,
    body: StatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    template = db.get(SpdPathTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    assert_org_writable(db, user, template.org_id)
    if body.status == "published":
        nodes = db.query(SpdPathNode).filter(SpdPathNode.template_id == template_id).all()
        if not nodes:
            raise HTTPException(status_code=422, detail="路径没有任何节点，不能发布")
        keys = {n.key for n in nodes}
        dangling = [n.key for n in nodes if n.next_key and n.next_key not in keys]
        if dangling:
            # 断头的 next_key 在运行期表现为"办完这个节点就没有下一步了"，
            # 而患者路径会停在那里不报错。发布前挡住，比事后查任务为什么不生成省事得多。
            raise HTTPException(
                status_code=422, detail=f"以下节点的下一节点不存在：{'、'.join(dangling)}"
            )
    template.status = body.status
    db.commit()
    return _template_out(template)


@router.delete("/path-templates/{template_id}", status_code=204,
               dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def delete_path_template(
    template_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    # 同 centers.org-tree：`config/` 是子包，少一级会解析成不存在的模块
    from ...models import SpdPathInstance

    template = db.get(SpdPathTemplate, template_id)
    if template is None:
        raise HTTPException(status_code=404, detail="路径模板不存在")
    assert_org_writable(db, user, template.org_id)
    used = (
        db.query(SpdPathInstance.id)
        .filter(SpdPathInstance.template_id == template_id)
        .first()
    )
    if used is not None:
        raise HTTPException(status_code=409, detail="该路径已被患者实例引用，只能停用不能删除")
    db.query(SpdPathNode).filter(SpdPathNode.template_id == template_id).delete()
    db.delete(template)
    db.commit()
    return Response(status_code=204)
