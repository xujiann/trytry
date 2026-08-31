"""阶段一：定时任务基座、内置任务、限流器行为。"""
from datetime import date, datetime, timedelta, timezone

from app.models import ChronicPatient, JobRun, ScheduledJob, SmsCode
from app.scheduler import REGISTRY, due_jobs, run_job, sync_registry, tick
from app.state_store import SlidingWindowRateLimiter


def _naive_now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------- 注册与同步


def test_registry_populated_on_startup(client, admin):
    rows = client.get("/api/jobs", headers=admin).json()
    names = {r["name"] for r in rows}
    assert names == set(REGISTRY)
    assert all(r["implemented"] for r in rows)


def test_sync_registry_is_idempotent(client):
    from app.database import SessionLocal

    with SessionLocal() as db:
        before = db.query(ScheduledJob).count()
        sync_registry(db)
        sync_registry(db)
        assert db.query(ScheduledJob).count() == before


def test_sync_registry_keeps_operator_tuning(client, admin):
    """运维在库里调过的间隔/启停，重新同步不得被代码默认值冲掉。"""
    from app.database import SessionLocal

    client.patch(
        "/api/jobs/sms_code_cleanup", json={"interval_seconds": 600, "enabled": False}, headers=admin
    )
    with SessionLocal() as db:
        sync_registry(db)
        job = db.query(ScheduledJob).filter(ScheduledJob.name == "sms_code_cleanup").first()
        assert job.interval_seconds == 600 and job.enabled is False
        job.enabled = True  # 复原，避免影响后续用例
        db.commit()


# ---------------------------------------------------------------- 到期判定与执行


def test_due_jobs_respects_next_run_and_enabled(client):
    from app.database import SessionLocal

    with SessionLocal() as db:
        for job in db.query(ScheduledJob).all():
            job.next_run_at = _naive_now() + timedelta(hours=1)
            job.enabled = True
        db.commit()
        assert due_jobs(db) == []

        target = db.query(ScheduledJob).filter(ScheduledJob.name == "chronic_overdue_scan").first()
        target.next_run_at = _naive_now() - timedelta(seconds=1)
        db.commit()
        assert due_jobs(db) == ["chronic_overdue_scan"]

        target.enabled = False
        db.commit()
        assert due_jobs(db) == []
        target.enabled = True
        db.commit()


def test_run_job_records_run_and_advances_next_run(client):
    from app.database import SessionLocal

    with SessionLocal() as db:
        run = run_job(db, "sms_code_cleanup", trigger="manual")
        assert run.status == "succeeded"
        assert run.duration_ms >= 0
        job = db.query(ScheduledJob).filter(ScheduledJob.name == "sms_code_cleanup").first()
        assert job.last_status == "succeeded"
        assert job.next_run_at > _naive_now()


def test_failing_job_recorded_not_raised(client):
    """任务抛异常记为 failed 并留痕，不得把调度轮次带崩。"""
    from app.database import SessionLocal
    from app.scheduler import JobSpec

    def boom(db):
        raise RuntimeError("模拟任务异常")

    REGISTRY["__test_boom"] = JobSpec("__test_boom", "测试异常任务", 3600, boom)
    try:
        with SessionLocal() as db:
            db.add(ScheduledJob(name="__test_boom", title="测试异常任务", interval_seconds=3600))
            db.commit()
            run = run_job(db, "__test_boom", trigger="manual")
            assert run.status == "failed"
            assert "模拟任务异常" in run.message
    finally:
        REGISTRY.pop("__test_boom", None)
        with SessionLocal() as db:
            db.query(ScheduledJob).filter(ScheduledJob.name == "__test_boom").delete()
            db.commit()


def test_tick_executes_due_jobs_only(client):
    from app.database import SessionLocal

    with SessionLocal() as db:
        for job in db.query(ScheduledJob).all():
            job.next_run_at = _naive_now() + timedelta(hours=1)
        db.query(ScheduledJob).filter(ScheduledJob.name == "medwaste_overdue_scan").update(
            {"next_run_at": _naive_now() - timedelta(seconds=1)}
        )
        db.commit()
        before = db.query(JobRun).count()
    assert tick() == 1
    with SessionLocal() as db:
        assert db.query(JobRun).count() == before + 1
        assert db.query(JobRun).order_by(JobRun.id.desc()).first().job_name == "medwaste_overdue_scan"


def test_unimplemented_job_row_is_skipped(client):
    """库里有、代码里没有实现的任务不参与调度（回滚版本后的残留行）。"""
    from app.database import SessionLocal

    with SessionLocal() as db:
        db.add(
            ScheduledJob(
                name="__ghost_job", title="幽灵任务", interval_seconds=3600,
                next_run_at=_naive_now() - timedelta(seconds=1),
            )
        )
        db.commit()
        assert "__ghost_job" not in due_jobs(db)
        db.query(ScheduledJob).filter(ScheduledJob.name == "__ghost_job").delete()
        db.commit()


# ---------------------------------------------------------------- 内置任务口径


