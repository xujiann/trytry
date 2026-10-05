"""专病管理（阶段八）：专病目录 — 入组 — 路径节点 — 疗效评价 — 出组。

与慢病管理（`/api/chronic`）分开，不是重复造轮子：慢病是长期随访分级
（录血压血糖、系统定级、到期提醒），专病是一条有始有终的诊疗路径。
把专病硬塞进慢病表，会得到一个既不像随访也不像路径的模型。

路径节点由目录配置（`path_nodes` JSON），**不预置任何具体病种**——
各地专病中心管什么病、分几步，差异极大，预置只会被删掉重配。
"""

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .. import clock
from ..concurrency import insert_or_conflict
from ..datetypes import OptionalDateStr, legacy_date
from ..texttypes import NON_BLANK
from ..visibility import assert_org_writable, assert_patient_visible, scope_patient_list
from ..database import get_db
from ..deps import get_current_user, paginate, require_admin, require_roles, resolve_org_scope, rows_by_id
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
    key: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    required: bool = True


class ProgramIn(BaseModel):
    code: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
    name: str = Field(min_length=1, max_length=64, pattern=NON_BLANK)
    description: str = Field(default="", max_length=512)
    org_id: int | None = None
    path_nodes: list[PathNode] = Field(default_factory=list)


class ProgramUpdate(BaseModel):
    # 改档与建档同口径（P1-98）：原先改名为空串照收
    name: str | None = Field(default=None, min_length=1, max_length=64, pattern=NON_BLANK)
    description: str | None = Field(default=None, max_length=512)
    path_nodes: list[PathNode] | None = None
    active: bool | None = None


class EnrollIn(BaseModel):
    patient_id: int
    org_id: int
    enrolled_at: OptionalDateStr = ""


