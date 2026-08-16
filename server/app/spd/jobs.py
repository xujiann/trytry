"""慢专病子系统的定时任务。

任务**注册在子系统内**、只用平台的调度基础设施（`scheduler.register`），
依赖方向因此仍是单向的：平台的 `app/jobs.py` 不知道慢专病的存在，
子系统关掉时这两个任务连注册都不会发生。

与平台既有任务同一约定：`def job(db) -> (处理对象数, 结果摘要)`，
查询口径复用业务侧的实现，不在这里另写一套判定。
"""
from sqlalchemy.orm import Session

from ..scheduler import register
from .platform import broadcast


@register("spd_data_source_sync", "慢专病数据源同步", 300)
def spd_data_source_sync(db: Session) -> tuple[int, str]:
    """按各数据源自己的 `freq_minutes` 跑到期的采集，成败都写同步日志。

    所有源用同一个周期是不合适的：HIS 可能要 5 分钟一次，体检系统一天一次就够。
    调度按最小粒度（5 分钟）唤醒，到期与否由 `run_due_sources` 逐源判定。
    """
    from .collectors import run_due_sources

    count, summary = run_due_sources(db)
    if count:
        broadcast("spd_sync", "慢专病数据源同步", count)
    return count, summary


@register("spd_task_overdue_scan", "慢专病任务超期扫描", 3600)
def spd_task_overdue_scan(db: Session) -> tuple[int, str]:
    """任务超期与升级扫描。

    与各端工作台进页面时的刷新是**同一个** `sweep_overdue`，两处都要有：
    只靠定时任务，演示环境没开调度就永远看不到超期；只靠进页面刷新，
    没人进页面的机构就永远不超期。
    """
    from .service import sweep_overdue

    result = sweep_overdue(db)
    broadcast("spd_task_overdue", "慢专病任务超期", result["overdue"])
    return result["overdue"], f"超期 {result['overdue']} 条，其中升级 {result['escalated']} 条"


def _frequency_due(frequency: str, now) -> bool:
    """任务频率是否在今天到期。

    daily/custom：每天到点即推（去重仍由 period_label 兜住）；
    weekly：只在周一推；monthly：只在每月 1 号推。
    原先 frequency 完全未被消费，"每周推送"配置形同虚设。
    """
    if frequency == "weekly":
        return now.weekday() == 0
    if frequency == "monthly":
        return now.day == 1
    return True


@register("spd_report_push", "慢专病报告推送", 300)
def spd_report_push(db: Session) -> tuple[int, str]:
    """按推送任务的频率与时点生成报告并投递订阅人（P0-1）。

    幂等口径：同一任务同一 `period_label`（含机构后缀）只生成一次——
    调度五分钟醒一次，一天要醒近三百次，靠"这次生成过了没有"判重，
    不靠"现在是不是正好那一分钟"。
    """
    from datetime import date as _date

    from ..clock import now_naive
    from .models import SpdReportInstance, SpdReportTask, SpdReportTemplate
    from .platform import notify_user
    from .reporting import compose_section, default_period_label

    now = now_naive()
    today = _date.today().isoformat()
    generated = 0
    tasks = (
        db.query(SpdReportTask)
        .filter(SpdReportTask.status == "active")
        .order_by(SpdReportTask.priority, SpdReportTask.id)
        .all()
    )
    for task in tasks:
        if task.valid_from and today < task.valid_from:
            continue
        if task.valid_to and today > task.valid_to:
            continue
        push_time = task.push_time or "08:00"
        if now.strftime("%H:%M") < push_time:
            continue  # 今天还没到推送时点
        if not _frequency_due(task.frequency, now):
            continue  # 频率未到（周报只在周一、月报只在 1 号推），原先 frequency 形同虚设
        template = db.get(SpdReportTemplate, task.template_id)
        if template is None or not template.active:
            continue
        period_label = default_period_label(template.period)
        # 每个绑定机构一份；没绑机构就出一份全域的
        org_ids = task.org_ids or [None]
        for org_id in org_ids:
            label = period_label if org_id is None else f"{period_label}·机构{org_id}"
            exists = (
                db.query(SpdReportInstance.id)
                .filter(
                    SpdReportInstance.task_id == task.id,
                    SpdReportInstance.period_label == label,
                )
                .first()
            )
            if exists is not None:
                continue
            content = {
                "period_label": period_label,
                "sections": [
                    compose_section(db, section, org_id, template.period)
                    for section in template.sections or []
                ],
            }
            instance = SpdReportInstance(
                task_id=task.id, template_code=template.code,
                title=f"{template.name}（{period_label}）", period_label=label,
                scope_level=template.scope_level, org_id=org_id, content=content,
                subscriber_ids=task.subscriber_ids or [],
            )
            db.add(instance)
            db.flush()
            for user_id in task.subscriber_ids or []:
                notify_user(
                    db, user_id, category="spd_report",
                    title=f"报告已生成：{instance.title}",
                    body="可在「智能辅助报告端」查看",
                    link_type="spd_report_instance", link_id=instance.id,
                )
            generated += 1
        task.last_run_at = now
    return generated, f"生成报告 {generated} 份" if generated else "没有到期的推送任务"


@register("spd_edu_push_dispatch", "慢专病宣教定时派发", 300)
def spd_edu_push_dispatch(db: Session) -> tuple[int, str]:
    """把到点的 pending 宣教推送真的发出去（P0-4 的定时部分）。

    与立即推送共用 `dispatch_edu_push`——同一个动作只有一份实现，
    失败置 failed 并可回溯，不静默置 sent。
    """
    from ..clock import now_naive
    from .models import SpdEduMaterial, SpdEduPush
    from .routers.care import dispatch_edu_push

    cutoff = now_naive().strftime("%Y-%m-%d %H:%M:%S")
    due = (
        db.query(SpdEduPush)
        .filter(SpdEduPush.status == "pending", SpdEduPush.send_at <= cutoff)
        .limit(500)
        .all()
    )
    sent = failed = 0
    materials = {
        m.id: m
        for m in db.query(SpdEduMaterial)
        .filter(SpdEduMaterial.id.in_({p.material_id for p in due} or {0}))
        .all()
    }
    for push in due:
        material = materials.get(push.material_id)
        if material is None:
            push.status = "failed"
            failed += 1
            continue
        if dispatch_edu_push(db, push, material):
            sent += 1
        else:
            failed += 1
    return len(due), f"派发 {sent} 条，失败 {failed} 条" if due else "没有到点的推送"
