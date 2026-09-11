"""慢专病子系统对平台领域事件的订阅。

这是子系统"听平台"的**唯一**入口——平台不知道这里的存在（它只 `publish`），
依赖方向因此仍是单向的。订阅在 `register_spd()` 时完成，子系统关掉就不订阅。

## 两个订阅，两种默认

| 事件 | 动作 | 默认 | 理由 |
|---|---|---|---|
| `admission.discharged` | 按随访方案生成出院随访计划 | **开** | 只有配了诊断关键词的方案才会命中；没配关键词的方案不匹配任何人，所以开着是安全的 |
| `encounter.created` | 按病种纳入规则识别疑似人群 | **关** | 全域自动识别会在生产上产生大量"疑似"记录。诊断数据质量参差时，这批记录会淹没真正要复核的人 |

第二条的开关（`MEDPLAT_SPD_AUTO_IDENTIFY_ON_ENCOUNTER`）交给各县在数据质量达标后再开；
关着的时候，管理端的 `POST /api/spd/screenings/auto-run` 仍可手工触发同一套判定——
**同一个判定只有一处实现**，自动与手工走的是同一段代码。

## 幂等

两个订阅者都会先查"这条已经派生过没有"。事件可能因重试而重复投递，
而重复投递造成的是"同一个病人两份随访计划"——基层只会当成系统出错。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy.orm import Session

from .. import clock
from .. import events
from ..concurrency import insert_if_absent
from ..config import settings
from .models import SpdCandidate, SpdFollowupRecord, SpdFollowupRule, SpdProgram, SpdScreening
from .service import match_program

logger = logging.getLogger("medplat.spd.subscribers")


def on_admission_discharged(db: Session, payload: dict) -> None:
    """出院即按随访方案派生多时间点随访计划（智能随访端 #4/#5）。

    与 `POST /api/spd/followup-plans/auto-match` 是同一套匹配口径，区别只是
    触发时机：这里在出院那一刻，那里是回溯扫描的兜底。
    """
    if not settings.spd_auto_followup_on_discharge:
        return
    patient_id = payload.get("patient_id")
    if not patient_id:
        return
    text = payload.get("diagnosis_name") or ""
    rules = (
        db.query(SpdFollowupRule)
        .filter(SpdFollowupRule.scene == "inpatient", SpdFollowupRule.active.is_(True))
        .all()
    )
    rule = next(
        (
            r for r in rules
            if any(k and k in text for k in (r.diagnosis_keywords or []))
        ),
        None,
    )
    if rule is None:
        return  # 没配关键词的方案不匹配任何人——与 auto-match 同一口径

    exists = (
        db.query(SpdFollowupRecord.id)
        .filter(
            SpdFollowupRecord.patient_id == patient_id,
            SpdFollowupRecord.rule_id == rule.id,
        )
        .first()
    )
    if exists is not None:
        return  # 幂等：同一患者同一方案只派生一次

    try:
        base = date.fromisoformat(payload.get("discharged_on") or "")
    except ValueError:
        base = clock.today()
    for offset in rule.points or []:
        db.add(
            SpdFollowupRecord(
                patient_id=patient_id, program_code=rule.program_code, rule_id=rule.id,
                questionnaire_code=rule.questionnaire_code, scene="inpatient",
                org_id=payload.get("org_id"), dept=rule.dept,
                planned_at=(base + timedelta(days=int(offset))).isoformat(),
                channel="phone", status="planned",
            )
        )
    logger.info("出院事件派生随访计划：patient=%s rule=%s", patient_id, rule.code)


def on_encounter_created(db: Session, payload: dict) -> None:
    """就诊登记即按病种纳入规则识别疑似人群（平台管理端 #8）。

    默认关闭，理由见模块文档。开启后仍**只写疑似**，不写纳管——
    "筛出来 ≠ 管起来"这条口径不因为触发方式变了就松动。
    """
    if not settings.spd_auto_identify_on_encounter:
        return
    patient_id = payload.get("patient_id")
    if not patient_id:
        return
    for program in (
        db.query(SpdProgram)
        .filter(SpdProgram.active.is_(True))
        .all()
    ):
        if not program.include_rules:
            continue
        already = (
            db.query(SpdCandidate.id)
            .filter(
                SpdCandidate.patient_id == patient_id,
                SpdCandidate.program_code == program.code,
            )
            .first()
        )
        if already is not None:
            continue  # 幂等：已在池中（含已纳管）的不重复识别
        matched = match_program(db, patient_id, program)
        if matched["result"] != "suspect":
            continue
        screening = SpdScreening(
            patient_id=patient_id, program_code=program.code, source="import",
            org_id=payload.get("org_id"), risk_level="mid", result="suspect",
            advice="就诊事件触发的规则自动识别",
        )
        db.add(screening)
        db.flush()
        # 上面那次「已在池中就 continue」是幂等判断，但它是 **check-then-act**：
        # 同一患者的两个就诊事件同时到达（门诊与检验回传各触发一次，或事件重投），
        # 两边都查不到、都来插，撞上 `spd_candidates` 的
        # `UniqueConstraint(patient_id, program_code)`。
        #
        # 而这里的 `IntegrityError` **不会**被 `events.publish()` 的 try/except 兜住：
        # 按事件总线契约 1「同事务、只 add 不 commit」，这条 INSERT 推迟到**发布方
        # commit** 时才执行，那时早已离开契约 2 的罩子——于是 500 落在**业务请求**
        # （登记就诊）头上，业务写入一并回滚。契约 2 说「订阅者炸掉不连累业务」，
        # 恰恰被契约 1 的延迟执行绕过去了。已实测复现。
        #
        # `insert_if_absent` 把冲突圈进 SAVEPOINT：撞了只退这一行，业务事务照常提交。
        # 返回 False 表示"并发下别人先插了"，与上面那个 continue 同义。
        # 残留：这种极少数的竞态下上面那条 screening 已 flush、不随之退掉——
        # 它记录的"本次自动筛查判为疑似"确实发生过，多一条不影响纳管，但统计口径上
        # 会多算一次筛查。要连它一起退需要手写 SAVEPOINT 包住两行，那是复制
        # `insert_if_absent` 的逻辑，按 §6 优先复用，这里如实记下取舍。
        if not insert_if_absent(
            db,
            SpdCandidate(
                patient_id=patient_id, program_code=program.code, status="suspect",
                source="event", screening_id=screening.id, org_id=payload.get("org_id"),
                risk_level="mid", matched_rules=matched["matched"],
                reason="；".join(
                    str(m.get("label") or m.get("field")) for m in matched["matched"]
                )[:256],
            ),
        ):
            continue
        logger.info("就诊事件识别疑似人群：patient=%s program=%s", patient_id, program.code)


#: 事件 → 订阅者。装卸时按这张表注册，测试也按它断言"该听的都听上了"。
SUBSCRIPTIONS = (
    (events.ADMISSION_DISCHARGED, on_admission_discharged),
    (events.ENCOUNTER_CREATED, on_encounter_created),
)


def register_subscribers() -> int:
    """注册全部订阅；返回订阅数。重复调用不会重复注册。"""
    registered = 0
    for event, handler in SUBSCRIPTIONS:
        existing = events._subscribers.get(event, [])
        if any(h is handler for h in existing):
            continue
        events.subscribe(event, handler)
        registered += 1
    return registered
