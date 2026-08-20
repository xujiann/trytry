"""全域慢专病 · 领域服务层：跨路由复用的业务动作。

放在这里而不是各路由内联，理由与 `visibility.scope_patient_list` 同一条：
**没抽出来的正确做法等于没有**。以下五件事分别有 3~6 个调用点，
内联就意味着以后有人只改了其中一处：

1. `build_facts`      —— 汇集患者事实供规则求值（筛查、纳入判定、转诊触发共用）
2. `start_path`/`advance_path` —— 路径实例推进与任务派生
3. `spawn_task`       —— 统一任务生成（路径、随访、干预、复诊都从这里出）
4. `award_points`     —— 村医积分入账（签约、上转、随访、上报四处触发）
5. `close_open_work`  —— 死亡/迁出/排除时终止后续任务（三处生命周期事件共用）
"""
from datetime import date, timedelta

from sqlalchemy.orm import Session

from ..clock import now_naive
from ..concurrency import add_amount
from .platform import diagnosis_codes, diagnosis_names, notify_user, patient_of
from .models import (
    SpdEnrollment,
    SpdIntervention,
    SpdMeasurement,
    SpdPathInstance,
    SpdPathNode,
    SpdPathTemplate,
    SpdPointAccount,
    SpdPointRecord,
    SpdPointRule,
    SpdProgram,
    SpdReferralCase,
    SpdRevisit,
    SpdTarget,
    SpdTask,
)
from .rules import evaluate, judge_level

#: 监测指标在 facts 里的键就是 `SpdMeasurement.metric`，与 `spd/rules.py::FIELD_SOURCES` 对齐。
MEASURE_FIELDS = (
    "bp_sys", "bp_dia", "glucose_fasting", "glucose_pp2h", "hba1c", "ua", "spo2",
    "bmi", "ldl", "creatinine", "egfr",
)


def _age_of(birth_date: str) -> int | None:
    try:
        born = date.fromisoformat(birth_date)
    except (ValueError, TypeError):
        return None
    today = date.today()
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def build_facts(db: Session, patient_id: int, extra: dict | None = None) -> dict:
    """汇集一名患者的事实字典，供纳入/排除/转诊规则求值。

    诊断取自 `platform.diagnosis_codes`（全部历史就诊 + ICD 父目，理由见那里）；
    指标取最近一次值——那才是"现在控制得怎么样"。
    """
    facts: dict = {}
    patient = patient_of(db, patient_id)
    if patient is not None:
        facts["age"] = _age_of(patient.birth_date)
        facts["gender"] = patient.gender
    # 诊断怎么取（全部历史 + ICD 父目）是**平台数据的形状**，实现放适配层，
    # 这里只管把它填进事实字典
    facts["diagnosis"] = diagnosis_codes(db, patient_id)
    facts["diagnosis_name"] = diagnosis_names(db, patient_id)

    for metric in MEASURE_FIELDS:
        latest = (
            db.query(SpdMeasurement)
            .filter(SpdMeasurement.patient_id == patient_id, SpdMeasurement.metric == metric)
            .order_by(SpdMeasurement.measured_at.desc())
            .first()
        )
        if latest is not None:
            facts[metric] = latest.value
    if extra:
        facts.update({k: v for k, v in extra.items() if v is not None})
    return facts


def match_program(db: Session, patient_id: int, program: SpdProgram, extra: dict | None = None):
    """对单个病种做纳入/排除判定，返回 `spd/rules.py::screen` 的结果 + 使用的规则版本。"""
    from .rules import screen

    facts = build_facts(db, patient_id, extra)
    result = screen(program.include_rules or [], program.exclude_rules or [], facts)
    result["program_code"] = program.code
    result["rule_version"] = program.version
    return result


def target_for(db: Session, program_code: str, stage: str, metric: str) -> SpdTarget | None:
    """取某病种某指标的管理目标，三级回落：本阶段 → 不分阶段 → 该病种任一阶段。

    第三级回落是刻意的：患者常常停在"筛查""诊断评估"这类前置阶段，而目标通常
    只配在"治疗干预""稳定期"上。没有回落的话，一个收缩压 178 的筛查期患者
    会被判成"正常"——因为他所在的阶段没配目标。**配了目标就该用上**，
    比"这个阶段没配所以不判"更接近临床预期。
    """
    program = db.query(SpdProgram).filter(SpdProgram.code == program_code).first()
    if program is None:
        return None
    query = db.query(SpdTarget).filter(
        SpdTarget.program_id == program.id,
        SpdTarget.metric == metric,
        SpdTarget.active.is_(True),
    )
    return (
        query.filter(SpdTarget.stage == stage).first()
        or query.filter(SpdTarget.stage == "").first()
        or query.order_by(SpdTarget.id).first()
    )


