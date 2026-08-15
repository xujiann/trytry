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

from .clock import now_naive
from .concurrency import add_amount
from .models import Encounter, Patient
from .models_spd import (
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
    SpdRevisit,
    SpdTarget,
    SpdTask,
)
from .spd_rules import evaluate, judge_level

#: 监测指标在 facts 里的键就是 `SpdMeasurement.metric`，与 `spd_rules.FIELD_SOURCES` 对齐。
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

    诊断取**全部历史就诊**的诊断编码而不是最近一次：慢病的纳入依据是
    "曾被确诊"，按最近一次就诊判定会让一个来看感冒的高血压患者掉出目标池。
    指标取最近一次值——那才是"现在控制得怎么样"。
    """
    facts: dict = {}
    patient = db.get(Patient, patient_id)
    if patient is not None:
        facts["age"] = _age_of(patient.birth_date)
        facts["gender"] = patient.gender
    diagnoses, names = [], []
    for enc in db.query(Encounter).filter(Encounter.patient_id == patient_id).all():
        if getattr(enc, "diagnosis_code", ""):
            diagnoses.append(enc.diagnosis_code)
            # ICD-10 亚目（I10.x）也要能被父目规则命中，否则规则得逐个亚目列全
            diagnoses.append(str(enc.diagnosis_code).split(".")[0])
        if getattr(enc, "diagnosis_name", ""):
            names.append(enc.diagnosis_name)
    facts["diagnosis"] = list(dict.fromkeys(diagnoses))
    facts["diagnosis_name"] = names

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
    """对单个病种做纳入/排除判定，返回 `spd_rules.screen` 的结果 + 使用的规则版本。"""
    from .spd_rules import screen

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
    """按管理目标判定单次监测值的等级。没有目标就是 normal，见 `spd_rules.judge_level`。"""
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
    if enrollment is not None:
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
    """把到期未办的任务置为超期，并按节点的 `timeout_action` 决定是否升级。

    做成一个函数供定时任务与"进页面时刷新"两处调用：
    只靠定时任务，演示环境没开调度就永远看不到超期；
    只靠进页面刷新，没人进页面的机构就永远不超期。
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
    return {"overdue": len(pending), "escalated": escalated}