class NodeRecordIn(BaseModel):
    node_key: str = Field(min_length=1, max_length=32, pattern=NON_BLANK)
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
    # 出组日期可补录（P2-1543）：缺省今天；原先没有这一项，9-01 已转院、今天补出组的照样记成今天
    exited_at: OptionalDateStr = ""


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
    # 入组日期不得晚于今天（P2-1543，与 maternal 末次月经 / 分娩日期 P2-1305 同一句）：记的是已经入组的那一天，原先只查格式，
    # 填成将来照收——节点完成日、出组日都拿它当下界，入组记录又没有改的入口
    if body.enrolled_at and body.enrolled_at > clock.today().isoformat():
        raise HTTPException(status_code=422, detail=f"入组日期（{body.enrolled_at}）不得晚于今天")
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
        enrolled_at=body.enrolled_at or clock.today().isoformat(),
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
    refs = _enrollment_refs(db, rows)
    return [_enrollment_out(r, db, refs) for r in rows]


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
    完成度就永远算不对。

    口径照抄同文件的 `enroll`（`assert_org_writable(db, user, body.org_id)`）：
    入组一直校验机构，记节点与出组却不校验——同一份病例上两套口径。
    归属判定排在状态机之前，免得用"该病例已出组"把别家病例的状态探出去。
    """
    enrollment = _enrollment(db, enrollment_id)
    assert_org_writable(db, user, enrollment.org_id)
    if enrollment.status != "enrolled":
        raise HTTPException(status_code=409, detail="该病例已出组，不可再记录路径节点")
    program = _program(db, enrollment.program_id)
    valid = {n["key"] for n in (program.path_nodes or [])}
    if body.node_key not in valid:
        raise HTTPException(
            status_code=422,
            detail=f"节点 {body.node_key} 不在本专病路径中，可用节点：{sorted(valid) or '（未配置）'}",
        )
    # 节点完成日期不得晚于今天、不得早于入组日期（P2-1543，与 maternal 的 P2-1305 / P2-1020 同一句）：原先只查格式，比入组早
    # 九个月、晚于今天的都照收，节点记录又没有删除入口
    performed_at = body.performed_at or clock.today().isoformat()
    if performed_at > clock.today().isoformat():
        raise HTTPException(status_code=422, detail=f"节点完成日期（{performed_at}）不得晚于今天")
    enrolled = _enrolled_on(enrollment)
    if enrolled is not None and performed_at < enrolled:
        raise HTTPException(status_code=422, detail=f"节点完成日期 {performed_at} 早于入组日期 {enrolled}")
    db.add(
        DiseasePathRecord(
            enrollment_id=enrollment_id,
            created_by=user.id,
            **{**body.model_dump(), "performed_at": performed_at},
        )
    )
    db.commit()
    return _enrollment_out(enrollment, db)


@router.post("/enrollments/{enrollment_id}/exit", response_model=DiseaseEnrollmentOut,
             dependencies=[Depends(require_roles("doctor", "public_health"))])
def exit_enrollment(
    enrollment_id: int, body: ExitIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """出组并做疗效评价。

    **必需节点未做完也允许出组**——患者转院、拒绝继续治疗都是现实，硬拦只会
    逼人补假记录。未完成的节点会留在完成度里如实呈现。

    机构口径同 `record_node`：照抄本文件 `enroll` 的 `assert_org_writable`。
    出组要写疗效评价（治愈/好转/死亡），那是本院随访的结论，不该由别家机构下。
    """
    enrollment = _enrollment(db, enrollment_id)
    assert_org_writable(db, user, enrollment.org_id)
    if enrollment.status != "enrolled":
        raise HTTPException(status_code=409, detail="该病例已出组")
    # 判 strip 之后的（P2-418，同 P2-309）：一串空格原先当成填了，出组留痕的退出原因是空白
    if body.status == "exited" and not body.exit_reason.strip():
        raise HTTPException(status_code=422, detail="中途退出须填写原因")
    # 出组日期可补录（P2-1543）：原先恒记今天——9-01 已转院、今天补出组的记成今天，比入组日还早也照收。缺省今天；不晚于今天、
    # 不早于入组日、不早于最晚的节点完成日（句式同 `record_node`）
    exited_at = body.exited_at or clock.today().isoformat()
    if exited_at > clock.today().isoformat():
        raise HTTPException(status_code=422, detail=f"出组日期（{exited_at}）不得晚于今天")
    enrolled = _enrolled_on(enrollment)
    if enrolled is not None and exited_at < enrolled:
        raise HTTPException(status_code=422, detail=f"出组日期 {exited_at} 早于入组日期 {enrolled}")
    latest = _latest_performed_on(db, enrollment_id)
    if latest is not None and exited_at < latest:
        raise HTTPException(status_code=422, detail=f"出组日期 {exited_at} 早于最晚的节点完成日期 {latest}")
    enrollment.status = body.status
    enrollment.outcome = body.outcome
    enrollment.outcome_note = body.outcome_note
    enrollment.exit_reason = body.exit_reason
    enrollment.exited_at = exited_at
    db.commit()
    return _enrollment_out(enrollment, db)


@router.get("/enrollments/{enrollment_id}", response_model=DiseaseEnrollmentOut)
def get_enrollment(
    enrollment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """入组明细（病种、在管 / 出组、疗效、每个节点的执行记录）。

    按患者可见性判并留痕，与同文件清单带 `patient_id` 时同一句（`scope_patient_list`）。原先按入组号直取、连调用方都
    不收：一家与患者毫无关系的机构按号就翻得到别家患者进了哪个专病、疗效如何、每一步谁做的（P0-45）。本机构入组本身
    就是一条服务关系，从清单点进来的一律看得到。
    """
    enrollment = _enrollment(db, enrollment_id)
    assert_patient_visible(db, user, enrollment.patient_id, resource="disease_program")
    return _enrollment_out(enrollment, db)


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


def _enrolled_on(enrollment: DiseaseEnrollment) -> str | None:
    """入组日期当下界用（P2-1543）：按存量写法读（`legacy_date`）；读不成的、修前存进去的将来日子当「不知道」、不当下界——
    同 P2-940「将来的出生日期按写坏处理」：入组记录没有改的入口，拿将来的入组日当下界，这份病例从此记不了节点、出不了组。"""
    day = legacy_date(enrollment.enrolled_at)
    return day if day is not None and day <= clock.today().isoformat() else None


def _latest_performed_on(db: Session, enrollment_id: int) -> str | None:
    """最晚的节点完成日期（P2-1543，出组日的下界）：读法与「将来的不当下界」同 `_enrolled_on`——节点记录同样没有删除入口。"""
    today = clock.today().isoformat()
    days = [legacy_date(performed_at) for (performed_at,) in
            db.query(DiseasePathRecord.performed_at).filter(DiseasePathRecord.enrollment_id == enrollment_id)]
    return max((day for day in days if day is not None and day <= today), default=None)


def _required_pct(required_keys: list[str], done: set[str]) -> float:
    """必需节点完成率。单独抽出来，好让批量统计与单条详情用同一套算法。"""
    if not required_keys:
        return 100.0
    return round(len([k for k in required_keys if k in done]) * 100 / len(required_keys), 2)


def _enrollment_refs(db: Session, rows: list[DiseaseEnrollment]) -> tuple[dict, dict]:
    """一页入组出参要的（专病目录、按入组分好的路径记录），各按页一次 IN 取齐（P2-1157）：清单原先逐行查两遍路径
    记录、再 `db.get` 一次专病目录，一页 50 行一百五十来条查询。路径记录先按编号排好再分，每条入组内的先后与逐行查时一样。"""
    records: dict[int, list[DiseasePathRecord]] = {}
    if rows:
        for record in (
            db.query(DiseasePathRecord)
            .filter(DiseasePathRecord.enrollment_id.in_([e.id for e in rows]))
            .order_by(DiseasePathRecord.id)
        ):
            records.setdefault(record.enrollment_id, []).append(record)
    return rows_by_id(db, DiseaseProgram, (e.program_id for e in rows)), records


def _completion(enrollment: DiseaseEnrollment, db: Session, refs: tuple[dict, dict] | None = None) -> dict:
    """`refs` 是清单按页取齐的那两样（`_enrollment_refs`）；单条出参不给，照旧逐条查。"""
    if refs is None:
        program = db.get(DiseaseProgram, enrollment.program_id)
        records = db.query(DiseasePathRecord).filter(DiseasePathRecord.enrollment_id == enrollment.id).all()
    else:
        program, records = refs[0].get(enrollment.program_id), refs[1].get(enrollment.id, [])
    nodes = (program.path_nodes or []) if program else []
    done = {r.node_key for r in records}
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


def _enrollment_out(enrollment: DiseaseEnrollment, db: Session, refs: tuple[dict, dict] | None = None) -> dict:
    """`refs` 同 `_completion`（P2-1157）。"""
    if refs is None:
        records = (
            db.query(DiseasePathRecord)
            .filter(DiseasePathRecord.enrollment_id == enrollment.id)
            .order_by(DiseasePathRecord.id)
            .all()
        )
    else:
        records = refs[1].get(enrollment.id, [])
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
        "completion": _completion(enrollment, db, refs),
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
