"""全域慢专病 · 配置域：病种目录、专病档案、管理目标（含元数据选项）。

由原 `config.py`（1549 行）按业务分节拆出，见 ADR-0008。
路由对象与跨节工具在 `._base`，本模块只放本域的端点。
"""
from typing import Any

from fastapi import Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ....database import get_db
from ....datetypes import OptionalDateStr
from ....deps import get_current_user, paginate, require_admin, require_roles
from ...platform import Organization, User
from ...models import (
    SpdProgram,
    SpdProgramVersion,
    SpdTarget,
)
from ...rules import FIELD_SOURCES, OPERATORS
from ._base import CONFIG_ROLES, _bump_version, _conditions, router


# ============================================================ 响应契约
#
# 模型集中放在**所有端点之前**：`response_model=` 是装饰器参数，在导入时就要求值，
# 模型定义晚于使用点会直接 F821。（这个坑在 analytics / portal 两批各踩过一次。）


class RuleOptionOut(BaseModel):
    key: str
    name: str


class RiskLevelOptionOut(RuleOptionOut):
    color: str


class RuleMetaOut(BaseModel):
    """规则编辑器的可选项。

    `fields`/`operators` 来自 `rules` 模块的字典，键随采集项扩充而变，故是列表；
    `task_types`/`member_roles` 直接是"码 → 中文名"的映射，键同样由代码维护，
    用 dict 而不是逐字段模型——把十个任务类型写成十个字段，加一种就要改契约。
    """

    fields: list[RuleOptionOut]
    operators: list[RuleOptionOut]
    risk_levels: list[RiskLevelOptionOut]
    task_types: dict[str, str]
    member_roles: dict[str, str]


class TargetOut(BaseModel):
    """管理目标。`target_low`/`target_high` 是**可空 Float**：定性目标（戒烟、
    规律服药）没有上下限，为 null；量化目标的整数下限读回来是 `90.0`。"""

    id: int
    program_id: int
    stage: str
    metric: str
    metric_name: str
    kind: str
    target_low: float | None
    target_high: float | None
    unit: str
    qualitative: str
    risk_level: str
    followup_interval_days: int
    form_code: str
    edu_code: str
    active: bool


class ProgramOut(BaseModel):
    id: int
    code: str
    name: str
    category: str
    # 未指定牵头机构时为 null
    lead_org_id: int | None
    lead_dept: str
    description: str
    # 四个都是 JSON 列：规则条件与阶段/里程碑定义，字段随规则类型而变
    include_rules: list[dict[str, Any]]
    exclude_rules: list[dict[str, Any]]
    stages: list[dict[str, Any]]
    milestones: list[dict[str, Any]]
    version: str
    effective_from: str
    active: bool


class ProgramDetailOut(ProgramOut):
    """单个专病档案。**是列表形状的严格超集**——`get_program` 在 `_program_out`
    的结果上追加 `targets`，别的键一个不差，所以这里继承是对的
    （spd/portal 那批的转诊详情不是超集，继承就错了）。"""

    targets: list[TargetOut]


class ProgramVersionOut(BaseModel):
    id: int
    version: str
    changed_by: str
    note: str
    # 历史快照：存的是当时的 `_program_out`，老快照可能缺字段（形状会随版本漂移），
    # 逐字段建模会给老快照注入 null——快照的意义就是"当时长什么样"，不该被改写
    snapshot: dict[str, Any]
    created_at: str


# ============================================================ 元数据（规则可选项）


@router.get("/meta", response_model=RuleMetaOut)
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


@router.post("/programs", response_model=ProgramOut, status_code=201,
             dependencies=[Depends(require_admin)])
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


@router.get("/programs", response_model=list[ProgramOut])
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


@router.get("/programs/{program_id}", response_model=ProgramDetailOut)
def get_program(program_id: int, db: Session = Depends(get_db)):
    program = db.get(SpdProgram, program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    out = _program_out(program)
    out["targets"] = [_target_out(t) for t in
                      db.query(SpdTarget).filter(SpdTarget.program_id == program_id).all()]
    return out


@router.patch("/programs/{program_id}", response_model=ProgramOut,
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
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


@router.get("/programs/{program_id}/versions", response_model=list[ProgramVersionOut])
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


@router.post("/programs/{program_id}/targets", response_model=TargetOut, status_code=201,
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


@router.get("/programs/{program_id}/targets", response_model=list[TargetOut])
def list_targets(program_id: int, stage: str | None = None, db: Session = Depends(get_db)):
    query = db.query(SpdTarget).filter(SpdTarget.program_id == program_id)
    if stage is not None:
        query = query.filter(SpdTarget.stage == stage)
    return [_target_out(t) for t in query.order_by(SpdTarget.id).all()]


@router.patch("/targets/{target_id}", response_model=TargetOut,
              dependencies=[Depends(require_roles(*CONFIG_ROLES))])
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