def judge_measurement(db: Session, program_code: str, stage: str, metric: str, value) -> str:
    """按管理目标判定单次监测值的等级。没有目标就是 normal，见 `spd/rules.py::judge_level`。"""
    target = target_for(db, program_code, stage, metric)
    if target is None:
        return "normal"
    return judge_level(value, target.target_low, target.target_high)


def spawn_task(
    db: Session,
    *,
    patient_id: int,
    title: str,
    task_type: str = "followup",
    program_code: str = "",
    enrollment: SpdEnrollment | None = None,
    instance: SpdPathInstance | None = None,
    node: SpdPathNode | None = None,
    assignee_id: int | None = None,
    org_id: int | None = None,
    team_id: int | None = None,
    due_days: int = 7,
    priority: int = 1,
    source: str = "auto",
    form_code: str = "",
    require_evidence: bool = False,
) -> SpdTask:
    """生成一条统一任务。**所有任务都从这里出**，包括手工建的。

    责任人缺省顺序：显式指定 > 节点执行角色对应的团队成员 > 纳管档案的主管医生。
    找不到人也照样建任务，落成待接收（`pending`）而不是报错——
    "没人认领的任务"在中心端待办里看得见，"没建出来的任务"谁也看不见。
    """
    due = date.today() + timedelta(days=max(due_days, 0))
    task = SpdTask(
        program_code=program_code or (enrollment.program_code if enrollment else ""),
        patient_id=patient_id,
        enrollment_id=enrollment.id if enrollment else None,
        instance_id=instance.id if instance else None,
        node_key=node.key if node else "",
        task_type=task_type,
        title=title,
        org_id=org_id or (enrollment.org_id if enrollment else None),
        team_id=team_id or (enrollment.team_id if enrollment else None),
        assignee_id=assignee_id or (enrollment.doctor_user_id if enrollment else None),
        exec_role=node.exec_role if node else "",
        status="pending",
        priority=priority,
        due_date=due.isoformat(),
        form_code=form_code or (node.form_code if node else ""),
        require_evidence=require_evidence or (node.require_evidence if node else False),
        source=source,
    )
    db.add(task)
    db.flush()
    return task


def start_path(
    db: Session, enrollment: SpdEnrollment, template: SpdPathTemplate, owner_user_id: int | None
) -> SpdPathInstance:
    """按模板为患者启动路径实例，并生成首节点任务。

    只允许引用**已发布**的模板：草稿模板还在改，引用它等于让患者跟着草稿走。
    """
    if template.status != "published":
        raise ValueError("只能引用已发布的路径模板")
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == template.id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    if not nodes:
        raise ValueError("路径模板没有节点")
    first = nodes[0]
    instance = SpdPathInstance(
        enrollment_id=enrollment.id,
        template_id=template.id,
        template_code=template.code,
        current_node_key=first.key,
        current_stage=first.stage,
        status="running",
        owner_user_id=owner_user_id,
    )
    db.add(instance)
    db.flush()
    spawn_task(
        db,
        patient_id=enrollment.patient_id,
        title=f"{template.name}·{first.name}",
        task_type="path",
        enrollment=enrollment,
        instance=instance,
        node=first,
        due_days=first.due_days,
        source="path",
    )
    return instance


