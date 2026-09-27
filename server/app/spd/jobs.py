"""慢专病子系统的定时任务。

任务**注册在子系统内**、只用平台的调度基础设施（`scheduler.register`），
依赖方向因此仍是单向的：平台的 `app/jobs.py` 不知道慢专病的存在，
子系统关掉时这些任务（数据源同步、超期扫描、报告推送、宣教定时派发）连注册都不会发生。

与平台既有任务同一约定：`def job(db) -> (处理对象数, 结果摘要)`，
查询口径复用业务侧的实现，不在这里另写一套判定。
"""
from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import clock
from ..scheduler import register
from .platform import broadcast


@register("spd_data_source_sync", "慢专病数据源同步", 300)
def spd_data_source_sync(db: Session) -> tuple[int, str]:
    """按各数据源自己的 `freq_minutes` 跑到期的采集，成败都写同步日志。

    所有源用同一个周期是不合适的：HIS 可能要 5 分钟一次，体检系统一天一次就够。
    调度按最小粒度（5 分钟）唤醒，到期与否由 `run_due_sources` 逐源判定。
    """
    from .collectors import run_due_sources_counted

    count, failed, summary = run_due_sources_counted(db)
    # 只推失败的（P2-506）：原先把「这一轮跑了几个源」当提醒推——内置页面不开 WebSocket，没配 Redis 时广播恒不达，
    # 于是每 5 分钟一条「慢专病数据源同步：N 条（无在线管理端，广播未送达）」运维告警（冷却 10 分钟，一天约 144 条），
    # 全部成功与有源失败一字不差，真出了故障反倒淹没在里面。全部成功不推
    if failed:
        broadcast("spd_sync_failed", "慢专病数据源同步失败", failed)
    return count, summary


@register("spd_task_overdue_scan", "慢专病任务超期扫描", 3600)
def spd_task_overdue_scan(db: Session) -> tuple[int, str]:
    """任务超期与升级扫描；同一趟也把过了日期的复诊、随访标成超期。

    与各端工作台进页面时的刷新是**同一个** `sweep_overdue`，两处都要有：
    只靠定时任务，演示环境没开调度就永远看不到超期；只靠进页面刷新，
    没人进页面的机构就永远不超期。

    处理数与摘要把复诊、随访也算上（P2-377）：原先只报任务，这一趟标了超期的复诊、随访在调度日志里查不到。
    推送频道（`spd_task_overdue`）仍只报任务数——它是任务超期的提醒。
    """
    from .service import sweep_overdue

    result = sweep_overdue(db)
    broadcast("spd_task_overdue", "慢专病任务超期", result["overdue"])
    total = result["overdue"] + result["revisits"] + result["followups"]
    return total, (f"任务超期 {result['overdue']} 条（其中升级 {result['escalated']} 条），"
                   f"复诊超期 {result['revisits']} 条，随访超期 {result['followups']} 条")


