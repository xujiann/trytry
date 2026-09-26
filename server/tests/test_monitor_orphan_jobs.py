"""监控概览把代码里已没有实现的定时任务列为「到点未跑」：改名 / 下线的任务永远挂在超期里（P2-267）。

调度器只跑代码里注册过的任务（`scheduler.due_jobs`：「只认代码里真实注册过的实现」）；改名或下线的任务在 `scheduled_jobs`
里留着一行，next_run_at 永远停在过去。监控概览的「到点未跑」原先按库里的行判，这一行永远在里头——注释写着这一格是用来看
「调度线程是不是死了」的，一条永远在的假超期把真信号淹掉。修法：到点未跑与调度器同一句只数有实现的，没实现的另列在
`unregistered_jobs`（没有就不出这个键）。
"""
from datetime import timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal
from app.models import ScheduledJob


@pytest.fixture()
def orphan():
    with SessionLocal() as db:
        row = ScheduledJob(name="p2267_retired_job", title="已下线的任务", interval_seconds=3600, enabled=True,
                           next_run_at=now_naive() - timedelta(days=3))
        db.add(row)
        db.commit()
    yield "p2267_retired_job"
    with SessionLocal() as db:
        db.query(ScheduledJob).filter_by(name="p2267_retired_job").delete()
        db.commit()


def test_没有实现的任务不算到点未跑_另列出来(client, admin, orphan):
    scheduler = client.get("/api/monitor/overview", headers=admin).json()["scheduler"]
    assert orphan not in scheduler["overdue_jobs"]   # 修前在：永远的假超期
    assert scheduler["unregistered_jobs"] == [orphan]


def test_没有孤儿任务时不出这个键(client, admin):
    scheduler = client.get("/api/monitor/overview", headers=admin).json()["scheduler"]
    assert "unregistered_jobs" not in scheduler