def advance_path(db: Session, instance: SpdPathInstance) -> dict:
    """节点完成后推进到下一节点，并派生下一节点任务。

    下一节点取 `next_key`，没配就按 `seq` 顺延——两种编排方式在实施期都会
    出现（有人画流程图连线，有人就想要个清单），支持一种会被另一种绊住。
    """
    nodes = (
        db.query(SpdPathNode)
        .filter(SpdPathNode.template_id == instance.template_id)
        .order_by(SpdPathNode.seq, SpdPathNode.id)
        .all()
    )
    if not nodes:
        return {"status": instance.status, "current_node_key": instance.current_node_key}
    by_key = {n.key: n for n in nodes}
    current = by_key.get(instance.current_node_key)
    nxt = None
    if current is not None:
        if current.next_key:
            nxt = by_key.get(current.next_key)
        else:
            index = nodes.index(current)
            nxt = nodes[index + 1] if index + 1 < len(nodes) else None

    done = sum(
        1
        for t in db.query(SpdTask)
        .filter(SpdTask.instance_id == instance.id, SpdTask.task_type == "path")
        .all()
        if t.status == "done"
    )
    instance.progress = min(int(done / len(nodes) * 100), 100)

    if nxt is None:
        instance.status = "completed"
        instance.current_node_key = ""
        instance.finished_at = now_naive()
        instance.progress = 100
        return {"status": "completed", "current_node_key": ""}

    instance.current_node_key = nxt.key
    instance.current_stage = nxt.stage
    enrollment = db.get(SpdEnrollment, instance.enrollment_id)
    template = db.get(SpdPathTemplate, instance.template_id)
    if enrollment is None:
        return {"status": instance.status, "current_node_key": nxt.key, "next_node": nxt.name}

    # P1-4：进入条件在**自动流转**时也生效。此前只有显式端点会校验，
    # 条件配了却拦不住自动派单，配置形同虚设。
    # 不满足时**暂停并通知**，不静默跳过——静默跳过的表现是
    # "路径停在那里且没人知道为什么"。
    allowed, _matched = node_enter_allowed(db, instance, nxt)
    if not allowed:
        instance.status = "paused"
        if enrollment.doctor_user_id is not None:
            notify_user(
                db, enrollment.doctor_user_id, category="spd_path",
                title="专病路径已暂停",
                body=f"患者路径进入「{nxt.name}」的条件未满足，已暂停；"
                     "条件满足后可在路径页手工推进恢复",
                link_type="spd_path_instance", link_id=instance.id,
            )
        return {"status": "paused", "current_node_key": nxt.key,
                "next_node": nxt.name, "paused_reason": "进入条件未满足"}

    if nxt.stage:
        enrollment.stage = nxt.stage
    spawn_task(
        db,
        patient_id=enrollment.patient_id,
        title=f"{template.name if template else '路径'}·{nxt.name}",
        task_type="path",
        enrollment=enrollment,
        instance=instance,
        node=nxt,
        due_days=nxt.due_days,
        source="path",
    )
    return {"status": instance.status, "current_node_key": nxt.key, "next_node": nxt.name}


def node_enter_allowed(db: Session, instance: SpdPathInstance, node: SpdPathNode) -> tuple[bool, list]:
    """判断患者是否满足某节点的进入条件。空条件视为可进入。"""
    if not node.enter_condition:
        return True, []
    enrollment = db.get(SpdEnrollment, instance.enrollment_id)
    if enrollment is None:
        return False, []
    facts = build_facts(
        db,
        enrollment.patient_id,
        {"risk_level": enrollment.risk_level, "stage": enrollment.stage},
    )
    return evaluate(node.enter_condition, facts, mode="all")


def award_points(
    db: Session,
    user_id: int | None,
    event: str,
    *,
    ref_type: str = "",
    ref_id: int | None = None,
    note: str = "",
    org_id: int | None = None,
) -> SpdPointRecord | None:
    """按积分规则给村医入账，命中每日上限则不入账并返回 None。

    每日上限按"当天该规则已入账分值"算而不是"次数"：规则里配的是分值上限
    （`daily_limit` 单位是分），这样调整单次分值时不用同时调次数上限。
    """
    if user_id is None:
        return None
    rule = (
        db.query(SpdPointRule)
        .filter(SpdPointRule.event == event, SpdPointRule.active.is_(True))
        .first()
    )
    if rule is None:
        return None
    account = db.query(SpdPointAccount).filter(SpdPointAccount.user_id == user_id).first()
    if account is None:
        account = SpdPointAccount(user_id=user_id, org_id=org_id, balance=0, earned=0, used=0)
        db.add(account)
        db.flush()
    if rule.daily_limit:
        today = date.today().isoformat()
        earned_today = sum(
            r.points
            for r in db.query(SpdPointRecord)
            .filter(
                SpdPointRecord.account_id == account.id,
                SpdPointRecord.rule_code == rule.code,
                SpdPointRecord.direction == "in",
            )
            .all()
            if r.created_at.date().isoformat() == today
        )
        if earned_today + rule.points > rule.daily_limit:
            return None
    # 与签到、兑换同一口径：积分入账一律走原子 UPDATE，不做读-改-写
    add_amount(db, SpdPointAccount, account.id, "balance", rule.points)
    add_amount(db, SpdPointAccount, account.id, "earned", rule.points)
    db.flush()
    db.refresh(account)
    account.updated_at = now_naive()
    record = SpdPointRecord(
        account_id=account.id, rule_code=rule.code, direction="in", points=rule.points,
        balance_after=account.balance, ref_type=ref_type, ref_id=ref_id,
        note=note or rule.name,
    )
    db.add(record)
    db.flush()
    return record


