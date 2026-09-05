"""专病管理（阶段八）：专病目录 — 入组 — 路径节点 — 疗效评价 — 出组。

与慢病管理（`/api/chronic`）分开，不是重复造轮子：慢病是长期随访分级
（录血压血糖、系统定级、到期提醒），专病是一条有始有终的诊疗路径。
把专病硬塞进慢病表，会得到一个既不像随访也不像路径的模型。

路径节点由目录配置（`path_nodes` JSON），**不预置任何具体病种**——
各地专病中心管什么病、分几步，差异极大，预置只会被删掉重配。
"""
from datetime import date

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..concurrency import insert_or_conflict
from ..datetypes import OptionalDateStr
from ..visibility import assert_org_writable, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, resolve_org_scope
from ..models import (
    DiseaseEnrollment,
    DiseasePathRecord,
    DiseaseProgram,
    Organization,
    Patient,
    User,
)

router = APIRouter(prefix="/api/disease-programs", tags=["专病管理"],
                   dependencies=[Depends(get_current_user)])

ENROLL_STATUS = {"enrolled": "在管", "completed": "完成出组", "exited": "中途退出"}
OUTCOMES = {"cured": "治愈", "improved": "好转", "stable": "稳定", "worsened": "加重",
            "died": "死亡"}


class PathNode(BaseModel):
    key: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    required: bool = True


class ProgramIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=512)
    org_id: int | None = None
    path_nodes: list[PathNode] = Field(default_factory=list)


class ProgramUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    path_nodes: list[PathNode] | None = None
    active: bool | None = None


class EnrollIn(BaseModel):
    patient_id: int
    org_id: int
    enrolled_at: OptionalDateStr = ""


class NodeRecordIn(BaseModel):
    node_key: str = Field(min_length=1, max_length=32)
    performed_at: OptionalDateStr = ""
    operator_name: str = Field(default="", max_length=64)
    result: str = Field(default="", max_length=256)
    note: str = Field(default="", max_length=512)


class ExitIn(BaseModel):
    status: str = Field(default="completed", pattern="^(completed|exited)$")
    # 留空表示未评价，不等于"无效"——与处置反应、并发症同一处理原则
    outcome: str = Field(default="", pattern="^(|cured|improved|stable|worsened|died)$")
    outcome_note: str = Field(default="", max_length=512)
    exit_reason: str = Field(default="", max_length=256)


def _program_out(p: DiseaseProgram) -> dict:
    return {
        "id": p.id, "code": p.code, "name": p.name, "description": p.description,
        "org_id": p.org_id, "path_nodes": p.path_nodes or [], "active": p.active,
    }


# ============================================================ 专病目录


# ---------------------------------------------------------------- 响应契约
#
# 模型集中放在所有端点之前（`response_model=` 是装饰器参数，导入时求值）。


class DiseaseProgramOut(BaseModel):
    id: int
    code: str
    name: str
    description: str
    org_id: int | None
    # 路径节点定义（JSON 列）：字段由节点类型决定，宽字典如实反映
    path_nodes: list[dict[str, Any]]
    active: bool


class PathRecordOut(BaseModel):
    node_key: str
    performed_at: str
    operator_name: str
    result: str
    note: str


class CompletionOut(BaseModel):
    """路径完成度。`nodes` 是 `{**节点定义, "done": bool}`——节点定义来自
    `path_nodes` JSON 列，字段不固定，故只能是宽字典。"""

    nodes: list[dict[str, Any]]
    required_total: int
    required_done: int
    required_done_pct: float
    pending_required: list[str]


class DiseaseEnrollmentOut(BaseModel):
    id: int
    program_id: int
    patient_id: int
    org_id: int
    status: str
    status_name: str
    enrolled_at: str
    exited_at: str
    outcome: str
    # 未评价时折成"未评价"而不是空串——报表上"空"和"未评价"是两回事
    outcome_name: str
    outcome_note: str
    exit_reason: str
    completion: CompletionOut
    records: list[PathRecordOut]


class CountedNameOut(BaseModel):
    count: int
    name: str


