"""改定时任务的间隔要重排下次到期；执行历史能按任务 / 结果筛（P2-467）。

① `next_run_at` 是上次执行时按旧间隔算好落库的，`PATCH /api/jobs/{name}` 原先只改间隔不动它：日跑的任务改成每小时，
   照旧要等到明天这个点才跑下一次，而运维手册「超过间隔 3 倍未执行即告警」按新间隔算，3 小时后就误报。
   修后取「原定到期」与「上次执行 + 新间隔」中早的那个：改短了提前，改长了不把排好的这一次往后推。
② 默认任务每小时跑百余次，执行历史只看最新 50 条，日跑任务的失败记录半小时就滚出这一页；接口早就收
   `job_name` / `status`，页面补上两个筛选。
"""
import os
from datetime import datetime, timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal
from app.models import ScheduledJob

STATIC = os.path.join(os.path.dirname(__file__), "..", "app", "static")


@pytest.fixture()
def job(client, admin):
    """库里第一个任务，改成「10 分钟前跑过、日跑、明天这个点到期」；跑完复原。"""
    name = client.get("/api/jobs", headers=admin).json()[0]["name"]
    with SessionLocal() as db:
        row = db.query(ScheduledJob).filter(ScheduledJob.name == name).one()
        saved = (row.interval_seconds, row.last_run_at, row.next_run_at)
        last = now_naive().replace(microsecond=0) - timedelta(minutes=10)
        row.interval_seconds, row.last_run_at, row.next_run_at = 86400, last, last + timedelta(days=1)
        db.commit()
    yield name, last
    with SessionLocal() as db:
        row = db.query(ScheduledJob).filter(ScheduledJob.name == name).one()
        row.interval_seconds, row.last_run_at, row.next_run_at = saved
        db.commit()


def _next_run(name) -> datetime:
    with SessionLocal() as db:
        return db.query(ScheduledJob).filter(ScheduledJob.name == name).one().next_run_at


def test_改短间隔_下次到期按新间隔提前(client, admin, job):
    name, last = job
    resp = client.patch(f"/api/jobs/{name}", headers=admin, json={"interval_seconds": 3600})
    assert resp.status_code == 200, resp.text
    assert _next_run(name) == last + timedelta(hours=1)   # 修前仍是明天这个点


def test_改长间隔_不把排好的这一次往后推(client, admin, job):
    name, last = job
    assert client.patch(f"/api/jobs/{name}", headers=admin, json={"interval_seconds": 172800}).status_code == 200
    assert _next_run(name) == last + timedelta(days=1)


def test_只改启停_下次到期不动(client, admin, job):
    name, last = job
    assert client.patch(f"/api/jobs/{name}", headers=admin, json={"enabled": True}).status_code == 200
    assert _next_run(name) == last + timedelta(days=1)


def test_改短到已经过点_下一轮调度就到期(client, admin, job):
    from app.scheduler import due_jobs

    name, _last = job
    assert client.patch(f"/api/jobs/{name}", headers=admin, json={"interval_seconds": 300}).status_code == 200
    with SessionLocal() as db:
        assert name in due_jobs(db)   # 10 分钟前跑过、改成 5 分钟一次：已过点


def test_执行历史按任务与结果筛():
    with open(os.path.join(STATIC, "pages-mgmt.js"), encoding="utf-8") as fh:
        source = fh.read()
    start = source.index("async function renderJobs()")
    body = source[start:source.index("\nasync function ", start + 1)]
    assert 'api(`/api/jobs/runs${runQuery.toString() ? `?${runQuery}` : ""}`)' in body   # 修前只取最新 50 条、不筛
    assert "new URLSearchParams(Object.entries(JOB_RUN_FILTER)" in body
    assert '<select name="job_name">' in body and '<select name="status">' in body
    assert '$("#job-run-filter").onchange = (e) => { JOB_RUN_FILTER[e.target.name] = e.target.value; route(); };' in body
    # 筛选只留在内存里：任务名会改名下线，存进 localStorage 的旧名字下次进来就对不上下拉框
    assert "const JOB_RUN_FILTER = { job_name: \"\", status: \"\" };" in source
    assert "medplat_job_runs" not in source