def close_open_work(db: Session, enrollment: SpdEnrollment, reason: str) -> dict:
    """终止一名患者在该病种下的全部未完成任务、路径、干预与复诊。

    死亡 / 迁出 / 排除三处生命周期事件共用。**不删除记录**，只置为取消并写明理由：
    删掉等于把"这个人曾经被管过"一并抹掉，考核与追溯都会对不上。
    """
    stats = {"tasks": 0, "instances": 0, "interventions": 0, "revisits": 0}
    tasks = (
        db.query(SpdTask)
        .filter(
            SpdTask.enrollment_id == enrollment.id,
            SpdTask.status.in_(["pending", "claimed", "doing", "submitted", "overdue"]),
        )
        .all()
    )
    for task in tasks:
        task.status = "cancelled"
        task.review_note = reason
        task.finished_at = now_naive()
        stats["tasks"] += 1

    instances = (
        db.query(SpdPathInstance)
        .filter(SpdPathInstance.enrollment_id == enrollment.id,
                SpdPathInstance.status == "running")
        .all()
    )
    for instance in instances:
        instance.status = "cancelled"
        instance.finished_at = now_naive()
        stats["instances"] += 1

    interventions = (
        db.query(SpdIntervention)
        .filter(
            SpdIntervention.enrollment_id == enrollment.id,
            SpdIntervention.status.in_(["planned", "doing"]),
        )
        .all()
    )
    for item in interventions:
        item.status = "removed"
        stats["interventions"] += 1

    revisits = (
        db.query(SpdRevisit)
        .filter(
            SpdRevisit.patient_id == enrollment.patient_id,
            SpdRevisit.program_code == enrollment.program_code,
            SpdRevisit.status == "planned",
        )
        .all()
    )
    for revisit in revisits:
        revisit.status = "removed"
        revisit.log = (revisit.log or []) + [{"at": date.today().isoformat(), "note": reason}]
        stats["revisits"] += 1
    return stats


def sweep_overdue(db: Session, today: date | None = None) -> dict:
    """三类超期一次扫：任务、复诊、随访。到期未办的置为 overdue。

    做成一个函数供定时任务与"进页面时刷新"两处调用：
    只靠定时任务，演示环境没开调度就永远看不到超期；
    只靠进页面刷新，没人进页面的机构就永远不超期。

    P0-2 之前只扫任务——复诊与随访的"逾期"是各查询现场用日期比出来的，
    督办清单按现算、考核取数按 status，两边数字对不上，而考核数字要进绩效。
    现在三类都落 status，查询一律按 status 过滤，口径只剩一个。

    随访的 `unreachable`（失访）**不会**被覆盖成 overdue：失访是执行过但没联系上，
    完成率的分母含它、分子不含；标成超期等于把"打过电话"抹掉了。
    """
    today = today or date.today()
    cutoff = today.isoformat()
    pending = (
        db.query(SpdTask)
        .filter(
            SpdTask.status.in_(["pending", "claimed", "doing"]),
            SpdTask.due_date != "",
            SpdTask.due_date < cutoff,
        )
        .all()
    )
    escalated = 0
    for task in pending:
        task.status = "overdue"
        node = None
        if task.instance_id and task.node_key:
            instance = db.get(SpdPathInstance, task.instance_id)
            if instance is not None:
                node = (
                    db.query(SpdPathNode)
                    .filter(
                        SpdPathNode.template_id == instance.template_id,
                        SpdPathNode.key == task.node_key,
                    )
                    .first()
                )
        if node is not None and node.timeout_action == "escalate":
            task.escalated = True
            task.priority = max(task.priority, 2)
            escalated += 1

    # 复诊：plan_date 已过且仍是 planned → overdue，并写日志（谁标的、何时标的）
    from .models import SpdFollowupRecord

    overdue_revisits = (
        db.query(SpdRevisit)
        .filter(SpdRevisit.status == "planned", SpdRevisit.plan_date != "",
                SpdRevisit.plan_date < cutoff)
        .all()
    )
    for revisit in overdue_revisits:
        revisit.status = "overdue"
        revisit.log = (revisit.log or []) + [
            {"at": cutoff, "note": "超期扫描：计划日期已过，置为逾期"}
        ]

    # 随访：只动 planned；unreachable / removed / done 一律不碰
    overdue_followups = (
        db.query(SpdFollowupRecord)
        .filter(SpdFollowupRecord.status == "planned", SpdFollowupRecord.planned_at != "",
                SpdFollowupRecord.planned_at < cutoff)
        .all()
    )
    for record in overdue_followups:
        record.status = "overdue"

    return {
        "overdue": len(pending),
        "escalated": escalated,
        "revisits": len(overdue_revisits),
        "followups": len(overdue_followups),
    }


