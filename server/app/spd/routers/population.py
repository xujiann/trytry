"""全域慢专病 · 人群域：筛查 → 目标池 → 签约建档纳管 → 生命周期 → 分组与服务包。

对应招标文件：平台管理端 #12/#13、全程管理中心端 #2/#3/#9/#17/#18、
服务团队专家端 #3、成员端 #3/#13/#20、个案管理师端 #2/#10~#13、患者端 #11/#13。

这条链上唯一不能含糊的口径：**筛出来 ≠ 管起来**。
筛查产出的是"疑似"，进目标池；目标池经复核、分发、认领、签约、建档，
才变成纳管档案。中间每一步都可能停下（居民不同意、复核排除、迁出），
所以三个阶段是三张表而不是一张表加状态列——一张表会让"筛查了几个人"
和"管了几个人"变成同一个数字，而这两个数字在考核里是两条指标。
"""
from copy import deepcopy
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...concurrency import insert_if_absent
from ...database import get_db
from ...datetypes import OptionalDateStr
from ...deps import get_current_user, paginate, require_roles
from ..platform import Organization, Patient, User
from ..models import (
    SpdAssessment,
    SpdCandidate,
    SpdEnrollment,
    SpdGroup,
    SpdGroupMember,
    SpdLifecycleEvent,
    SpdMeasurement,
    SpdPackageBinding,
    SpdPackageUsage,
    SpdPathInstance,
    SpdProgram,
    SpdRecall,
    SpdReferralCase,
    SpdScale,
    SpdScreening,
    SpdServiceApply,
    SpdServicePackage,
    SpdTask,
    SpdTeam,
)
from ..rules import evaluate, score_scale
from ..service import award_points, build_facts, close_open_work, match_program
from ...visibility import assert_org_writable, assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·人群"],
    dependencies=[Depends(get_current_user)],
)

SERVICE_ROLES = ("doctor", "public_health", "director")


def _patient_brief(db: Session, patient_ids: list[int]) -> dict[int, dict]:
    if not patient_ids:
        return {}
    rows = db.query(Patient).filter(Patient.id.in_(patient_ids)).all()
    return {
        p.id: {"name": p.name, "gender": p.gender, "birth_date": p.birth_date,
               "ehc_no": p.ehc_no, "phone": p.phone}
        for p in rows
    }


# ============================================================ 筛查


class ScreeningIn(BaseModel):
    patient_id: int
    program_code: str = Field(min_length=1, max_length=32)
    source: str = Field(default="opportunistic", pattern="^(opportunistic|active|self|import)$")
    org_id: int | None = None
    scale_code: str = Field(default="", max_length=32)
    answers: dict = Field(default_factory=dict)


def _screening_out(s: SpdScreening, brief: dict | None = None) -> dict:
    out = {
        "id": s.id, "patient_id": s.patient_id, "program_code": s.program_code,
        "source": s.source, "org_id": s.org_id, "scale_code": s.scale_code,
        "score": s.score, "risk_level": s.risk_level, "result": s.result,
        "advice": s.advice, "reviewed": s.reviewed, "review_result": s.review_result,
        "review_note": s.review_note, "answers": s.answers or {},
        "created_at": s.created_at.isoformat(),
    }
    if brief:
        out.update({"patient_name": brief.get("name", ""), "gender": brief.get("gender", "")})
    return out


