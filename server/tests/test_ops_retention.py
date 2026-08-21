"""运行数据保留期（A9）：JobRun 清理 + AccessLog/AuditLog 归档后截断。

覆盖点：
- 造旧数据 → 跑任务 → 超期行被删、近期行保留（把删除逻辑注掉必红）；
- 归档文件存在、行数与删除量一致、manifest 的 MAC 可用 settings.secret 复算；
- 保留期/归档天数为 0 时不动任何数据、不产生文件；
- 审计归档在 manifest 里记录哈希链锚点，库内剩余首条可与归档段尾哈希续接。
"""
import gzip
import hashlib
import hmac
import json
from datetime import timedelta
from pathlib import Path

import pytest

from conftest import reset_database

from app import jobs
from app.audit_chain import audit_entry_hash
from app.clock import now_naive
from app.config import settings
from app.database import SessionLocal
from app.models import AccessLog, AuditLog, JobRun, Patient


@pytest.fixture(scope="module")
def db_ready():
    reset_database()
    with SessionLocal() as db:
        db.add(Patient(ehc_no="EHC-OPS-1", name="留痕测试患者", id_card="330100199001010011"))
        db.commit()
        patient_id = db.query(Patient).filter(Patient.ehc_no == "EHC-OPS-1").first().id
    return patient_id


@pytest.fixture()
def archive_dir(tmp_path, monkeypatch):
    """归档落临时目录：settings 是进程单例，monkeypatch 属性即可（自动还原）。"""
    monkeypatch.setattr(settings, "upload_dir", str(tmp_path))
    return tmp_path / "archives"


def _old(days: int):
    return now_naive() - timedelta(days=days)


def _read_ndjson_gz(path: Path) -> list[dict]:
    with gzip.open(path, "rb") as f:
        raw = f.read()
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line]


def _recalc_mac(path: Path) -> str:
    """按 manifest 约定复算 MAC：HMAC-SHA256(secret, 未压缩 NDJSON 字节流)。"""
    with gzip.open(path, "rb") as f:
        raw = f.read()
    return hmac.new(settings.secret.encode(), raw, hashlib.sha256).hexdigest()


# ---------------------------------------------------------------- jobrun_cleanup


def test_jobrun_cleanup_deletes_only_expired(db_ready):
    with SessionLocal() as db:
        db.query(JobRun).delete()
        db.add(JobRun(job_name="old_job", created_at=_old(settings.jobrun_retention_days + 1)))
        db.add(JobRun(job_name="fresh_job", created_at=now_naive()))
        db.commit()

        affected, message = jobs.jobrun_cleanup(db)
        assert affected == 1
        remaining = db.query(JobRun).all()
        assert [r.job_name for r in remaining] == ["fresh_job"]
        assert str(settings.jobrun_retention_days) in message


def test_jobrun_cleanup_skips_when_disabled(db_ready, monkeypatch):
    monkeypatch.setattr(settings, "jobrun_retention_days", 0)
    with SessionLocal() as db:
        db.query(JobRun).delete()
        db.add(JobRun(job_name="ancient", created_at=_old(3650)))
        db.commit()

        affected, message = jobs.jobrun_cleanup(db)
        assert affected == 0
        assert "跳过" in message
        assert db.query(JobRun).count() == 1


# ---------------------------------------------------------------- access_log_archive


def test_access_log_archive_exports_then_deletes(db_ready, archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "access_log_archive_days", 180)
    # 批大小压到 2：5 行旧数据必须走多批循环，覆盖分批删除路径
    monkeypatch.setattr(jobs, "ARCHIVE_BATCH_SIZE", 2)
    with SessionLocal() as db:
        db.query(AccessLog).delete()
        for i in range(5):
            db.add(
                AccessLog(
                    username=f"doc{i}", patient_id=db_ready, resource="archive",
                    basis="contract", created_at=_old(181 + i),
                )
            )
        db.add(
            AccessLog(
                username="recent", patient_id=db_ready, resource="archive",
                basis="contract", created_at=now_naive(),
            )
        )
        db.commit()

        affected, message = jobs.access_log_archive(db)
        assert affected == 5
        # 超期行被删、近期行保留
        rest = db.query(AccessLog).all()
        assert len(rest) == 1 and rest[0].username == "recent"

    # 归档文件与 manifest
    files = list(archive_dir.glob("access_logs_*.ndjson.gz"))
    assert len(files) == 1
    rows = _read_ndjson_gz(files[0])
    assert len(rows) == 5
    assert {r["username"] for r in rows} == {f"doc{i}" for i in range(5)}

    manifest = [json.loads(x) for x in (archive_dir / "manifest.jsonl").read_text("utf-8").splitlines()]
    entry = manifest[-1]
    assert entry["table"] == "access_logs" and entry["rows"] == 5
    assert entry["file"] == files[0].name
    # MAC 用 settings.secret 可复算
    assert entry["mac"] == _recalc_mac(files[0])
    # 时间范围覆盖被归档行
    assert entry["from"] <= entry["to"]