# ---------------------------------------------------------------------------
# 居民端转诊读侧聚合的 spd 源（ADR-0003 方案 B）
# ---------------------------------------------------------------------------

#: `spd_referral_cases.status` → 中文。措辞与居民端 `static/m/m.js` 的 `SPD_REF_TEXT`
#: **逐字一致**：口径统一的意义就在于只有一套说法，后端另造一套只会让同一个状态
#: 在两个页面读起来不一样。
#:
#: 与平台 `referrals` 存在**同名不同义**：平台 `accepted` 是"已接收"（两点之间
#: 那一次转诊被对方接了），这里是"县级医院已接收"（村→乡→县链路走到了县级）。
#: 所以聚合列表里每条都带 `source`、标签分源映射，不能合并成一张表。
_STATUS_LABELS = {
    "submitted": "村医已发起，待卫生院审核",
    # 存量兼容，新单不再产生（ADR-0005）
    "station_reviewed": "服务站已复核，待卫生院审核(存量)",
    "township_reviewed": "卫生院已审核，待县级医院接收",
    "accepted": "县级医院已接收",
    "arrived": "已到院就诊",
    "down_referred": "已下转基层",
    "followup_received": "下转随访已接收",
    "closed": "已完成闭环",
    "rejected": "已退回",
    "withdrawn": "已撤回",
}


def referral_feed(db: Session, patient_id: int) -> list[dict]:
    """把本子系统的转诊单产出成聚合列表的统一形状。

    只读、不改任何状态；`/api/portal/spd/referrals` 那个单源接口原样保留
    （既有契约），这里是并给 `/api/portal/me/referrals/all` 用的另一份视图。
    """
    from .platform import REFERRAL_FEED_LIMIT, org_names as _org_names, referral_feed_item

    rows = (
        db.query(SpdReferralCase)
        .filter(SpdReferralCase.patient_id == patient_id)
        .order_by(SpdReferralCase.id.desc())
        .limit(REFERRAL_FEED_LIMIT)
        .all()
    )
    # 只取这几条单子用到的机构名，不整表拉 organizations
    names = _org_names(db, {r.initiator_org_id for r in rows} | {r.target_org_id for r in rows})
    return [
        referral_feed_item(
            source="spd",
            id=r.id,
            direction=r.direction,
            status=r.status,
            status_label=_STATUS_LABELS.get(r.status, r.status),
            reason=r.reason,
            from_org=names.get(r.initiator_org_id, ""),
            # 目标机构可能尚未确定（逐级审核中），此时留空而不是编一个
            to_org=names.get(r.target_org_id, "") if r.target_org_id else "",
            created_at=r.created_at.isoformat(),
            # 必须带上 patient_id：一个居民账号可以代管家属，详情端点按这个参数
            # 决定看谁的档案，不带就会拿默认患者去查，代管家属的单子直接 404。
            detail_path=f"/api/portal/spd/referrals/{r.id}?patient_id={patient_id}",
        )
        for r in rows
    ]
