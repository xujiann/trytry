"""全域慢专病 · 配置域：病种规则、管理目标、标准路径、量表、宣教、服务包、
团队与村医、设备、数据源、专病中心。

对应招标文件：平台管理端 #2~#17、专病专家端 #1~#3、卫健管理端 #5~#8。

配置写接口统一收在 `require_roles("director")`（admin 自动通过）而不是 `require_admin`：
专病中心的牵头科室主任要能改自己病种的路径与目标，全部压到平台管理员会让配置
变成一件"要提工单"的事，实施期没人受得了。真正的平台级开关（病种启停、
数据源接入）仍然要 admin。
"""
from datetime import timedelta
from secrets import token_urlsafe

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import insert_if_absent
from ...database import get_db
from ...datetypes import OptionalDateStr
from ...deps import get_current_user, paginate, require_admin, require_roles
from ..platform import Organization, User
from ..models import (
    SpdCenter,
    SpdDataSource,
    SpdDevice,
    SpdEduMaterial,
    SpdPathNode,
    SpdPathTemplate,
    SpdProgram,
    SpdProgramVersion,
    SpdScale,
    SpdServicePackage,
    SpdSyncLog,
    SpdTag,
    SpdTarget,
    SpdTeam,
    SpdTeamMember,
    SpdVillageDoctor,
)
from ..rules import FIELD_SOURCES, OPERATORS, RuleError, validate_conditions
from ...visibility import assert_org_writable

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·配置"],
    dependencies=[Depends(get_current_user)],
)

CONFIG_ROLES = ("director", "doctor")


def _conditions(raw: list[dict] | None) -> list[dict]:
    try:
        return validate_conditions(raw or [])
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


# ============================================================ 元数据（规则可选项）


@router.get("/meta")
def rule_meta():
    """规则可用字段与比较符，供管理端渲染规则编辑器。

    做成接口而不是前端写死：字段表将来会随采集项扩充，
    两处各维护一份的结果一定是前端能选、后端不认。
    """
    return {
        "fields": [{"key": k, "name": v} for k, v in FIELD_SOURCES.items()],
        "operators": [{"key": k, "name": v} for k, v in OPERATORS.items()],
        "risk_levels": [
            {"key": "low", "name": "低危", "color": "#2e9e5b"},
            {"key": "mid", "name": "中危", "color": "#e0a325"},
            {"key": "high", "name": "高危", "color": "#e06c25"},
            {"key": "very_high", "name": "极高危", "color": "#d9363e"},
        ],
        "task_types": {
            "path": "路径节点", "followup": "随访", "intervention": "干预",
            "assess": "评估", "revisit": "复诊", "referral": "转诊",
            "report": "上报", "recall": "召回", "edu": "宣教", "screen": "筛查复核",
        },
        "member_roles": {
            "doctor": "医生", "nurse": "护士", "rehab": "康复治疗师",
            "case_manager": "个案管理师", "village_doctor": "村医", "expert": "专家",
        },
    }


# ============================================================ 专病档案（病种）


class ProgramIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="chronic", pattern="^(chronic|specialty)$")
    lead_org_id: int | None = None
    lead_dept: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=512)
    include_rules: list[dict] = Field(default_factory=list)
    exclude_rules: list[dict] = Field(default_factory=list)
    stages: list[dict] = Field(default_factory=list)
    milestones: list[dict] = Field(default_factory=list)
    effective_from: OptionalDateStr = ""


class ProgramUpdate(BaseModel):
    name: str | None = None
    lead_org_id: int | None = None
    lead_dept: str | None = None
    description: str | None = None
    include_rules: list[dict] | None = None
    exclude_rules: list[dict] | None = None
    stages: list[dict] | None = None
    milestones: list[dict] | None = None
    active: bool | None = None
    note: str = Field(default="", max_length=256)


def _program_out(p: SpdProgram) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "category": p.category,
        "lead_org_id": p.lead_org_id, "lead_dept": p.lead_dept,
        "description": p.description, "include_rules": p.include_rules or [],
        "exclude_rules": p.exclude_rules or [], "stages": p.stages or [],
        "milestones": p.milestones or [], "version": p.version,
        "effective_from": p.effective_from, "active": p.active,
    }


def _bump_version(version: str) -> str:
    """v1 → v2。非 v 开头的自定义版本号原样保留并追加 -r2，不猜用户的编号规则。"""
    if version.startswith("v") and version[1:].isdigit():
        return f"v{int(version[1:]) + 1}"
    return f"{version}-r2"