def test_access_log_archive_disabled_touches_nothing(db_ready, archive_dir):
    assert settings.access_log_archive_days == 0  # 默认关闭
    with SessionLocal() as db:
        db.query(AccessLog).delete()
        db.add(
            AccessLog(
                username="ancient", patient_id=db_ready, resource="archive",
                basis="contract", created_at=_old(3650),
            )
        )
        db.commit()

        affected, message = jobs.access_log_archive(db)
        assert affected == 0 and "跳过" in message
        assert db.query(AccessLog).count() == 1
    assert not archive_dir.exists() or not list(archive_dir.glob("access_logs_*"))


def test_access_log_archive_no_expired_rows_writes_no_file(db_ready, archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "access_log_archive_days", 180)
    with SessionLocal() as db:
        db.query(AccessLog).delete()
        db.commit()
        affected, message = jobs.access_log_archive(db)
        assert affected == 0 and "无超期数据" in message
    assert not archive_dir.exists() or not list(archive_dir.glob("access_logs_*"))


# ---------------------------------------------------------------- audit_archive


def _seed_audit_chain(db, count: int, old_count: int) -> list[AuditLog]:
    """按真实链算法造一段连续审计链，前 old_count 条置为超期。"""
    db.query(AuditLog).delete()
    db.commit()
    prev = ""
    entries = []
    for i in range(count):
        entry_hash = audit_entry_hash(prev, f"u{i}", "POST", f"/api/x/{i}", 200)
        row = AuditLog(
            username=f"u{i}", method="POST", path=f"/api/x/{i}", status_code=200,
            prev_hash=prev, entry_hash=entry_hash,
            created_at=_old(400 - i) if i < old_count else now_naive(),
        )
        db.add(row)
        db.commit()
        entries.append(row)
        prev = entry_hash
    return entries


def test_audit_archive_records_chain_anchors(db_ready, archive_dir, monkeypatch):
    monkeypatch.setattr(settings, "audit_log_archive_days", 180)
    with SessionLocal() as db:
        entries = _seed_audit_chain(db, count=6, old_count=4)
        old_hashes = [e.entry_hash for e in entries[:4]]
        first_prev = entries[0].prev_hash

        affected, _ = jobs.audit_archive(db)
        assert affected == 4
        # 库内剩余 2 条（近期），其首条 prev_hash 指向归档段尾哈希——断点可续接
        rest = db.query(AuditLog).order_by(AuditLog.id).all()
        assert len(rest) == 2
        assert rest[0].prev_hash == old_hashes[-1]

    files = list(archive_dir.glob("audit_logs_*.ndjson.gz"))
    assert len(files) == 1
    rows = _read_ndjson_gz(files[0])
    assert len(rows) == 4
    # 归档文件内的链可独立复算
    for row in rows:
        assert row["entry_hash"] == audit_entry_hash(
            row["prev_hash"], row["username"], row["method"], row["path"], row["status_code"]
        )

    entry = [json.loads(x) for x in (archive_dir / "manifest.jsonl").read_text("utf-8").splitlines()][-1]
    assert entry["table"] == "audit_logs" and entry["rows"] == 4
    # 锚点：首条 prev/entry 哈希 + 尾条 entry 哈希
    assert entry["first_prev_hash"] == first_prev
    assert entry["first_entry_hash"] == old_hashes[0]
    assert entry["last_entry_hash"] == old_hashes[-1]
    assert entry["mac"] == _recalc_mac(files[0])


def test_audit_archive_disabled_by_default(db_ready, archive_dir):
    assert settings.audit_log_archive_days == 0  # 开启是运维决策，默认关
    with SessionLocal() as db:
        _seed_audit_chain(db, count=2, old_count=2)
        affected, message = jobs.audit_archive(db)
        assert affected == 0 and "跳过" in message
        assert db.query(AuditLog).count() == 2
    assert not archive_dir.exists() or not list(archive_dir.glob("audit_logs_*"))


# ---------------------------------------------------------------- 任务已注册进调度器


def test_retention_jobs_registered():
    from app.scheduler import REGISTRY

    for name in ("jobrun_cleanup", "access_log_archive", "audit_archive"):
        assert name in REGISTRY
