"""D1 开办批量建号（scripts/import_users.py）：角色校验、幂等、口令生成。"""
import sys
from pathlib import Path

import pytest

from conftest import reset_database

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from import_legacy import run_import as run_legacy  # noqa: E402
from import_users import run_import  # noqa: E402

from app.database import SessionLocal
from app.models import Role, User

SAMPLES = Path(__file__).resolve().parent.parent / "scripts" / "samples"


@pytest.fixture(scope="module", autouse=True)
def base_data():
    reset_database()
    assert run_legacy("organizations", SAMPLES / "organizations.csv").errors == []
    yield


def _count() -> int:
    db = SessionLocal()
    try:
        return db.query(User).count()
    finally:
        db.close()


def test_import_and_idempotent_rerun():
    report = run_import(SAMPLES / "users.csv")
    assert report.imported == 3 and report.errors == []
    # 口令列留空的自动生成并回报
    assert [u for u, _pw in report.generated] == ["sunhushi"]
    db = SessionLocal()
    try:
        doctor = db.query(User).filter(User.username == "qianyisheng").one()
        assert doctor.role == "doctor" and doctor.org_id is not None
        assert doctor.password_hash and doctor.password_hash != "Qy2026abcd"  # 只存散列
    finally:
        db.close()
    # 幂等重跑：用户名查重全跳过，不重置口令不改角色
    rerun = run_import(SAMPLES / "users.csv")
    assert rerun.imported == 0 and rerun.skipped == 3 and rerun.generated == []
    assert _count() == 3


def test_role_validation(tmp_path):
    bad = tmp_path / "bad_role.csv"
    bad.write_text(
        "username,full_name,role,org_name,password\n"
        "hacker01,越权甲,superadmin,,Aa12345678\n"
        "okuser01,正常乙,operator,,Aa12345678\n",
        encoding="utf-8",
    )
    rep = run_import(bad)
    assert rep.imported == 1 and len(rep.errors) == 1
    assert "role 非法" in rep.errors[0][1]
    db = SessionLocal()
    try:
        assert db.query(User).filter(User.username == "hacker01").first() is None
    finally:
        db.close()


def test_custom_active_role_accepted(tmp_path):
    db = SessionLocal()
    try:
        db.add(Role(key="triage_nurse", name="分诊护士", builtin=False, active=True))
        db.commit()
    finally:
        db.close()
    f = tmp_path / "custom_role.csv"
    f.write_text(
        "username,full_name,role,org_name,password\nfenzhen01,分诊丙,triage_nurse,,Aa12345678\n",
        encoding="utf-8",
    )
    rep = run_import(f)
    assert rep.imported == 1 and rep.errors == []


def test_weak_password_and_missing_org_are_errors(tmp_path):
    f = tmp_path / "bad_rows.csv"
    f.write_text(
        "username,full_name,role,org_name,password\n"
        "weakpw01,弱丁,operator,,short\n"
        "noorg001,无戊,operator,不存在的机构,Aa12345678\n",
        encoding="utf-8",
    )
    rep = run_import(f)
    assert rep.imported == 0 and len(rep.errors) == 2
    assert "password 不合规" in rep.errors[0][1]
    assert "机构不存在" in rep.errors[1][1]


def test_dry_run_does_not_write(tmp_path):
    before = _count()
    f = tmp_path / "dry.csv"
    f.write_text(
        "username,full_name,role,org_name,password\ndryrun01,试己,operator,,Aa12345678\n",
        encoding="utf-8",
    )
    rep = run_import(f, dry_run=True)
    assert rep.dry_run is True and rep.imported == 1
    assert _count() == before
