"""全域慢专病 · 转诊域：转诊规则 + 村医→服务站→卫生院→县级医院逐级链路。

对应招标文件：专家端 #8、全程管理中心端 #14/#15、医生移动端 #16/#18、患者端 #16/#17。

链路状态机（上转）：

    submitted ──复核──▶ station_reviewed ──审核──▶ township_reviewed
        │                                              │
        │◀──────────── rejected（任一环节退回）         ▼
        └──withdrawn（发起人撤回）              accepted ──到院──▶ arrived
                                                          │
                              closed ◀──随访接收── down_referred（下转）

每一格只能由**当前环节**的机构推进，且每推进一格写一条 `SpdReferralStep`。
状态机写死在 `_NEXT` 表里而不是让调用方传目标状态：让调用方传，等于把
"谁能把单子推到哪一步"交给前端决定。
"""
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ...clock import now_naive
from ...database import get_db
from ...deps import get_current_user, paginate, require_roles
from ..platform import Organization, Patient, User, org_level
from ..models import (
    SpdEnrollment,
    SpdReferralCase,
    SpdReferralRule,
    SpdReferralStep,
)
from ..rules import RuleError, evaluate, validate_conditions
from ..service import award_points, build_facts, spawn_task
from ...visibility import assert_patient_visible, visible_org_ids

router = APIRouter(
    prefix="/api/spd",
    tags=["全域慢专病·转诊"],
    dependencies=[Depends(get_current_user)],
)

SERVICE_ROLES = ("doctor", "public_health", "director")

#: 逐级审核链：当前状态 → (下一状态, 环节名, 需要的处理层级)
_NEXT = {
    "submitted": ("station_reviewed", "服务站复核", "station"),
    "station_reviewed": ("township_reviewed", "卫生院审核", "township"),
    "township_reviewed": ("accepted", "县级医院接收", "county"),
}

#: 已进入终态的单子不接受任何推进动作
_TERMINAL = ("closed", "rejected", "withdrawn")


# ============================================================ 转诊规则


class ReferralRuleIn(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    program_code: str = Field(default="", max_length=32)
    scene: str = Field(default="followup", max_length=16)
    conditions: list[dict] = Field(default_factory=list)
    notify_role: str = Field(default="doctor", max_length=32)
    handle_level: str = Field(default="township", pattern="^(village|station|township|county)$")
    target_org_id: int | None = None
    auto_task: bool = True


def _rule_out(r: SpdReferralRule) -> dict:
    return {
        "id": r.id, "code": r.code, "name": r.name, "program_code": r.program_code,
        "scene": r.scene, "conditions": r.conditions or [], "notify_role": r.notify_role,
        "handle_level": r.handle_level, "target_org_id": r.target_org_id,
        "auto_task": r.auto_task, "active": r.active,
    }


@router.post("/referral-rules", status_code=201,
             dependencies=[Depends(require_roles("director", "doctor"))])
def create_referral_rule(body: ReferralRuleIn, db: Session = Depends(get_db)):
    try:
        conditions = validate_conditions(body.conditions)
    except RuleError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    if not conditions:
        raise HTTPException(status_code=422, detail="转诊规则至少要有一个触发条件")
    rule = SpdReferralRule(**body.model_dump(exclude={"conditions"}), conditions=conditions)
    db.add(rule)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="该转诊规则编码已存在") from None
    return _rule_out(rule)


@router.get("/referral-rules")
def list_referral_rules(
    program_code: str | None = None, active: bool | None = None, db: Session = Depends(get_db)
):
    query = db.query(SpdReferralRule)
    if program_code:
        query = query.filter(SpdReferralRule.program_code == program_code)
    if active is not None:
        query = query.filter(SpdReferralRule.active.is_(active))
    return [_rule_out(r) for r in query.order_by(SpdReferralRule.id).limit(200).all()]


@router.patch("/referral-rules/{rule_id}",
              dependencies=[Depends(require_roles("director", "doctor"))])
def update_referral_rule(rule_id: int, body: dict, db: Session = Depends(get_db)):
    rule = db.get(SpdReferralRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="转诊规则不存在")
    if "conditions" in body:
        try:
            rule.conditions = validate_conditions(body["conditions"])
        except RuleError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
    for key in ("name", "scene", "notify_role", "handle_level", "target_org_id",
                "auto_task", "active"):
        if key in body:
            setattr(rule, key, body[key])
    db.commit()
    return _rule_out(rule)