class ProgramStatsOut(BaseModel):
    """按状态与疗效的构成。两个 dict 的键都是**实际出现过的**取值，
    没出现的不该硬塞 0；`unrated`（出组但未评价）与各项疗效并列，
    不并进任何一项——并进去就等于替临床下了结论。"""

    program_id: int
    total: int
    by_status: dict[str, CountedNameOut]
    by_outcome: dict[str, CountedNameOut]
    avg_required_completion_pct: float
    # 口径随数字一起出：不写在响应里，看的人会按自己的理解解释这个百分比
    caliber: str


@router.post("", response_model=DiseaseProgramOut, status_code=201,
             dependencies=[Depends(require_admin)])
def create_program(body: ProgramIn, db: Session = Depends(get_db)):
    if body.org_id is not None and db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    keys = [n.key for n in body.path_nodes]
    if len(keys) != len(set(keys)):
        raise HTTPException(status_code=422, detail="路径节点 key 不得重复")
    program = DiseaseProgram(
        **body.model_dump(exclude={"path_nodes"}),
        path_nodes=[n.model_dump() for n in body.path_nodes],
    )
    db.add(program)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该专病编码已存在") from None
    return _program_out(program)


@router.get("", response_model=list[DiseaseProgramOut])
def list_programs(active: bool | None = None, db: Session = Depends(get_db)):
    query = db.query(DiseaseProgram)
    if active is not None:
        query = query.filter(DiseaseProgram.active.is_(active))
    return [_program_out(p) for p in query.order_by(DiseaseProgram.id).limit(200).all()]


@router.patch("/{program_id}", response_model=DiseaseProgramOut,
              dependencies=[Depends(require_admin)])
def update_program(program_id: int, body: ProgramUpdate, db: Session = Depends(get_db)):
    """改路径只影响**此后**的执行判定，已记录的节点不会被删。

    在管病例中途改路径是现实常态（指南更新），所以不拦；但改完之后
    在管病例的"未完成节点"会随之变化，这一点由完成度接口如实反映。
    """
    program = _program(db, program_id)
    changes = body.model_dump(exclude_unset=True)
    if changes.get("path_nodes") is not None:
        keys = [n["key"] for n in changes["path_nodes"]]
        if len(keys) != len(set(keys)):
            raise HTTPException(status_code=422, detail="路径节点 key 不得重复")
    for field, value in changes.items():
        if value is not None:
            setattr(program, field, value)
    db.commit()
    return _program_out(program)


# ============================================================ 入组与路径推进


@router.post("/{program_id}/enrollments", response_model=DiseaseEnrollmentOut,
             status_code=201,
             dependencies=[Depends(require_roles("doctor", "public_health"))])
