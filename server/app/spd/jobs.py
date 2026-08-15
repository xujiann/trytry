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