@register("spd_report_push", "慢专病报告推送", 300)
def spd_report_push(db: Session) -> tuple[int, str]:
    """按推送任务的频率与时点生成报告并投递订阅人（P0-1）。

    幂等口径：同一任务同一 `period_label`（含机构后缀）只生成一次——
    调度五分钟醒一次，一天要醒近三百次，靠"这次生成过了没有"判重，
    不靠"现在是不是正好那一分钟"。
    """

    from ..clock import now_local, now_naive
    from .models import SpdReportInstance, SpdReportTask, SpdReportTemplate
    from .platform import Organization, User, notify_user
    from .reporting import compose_section, default_period_label

    now = now_naive()
    today = clock.today().isoformat()
    # 推送时点（「08:00」）是人填的本地钟点，与本地时刻比（P2-215）；原先与 UTC 比，东八区要到下午 4 点才过「08:00」。
    # 落库的 last_run_at 仍记 UTC（DateTime 列一律 naive UTC）
    local_hhmm = now_local().strftime("%H:%M")
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
        if local_hhmm < push_time:
            continue  # 今天还没到推送时点
        template = db.get(SpdReportTemplate, task.template_id)
        if template is None or not template.active:
            continue
        period_label = default_period_label(template.period)
        # 每个绑定机构一份；没绑机构就出一份全域的。存量里悬空的机构与订阅人跳过（P1-124）：实例的机构、站内消息的
        # 收件人都是外键，写进去就撞约束、整轮回滚，所有任务的报告都不出。全悬空的一份也不出——不退回全域
        if task.org_ids:
            live_orgs = {i for (i,) in db.query(Organization.id).filter(Organization.id.in_(task.org_ids))}
            org_ids: list[int | None] = [o for o in task.org_ids if o in live_orgs]
        else:
            org_ids = [None]
        subscribers = task.subscriber_ids or []
        live_users = {i for (i,) in db.query(User.id).filter(User.id.in_(subscribers))} if subscribers else set()
        for org_id in org_ids:
            label = period_label if org_id is None else f"{period_label}·机构{org_id}"
            exists = (
                db.query(SpdReportInstance.id)
                .filter(
                    SpdReportInstance.task_id == task.id,
                    SpdReportInstance.period_label == label,
                    # 判重连机构一起判（P2-255）：「立即执行」（`POST /report-instances`）按本人机构出一份、期间标签不带
                    # 机构后缀，原先只比标签——没绑机构的全域任务，谁在本机构点一下立即执行，当期的全域报告就被当成
                    # 「已出过」，定时推送不再生成、订阅人也收不到
                    SpdReportInstance.org_id.is_(None) if org_id is None else SpdReportInstance.org_id == org_id,
                    # 只认定时推送自己出的（P2-531）：没有所属机构的管理员 / 主任点「立即执行」，出的那份正好是机构为空、
                    # 标签不带后缀——P2-255 按机构区分挡不住它，当期推送照样被当成「已出过」、订阅人收不到
                    SpdReportInstance.manual.is_(False),
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
            for user_id in (u for u in subscribers if u in live_users):
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
    from ..clock import now_local
    from .models import SpdEduMaterial, SpdEduPush
    from .routers.care import dispatch_edu_push

    # 到点与否按本地时刻比（P2-215）：`send_at` 是页面上 datetime-local 手填的本地时间，原先拿 UTC 去比，
    # 东八区约好晚上 8 点推的宣教，要到次日凌晨 4 点才发出去
    cutoff = now_local().strftime("%Y-%m-%d %H:%M:%S")
    due = (
        db.query(SpdEduPush)
        # 按字符串比较到点：`T` 写法先换成空格再比——同一天里 `T` 排在空格之后，原先晚到第二天零点（P1-100）
        .filter(SpdEduPush.status == "pending", func.replace(SpdEduPush.send_at, "T", " ") <= cutoff)
        .order_by(SpdEduPush.id)
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
        # 素材停用了就不再发（P2-252）：立即推送对停用的素材 404「宣教素材不存在或已停用」，定时派发原先不看——约好的
        # 推送照样把停用（内容过时、有误而撤下）的素材发到患者手机上。如实置失败并写明原因，推送清单上看得见
        if material is None or not material.active:
            push.status = "failed"
            push.fail_reason = "宣教素材不存在" if material is None else "宣教素材已停用，未发送"
            failed += 1
        elif dispatch_edu_push(db, push, material):
            sent += 1
        else:
            failed += 1
        # 发一条提交一条（P2-641）：原先一轮最多 500 条短信 / 微信都在同一个事务里发、由调度器最后一次提交——中途中断
        # （进程被杀、库连接断）整轮回滚成「待发送」，已经送到患者手机上的，下一轮再发一遍；短信通道单条最长等 5 秒，
        # 这个事务还能挂着锁半小时以上。逐条提交之后，中断只可能重发正在发的那一条（与 ESB 出站逐条提交同一个做法）
        db.commit()
    return len(due), f"派发 {sent} 条，失败 {failed} 条" if due else "没有到点的推送"