@router.post("/programs", status_code=201, dependencies=[Depends(require_admin)])
def create_program(body: ProgramIn, db: Session = Depends(get_db)):
    if body.lead_org_id is not None and db.get(Organization, body.lead_org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    program = SpdProgram(
        **body.model_dump(exclude={"include_rules", "exclude_rules"}),
        include_rules=_conditions(body.include_rules),
        exclude_rules=_conditions(body.exclude_rules),
    )
    db.add(program)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该专病编码已存在") from None
    return _program_out(program)


@router.get("/programs")
def list_programs(
    response: Response,
    category: str | None = None,
    active: bool | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdProgram)
    if category:
        query = query.filter(SpdProgram.category == category)
    if active is not None:
        query = query.filter(SpdProgram.active.is_(active))
    if keyword:
        query = query.filter(SpdProgram.name.contains(keyword))
    rows = paginate(query.order_by(SpdProgram.id), response, offset, limit)
    return [_program_out(p) for p in rows]


@router.get("/programs/{program_id}")
def get_program(program_id: int, db: Session = Depends(get_db)):
    program = db.get(SpdProgram, program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    out = _program_out(program)
    out["targets"] = [_target_out(t) for t in
                      db.query(SpdTarget).filter(SpdTarget.program_id == program_id).all()]
    return out


@router.patch("/programs/{program_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_program(
    program_id: int,
    body: ProgramUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """改规则即升版本并留快照——半年后要能回答"这批人当初按哪版规则纳的管"。"""
    program = db.get(SpdProgram, program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    data = body.model_dump(exclude_unset=True, exclude={"note"})
    rules_changed = any(
        key in data for key in ("include_rules", "exclude_rules", "stages", "milestones")
    )
    if rules_changed:
        db.add(
            SpdProgramVersion(
                program_id=program.id, version=program.version,
                snapshot=_program_out(program), changed_by=user.username, note=body.note,
            )
        )
    for key in ("include_rules", "exclude_rules"):
        if key in data:
            data[key] = _conditions(data[key])
    for key, value in data.items():
        setattr(program, key, value)
    if rules_changed:
        program.version = _bump_version(program.version)
    db.commit()
    return _program_out(program)


@router.get("/programs/{program_id}/versions")
def program_versions(program_id: int, db: Session = Depends(get_db)):
    rows = (
        db.query(SpdProgramVersion)
        .filter(SpdProgramVersion.program_id == program_id)
        .order_by(SpdProgramVersion.id.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": v.id, "version": v.version, "changed_by": v.changed_by, "note": v.note,
         "snapshot": v.snapshot, "created_at": v.created_at.isoformat()}
        for v in rows
    ]


# ============================================================ 管理目标


class TargetIn(BaseModel):
    stage: str = Field(default="", max_length=32)
    metric: str = Field(min_length=1, max_length=32)
    metric_name: str = Field(default="", max_length=64)
    kind: str = Field(default="quantitative", pattern="^(quantitative|qualitative)$")
    target_low: float | None = None
    target_high: float | None = None
    unit: str = Field(default="", max_length=16)
    qualitative: str = Field(default="", max_length=128)
    risk_level: str = Field(default="", max_length=16)
    followup_interval_days: int = Field(default=90, ge=1, le=3650)
    form_code: str = Field(default="", max_length=32)
    edu_code: str = Field(default="", max_length=32)


def _target_out(t: SpdTarget) -> dict:
    return {
        "id": t.id, "program_id": t.program_id, "stage": t.stage, "metric": t.metric,
        "metric_name": t.metric_name, "kind": t.kind, "target_low": t.target_low,
        "target_high": t.target_high, "unit": t.unit, "qualitative": t.qualitative,
        "risk_level": t.risk_level, "followup_interval_days": t.followup_interval_days,
        "form_code": t.form_code, "edu_code": t.edu_code, "active": t.active,
    }


@router.post("/programs/{program_id}/targets", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_target(program_id: int, body: TargetIn, db: Session = Depends(get_db)):
    if db.get(SpdProgram, program_id) is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    if body.kind == "quantitative" and body.target_low is None and body.target_high is None:
        raise HTTPException(status_code=422, detail="量化目标须至少给出上限或下限")
    if (
        body.target_low is not None
        and body.target_high is not None
        and body.target_low > body.target_high
    ):
        raise HTTPException(status_code=422, detail="目标下限不得大于上限")
    target = SpdTarget(program_id=program_id, **body.model_dump())
    db.add(target)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该阶段的同一指标目标已存在") from None
    return _target_out(target)


@router.get("/programs/{program_id}/targets")
def list_targets(program_id: int, stage: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdTarget).filter(SpdTarget.program_id == program_id)
    if stage is not None:
        query = query.filter(SpdTarget.stage == stage)
    return [_target_out(t) for t in query.order_by(SpdTarget.id).all()]


@router.patch("/targets/{target_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_target(target_id: int, body: dict, db: Session = Depends(get_db)):
    target = db.get(SpdTarget, target_id)
    if target is None:
        raise HTTPException(status_code=404, detail="管理目标不存在")
    allowed = {
        "metric_name", "target_low", "target_high", "unit", "qualitative", "risk_level",
        "followup_interval_days", "form_code", "edu_code", "active",
    }
    for key, value in body.items():
        if key in allowed:
            setattr(target, key, value)
    db.commit()
    return _target_out(target)


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
    counts = {}
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
    from ..models import SpdPathInstance

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


# ============================================================ 评估量表


class ScaleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="risk", pattern="^(risk|stage|rehab|screen)$")
    program_code: str = Field(default="", max_length=32)
    version: str = Field(default="v1", max_length=16)
    items: list[dict] = Field(default_factory=list)
    scoring: dict = Field(default_factory=dict)
    owner_team_id: int | None = None


def _scale_out(s: SpdScale) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "category": s.category,
        "program_code": s.program_code, "version": s.version, "status": s.status,
        "items": s.items or [], "scoring": s.scoring or {}, "qr_token": s.qr_token,
        "owner_team_id": s.owner_team_id,
    }


@router.post("/scales", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_scale(body: ScaleIn, db: Session = Depends(get_db)):
    keys = [i.get("key") for i in body.items]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="量表题目 key 不得重复")
    scale = SpdScale(**body.model_dump(), status="draft")
    db.add(scale)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该量表编码与版本已存在") from None
    return _scale_out(scale)


@router.get("/scales")
def list_scales(
    response: Response,
    category: str | None = None,
    program_code: str | None = None,
    status: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdScale)
    if category:
        query = query.filter(SpdScale.category == category)
    if program_code:
        query = query.filter(SpdScale.program_code == program_code)
    if status:
        query = query.filter(SpdScale.status == status)
    return [_scale_out(s) for s in paginate(query.order_by(SpdScale.id), response, offset, limit)]


@router.get("/scales/{scale_id}")
def get_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    return _scale_out(scale)


@router.patch("/scales/{scale_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_scale(scale_id: int, body: dict, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if scale.status == "published" and ("items" in body or "scoring" in body):
        raise HTTPException(status_code=409, detail="已发布量表不可改题目或评分，请新建版本")
    for key in ("name", "items", "scoring", "category", "owner_team_id"):
        if key in body:
            setattr(scale, key, body[key])
    db.commit()
    return _scale_out(scale)


@router.post("/scales/{scale_id}/publish", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def publish_scale(scale_id: int, db: Session = Depends(get_db)):
    """发布并生成二维码令牌——量表要"经审核发布"后才允许被评估引用。"""
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if not scale.items:
        raise HTTPException(status_code=422, detail="量表没有题目，不能发布")
    scale.status = "published"
    if not scale.qr_token:
        scale.qr_token = token_urlsafe(12)
    db.commit()
    return _scale_out(scale)


def _qr_svg(content: str) -> str:
    """把一段文本编成二维码 SVG。

    `qrcode` 是纯 Python 实现（无 Pillow 也能出 SVG），符合"不引重依赖"的约束；
    SVG 而不是 PNG：打印培训海报要放大到 A4，位图会糊。
    """
    import io

    import qrcode
    import qrcode.image.svg

    img = qrcode.make(content, image_factory=qrcode.image.svg.SvgPathImage, box_size=16)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


@router.get("/scales/{scale_id}/qr.svg")
def scale_qr(scale_id: int, request: Request, db: Session = Depends(get_db)):
    """量表评估二维码（成员端 #8）：扫码直达居民端自查页并预选该量表。

    编码的是**页面地址**（`/m/#scale=<token>`）而不是 API 地址——扫码的人
    要看到的是问卷，不是一段 JSON。令牌失效（量表停用）时页面自然回落到
    量表列表，码不用重印。
    """
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    if scale.status != "published" or not scale.qr_token:
        raise HTTPException(status_code=409, detail="量表未发布，先发布生成令牌")
    url = f"{str(request.base_url).rstrip('/')}/m/#scale={scale.qr_token}"
    return Response(content=_qr_svg(url), media_type="image/svg+xml")


@router.post("/scales/{scale_id}/disable", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def disable_scale(scale_id: int, db: Session = Depends(get_db)):
    scale = db.get(SpdScale, scale_id)
    if scale is None:
        raise HTTPException(status_code=404, detail="量表不存在")
    scale.status = "disabled"
    db.commit()
    return _scale_out(scale)


# ============================================================ 宣教素材


class EduIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=128)
    program_code: str = Field(default="", max_length=32)
    media_type: str = Field(default="text", pattern="^(text|audio|video)$")
    content: str = Field(default="", max_length=8192)
    media_url: str = Field(default="", max_length=256)
    dept: str = Field(default="", max_length=64)


@router.post("/edu-materials", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def create_edu(body: EduIn, db: Session = Depends(get_db)):
    material = SpdEduMaterial(**body.model_dump())
    db.add(material)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该宣教编码已存在") from None
    return _edu_out(material)


def _edu_out(m: SpdEduMaterial) -> dict:
    return {
        "id": m.id, "code": m.code, "title": m.title, "program_code": m.program_code,
        "media_type": m.media_type, "content": m.content, "media_url": m.media_url,
        "dept": m.dept, "active": m.active,
    }


@router.get("/edu-materials")
def list_edu(
    response: Response,
    program_code: str | None = None,
    media_type: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdEduMaterial).filter(SpdEduMaterial.active.is_(True))
    if program_code:
        query = query.filter(SpdEduMaterial.program_code == program_code)
    if media_type:
        query = query.filter(SpdEduMaterial.media_type == media_type)
    if keyword:
        query = query.filter(SpdEduMaterial.title.contains(keyword))
    rows = paginate(query.order_by(SpdEduMaterial.id.desc()), response, offset, limit)
    return [_edu_out(m) for m in rows]


@router.patch("/edu-materials/{material_id}",
              dependencies=[Depends(require_roles("director", "doctor", "public_health"))])
def update_edu(material_id: int, body: dict, db: Session = Depends(get_db)):
    material = db.get(SpdEduMaterial, material_id)
    if material is None:
        raise HTTPException(status_code=404, detail="宣教素材不存在")
    for key in ("title", "content", "media_url", "media_type", "dept", "active", "program_code"):
        if key in body:
            setattr(material, key, body[key])
    db.commit()
    return _edu_out(material)


# ============================================================ 服务包


class PackageIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(default="", max_length=32)
    price: float = Field(default=0, ge=0)
    period_days: int = Field(default=365, ge=1, le=3650)
    items: list[dict] = Field(default_factory=list)


def _package_out(p: SpdServicePackage) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "program_code": p.program_code,
        "price": p.price, "period_days": p.period_days, "items": p.items or [],
        "active": p.active,
    }


@router.post("/service-packages", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_package(body: PackageIn, db: Session = Depends(get_db)):
    for item in body.items:
        if not item.get("code") or int(item.get("times", 0)) <= 0:
            raise HTTPException(status_code=422, detail="服务包项目须有编码且次数大于0")
    package = SpdServicePackage(**body.model_dump())
    db.add(package)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该服务包编码已存在") from None
    return _package_out(package)


@router.get("/service-packages")
def list_packages(
    response: Response,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdServicePackage)
    if program_code:
        query = query.filter(SpdServicePackage.program_code == program_code)
    rows = paginate(query.order_by(SpdServicePackage.id), response, offset, limit)
    return [_package_out(p) for p in rows]


@router.patch("/service-packages/{package_id}",
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_package(package_id: int, body: dict, db: Session = Depends(get_db)):
    package = db.get(SpdServicePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    for key in ("name", "price", "period_days", "items", "active"):
        if key in body:
            setattr(package, key, body[key])
    db.commit()
    return _package_out(package)


# ============================================================ 标签


class TagIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    category: str = Field(default="patient", max_length=32)
    color: str = Field(default="", max_length=16)


@router.post("/tags", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_tag(body: TagIn, db: Session = Depends(get_db)):
    tag = SpdTag(**body.model_dump())
    db.add(tag)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该标签编码已存在") from None
    return {"id": tag.id, "code": tag.code, "name": tag.name, "category": tag.category,
            "color": tag.color, "active": tag.active}


@router.get("/tags")
def list_tags(category: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdTag).filter(SpdTag.active.is_(True))
    if category:
        query = query.filter(SpdTag.category == category)
    return [
        {"id": t.id, "code": t.code, "name": t.name, "category": t.category, "color": t.color}
        for t in query.order_by(SpdTag.id).limit(300).all()
    ]


# ============================================================ 服务团队


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    org_id: int
    level: str = Field(default="township", pattern="^(county|township|village|center)$")
    program_codes: list[str] = Field(default_factory=list)
    leader_user_id: int | None = None
    dept: str = Field(default="", max_length=64)
    service_area: str = Field(default="", max_length=256)
    data_scope: str = Field(default="org", pattern="^(org|group|region)$")


class MemberIn(BaseModel):
    user_id: int
    member_role: str = Field(
        default="doctor",
        pattern="^(doctor|nurse|rehab|case_manager|village_doctor|expert)$",
    )
    program_codes: list[str] = Field(default_factory=list)
    stage_scope: str = Field(default="", max_length=64)
    patient_scope: str = Field(default="team", pattern="^(self|team|org|region)$")
    can_view: bool = True
    can_followup: bool = True
    can_referral: bool = False
    can_audit: bool = False
    can_assess: bool = False


def _team_out(t: SpdTeam, members: int | None = None) -> dict:
    out = {
        "id": t.id, "name": t.name, "org_id": t.org_id, "level": t.level,
        "program_codes": t.program_codes or [], "leader_user_id": t.leader_user_id,
        "dept": t.dept, "service_area": t.service_area, "data_scope": t.data_scope,
        "active": t.active,
    }
    if members is not None:
        out["member_count"] = members
    return out


@router.post("/teams", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_team(
    body: TeamIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    assert_org_writable(db, user, body.org_id)
    team = SpdTeam(**body.model_dump())
    db.add(team)
    db.commit()
    return _team_out(team, 0)


@router.get("/teams")
def list_teams(
    response: Response,
    org_id: int | None = None,
    level: str | None = None,
    program_code: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdTeam).filter(SpdTeam.active.is_(True))
    if org_id is not None:
        query = query.filter(SpdTeam.org_id == org_id)
    if level:
        query = query.filter(SpdTeam.level == level)
    rows = paginate(query.order_by(SpdTeam.id), response, offset, limit)
    if program_code:
        rows = [t for t in rows if program_code in (t.program_codes or [])]
    counts: dict[int, int] = {}
    if rows:
        ids = [t.id for t in rows]
        for m in db.query(SpdTeamMember).filter(SpdTeamMember.team_id.in_(ids)).all():
            counts[m.team_id] = counts.get(m.team_id, 0) + 1
    return [_team_out(t, counts.get(t.id, 0)) for t in rows]


@router.get("/teams/{team_id}")
def get_team(team_id: int, db: Session = Depends(get_db)):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    members = db.query(SpdTeamMember).filter(SpdTeamMember.team_id == team_id).all()
    user_names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([m.user_id for m in members] or [0])).all()
    }
    out = _team_out(team, len(members))
    out["members"] = [
        {
            "id": m.id, "user_id": m.user_id, "user_name": user_names.get(m.user_id, ""),
            "member_role": m.member_role, "program_codes": m.program_codes or [],
            "stage_scope": m.stage_scope, "patient_scope": m.patient_scope,
            "can_view": m.can_view, "can_followup": m.can_followup,
            "can_referral": m.can_referral, "can_audit": m.can_audit,
            "can_assess": m.can_assess, "active": m.active,
        }
        for m in members
    ]
    return out


@router.patch("/teams/{team_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_team(
    team_id: int, body: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    assert_org_writable(db, user, team.org_id)
    for key in ("name", "level", "program_codes", "leader_user_id", "dept", "service_area",
                "data_scope", "active"):
        if key in body:
            setattr(team, key, body[key])
    db.commit()
    return _team_out(team)


@router.post("/teams/{team_id}/members", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def add_team_member(
    team_id: int,
    body: MemberIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    team = db.get(SpdTeam, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    assert_org_writable(db, user, team.org_id)
    if db.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    member = SpdTeamMember(team_id=team_id, **body.model_dump())
    db.add(member)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该成员已在团队内") from None
    return {"id": member.id, "team_id": team_id, "user_id": member.user_id,
            "member_role": member.member_role}


@router.patch("/team-members/{member_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_team_member(member_id: int, body: dict, db: Session = Depends(get_db)):
    member = db.get(SpdTeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="团队成员不存在")
    for key in ("member_role", "program_codes", "stage_scope", "patient_scope", "can_view",
                "can_followup", "can_referral", "can_audit", "can_assess", "active"):
        if key in body:
            setattr(member, key, body[key])
    db.commit()
    return {"id": member.id, "member_role": member.member_role, "active": member.active}


@router.delete("/team-members/{member_id}", status_code=204,
               dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def remove_team_member(member_id: int, db: Session = Depends(get_db)):
    member = db.get(SpdTeamMember, member_id)
    if member is None:
        raise HTTPException(status_code=404, detail="团队成员不存在")
    db.delete(member)
    db.commit()
    return Response(status_code=204)


# ============================================================ 村医档案


class VillageDoctorIn(BaseModel):
    user_id: int
    org_id: int
    township: str = Field(default="", max_length=64)
    village: str = Field(default="", max_length=64)
    license_no: str = Field(default="", max_length=64)
    license_valid_to: OptionalDateStr = ""
    phone: str = Field(default="", max_length=20)


class VillageDoctorBatchIn(BaseModel):
    items: list[VillageDoctorIn] = Field(min_length=1, max_length=500)


def _vd_out(v: SpdVillageDoctor, name: str = "") -> dict:
    return {
        "id": v.id, "user_id": v.user_id, "user_name": name, "org_id": v.org_id,
        "township": v.township, "village": v.village, "license_no": v.license_no,
        "license_valid_to": v.license_valid_to, "phone": v.phone,
        "bind_token": v.bind_token, "active": v.active,
    }


@router.post("/village-doctors", status_code=201,
             dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_village_doctor(
    body: VillageDoctorIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    if db.get(User, body.user_id) is None:
        raise HTTPException(status_code=404, detail="用户不存在")
    record = SpdVillageDoctor(**body.model_dump(), bind_token=token_urlsafe(12))
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该用户已建村医档案") from None
    return _vd_out(record)


@router.post("/village-doctors/batch", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def batch_village_doctors(
    body: VillageDoctorBatchIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量开通村医账号档案。

    逐条 savepoint 而不是整批一次提交：一条重复就整批回滚，导入方只知道"失败了"，
    还得自己二分查是哪一行。这里返回逐行结果，重复的跳过、其余照常建。
    """
    created, skipped = [], []
    for item in body.items:
        assert_org_writable(db, user, item.org_id)
        if db.get(User, item.user_id) is None:
            skipped.append({"user_id": item.user_id, "reason": "用户不存在"})
            continue
        exists = (
            db.query(SpdVillageDoctor.id)
            .filter(SpdVillageDoctor.user_id == item.user_id)
            .first()
        )
        if exists is not None:
            skipped.append({"user_id": item.user_id, "reason": "已建档"})
            continue
        if insert_if_absent(
            db, SpdVillageDoctor(**item.model_dump(), bind_token=token_urlsafe(12))
        ):
            created.append(item.user_id)
        else:
            skipped.append({"user_id": item.user_id, "reason": "并发写入冲突，已跳过"})
    db.commit()
    return {"created": len(created), "skipped": skipped}


@router.get("/village-doctors")
def list_village_doctors(
    response: Response,
    org_id: int | None = None,
    township: str | None = None,
    village: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdVillageDoctor)
    if org_id is not None:
        query = query.filter(SpdVillageDoctor.org_id == org_id)
    if township:
        query = query.filter(SpdVillageDoctor.township == township)
    if village:
        query = query.filter(SpdVillageDoctor.village == village)
    rows = paginate(query.order_by(SpdVillageDoctor.id), response, offset, limit)
    names = {
        u.id: u.full_name or u.username
        for u in db.query(User).filter(User.id.in_([v.user_id for v in rows] or [0])).all()
    }
    return [_vd_out(v, names.get(v.user_id, "")) for v in rows]


@router.patch("/village-doctors/{vd_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_village_doctor(
    vd_id: int, body: dict, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    record = db.get(SpdVillageDoctor, vd_id)
    if record is None:
        raise HTTPException(status_code=404, detail="村医档案不存在")
    assert_org_writable(db, user, record.org_id)
    for key in ("township", "village", "license_no", "license_valid_to", "phone", "active"):
        if key in body:
            setattr(record, key, body[key])
    db.commit()
    return _vd_out(record)


@router.get("/village-doctors/{vd_id}/qr.svg")
def village_doctor_qr(vd_id: int, request: Request, db: Session = Depends(get_db)):
    """村医绑定二维码：扫码进入医生移动端并带上绑定令牌（村医赋能端 #1）。

    停用的村医不出码——码是入口，入口先于账号被回收。
    """
    record = db.get(SpdVillageDoctor, vd_id)
    if record is None:
        raise HTTPException(status_code=404, detail="村医档案不存在")
    if not record.active or not record.bind_token:
        raise HTTPException(status_code=409, detail="村医已停用或没有绑定令牌")
    url = f"{str(request.base_url).rstrip('/')}/m/doctor.html#bind={record.bind_token}"
    return Response(content=_qr_svg(url), media_type="image/svg+xml")


# ============================================================ 设备


class DeviceIn(BaseModel):
    sn: str = Field(min_length=1, max_length=64)
    device_type: str = Field(pattern="^(bp|glucose|band|scale|poct|ecg)$")
    model: str = Field(default="", max_length=64)
    org_id: int | None = None


def _device_out(d: SpdDevice) -> dict:
    return {
        "id": d.id, "sn": d.sn, "device_type": d.device_type, "model": d.model,
        "org_id": d.org_id, "bound_patient_id": d.bound_patient_id, "status": d.status,
        "last_sync_at": d.last_sync_at.isoformat() if d.last_sync_at else "",
    }


@router.post("/devices", status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def create_device(
    body: DeviceIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    assert_org_writable(db, user, body.org_id)
    device = SpdDevice(**body.model_dump())
    db.add(device)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该设备序列号已登记") from None
    return _device_out(device)


@router.get("/devices")
def list_devices(
    response: Response,
    device_type: str | None = None,
    status: str | None = None,
    org_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdDevice)
    if device_type:
        query = query.filter(SpdDevice.device_type == device_type)
    if status:
        query = query.filter(SpdDevice.status == status)
    if org_id is not None:
        query = query.filter(SpdDevice.org_id == org_id)
    rows = paginate(query.order_by(SpdDevice.id), response, offset, limit)
    return [_device_out(d) for d in rows]


class DeviceBindIn(BaseModel):
    patient_id: int | None = None


@router.post("/devices/{device_id}/bind",
             dependencies=[Depends(require_roles("director", "doctor", "operator"))])
def bind_device(
    device_id: int,
    body: DeviceBindIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """绑定/解绑设备。`patient_id` 为空即解绑——两个动作合一个接口，
    因为它们改的是同一列，分开会出现"解绑接口忘了改 status"这类不同步。"""
    device = db.get(SpdDevice, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="设备不存在")
    assert_org_writable(db, user, device.org_id)
    device.bound_patient_id = body.patient_id
    device.status = "bound" if body.patient_id else "idle"
    db.commit()
    return _device_out(device)


# ============================================================ 数据源接入与监控


class DataSourceIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    source_type: str = Field(pattern="^(HIS|EMR|LIS|PACS|checkup|publichealth|device)$")
    org_id: int | None = None
    endpoint: str = Field(default="", max_length=256)
    freq_minutes: int = Field(default=60, ge=1, le=1440)
    scope: str = Field(default="", max_length=256)


def _source_out(s: SpdDataSource) -> dict:
    return {
        "id": s.id, "code": s.code, "name": s.name, "source_type": s.source_type,
        "org_id": s.org_id, "endpoint": s.endpoint, "freq_minutes": s.freq_minutes,
        "scope": s.scope, "active": s.active, "status": s.status,
        "last_sync_at": s.last_sync_at.isoformat() if s.last_sync_at else "",
        "last_rows": s.last_rows, "last_latency_ms": s.last_latency_ms,
        "success_rate": round(s.success_rate, 2),
    }


@router.post("/data-sources", status_code=201, dependencies=[Depends(require_admin)])
def create_data_source(body: DataSourceIn, db: Session = Depends(get_db)):
    source = SpdDataSource(**body.model_dump())
    db.add(source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该数据源编码已存在") from None
    return _source_out(source)


@router.get("/data-sources")
def list_data_sources(source_type: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdDataSource)
    if source_type:
        query = query.filter(SpdDataSource.source_type == source_type)
    return [_source_out(s) for s in query.order_by(SpdDataSource.id).limit(200).all()]


@router.patch("/data-sources/{source_id}", dependencies=[Depends(require_admin)])
def update_data_source(
    source_id: int, body: dict, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    source = db.get(SpdDataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    assert_org_writable(db, user, source.org_id)
    for key in ("name", "endpoint", "freq_minutes", "scope", "active", "status"):
        if key in body:
            setattr(source, key, body[key])
    db.commit()
    return _source_out(source)


class SyncLogIn(BaseModel):
    rows: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    success: bool = True
    message: str = Field(default="", max_length=256)


@router.post("/data-sources/{source_id}/sync-logs", status_code=201,
             dependencies=[Depends(require_roles("director", "operator"))])
def record_sync(
    source_id: int, body: SyncLogIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记一次同步结果，并刷新数据源的监控冗余列。

    成功率按最近 100 次算，而不是自建库以来的全量：一个月前的一次抖动
    不该永远压着今天的成功率，运维看的是"现在稳不稳"。
    """
    source = db.get(SpdDataSource, source_id)
    if source is None:
        raise HTTPException(status_code=404, detail="数据源不存在")
    assert_org_writable(db, user, source.org_id)
    log = SpdSyncLog(source_id=source_id, **body.model_dump())
    db.add(log)
    db.flush()
    recent = (
        db.query(SpdSyncLog.success)
        .filter(SpdSyncLog.source_id == source_id)
        .order_by(SpdSyncLog.id.desc())
        .limit(100)
        .all()
    )
    ok = sum(1 for (s,) in recent if s)
    source.success_rate = round(ok / len(recent) * 100, 2) if recent else 0.0
    source.last_sync_at = log.started_at
    source.last_rows = body.rows
    source.last_latency_ms = body.latency_ms
    if not body.success:
        source.status = "failed"
    elif body.latency_ms > source.freq_minutes * 60 * 1000:
        source.status = "delayed"
    else:
        source.status = "running"
    db.commit()
    return {"id": log.id, "source": _source_out(source)}


@router.get("/data-sources/{source_id}/sync-logs")
def list_sync_logs(source_id: int, response: Response, offset: int = 0, limit: int = 50,
                   db: Session = Depends(get_db)):
    query = db.query(SpdSyncLog).filter(SpdSyncLog.source_id == source_id)
    rows = paginate(query.order_by(SpdSyncLog.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "started_at": r.started_at.isoformat(), "rows": r.rows,
         "latency_ms": r.latency_ms, "success": r.success, "message": r.message}
        for r in rows
    ]


@router.get("/data-sources-monitor")
def data_source_monitor(db: Session = Depends(get_db)):
    """接入总览：按状态汇总 + 24 小时内未同步的数据源清单。"""
    sources = db.query(SpdDataSource).filter(SpdDataSource.active.is_(True)).all()
    cutoff = now_naive() - timedelta(hours=24)
    stale = [
        _source_out(s) for s in sources if s.last_sync_at is None or s.last_sync_at < cutoff
    ]
    summary: dict[str, int] = {}
    for s in sources:
        summary[s.status] = summary.get(s.status, 0) + 1
    return {
        "total": len(sources),
        "by_status": summary,
        "stale_over_24h": stale,
        "avg_success_rate": round(
            sum(s.success_rate for s in sources) / len(sources), 2
        ) if sources else 0.0,
    }


# ============================================================ 重点慢专病中心


class CenterIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(min_length=1, max_length=32)
    lead_org_id: int | None = None
    lead_dept: str = Field(default="", max_length=64)
    leader_user_id: int | None = None
    org_ids: list[int] = Field(default_factory=list)
    team_ids: list[int] = Field(default_factory=list)


def _center_out(c: SpdCenter) -> dict:
    return {
        "id": c.id, "code": c.code, "name": c.name, "program_code": c.program_code,
        "lead_org_id": c.lead_org_id, "lead_dept": c.lead_dept,
        "leader_user_id": c.leader_user_id, "org_ids": c.org_ids or [],
        "team_ids": c.team_ids or [], "version": c.version, "status": c.status,
    }


@router.post("/centers", status_code=201, dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def create_center(body: CenterIn, db: Session = Depends(get_db)):
    if db.query(SpdProgram).filter(SpdProgram.code == body.program_code).first() is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    center = SpdCenter(**body.model_dump())
    db.add(center)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该中心编码已存在") from None
    return _center_out(center)


@router.get("/centers")
def list_centers(program_code: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdCenter)
    if program_code:
        query = query.filter(SpdCenter.program_code == program_code)
    return [_center_out(c) for c in query.order_by(SpdCenter.id).limit(200).all()]


@router.patch("/centers/{center_id}", dependencies=[Depends(require_roles(*CONFIG_ROLES))])
def update_center(center_id: int, body: dict, db: Session = Depends(get_db)):
    center = db.get(SpdCenter, center_id)
    if center is None:
        raise HTTPException(status_code=404, detail="专病中心不存在")
    for key in ("name", "lead_org_id", "lead_dept", "leader_user_id", "org_ids", "team_ids",
                "status", "version"):
        if key in body:
            setattr(center, key, body[key])
    db.commit()
    return _center_out(center)


# ============================================================ 机构树（三级）


@router.get("/org-tree")
def org_tree(db: Session = Depends(get_db)):
    """县—乡—村三级机构树，附各机构的慢专病团队数与在管人数。

    平台管理端 #5 要求"统一组织层级用于患者归属、任务派发、逐级转诊、
    团队授权和考核"——所以这棵树不只是机构名称，还要带上这几项的实际数量，
    否则配置的人无法判断改动会影响到谁。
    """
    from ..models import SpdEnrollment

    orgs = db.query(Organization).order_by(Organization.id).all()
    team_counts: dict[int, int] = {}
    for (org_id,) in db.query(SpdTeam.org_id).filter(SpdTeam.active.is_(True)).all():
        team_counts[org_id] = team_counts.get(org_id, 0) + 1
    enroll_counts: dict[int, int] = {}
    for (org_id,) in (
        db.query(SpdEnrollment.org_id).filter(SpdEnrollment.status == "active").all()
    ):
        enroll_counts[org_id] = enroll_counts.get(org_id, 0) + 1

    nodes = {
        o.id: {
            "id": o.id, "name": o.name, "org_type": o.org_type, "level": o.level,
            "parent_id": o.parent_id, "team_count": team_counts.get(o.id, 0),
            "enrolled": enroll_counts.get(o.id, 0), "children": [],
        }
        for o in orgs
    }
    roots = []
    for node in nodes.values():
        parent = nodes.get(node["parent_id"])
        (parent["children"] if parent else roots).append(node)
    return roots