class RuleCheckIn(BaseModel):
    patient_id: int
    program_code: str = Field(default="", max_length=32)
    extra: dict = Field(default_factory=dict)
    auto_create: bool = False


@router.post("/referral-rules/check", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def check_referral_rules(
    body: RuleCheckIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按规则判断某患者是否触发转诊，可选直接生成上转单。

    `auto_create=false` 是默认：把"是否真的要转"留给医生点一下。
    规则命中即自动开单，在实施期会被投诉——一次批量随访录入能开出几十张单子。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_referral")
    enrollment = None
    if body.program_code:
        enrollment = (
            db.query(SpdEnrollment)
            .filter(
                SpdEnrollment.patient_id == body.patient_id,
                SpdEnrollment.program_code == body.program_code,
            )
            .first()
        )
    facts = build_facts(
        db, body.patient_id,
        {
            "risk_level": enrollment.risk_level if enrollment else None,
            "stage": enrollment.stage if enrollment else None,
            **body.extra,
        },
    )
    query = db.query(SpdReferralRule).filter(SpdReferralRule.active.is_(True))
    if body.program_code:
        query = query.filter(
            SpdReferralRule.program_code.in_([body.program_code, ""])
        )
    hits = []
    for rule in query.all():
        hit, matched = evaluate(rule.conditions or [], facts, mode="any")
        if hit:
            hits.append({"rule": _rule_out(rule), "matched": matched})

    created = None
    if hits and body.auto_create:
        top = hits[0]
        created = _create_case(
            db, user,
            patient_id=body.patient_id,
            program_code=body.program_code,
            enrollment=enrollment,
            reason=top["rule"]["name"],
            target_org_id=top["rule"]["target_org_id"],
            trigger_rule_code=top["rule"]["code"],
            trigger_evidence={"matched": top["matched"]},
            materials=[],
        )
        db.commit()
    return {
        "triggered": bool(hits),
        "hits": hits,
        "facts": facts,
        "case": _case_out(db, created) if created else None,
    }


# ============================================================ 转诊单


class ReferralIn(BaseModel):
    patient_id: int
    program_code: str = Field(default="", max_length=32)
    direction: str = Field(default="up", pattern="^(up|down)$")
    target_org_id: int | None = None
    reason: str = Field(default="", max_length=512)
    materials: list[dict] = Field(default_factory=list)
    trigger_rule_code: str = Field(default="", max_length=32)
    trigger_evidence: dict = Field(default_factory=dict)


#: 机构层级 → 转诊链路层级的换算在适配层（那是平台机构树的形状，不是转诊的规则）
_level_of = org_level


def _case_out(db: Session, c: SpdReferralCase, steps: list[SpdReferralStep] | None = None) -> dict:
    patient = db.get(Patient, c.patient_id)
    out = {
        "id": c.id, "patient_id": c.patient_id,
        "patient_name": patient.name if patient else "",
        "program_code": c.program_code, "enrollment_id": c.enrollment_id,
        "direction": c.direction, "initiator_org_id": c.initiator_org_id,
        "initiator_id": c.initiator_id, "current_org_id": c.current_org_id,
        "current_level": c.current_level, "target_org_id": c.target_org_id,
        "status": c.status, "reason": c.reason, "trigger_rule_code": c.trigger_rule_code,
        "trigger_evidence": c.trigger_evidence or {}, "materials": c.materials or [],
        "effective_visit": c.effective_visit, "stable_for_down": c.stable_for_down,
        "created_at": c.created_at.isoformat(),
        "closed_at": c.closed_at.isoformat() if c.closed_at else "",
    }
    if steps is not None:
        out["steps"] = [
            {"id": s.id, "step": s.step, "action": s.action, "actor_id": s.actor_id,
             "org_id": s.org_id, "opinion": s.opinion, "created_at": s.created_at.isoformat()}
            for s in steps
        ]
    return out


def _add_step(
    db: Session, case: SpdReferralCase, step: str, action: str, user: User, opinion: str = ""
) -> None:
    db.add(
        SpdReferralStep(
            case_id=case.id, step=step, action=action, actor_id=user.id,
            org_id=user.org_id, opinion=opinion,
        )
    )


def _create_case(
    db: Session,
    user: User,
    *,
    patient_id: int,
    program_code: str,
    enrollment: SpdEnrollment | None,
    reason: str,
    target_org_id: int | None,
    trigger_rule_code: str,
    trigger_evidence: dict,
    materials: list,
    direction: str = "up",
) -> SpdReferralCase:
    origin_org_id = user.org_id or (enrollment.org_id if enrollment else None)
    case = SpdReferralCase(
        patient_id=patient_id, program_code=program_code,
        enrollment_id=enrollment.id if enrollment else None,
        direction=direction,
        initiator_org_id=origin_org_id,
        initiator_id=user.id,
        current_org_id=origin_org_id,
        current_level=_level_of(db, origin_org_id),
        target_org_id=target_org_id,
        status="submitted",
        reason=reason,
        trigger_rule_code=trigger_rule_code,
        trigger_evidence=trigger_evidence,
        materials=materials,
    )
    db.add(case)
    db.flush()
    _add_step(db, case, "发起", "submit", user, reason)
    return case


@router.post("/referrals", status_code=201, dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def create_referral(
    body: ReferralIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """发起转诊（村医上转 / 上级下转）。

    发起机构取账号绑定的机构；平台管理员/管理层这类不绑机构的账号代基层开单时，
    回落到患者纳管档案所在机构——两者都没有才拒绝。直接要求 `user.org_id`
    会让管理员连代录都做不了，而县里确实存在"电话上报、中心代录"的场景。
    """
    assert_patient_visible(db, user, body.patient_id, resource="spd_referral")
    if body.target_org_id is not None and db.get(Organization, body.target_org_id) is None:
        raise HTTPException(status_code=404, detail="目标机构不存在")
    enrollment = None
    if body.program_code:
        enrollment = (
            db.query(SpdEnrollment)
            .filter(
                SpdEnrollment.patient_id == body.patient_id,
                SpdEnrollment.program_code == body.program_code,
            )
            .first()
        )
    if user.org_id is None and enrollment is None:
        raise HTTPException(status_code=422, detail="账号未绑定机构且患者未纳管，无法确定发起机构")
    case = _create_case(
        db, user, patient_id=body.patient_id, program_code=body.program_code,
        enrollment=enrollment, reason=body.reason, target_org_id=body.target_org_id,
        trigger_rule_code=body.trigger_rule_code, trigger_evidence=body.trigger_evidence,
        materials=body.materials, direction=body.direction,
    )
    db.commit()
    return _case_out(db, case)


@router.get("/referrals")
def list_referrals(
    response: Response,
    patient_id: int | None = None,
    program_code: str | None = None,
    status: str | None = None,
    direction: str | None = None,
    current_org_id: int | None = None,
    initiator_org_id: int | None = None,
    open_only: bool = False,
    offset: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    query = db.query(SpdReferralCase)
    if patient_id is not None:
        assert_patient_visible(db, user, patient_id, resource="spd_referral")
        query = query.filter(SpdReferralCase.patient_id == patient_id)
    else:
        orgs = visible_org_ids(db, user)
        if orgs is not None:
            # 转诊天然跨机构：发起方、当前处理方、目标方任一在可见范围内即可见，
            # 否则接收方在单子转到自己手上之前根本看不见它。
            query = query.filter(
                SpdReferralCase.initiator_org_id.in_(orgs)
                | SpdReferralCase.current_org_id.in_(orgs)
                | SpdReferralCase.target_org_id.in_(orgs)
            )
    for column, value in (
        (SpdReferralCase.program_code, program_code), (SpdReferralCase.status, status),
        (SpdReferralCase.direction, direction),
        (SpdReferralCase.current_org_id, current_org_id),
        (SpdReferralCase.initiator_org_id, initiator_org_id),
    ):
        if value is not None and value != "":
            query = query.filter(column == value)
    if open_only:
        query = query.filter(SpdReferralCase.status.notin_(_TERMINAL))
    rows = paginate(query.order_by(SpdReferralCase.id.desc()), response, offset, limit)
    return [_case_out(db, r) for r in rows]


@router.get("/referrals/{case_id}")
def get_referral(
    case_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """转诊单详情 + 全轨迹（患者端 #16 的"查看触发依据、异常指标、提交资料和处理意见"）。"""
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    assert_patient_visible(db, user, case.patient_id, resource="spd_referral")
    steps = (
        db.query(SpdReferralStep)
        .filter(SpdReferralStep.case_id == case_id)
        .order_by(SpdReferralStep.id)
        .all()
    )
    return _case_out(db, case, steps)


class ReviewIn(BaseModel):
    action: str = Field(pattern="^(pass|reject)$")
    opinion: str = Field(default="", max_length=512)
    target_org_id: int | None = None


@router.post("/referrals/{case_id}/review", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def review_referral(
    case_id: int, body: ReviewIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """分级审核：服务站复核 → 卫生院审核 → 县级医院接收，一次推进一格。"""
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    if case.status in _TERMINAL:
        raise HTTPException(status_code=409, detail="该转诊单已结束")
    nxt = _NEXT.get(case.status)
    if nxt is None:
        raise HTTPException(status_code=409, detail="当前状态不需要审核")
    next_status, step_name, level = nxt
    if body.action == "reject":
        case.status = "rejected"
        case.closed_at = now_naive()
        _add_step(db, case, step_name, "reject", user, body.opinion)
        db.commit()
        return _case_out(db, case)
    case.status = next_status
    case.current_level = level
    case.current_org_id = user.org_id
    if body.target_org_id is not None:
        case.target_org_id = body.target_org_id
    _add_step(db, case, step_name, "pass", user, body.opinion)
    if next_status == "accepted":
        # 接收即给发起方（村医）建一条"跟踪到院"的任务，闭环不靠人惦记
        enrollment = (
            db.get(SpdEnrollment, case.enrollment_id) if case.enrollment_id else None
        )
        spawn_task(
            db, patient_id=case.patient_id, title="上转患者到院跟踪",
            task_type="referral", program_code=case.program_code, enrollment=enrollment,
            assignee_id=case.initiator_id, org_id=case.initiator_org_id,
            due_days=7, priority=2, source="referral",
        )
    db.commit()
    return _case_out(db, case)


class ArriveIn(BaseModel):
    effective_visit: bool = True
    opinion: str = Field(default="", max_length=512)


@router.post("/referrals/{case_id}/arrive", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def arrive_referral(
    case_id: int, body: ArriveIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """到院登记与有效就诊判定（中心端 #14）。

    "有效上转"是村医积分与考核的计分依据，所以计分放在这里而不是发起时：
    发起就计分，会让开单变成刷分动作。
    """
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    if case.status != "accepted":
        raise HTTPException(status_code=409, detail="只有已接收的转诊单可登记到院")
    case.status = "arrived"
    case.effective_visit = body.effective_visit
    _add_step(db, case, "到院", "arrive", user, body.opinion)
    if body.effective_visit:
        enrollment = (
            db.get(SpdEnrollment, case.enrollment_id) if case.enrollment_id else None
        )
        award_points(
            db,
            (enrollment.village_doctor_id if enrollment else None) or case.initiator_id,
            "referral_up", ref_type="referral", ref_id=case.id, note="有效上转",
            org_id=case.initiator_org_id,
        )
    db.commit()
    return _case_out(db, case)


class DownIn(BaseModel):
    target_org_id: int
    stable: bool = True
    opinion: str = Field(default="", max_length=512)
    followup_days: int = Field(default=14, ge=1, le=365)


@router.post("/referrals/{case_id}/down", dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def down_referral(
    case_id: int, body: DownIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """病情稳定患者下转，并生成下转后随访计划（中心端 #14）。"""
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    if case.status not in ("accepted", "arrived"):
        raise HTTPException(status_code=409, detail="只有已接收/已到院的患者可下转")
    if db.get(Organization, body.target_org_id) is None:
        raise HTTPException(status_code=404, detail="下转目标机构不存在")
    case.status = "down_referred"
    case.stable_for_down = body.stable
    case.target_org_id = body.target_org_id
    case.current_org_id = body.target_org_id
    case.current_level = _level_of(db, body.target_org_id)
    _add_step(db, case, "下转", "down", user, body.opinion)
    enrollment = db.get(SpdEnrollment, case.enrollment_id) if case.enrollment_id else None
    spawn_task(
        db, patient_id=case.patient_id, title="下转承接与随访",
        task_type="followup", program_code=case.program_code, enrollment=enrollment,
        org_id=body.target_org_id, due_days=body.followup_days, priority=2,
        source="referral",
    )
    db.commit()
    return _case_out(db, case)


class ReceiveIn(BaseModel):
    opinion: str = Field(default="", max_length=512)


@router.post("/referrals/{case_id}/receive-followup",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def receive_followup(
    case_id: int, body: ReceiveIn, db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """下转随访接收——闭环的最后一格。到这一步该单才计入闭环率。"""
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    if case.status != "down_referred":
        raise HTTPException(status_code=409, detail="只有已下转的转诊单可接收随访")
    case.status = "closed"
    case.closed_at = now_naive()
    _add_step(db, case, "随访接收", "receive", user, body.opinion)
    award_points(
        db, user.id, "referral_down", ref_type="referral", ref_id=case.id,
        note="下转承接", org_id=user.org_id,
    )
    db.commit()
    return _case_out(db, case)


@router.post("/referrals/{case_id}/withdraw",
             dependencies=[Depends(require_roles(*SERVICE_ROLES))])
def withdraw_referral(
    case_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """撤回：只有发起人本人、且尚未被上级接收时可撤。"""
    case = db.get(SpdReferralCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="转诊单不存在")
    if case.initiator_id != user.id and user.role not in ("admin", "director"):
        raise HTTPException(status_code=403, detail="只有发起人可以撤回转诊单")
    if case.status not in ("submitted", "station_reviewed"):
        raise HTTPException(status_code=409, detail="已进入上级审核的转诊单不能撤回")
    case.status = "withdrawn"
    case.closed_at = now_naive()
    _add_step(db, case, "撤回", "withdraw", user, "")
    db.commit()
    return _case_out(db, case)


@router.get("/referrals-stats/closure")
def closure_rate(
    program_code: str | None = None,
    org_id: int | None = None,
    date_from: str = "",
    date_to: str = "",
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    """转诊闭环率与环节分布（中心端 #15、卫健端 #11）。

    闭环率分母**不含** withdrawn 与 rejected：撤回的单子从未进入流程，
    退回的单子是被判定不该转——把它们算进分母，等于把"该拦的拦住了"
    记成"没闭环"，考核上会逼着基层不敢退回。
    """
    query = db.query(SpdReferralCase)
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(
            SpdReferralCase.initiator_org_id.in_(orgs)
            | SpdReferralCase.current_org_id.in_(orgs)
        )
    if program_code:
        query = query.filter(SpdReferralCase.program_code == program_code)
    if org_id is not None:
        query = query.filter(SpdReferralCase.initiator_org_id == org_id)
    if date_from:
        query = query.filter(SpdReferralCase.created_at >= f"{date_from} 00:00:00")
    if date_to:
        query = query.filter(SpdReferralCase.created_at <= f"{date_to} 23:59:59")

    by_status = dict(
        query.with_entities(SpdReferralCase.status, func.count(SpdReferralCase.id))
        .group_by(SpdReferralCase.status).all()
    )
    total = sum(by_status.values())
    denominator = total - by_status.get("withdrawn", 0) - by_status.get("rejected", 0)
    closed = by_status.get("closed", 0)
    effective = query.filter(SpdReferralCase.effective_visit.is_(True)).count()
    return {
        "total": total,
        "denominator": denominator,
        "closed": closed,
        "closure_rate": round(closed / denominator * 100, 1) if denominator else 0.0,
        "effective_visits": effective,
        "effective_rate": round(effective / denominator * 100, 1) if denominator else 0.0,
        "by_status": by_status,
        "pending_by_level": dict(
            query.filter(SpdReferralCase.status.notin_(_TERMINAL))
            .with_entities(SpdReferralCase.current_level, func.count(SpdReferralCase.id))
            .group_by(SpdReferralCase.current_level).all()
        ),
    }


@router.get("/referrals-alerts")
def referral_alerts(
    hours: int = 48, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """转诊审核超时预警（医生移动端 #19）：超过 N 小时未推进的在途单。"""
    cutoff = now_naive() - timedelta(hours=max(min(hours, 720), 1))
    query = db.query(SpdReferralCase).filter(
        SpdReferralCase.status.notin_(_TERMINAL),
        SpdReferralCase.created_at < cutoff,
    )
    orgs = visible_org_ids(db, user)
    if orgs is not None:
        query = query.filter(
            SpdReferralCase.initiator_org_id.in_(orgs)
            | SpdReferralCase.current_org_id.in_(orgs)
        )
    rows = query.order_by(SpdReferralCase.created_at).limit(200).all()
    return {
        "threshold_hours": hours,
        "count": len(rows),
        "items": [_case_out(db, r) for r in rows],
    }