def test_chronic_overdue_job_matches_endpoint(client, admin):
    """定时任务的超期口径必须与 GET /api/chronic/overdue 完全一致。"""
    from app.database import SessionLocal

    org = client.post(
        "/api/organizations", json={"name": "任务演示卫生院", "org_type": "township", "level": "township"},
        headers=admin,
    ).json()
    patient = client.post(
        "/api/patients", json={"name": "任务居民", "id_card": "331082199001011234"}, headers=admin
    ).json()
    chronic = client.post(
        "/api/chronic",
        json={"patient_id": patient["id"], "disease": "hypertension", "managed_by_org_id": org["id"]},
        headers=admin,
    ).json()
    with SessionLocal() as db:
        row = db.get(ChronicPatient, chronic["id"])
        row.next_due = (date.today() - timedelta(days=5)).isoformat()
        db.commit()

    endpoint_count = len(client.get("/api/chronic/overdue", headers=admin).json())
    with SessionLocal() as db:
        run = run_job(db, "chronic_overdue_scan", trigger="manual")
    assert run.affected == endpoint_count >= 1


def test_sms_cleanup_removes_expired_and_consumed_only(client):
    from app.database import SessionLocal

    now = _naive_now()
    with SessionLocal() as db:
        db.query(SmsCode).delete()
        db.add_all([
            SmsCode(phone="13500000001", code_hash="x", expires_at=now - timedelta(minutes=1)),
            SmsCode(phone="13500000002", code_hash="x", expires_at=now + timedelta(minutes=5), consumed=True),
            SmsCode(phone="13500000003", code_hash="x", expires_at=now + timedelta(minutes=5)),
        ])
        db.commit()
        run = run_job(db, "sms_code_cleanup", trigger="manual")
        assert run.affected == 2
        remaining = db.query(SmsCode).all()
        assert [r.phone for r in remaining] == ["13500000003"]


# ---------------------------------------------------------------- 管理接口


def test_manual_trigger_requires_director(client, admin):
    resp = client.post("/api/jobs/chronic_overdue_scan/run", headers=admin)
    assert resp.status_code == 201
    assert resp.json()["status"] == "succeeded"

    client.post(
        "/api/users",
        json={"username": "job_doc", "password": "doctor123", "full_name": "任务医生", "role": "doctor"},
        headers=admin,
    )
    doc = client.post("/api/auth/login", json={"username": "job_doc", "password": "doctor123"}).json()
    denied = client.post(
        "/api/jobs/chronic_overdue_scan/run",
        headers={"Authorization": f"Bearer {doc['access_token']}"},
    )
    assert denied.status_code == 403


def test_trigger_unknown_job_404(client, admin):
    assert client.post("/api/jobs/nope/run", headers=admin).status_code == 404


def test_patch_rejects_too_short_interval(client, admin):
    resp = client.patch("/api/jobs/sms_code_cleanup", json={"interval_seconds": 5}, headers=admin)
    assert resp.status_code == 422


def test_runs_history_filterable(client, admin):
    rows = client.get("/api/jobs/runs?job_name=sms_code_cleanup", headers=admin).json()
    assert rows and all(r["job_name"] == "sms_code_cleanup" for r in rows)
    assert all(r["trigger"] in ("manual", "scheduled") for r in rows)


def test_jobs_require_login(client):
    assert client.get("/api/jobs").status_code == 401


def test_jobs_restricted_to_management(client, admin):
    """T6.7：任务摘要带着各类超期数量，属运营信息，不对医师药师开放。"""
    for username, role in [("job_pha", "pharmacist"), ("job_dir2", "director")]:
        client.post(
            "/api/users",
            json={"username": username, "password": "passw0rd1", "full_name": username, "role": role},
            headers=admin,
        )

    def headers(username):
        token = client.post(
            "/api/auth/login", json={"username": username, "password": "passw0rd1"}
        ).json()["access_token"]
        return {"Authorization": f"Bearer {token}"}

    pharmacist = headers("job_pha")
    assert client.get("/api/jobs", headers=pharmacist).status_code == 403
    assert client.get("/api/jobs/runs", headers=pharmacist).status_code == 403
    # 管理层与 admin 正常
    assert client.get("/api/jobs", headers=headers("job_dir2")).status_code == 200
    assert client.get("/api/jobs", headers=admin).status_code == 200


# ---------------------------------------------------------------- 限流器


def test_sliding_window_limiter_memory_path():
    limiter = SlidingWindowRateLimiter(max_events=3, window_seconds=60)
    assert [limiter.allow("k") for _ in range(4)] == [True, True, True, False]
    # 不同主体互不影响
    assert limiter.allow("other") is True
    limiter.clear_all()
    assert limiter.allow("k") is True


def test_sliding_window_limiter_has_redis_backend():
    """T1.2：限流器必须具备 Redis 分支，否则多实例下配额被放大 N 倍。"""
    import inspect

    from app import state_store

    source = inspect.getsource(state_store.SlidingWindowRateLimiter)
    assert "_redis_client()" in source
    assert "ZREMRANGEBYSCORE" in state_store._SLIDING_WINDOW_LUA