def enroll(
    program_id: int,
    body: EnrollIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """入组。同一患者同一专病同时只允许一条在管记录，但**允许复发再入组**——
    治好出组之后又犯是常态，不该被"已入组"永久挡住。"""
    assert_org_writable(db, user, body.org_id)
    program = _program(db, program_id)
    if not program.active:
        raise HTTPException(status_code=409, detail="该专病目录已停用")
    if db.get(Patient, body.patient_id) is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    if db.get(Organization, body.org_id) is None:
        raise HTTPException(status_code=404, detail="机构不存在")
    existing = (
        db.query(DiseaseEnrollment)
        .filter(
            DiseaseEnrollment.program_id == program_id,
            DiseaseEnrollment.patient_id == body.patient_id,
            DiseaseEnrollment.status == "enrolled",
        )
        .first()
    )
    if existing is not None:
        raise HTTPException(status_code=409, detail="该患者已在本专病在管中")
    row = DiseaseEnrollment(
        program_id=program_id,
        patient_id=body.patient_id,
        org_id=body.org_id,
        enrolled_at=body.enrolled_at or date.today().isoformat(),
        created_by=user.id,
    )
    # 上面那句"已在管"判定是 check-then-act：并发下两路都查不到在管记录都会建组，
    # 静默写出两条——program_stats 双计、出组只翻掉一条，剩下那条永远挡着复发再入组。
    # uq_disease_enrollment_program_patient_enrolled（部分唯一索引，只锁 enrolled 一态）
    # 是兜底，抢输者拿到的 409 文案与上面顺序请求那句完全一致；两处文案必须同改，
    # 改一处会让并发输家拿到旧话。代价与 inpatient.create_admission 同：上面那三条
    # 404 之后目录/患者/机构被删的话，PG 的外键冲突也会被归到这句 409（SQLite 不查外键）。
    insert_or_conflict(db, row, "该患者已在本专病在管中")
    return _enrollment_out(row, db)


@router.get("/enrollments", response_model=list[DiseaseEnrollmentOut])
def list_enrollments(
    response: Response,
    program_id: int | None = None,
    patient_id: int | None = None,
    status: str | None = None,
    org_id: int | None = None,
    group_id: int | None = None,
    offset: int = 0,
    limit: int = 50,
    db: Session = Depends(get_db), user: User = Depends(get_current_user),
):
    query = db.query(DiseaseEnrollment)
    if program_id is not None:
        query = query.filter(DiseaseEnrollment.program_id == program_id)
    query = scope_patient_list(db, user, query, DiseaseEnrollment, patient_id, "disease_program")
    if status:
        query = query.filter(DiseaseEnrollment.status == status)
    scope = resolve_org_scope(db, group_id, org_id)
    if scope is not None:
        query = query.filter(DiseaseEnrollment.org_id.in_(scope))
    rows = paginate(query.order_by(DiseaseEnrollment.id.desc()), response, offset, limit)
    return [_enrollment_out(r, db) for r in rows]


@router.post("/enrollments/{enrollment_id}/records", response_model=DiseaseEnrollmentOut,
             status_code=201,
             dependencies=[Depends(require_roles("doctor", "public_health"))])
def record_node(
    enrollment_id: int,
    body: NodeRecordIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """记录路径节点执行。节点 key 必须在目录里——写个不存在的节点，
    完成度就永远算不对。"""
    enrollment = _enrollment(db, enrollment_id)
    if enrollment.status != "enrolled":
        raise HTTPException(status_code=409, detail="该病例已出组，不可再记录路径节点")
    program = _program(db, enrollment.program_id)
    valid = {n["key"] for n in (program.path_nodes or [])}
    if body.node_key not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"节点 {body.node_key} 不在本专病路径中，可用节点：{sorted(valid) or '（未配置）'}",
        )
    db.add(
        DiseasePathRecord(
            enrollment_id=enrollment_id,
            created_by=user.id,
            **{**body.model_dump(),
               "performed_at": body.performed_at or date.today().isoformat()},
        )
    )
    db.commit()
    return _enrollment_out(enrollment, db)


@router.post("/enrollments/{enrollment_id}/exit", response_model=DiseaseEnrollmentOut,
             dependencies=[Depends(require_roles("doctor", "public_health"))])
def exit_enrollment(enrollment_id: int, body: ExitIn, db: Session = Depends(get_db)):
    """出组并做疗效评价。

    **必需节点未做完也允许出组**——患者转院、拒绝继续治疗都是现实，硬拦只会
    逼人补假记录。未完成的节点会留在完成度里如实呈现。
    """
    enrollment = _enrollment(db, enrollment_id)
    if enrollment.status != "enrolled":
        raise HTTPException(status_code=409, detail="该病例已出组")
    if body.status == "exited" and not body.exit_reason:
        raise HTTPException(status_code=422, detail="中途退出须填写原因")
    enrollment.status = body.status
    enrollment.outcome = body.outcome
    enrollment.outcome_note = body.outcome_note
    enrollment.exit_reason = body.exit_reason
    enrollment.exited_at = date.today().isoformat()
    db.commit()
    return _enrollment_out(enrollment, db)


@router.get("/enrollments/{enrollment_id}", response_model=DiseaseEnrollmentOut)
def get_enrollment(enrollment_id: int, db: Session = Depends(get_db)):
    return _enrollment_out(_enrollment(db, enrollment_id), db)


@router.get("/{program_id}/stats", response_model=ProgramStatsOut)
def program_stats(
    program_id: int, group_id: int | None = None, db: Session = Depends(get_db)
):
    """专病统计：在管/出组人数、疗效构成、路径完成度。"""
    program = _program(db, program_id)
    query = db.query(DiseaseEnrollment).filter(DiseaseEnrollment.program_id == program_id)
    scope = resolve_org_scope(db, group_id, None)
    if scope is not None:
        query = query.filter(DiseaseEnrollment.org_id.in_(scope))
    rows = query.all()
    by_status: dict[str, int] = {}
    by_outcome: dict[str, int] = {}
    for r in rows:
        by_status[r.status] = by_status.get(r.status, 0) + 1
        if r.status != "enrolled":
            key = r.outcome or "unrated"
            by_outcome[key] = by_outcome.get(key, 0) + 1
    # P0-1：这里原先在循环里逐条调 `_completion(r, db)`，每条入组各查一次
    # 路径记录，实测 50 条入组 → 103 次 SQL（2N+3）。改成一次取回全部路径记录
    # 在内存里按 enrollment_id 分组——与 analytics.py 已用过的做法一致。
    required_keys = [
        n["key"] for n in (program.path_nodes or []) if n.get("required", True)
    ]
    done_by_enrollment: dict[int, set[str]] = {}
    if rows:
        for eid, node_key in (
            db.query(DiseasePathRecord.enrollment_id, DiseasePathRecord.node_key)
            .filter(DiseasePathRecord.enrollment_id.in_([r.id for r in rows]))
            .all()
        ):
            done_by_enrollment.setdefault(eid, set()).add(node_key)
    completions = [
        _required_pct(required_keys, done_by_enrollment.get(r.id, set())) for r in rows
    ]
    return {
        "program_id": program_id,
        "total": len(rows),
        "by_status": {k: {"count": v, "name": ENROLL_STATUS.get(k, k)}
                      for k, v in by_status.items()},
        # unrated 是"出组了但没做疗效评价"，与各项疗效并列报出，不并进任何一项
        "by_outcome": {k: {"count": v, "name": OUTCOMES.get(k, "未评价")}
                       for k, v in by_outcome.items()},
        "avg_required_completion_pct": (
            round(sum(completions) / len(completions), 2) if completions else 0.0
        ),
        "caliber": "路径完成度只统计必需节点；未评价疗效单列 unrated，不计入任何疗效档",
    }


# ============================================================ 内部


def _required_pct(required_keys: list[str], done: set[str]) -> float:
    """必需节点完成率。单独抽出来，好让批量统计与单条详情用同一套算法。"""
    if not required_keys:
        return 100.0
    return round(len([k for k in required_keys if k in done]) * 100 / len(required_keys), 2)


def _completion(enrollment: DiseaseEnrollment, db: Session) -> dict:
    program = db.get(DiseaseProgram, enrollment.program_id)
    nodes = (program.path_nodes or []) if program else []
    done = {
        r.node_key
        for r in db.query(DiseasePathRecord)
        .filter(DiseasePathRecord.enrollment_id == enrollment.id)
        .all()
    }
    required = [n for n in nodes if n.get("required", True)]
    required_done = [n for n in required if n["key"] in done]
    return {
        "nodes": [
            {**n, "done": n["key"] in done}
            for n in nodes
        ],
        "required_total": len(required),
        "required_done": len(required_done),
        "required_done_pct": _required_pct([n["key"] for n in required], done),
        "pending_required": [n["name"] for n in required if n["key"] not in done],
    }


def _enrollment_out(enrollment: DiseaseEnrollment, db: Session) -> dict:
    records = (
        db.query(DiseasePathRecord)
        .filter(DiseasePathRecord.enrollment_id == enrollment.id)
        .order_by(DiseasePathRecord.id)
        .all()
    )
    return {
        "id": enrollment.id,
        "program_id": enrollment.program_id,
        "patient_id": enrollment.patient_id,
        "org_id": enrollment.org_id,
        "status": enrollment.status,
        "status_name": ENROLL_STATUS.get(enrollment.status, enrollment.status),
        "enrolled_at": enrollment.enrolled_at,
        "exited_at": enrollment.exited_at,
        "outcome": enrollment.outcome,
        "outcome_name": OUTCOMES.get(enrollment.outcome, "未评价"),
        "outcome_note": enrollment.outcome_note,
        "exit_reason": enrollment.exit_reason,
        "completion": _completion(enrollment, db),
        "records": [
            {"node_key": r.node_key, "performed_at": r.performed_at,
             "operator_name": r.operator_name, "result": r.result, "note": r.note}
            for r in records
        ],
    }


def _program(db: Session, program_id: int) -> DiseaseProgram:
    program = db.get(DiseaseProgram, program_id)
    if program is None:
        raise HTTPException(status_code=404, detail="专病目录不存在")
    return program


def _enrollment(db: Session, enrollment_id: int) -> DiseaseEnrollment:
    row = db.get(DiseaseEnrollment, enrollment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="入组记录不存在")
    return row