@router.post("/screenings", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES, "operator"))])
def create_screening(
    body: ScreeningIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """登记一次筛查：量表评分 + 病种规则判定，双通道产出结论。

    量表给风险等级（问卷答出来的），规则给纳入判定（诊断与指标推出来的）。
    两者都可能单独存在——只填了问卷没做检查、或者只有诊断没填问卷——
    所以取**更重的那个**作为结论：问卷高危或规则命中，都算疑似。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_screening")
    org_id = body.org_id if body.org_id is not None else user.org_id
    assert_org_writable(db, user, org_id)
    program = db.query(SpdProgram).filter(SpdProgram.code == body.program_code).first()
    if program is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")

    score, risk, advice = 0.0, "low", ""
    if body.scale_code:
        scale = (
            db.query(SpdScale)
            .filter(SpdScale.code == body.scale_code, SpdScale.status == "published")
            .order_by(SpdScale.id.desc())
            .first()
        )
        if scale is None:
            raise HTTPException(status_code=404, detail="量表不存在或未发布")
        graded = score_scale(scale.items or [], body.answers, scale.scoring or {})
        score, risk, advice = graded["score"], graded["risk_level"] or "low", graded["advice"]

    matched = match_program(db, body.patient_id, program, extra=body.answers)
    if matched["result"] == "excluded":
        result = "excluded"
    elif matched["result"] == "suspect" or risk == "high":
        result = "suspect"
    else:
        result = "normal"

    screening = SpdScreening(
        patient_id=body.patient_id, program_code=body.program_code, source=body.source,
        org_id=org_id, operator_id=user.id, scale_code=body.scale_code,
        answers=body.answers, score=score, risk_level=risk, result=result, advice=advice,
    )
    db.add(screening)
    db.flush()

    if result == "suspect":
        _upsert_candidate(
            db, screening, matched["matched"], org_id, risk, source="screening"
        )
    elif result == "excluded":
        _upsert_candidate(
            db, screening, matched["excluded_by"], org_id, risk, source="screening",
            status="excluded",
        )
    db.commit()
    return _screening_out(screening)


def _upsert_candidate(
    db: Session,
    screening: SpdScreening,
    matched: list,
    org_id: int | None,
    risk: str,
    source: str,
    status: str = "suspect",
) -> SpdCandidate:
    """筛查结论写入目标池。已纳管的不回退状态——复筛一次不该把在管患者打回疑似。"""
    existing = (
        db.query(SpdCandidate)
        .filter(
            SpdCandidate.patient_id == screening.patient_id,
            SpdCandidate.program_code == screening.program_code,
        )
        .first()
    )
    if existing is not None:
        if existing.status != "enrolled":
            existing.status = status
            existing.risk_level = risk
            existing.matched_rules = matched
            existing.screening_id = screening.id
        return existing
    candidate = SpdCandidate(
        patient_id=screening.patient_id, program_code=screening.program_code,
        status=status, source=source, screening_id=screening.id, org_id=org_id,
        risk_level=risk, matched_rules=matched,
        reason="；".join(str(m.get("label") or m.get("field")) for m in matched)[:256],
    )
    # 两次筛查并发落到同一人同一病种时会撞唯一键；SAVEPOINT 把冲突圈在这一行，
    # 冲突了就取回既有那条，整次筛查不因此回滚。
    if not insert_if_absent(db, candidate):
        return (
            db.query(SpdCandidate)
            .filter(
                SpdCandidate.patient_id == screening.patient_id,
                SpdCandidate.program_code == screening.program_code,
            )
            .first()
        )
    return candidate


@router.get("/screenings")
def list_screenings(
    response: Response,
    patient_id: int | None = None,
    program_code: str | None = None,
    result: str | None = None,
    risk_level: str | None = None,
    reviewed: bool | None = None,
    source: str | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdScreening)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_screening")
        query = query.filter(SpdScreening.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(SpdScreening.org_id.in_(orgs))
    for column, value in (
        (SpdScreening.program_code, program_code), (SpdScreening.result, result),
        (SpdScreening.risk_level, risk_level), (SpdScreening.source, source),
    ):
        if value:
            query = query.filter(column == value)
    if reviewed is not None:
        query = query.filter(SpdScreening.reviewed.is_(reviewed))
    rows = paginate(query.order_by(SpdScreening.id.desc()), response, offset, limit)
    briefs = _patient_brief(db, [r.patient_id for r in rows])
    return [_screening_out(r, briefs.get(r.patient_id)) for r in rows]


class ReviewIn(BaseModel):
    review_result: str = Field(pattern="^(confirmed|excluded|pending)$")
    review_note: str = Field(default="", max_length=256)


@router.post("/screenings/{screening_id}/review",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def review_screening(
    screening_id: int,
    body: ReviewIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """高风险复核：确认则留在目标池，排除则移出，待定保持原状。"""
    screening = db.get(SpdScreening, screening_id)
    if screening is None:
        raise HTTPException(status_code=404, detail="筛查记录不存在")
    assert_org_writable(db, user, screening.org_id)
    screening.reviewed = True
    screening.review_result = body.review_result
    screening.review_note = body.review_note
    screening.reviewer_id = user.id
    candidate = (
        db.query(SpdCandidate)
        .filter(
            SpdCandidate.patient_id == screening.patient_id,
            SpdCandidate.program_code == screening.program_code,
        )
        .first()
    )
    if candidate is not None and candidate.status != "enrolled":
        if body.review_result == "confirmed":
            candidate.status = "target"
        elif body.review_result == "excluded":
            candidate.status = "excluded"
    db.commit()
    return _screening_out(screening)


class AutoScreenIn(BaseModel):
    program_code: str = Field(min_length=1, max_length=32)
    org_id: int | None = None
    limit: int = Field(default=500, ge=1, le=5000)


@router.post("/screenings/auto-run", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def auto_screen(
    body: AutoScreenIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按病种纳入/排除规则批量识别患者（平台管理端 #8"自动识别患者"）。

    只扫**本机构有就诊记录的患者**，不扫全县主索引：全域扫描是一次全表笛卡尔，
    而且业务上没有意义——本机构没接触过的人，识别出来也没人去随访。
    需要全域跑批时由 admin 走定时任务，那里可以慢慢跑。
    """
    program = db.query(SpdProgram).filter(SpdProgram.code == body.program_code).first()
    if program is None:
        raise HTTPException(status_code=404, detail="专病档案不存在")
    if not program.include_rules:
        raise HTTPException(status_code=422, detail="该病种未配置纳入规则，无法自动识别")
    org_id = body.org_id if body.org_id is not None else user.org_id
    assert_org_writable(db, user, org_id)

    from ..platform import Encounter

    patient_ids = [
        pid
        for (pid,) in db.query(Encounter.patient_id)
        .filter(Encounter.org_id == org_id)
        .distinct()
        .limit(body.limit)
        .all()
    ]
    suspect, excluded, normal = 0, 0, 0
    for pid in patient_ids:
        matched = match_program(db, pid, program)
        if matched["result"] == "normal":
            normal += 1
            continue
        screening = SpdScreening(
            patient_id=pid, program_code=program.code, source="import", org_id=org_id,
            operator_id=user.id, risk_level="mid" if matched["result"] == "suspect" else "low",
            result=matched["result"], advice="规则自动识别",
        )
        db.add(screening)
        db.flush()
        _upsert_candidate(
            db, screening,
            matched["matched"] or matched["excluded_by"], org_id,
            screening.risk_level, source="auto",
            status="suspect" if matched["result"] == "suspect" else "excluded",
        )
        if matched["result"] == "suspect":
            suspect += 1
        else:
            excluded += 1
    db.commit()
    return {
        "scanned": len(patient_ids), "suspect": suspect, "excluded": excluded,
        "normal": normal, "rule_version": program.version,
    }


# ============================================================ 目标池


def _candidate_out(c: SpdCandidate, brief: dict | None = None) -> dict:
    out = {
        "id": c.id, "patient_id": c.patient_id, "program_code": c.program_code,
        "status": c.status, "source": c.source, "org_id": c.org_id, "team_id": c.team_id,
        "assigned_user_id": c.assigned_user_id, "risk_level": c.risk_level,
        "reason": c.reason, "matched_rules": c.matched_rules or [],
        "claimed_at": c.claimed_at.isoformat() if c.claimed_at else "",
        "created_at": c.created_at.isoformat(),
    }
    if brief:
        out.update({
            "patient_name": brief.get("name", ""), "gender": brief.get("gender", ""),
            "birth_date": brief.get("birth_date", ""), "phone": brief.get("phone", ""),
        })
    return out


@router.get("/candidates")
def list_candidates(
    response: Response,
    program_code: str | None = None,
    status: str | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    assigned_user_id: int | None = None,
    risk_level: str | None = None,
    keyword: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdCandidate)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdCandidate.org_id.in_(orgs))
    for column, value in (
        (SpdCandidate.program_code, program_code), (SpdCandidate.status, status),
        (SpdCandidate.org_id, org_id), (SpdCandidate.team_id, team_id),
        (SpdCandidate.assigned_user_id, assigned_user_id),
        (SpdCandidate.risk_level, risk_level),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if keyword:
        ids = [p.id for p in db.query(Patient).filter(Patient.name.contains(keyword)).limit(500)]
        query = query.filter(SpdCandidate.patient_id.in_(ids or [0]))
    rows = paginate(query.order_by(SpdCandidate.id.desc()), response, offset, limit)
    briefs = _patient_brief(db, [r.patient_id for r in rows])
    return [_candidate_out(r, briefs.get(r.patient_id)) for r in rows]


class DistributeIn(BaseModel):
    candidate_ids: list[int] = Field(min_length=1, max_length=500)
    team_id: int | None = None
    assigned_user_id: int | None = None
    org_id: int | None = None


@router.post("/candidates/distribute", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def distribute_candidates(
    body: DistributeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按辖区/病种/风险把目标池患者分发给服务团队（全程管理中心端 #3）。"""
    if body.team_id is not None and db.get(SpdTeam, body.team_id) is None:
        raise HTTPException(status_code=404, detail="团队不存在")
    rows = db.query(SpdCandidate).filter(SpdCandidate.id.in_(body.candidate_ids)).all()
    for candidate in rows:
        if body.team_id is not None:
            candidate.team_id = body.team_id
        if body.assigned_user_id is not None:
            candidate.assigned_user_id = body.assigned_user_id
        if body.org_id is not None:
            candidate.org_id = body.org_id
        if candidate.status == "suspect":
            candidate.status = "target"
    db.commit()
    return {"distributed": len(rows), "not_found": len(body.candidate_ids) - len(rows)}


@router.post("/candidates/{candidate_id}/claim",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def claim_candidate(
    candidate_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """团队认领目标患者（服务团队专家端 #3）。已被他人认领的返回 409，不静默改人。"""
    candidate = db.get(SpdCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="目标池记录不存在")
    assert_org_writable(db, user, candidate.org_id)
    if candidate.assigned_user_id not in (None, user.id):
        raise HTTPException(status_code=409, detail="该患者已被其他人员认领")
    candidate.assigned_user_id = user.id
    candidate.claimed_at = now_naive()
    if candidate.status == "suspect":
        candidate.status = "target"
    db.commit()
    return _candidate_out(candidate)


class CandidateStatusIn(BaseModel):
    status: str = Field(pattern="^(suspect|target|excluded)$")
    reason: str = Field(default="", max_length=256)


@router.post("/candidates/{candidate_id}/status",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def set_candidate_status(
    candidate_id: int,
    body: CandidateStatusIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    candidate = db.get(SpdCandidate, candidate_id)
    if candidate is None:
        raise HTTPException(status_code=404, detail="目标池记录不存在")
    assert_org_writable(db, user, candidate.org_id)
    if candidate.status == "enrolled":
        raise HTTPException(status_code=409, detail="已纳管患者请走生命周期接口调整")
    candidate.status = body.status
    if body.reason:
        candidate.reason = body.reason
    db.commit()
    return _candidate_out(candidate)


# ============================================================ 签约建档纳管


class EnrollIn(BaseModel):
    patient_id: int
    program_code: str = Field(min_length=1, max_length=32)
    org_id: int | None = None
    team_id: int | None = None
    doctor_user_id: int | None = None
    manager_user_id: int | None = None
    village_doctor_id: int | None = None
    stage: str = Field(default="", max_length=32)
    risk_level: str = Field(default="low", pattern="^(low|mid|high|very_high)$")
    sign_date: OptionalDateStr = ""
    consent_signed: bool = False
    consent_no: str = Field(default="", max_length=64)
    service_start: OptionalDateStr = ""
    service_end: OptionalDateStr = ""
    source: str = Field(default="screening", max_length=16)
    package_id: int | None = None
    habits: dict = Field(default_factory=dict)
    risk_factors: list[str] = Field(default_factory=list)
    complications: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)


def _enroll_out(e: SpdEnrollment, brief: dict | None = None) -> dict:
    out = {
        "id": e.id, "patient_id": e.patient_id, "program_code": e.program_code,
        "org_id": e.org_id, "team_id": e.team_id, "doctor_user_id": e.doctor_user_id,
        "manager_user_id": e.manager_user_id, "village_doctor_id": e.village_doctor_id,
        "stage": e.stage, "risk_level": e.risk_level, "status": e.status,
        "source": e.source, "sign_date": e.sign_date, "consent_signed": e.consent_signed,
        "consent_no": e.consent_no, "service_start": e.service_start,
        "service_end": e.service_end, "archived": e.archived, "habits": e.habits or {},
        "risk_factors": e.risk_factors or [], "complications": e.complications or [],
        "tags": e.tags or [], "last_followup_at": e.last_followup_at,
        "next_followup_at": e.next_followup_at, "created_at": e.created_at.isoformat(),
    }
    if brief:
        out.update({
            "patient_name": brief.get("name", ""), "gender": brief.get("gender", ""),
            "birth_date": brief.get("birth_date", ""), "phone": brief.get("phone", ""),
            "ehc_no": brief.get("ehc_no", ""),
        })
    return out


@router.post("/enrollments", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_enrollment(
    body: EnrollIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """签约纳管建档。同一患者同一病种只能有一条在管档案。

    知情同意书号缺失时**不阻断**建档，但 `consent_signed=False` 会在档案完整性
    统计里被单列出来——公卫业务里"先建档、后补签"是常态，一刀切拦住的结果
    是有人把假号填进去。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_enrollment")
    org_id = body.org_id if body.org_id is not None else user.org_id
    if org_id is None:
        raise HTTPException(status_code=422, detail="纳管机构不能为空")
    assert_org_writable(db, user, org_id)
    program = db.query(SpdProgram).filter(SpdProgram.code == body.program_code).first()
    if program is None or not program.active:
        raise HTTPException(status_code=404, detail="专病档案不存在或已停用")
    if body.package_id is not None and db.get(SpdServicePackage, body.package_id) is None:
        raise HTTPException(status_code=404, detail="服务包不存在")

    stage = body.stage or ((program.stages or [{}])[0].get("key", "") if program.stages else "")
    enrollment = SpdEnrollment(
        **body.model_dump(exclude={"org_id", "package_id", "stage"}),
        org_id=org_id,
        stage=stage,
        status="active",
        archived=bool(body.habits or body.risk_factors or body.complications),
    )
    db.add(enrollment)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该患者已纳管此病种") from None

    candidate = (
        db.query(SpdCandidate)
        .filter(
            SpdCandidate.patient_id == body.patient_id,
            SpdCandidate.program_code == body.program_code,
        )
        .first()
    )
    if candidate is not None:
        candidate.status = "enrolled"

    if body.package_id is not None:
        _bind_package(db, enrollment, body.package_id)

    # 村医签约计分：口径是"谁签的谁得分"，村医字段没填就按操作人算
    award_points(
        db, body.village_doctor_id or user.id, "sign",
        ref_type="enrollment", ref_id=enrollment.id, note=f"签约{program.name}",
        org_id=org_id,
    )
    db.commit()
    return _enroll_out(enrollment)


@router.get("/enrollments")
def list_enrollments(
    response: Response,
    program_code: str | None = None,
    status: str | None = "active",
    org_id: int | None = None,
    team_id: int | None = None,
    doctor_user_id: int | None = None,
    manager_user_id: int | None = None,
    village_doctor_id: int | None = None,
    risk_level: str | None = None,
    stage: str | None = None,
    archived: bool | None = None,
    keyword: str = "",
    due_before: str = "",
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """在管患者多条件查询——全程管理中心端 #9 列的九个筛选维度都在这里。"""
    query = db.query(SpdEnrollment)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(SpdEnrollment.org_id.in_(orgs))
    for column, value in (
        (SpdEnrollment.program_code, program_code), (SpdEnrollment.status, status),
        (SpdEnrollment.org_id, org_id), (SpdEnrollment.team_id, team_id),
        (SpdEnrollment.doctor_user_id, doctor_user_id),
        (SpdEnrollment.manager_user_id, manager_user_id),
        (SpdEnrollment.village_doctor_id, village_doctor_id),
        (SpdEnrollment.risk_level, risk_level), (SpdEnrollment.stage, stage),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if archived is not None:
        query = query.filter(SpdEnrollment.archived.is_(archived))
    if due_before:
        query = query.filter(
            SpdEnrollment.next_followup_at != "",
            SpdEnrollment.next_followup_at <= due_before,
        )
    if keyword:
        ids = [
            p.id
            for p in db.query(Patient)
            .filter(Patient.name.contains(keyword) | Patient.id_card.contains(keyword))
            .limit(500)
        ]
        query = query.filter(SpdEnrollment.patient_id.in_(ids or [0]))
    rows = paginate(query.order_by(SpdEnrollment.id.desc()), response, offset, limit)
    briefs = _patient_brief(db, [r.patient_id for r in rows])
    return [_enroll_out(r, briefs.get(r.patient_id)) for r in rows]


@router.get("/enrollments/{enrollment_id}")
def get_enrollment(
    enrollment_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    enrollment = db.get(SpdEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    assert_patient_visible(db, user, enrollment.patient_id, resource="spd_enrollment")
    briefs = _patient_brief(db, [enrollment.patient_id])
    out = _enroll_out(enrollment, briefs.get(enrollment.patient_id))
    out["packages"] = [
        _binding_out(db, b)
        for b in db.query(SpdPackageBinding)
        .filter(SpdPackageBinding.enrollment_id == enrollment_id)
        .all()
    ]
    out["paths"] = [
        {"id": i.id, "template_code": i.template_code, "status": i.status,
         "current_node_key": i.current_node_key, "current_stage": i.current_stage,
         "progress": i.progress}
        for i in db.query(SpdPathInstance)
        .filter(SpdPathInstance.enrollment_id == enrollment_id)
        .all()
    ]
    return out


class EnrollUpdate(BaseModel):
    team_id: int | None = None
    doctor_user_id: int | None = None
    manager_user_id: int | None = None
    village_doctor_id: int | None = None
    stage: str | None = None
    risk_level: str | None = None
    consent_signed: bool | None = None
    consent_no: str | None = None
    service_start: OptionalDateStr | None = None
    service_end: OptionalDateStr | None = None
    habits: dict | None = None
    risk_factors: list[str] | None = None
    complications: list[str] | None = None
    tags: list[str] | None = None
    next_followup_at: OptionalDateStr | None = None


@router.patch("/enrollments/{enrollment_id}",
              dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def update_enrollment(
    enrollment_id: int,
    body: EnrollUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enrollment = db.get(SpdEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    if enrollment.status != "active":
        raise HTTPException(status_code=409, detail="非在管状态的档案不可修改，请先恢复管理")
    assert_org_writable(db, user, enrollment.org_id)
    for key, value in body.model_dump(exclude_unset=True).items():
        setattr(enrollment, key, value)
    # 建档完整性：三项关键信息任一有值即视为已建档（成员端 #20 的"建档纳管衔接"）
    enrollment.archived = bool(
        enrollment.habits or enrollment.risk_factors or enrollment.complications
    )
    db.commit()
    return _enroll_out(enrollment)


# ============================================================ 生命周期事件


class LifecycleIn(BaseModel):
    event: str = Field(pattern="^(exclude|migrate|death|recall|resume)$")
    reason: str = Field(default="", max_length=256)
    detail: str = Field(default="", max_length=512)
    target_org_id: int | None = None
    occurred_at: OptionalDateStr = ""


#: 事件 → 纳管档案的新状态。resume 单独处理（回到 active）。
_EVENT_STATUS = {
    "exclude": "excluded", "migrate": "migrated", "death": "dead", "recall": "recalled",
}


@router.post("/enrollments/{enrollment_id}/lifecycle",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def lifecycle_event(
    enrollment_id: int,
    body: LifecycleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """登记生命周期事件并终止后续任务。

    跨机构迁出（给了 `target_org_id`）需目标机构确认后才生效，未确认前
    档案保持在管——否则患者会在两家机构之间"消失"一段时间，这段时间里
    没人对他负责，而两边的在管数都不含他。
    """
    enrollment = db.get(SpdEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    assert_org_writable(db, user, enrollment.org_id)

    if body.event == "resume":
        if enrollment.status == "dead":
            raise HTTPException(status_code=409, detail="已登记死亡的档案不可恢复管理")
        enrollment.status = "active"
        db.add(
            SpdLifecycleEvent(
                enrollment_id=enrollment_id, event="resume", reason=body.reason,
                detail=body.detail, operator_id=user.id,
                occurred_at=body.occurred_at or date.today().isoformat(),
            )
        )
        db.commit()
        return {"enrollment": _enroll_out(enrollment), "closed": {}}

    cross_org = body.event == "migrate" and body.target_org_id is not None
    if cross_org and db.get(Organization, body.target_org_id) is None:
        raise HTTPException(status_code=404, detail="目标机构不存在")

    event = SpdLifecycleEvent(
        enrollment_id=enrollment_id, event=body.event, reason=body.reason,
        detail=body.detail, target_org_id=body.target_org_id,
        confirmed=not cross_org, operator_id=user.id,
        occurred_at=body.occurred_at or date.today().isoformat(),
    )
    db.add(event)

    closed = {}
    if not cross_org:
        enrollment.status = _EVENT_STATUS[body.event]
        closed = close_open_work(db, enrollment, f"{body.event}:{body.reason}"[:250])
        if body.event == "recall":
            db.add(
                SpdRecall(
                    enrollment_id=enrollment_id, reason=body.reason, operator_id=user.id
                )
            )
    db.commit()
    return {
        "enrollment": _enroll_out(enrollment),
        "event_id": event.id,
        "pending_confirm": cross_org,
        "closed": closed,
    }


@router.post("/lifecycle-events/{event_id}/confirm",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def confirm_migration(
    event_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """目标机构确认迁入：原档案置迁出并终止任务，**并在目标机构重建责任关系**。

    P1-2 之前只完成了"断"（原机构停管），没完成招标个案管理师端 #11 要求的
    "由目标机构确认后重新建立责任关系"——患者在两家机构之间消失。
    现在确认即在目标机构建新档案：同病种、继承风险等级与建档信息、
    `migrated_from_id` 指回原档案。

    **不迁历史任务与随访**：那些是原机构的工作留痕，迁走会让原机构的考核
    凭空少一截；新机构的服务从确认那天重新开始计。
    """
    event = db.get(SpdLifecycleEvent, event_id)
    if event is None or event.event != "migrate":
        raise HTTPException(status_code=404, detail="迁出事件不存在")
    if event.confirmed:
        raise HTTPException(status_code=409, detail="该迁出已确认")
    assert_org_writable(db, user, event.target_org_id)
    enrollment = db.get(SpdEnrollment, event.enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    event.confirmed = True
    event.confirmed_by = user.id
    enrollment.status = "migrated"
    closed = close_open_work(db, enrollment, "迁出至其他机构")

    # 目标机构重建档案。唯一性是**部分唯一索引**（仅 status='active'）：
    # 原档案已置 migrated，不占键；若目标机构已有同病种在管档案则撞索引，
    # 走"不重复建、只接关系"分支。
    incoming = SpdEnrollment(
        patient_id=enrollment.patient_id, program_code=enrollment.program_code,
        org_id=event.target_org_id, risk_level=enrollment.risk_level,
        stage=enrollment.stage, status="active", source="migrate",
        migrated_from_id=enrollment.id, sign_date=date.today().isoformat(),
        consent_signed=enrollment.consent_signed, consent_no=enrollment.consent_no,
        habits=enrollment.habits, risk_factors=enrollment.risk_factors,
        complications=enrollment.complications, tags=enrollment.tags,
        archived=enrollment.archived,
    )
    if not insert_if_absent(db, incoming):
        # 目标机构已有同病种在管档案（比如患者早已在那边建档）：
        # 不重复建，只把关系接上
        incoming = (
            db.query(SpdEnrollment)
            .filter(
                SpdEnrollment.patient_id == enrollment.patient_id,
                SpdEnrollment.program_code == enrollment.program_code,
                SpdEnrollment.status == "active",
                SpdEnrollment.id != enrollment.id,
            )
            .first()
        )
        if incoming is not None and incoming.migrated_from_id is None:
            incoming.migrated_from_id = enrollment.id
    db.commit()
    return {
        "enrollment": _enroll_out(enrollment),
        "incoming_enrollment": _enroll_out(incoming) if incoming is not None else None,
        "closed": closed,
    }


@router.get("/lifecycle-events")
def list_lifecycle_events(
    response: Response,
    event: str | None = None,
    confirmed: bool | None = None,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdLifecycleEvent)
    if event:
        query = query.filter(SpdLifecycleEvent.event == event)
    if confirmed is not None:
        query = query.filter(SpdLifecycleEvent.confirmed.is_(confirmed))
    rows = paginate(query.order_by(SpdLifecycleEvent.id.desc()), response, offset, limit)
    enrollments = {
        e.id: e
        for e in db.query(SpdEnrollment)
        .filter(SpdEnrollment.id.in_([r.enrollment_id for r in rows] or [0]))
        .all()
    }
    briefs = _patient_brief(db, [e.patient_id for e in enrollments.values()])
    out = []
    for row in rows:
        enrollment = enrollments.get(row.enrollment_id)
        brief = briefs.get(enrollment.patient_id) if enrollment else None
        out.append({
            "id": row.id, "enrollment_id": row.enrollment_id, "event": row.event,
            "reason": row.reason, "detail": row.detail, "target_org_id": row.target_org_id,
            "confirmed": row.confirmed, "occurred_at": row.occurred_at,
            "program_code": enrollment.program_code if enrollment else "",
            "patient_id": enrollment.patient_id if enrollment else None,
            "patient_name": (brief or {}).get("name", ""),
            "created_at": row.created_at.isoformat(),
        })
    return out


class RecallUpdate(BaseModel):
    status: str = Field(pattern="^(pending|contacted|returned|failed)$")
    contact_note: str = Field(default="", max_length=256)
    result: str = Field(default="", max_length=256)


@router.post("/recalls/{recall_id}/progress",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def update_recall(
    recall_id: int,
    body: RecallUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """召回过程留痕。召回成功（returned）自动把档案恢复为在管。"""
    recall = db.get(SpdRecall, recall_id)
    if recall is None:
        raise HTTPException(status_code=404, detail="召回记录不存在")
    enrollment = db.get(SpdEnrollment, recall.enrollment_id)
    assert_org_writable(db, user, enrollment.org_id if enrollment else None)
    recall.status = body.status
    recall.result = body.result or recall.result
    if body.contact_note:
        recall.contacts = (recall.contacts or []) + [
            {"at": date.today().isoformat(), "note": body.contact_note}
        ]
    if body.status in ("returned", "failed"):
        recall.closed_at = now_naive()
    if body.status == "returned":
        if enrollment is not None and enrollment.status == "recalled":
            enrollment.status = "active"
    db.commit()
    return {"id": recall.id, "status": recall.status, "result": recall.result}


@router.get("/recalls")
def list_recalls(
    response: Response, status: str | None = None, offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdRecall)
    if status:
        query = query.filter(SpdRecall.status == status)
    rows = paginate(query.order_by(SpdRecall.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "enrollment_id": r.enrollment_id, "reason": r.reason,
         "status": r.status, "result": r.result, "contacts": r.contacts or [],
         "created_at": r.created_at.isoformat()}
        for r in rows
    ]


# ============================================================ 患者分组


class GroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    scope: str = Field(default="personal", pattern="^(personal|dept|team)$")
    dept: str = Field(default="", max_length=64)
    auto_rule: list[dict] = Field(default_factory=list)


@router.post("/groups", status_code=201, dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_group(
    body: GroupIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    group = SpdGroup(**body.model_dump(), owner_user_id=user.id, org_id=user.org_id)
    db.add(group)
    db.commit()
    return {"id": group.id, "name": group.name, "scope": group.scope, "member_count": 0}


@router.get("/groups")
def list_groups(
    scope: str | None = None, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """本人与本科室分组。个人分组只看自己的，科室/团队分组按机构可见。"""
    query = db.query(SpdGroup)
    if scope:
        query = query.filter(SpdGroup.scope == scope)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(
            (SpdGroup.owner_user_id == user.id)
            | ((SpdGroup.scope != "personal") & SpdGroup.org_id.in_(orgs))
        )
    rows = query.order_by(SpdGroup.id.desc()).limit(200).all()
    counts = dict(
        db.query(SpdGroupMember.group_id, func.count(SpdGroupMember.id))
        .filter(SpdGroupMember.group_id.in_([g.id for g in rows] or [0]))
        .group_by(SpdGroupMember.group_id)
        .all()
    )
    return [
        {"id": g.id, "name": g.name, "scope": g.scope, "dept": g.dept,
         "owner_user_id": g.owner_user_id, "auto_rule": g.auto_rule or [],
         "member_count": counts.get(g.id, 0),
         "updated_at": g.updated_at.isoformat()}
        for g in rows
    ]


class GroupMembersIn(BaseModel):
    patient_ids: list[int] = Field(default_factory=list, max_length=1000)
    use_auto_rule: bool = False
    program_code: str = Field(default="", max_length=32)


@router.post("/groups/{group_id}/members",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def add_group_members(
    group_id: int,
    body: GroupMembersIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """批量加入分组；`use_auto_rule` 时按分组的自动规则从在管人群里筛。"""
    group = db.get(SpdGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="分组不存在")
    assert_org_writable(db, user, group.org_id)
    patient_ids = list(body.patient_ids)
    if body.use_auto_rule:
        if not group.auto_rule:
            raise HTTPException(status_code=422, detail="该分组未配置自动分组规则")
        query = db.query(SpdEnrollment).filter(SpdEnrollment.status == "active")
        if body.program_code:
            query = query.filter(SpdEnrollment.program_code == body.program_code)
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            query = query.filter(SpdEnrollment.org_id.in_(orgs))
        for enrollment in query.limit(1000).all():
            facts = build_facts(
                db, enrollment.patient_id,
                {"risk_level": enrollment.risk_level, "stage": enrollment.stage},
            )
            hit, _ = evaluate(group.auto_rule, facts, mode="all")
            if hit:
                patient_ids.append(enrollment.patient_id)

    existing = {
        pid
        for (pid,) in db.query(SpdGroupMember.patient_id)
        .filter(SpdGroupMember.group_id == group_id)
        .all()
    }
    added = 0
    for pid in dict.fromkeys(patient_ids):
        if pid in existing:
            continue
        if insert_if_absent(db, SpdGroupMember(group_id=group_id, patient_id=pid)):
            added += 1
    group.updated_at = now_naive()
    db.commit()
    return {"added": added, "total": len(existing) + added}


@router.get("/groups/{group_id}/members")
def list_group_members(
    group_id: int, response: Response, offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdGroupMember).filter(SpdGroupMember.group_id == group_id)
    rows = paginate(query.order_by(SpdGroupMember.id.desc()), response, offset, limit)
    briefs = _patient_brief(db, [r.patient_id for r in rows])
    return [
        {"id": r.id, "patient_id": r.patient_id, "added_at": r.added_at.isoformat(),
         **(briefs.get(r.patient_id) or {})}
        for r in rows
    ]


@router.delete("/groups/{group_id}/members/{patient_id}", status_code=204,
               dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def remove_group_member(group_id: int, patient_id: int, db: Session = Depends(get_db)):
    member = (
        db.query(SpdGroupMember)
        .filter(SpdGroupMember.group_id == group_id, SpdGroupMember.patient_id == patient_id)
        .first()
    )
    if member is None:
        raise HTTPException(status_code=404, detail="分组成员不存在")
    db.delete(member)
    db.commit()
    return Response(status_code=204)


# ============================================================ 服务包绑定与扣减


def _bind_package(db: Session, enrollment: SpdEnrollment, package_id: int) -> SpdPackageBinding:
    package = db.get(SpdServicePackage, package_id)
    if package is None:
        raise HTTPException(status_code=404, detail="服务包不存在")
    items = [
        {"code": i.get("code"), "name": i.get("name", ""), "total": int(i.get("times", 0)),
         "used": 0, "price": i.get("price", 0)}
        for i in package.items or []
    ]
    period_end = ""
    if enrollment.service_start:
        try:
            period_end = (
                date.fromisoformat(enrollment.service_start)
                + timedelta(days=package.period_days)
            ).isoformat()
        except ValueError:
            period_end = ""
    binding = SpdPackageBinding(
        enrollment_id=enrollment.id, package_id=package_id, items=items,
        status="bound", period_end=period_end,
    )
    db.add(binding)
    db.flush()
    return binding


def _binding_out(db: Session, b: SpdPackageBinding) -> dict:
    package = db.get(SpdServicePackage, b.package_id)
    items = b.items or []
    total = sum(int(i.get("total", 0)) for i in items)
    used = sum(int(i.get("used", 0)) for i in items)
    return {
        "id": b.id, "enrollment_id": b.enrollment_id, "package_id": b.package_id,
        "package_name": package.name if package else "", "price": package.price if package else 0,
        "items": items, "status": b.status, "period_end": b.period_end,
        "bound_at": b.bound_at.isoformat(),
        "usage_rate": round(used / total * 100, 1) if total else 0.0,
        "remaining": total - used,
    }


class BindPackageIn(BaseModel):
    package_id: int


@router.post("/enrollments/{enrollment_id}/packages", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def bind_package(
    enrollment_id: int,
    body: BindPackageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    enrollment = db.get(SpdEnrollment, enrollment_id)
    if enrollment is None:
        raise HTTPException(status_code=404, detail="纳管档案不存在")
    assert_org_writable(db, user, enrollment.org_id)
    exists = (
        db.query(SpdPackageBinding)
        .filter(
            SpdPackageBinding.enrollment_id == enrollment_id,
            SpdPackageBinding.package_id == body.package_id,
            SpdPackageBinding.status == "bound",
        )
        .first()
    )
    if exists is not None:
        raise HTTPException(status_code=409, detail="该服务包已绑定")
    binding = _bind_package(db, enrollment, body.package_id)
    db.commit()
    return _binding_out(db, binding)


@router.post("/package-bindings/{binding_id}/unbind",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def unbind_package(binding_id: int, db: Session = Depends(get_db)):
    binding = db.get(SpdPackageBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="服务包绑定不存在")
    binding.status = "unbound"
    binding.unbound_at = now_naive()
    db.commit()
    return _binding_out(db, binding)


class UsageIn(BaseModel):
    item_code: str = Field(min_length=1, max_length=32)
    qty: int = Field(default=1, ge=1, le=100)
    note: str = Field(default="", max_length=256)


@router.post("/package-bindings/{binding_id}/usages", status_code=201,
             dependencies=[Depends(require_roles(*SERVICE_ROLES, "operator"))])
def add_usage(
    binding_id: int,
    body: UsageIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """服务项目扣减登记。剩余次数不足时拒绝，不允许扣成负数。"""
    binding = db.get(SpdPackageBinding, binding_id)
    if binding is None:
        raise HTTPException(status_code=404, detail="服务包绑定不存在")
    if binding.status != "bound":
        raise HTTPException(status_code=409, detail="该服务包已解绑，不能扣减")
    # 深拷贝再改：JSON 列没开 MutableList，就地改内层 dict 时 SQLAlchemy 比对
    # 新旧值会认为"没变"，UPDATE 不会发出——表现是扣减看着成功、次数永远不减。
    items = deepcopy(binding.items or [])
    target = next((i for i in items if i.get("code") == body.item_code), None)
    if target is None:
        raise HTTPException(status_code=404, detail="服务包中没有该项目")
    if int(target.get("used", 0)) + body.qty > int(target.get("total", 0)):
        raise HTTPException(status_code=409, detail="该项目剩余次数不足")
    target["used"] = int(target.get("used", 0)) + body.qty
    binding.items = items
    usage = SpdPackageUsage(
        binding_id=binding_id, item_code=body.item_code, item_name=target.get("name", ""),
        qty=body.qty, price=target.get("price", 0), operator_id=user.id, note=body.note,
    )
    db.add(usage)
    db.commit()
    return {"usage_id": usage.id, "binding": _binding_out(db, binding)}


@router.get("/package-bindings/{binding_id}/usages")
def list_usages(binding_id: int, response: Response, offset: int = 0, limit: int = 100,
                db: Session = Depends(get_db)):
    query = db.query(SpdPackageUsage).filter(SpdPackageUsage.binding_id == binding_id)
    rows = paginate(query.order_by(SpdPackageUsage.id.desc()), response, offset, limit)
    return [
        {"id": r.id, "item_code": r.item_code, "item_name": r.item_name, "qty": r.qty,
         "price": r.price, "note": r.note, "used_at": r.used_at.isoformat()}
        for r in rows
    ]


# ============================================================ 居民专病服务申请


@router.get("/service-applies")
def list_service_applies(
    response: Response, status: str | None = "pending", offset: int = 0, limit: int = 100,
    db: Session = Depends(get_db),
):
    query = db.query(SpdServiceApply)
    if status:
        query = query.filter(SpdServiceApply.status == status)
    rows = paginate(query.order_by(SpdServiceApply.id.desc()), response, offset, limit)
    briefs = _patient_brief(db, [r.patient_id for r in rows])
    return [
        {"id": r.id, "patient_id": r.patient_id, "program_code": r.program_code,
         "note": r.note, "status": r.status, "handle_note": r.handle_note,
         "created_at": r.created_at.isoformat(),
         **(briefs.get(r.patient_id) or {})}
        for r in rows
    ]


class ApplyHandleIn(BaseModel):
    status: str = Field(pattern="^(accepted|rejected)$")
    handle_note: str = Field(default="", max_length=256)


@router.post("/service-applies/{apply_id}/handle",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def handle_service_apply(
    apply_id: int,
    body: ApplyHandleIn,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """受理居民专病服务申请；受理即把居民放进目标池，等待签约建档。"""
    apply = db.get(SpdServiceApply, apply_id)
    if apply is None:
        raise HTTPException(status_code=404, detail="服务申请不存在")
    if apply.status != "pending":
        raise HTTPException(status_code=409, detail="该申请已处理")
    apply.status = body.status
    apply.handle_note = body.handle_note
    apply.handled_by = user.id
    apply.handled_at = now_naive()
    if body.status == "accepted":
        candidate = (
            db.query(SpdCandidate)
            .filter(
                SpdCandidate.patient_id == apply.patient_id,
                SpdCandidate.program_code == apply.program_code,
            )
            .first()
        )
        if candidate is None:
            insert_if_absent(
                db,
                SpdCandidate(
                    patient_id=apply.patient_id, program_code=apply.program_code,
                    status="target", source="apply", org_id=user.org_id,
                    assigned_user_id=user.id, reason="居民申请受理",
                ),
            )
        elif candidate.status not in ("enrolled",):
            candidate.status = "target"
            candidate.assigned_user_id = user.id
    db.commit()
    return {"id": apply.id, "status": apply.status}


# ============================================================ 患者专病 360 档案


@router.get("/patients/{patient_id}/profile")
def patient_profile(
    patient_id: int,
    program_code: str = Query(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """专病 360 档案：纳管、路径、任务、监测、评估、转诊一屏聚合。

    多病种聚合视图（个案管理师端 #7 / 中心端 #8）：不传 `program_code` 就返回
    该患者的**全部**签约专病，每个病种一个卡片。
    """
    assert_patient_visible(db, user, patient_id, resource="spd_profile")
    patient = db.get(Patient, patient_id)
    if patient is None:
        raise HTTPException(status_code=404, detail="患者不存在")
    query = db.query(SpdEnrollment).filter(SpdEnrollment.patient_id == patient_id)
    if program_code:
        query = query.filter(SpdEnrollment.program_code == program_code)
    enrollments = query.all()
    program_names = {
        p.code: p.name
        for p in db.query(SpdProgram)
        .filter(SpdProgram.code.in_([e.program_code for e in enrollments] or [""]))
        .all()
    }

    programs = []
    for enrollment in enrollments:
        instances = (
            db.query(SpdPathInstance)
            .filter(SpdPathInstance.enrollment_id == enrollment.id)
            .all()
        )
        tasks = (
            db.query(SpdTask)
            .filter(SpdTask.enrollment_id == enrollment.id)
            .order_by(SpdTask.id.desc())
            .limit(20)
            .all()
        )
        programs.append({
            "enrollment": _enroll_out(enrollment),
            "program_name": program_names.get(enrollment.program_code, enrollment.program_code),
            "paths": [
                {"id": i.id, "template_code": i.template_code, "status": i.status,
                 "current_node_key": i.current_node_key, "progress": i.progress}
                for i in instances
            ],
            "packages": [
                _binding_out(db, b)
                for b in db.query(SpdPackageBinding)
                .filter(SpdPackageBinding.enrollment_id == enrollment.id)
                .all()
            ],
            "open_tasks": sum(
                1 for t in tasks if t.status in ("pending", "claimed", "doing", "overdue")
            ),
            "recent_tasks": [
                {"id": t.id, "title": t.title, "task_type": t.task_type,
                 "status": t.status, "due_date": t.due_date}
                for t in tasks[:10]
            ],
        })

    measurements = (
        db.query(SpdMeasurement)
        .filter(SpdMeasurement.patient_id == patient_id)
        .order_by(SpdMeasurement.measured_at.desc())
        .limit(50)
        .all()
    )
    assessments = (
        db.query(SpdAssessment)
        .filter(SpdAssessment.patient_id == patient_id)
        .order_by(SpdAssessment.id.desc())
        .limit(10)
        .all()
    )
    referrals = (
        db.query(SpdReferralCase)
        .filter(SpdReferralCase.patient_id == patient_id)
        .order_by(SpdReferralCase.id.desc())
        .limit(10)
        .all()
    )
    return {
        "patient": {
            "id": patient.id, "name": patient.name, "gender": patient.gender,
            "birth_date": patient.birth_date, "ehc_no": patient.ehc_no,
            "phone": patient.phone,
        },
        "programs": programs,
        "measurements": [
            {"metric": m.metric, "value": m.value, "unit": m.unit, "level": m.level,
             "source": m.source, "measured_at": m.measured_at.isoformat()}
            for m in measurements
        ],
        "assessments": [
            {"id": a.id, "scale_code": a.scale_code, "score": a.score,
             "risk_level": a.risk_level, "created_at": a.created_at.isoformat()}
            for a in assessments
        ],
        "referrals": [
            {"id": r.id, "direction": r.direction, "status": r.status,
             "created_at": r.created_at.isoformat()}
            for r in referrals
        ],
        "facts": build_facts(db, patient_id),
    }
