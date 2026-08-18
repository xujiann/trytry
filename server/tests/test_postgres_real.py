"""真 PostgreSQL 验证（P3-4）：迁移链与方言敏感点。

默认跳过——CI/开发机不一定有 PG。开启方式：

    export MEDPLAT_PG_TEST_URL=postgresql://postgres@127.0.0.1:5432/medplat_pg_test
    python -m pytest tests/test_postgres_real.py -q

这套用例守的是 **SQLite 上测不出来的东西**：

- 双分支迁移（平台 + spd）在 PG 上能从零跑到 heads——SQLite 的 batch_alter
  与类型宽容会把很多 PG 才报的错吞掉（已实测抓到过一个：boolean 列配了
  整数默认值，PG 直接 DatatypeMismatch）；
- 部分唯一索引（仅 status='active'）真的只锁在管档案；
- 业务测试套在 PG 上跑通由 conftest 的 MEDPLAT_DATABASE_URL 支持，
  不在本文件重复。
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

PG_URL = os.environ.get("MEDPLAT_PG_TEST_URL", "")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_URL, reason="需要 MEDPLAT_PG_TEST_URL 指向可用的 PostgreSQL"
    ),
]

SERVER_DIR = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def pg_engine():
    from sqlalchemy import create_engine, text

    engine = create_engine(PG_URL)
    # 从零开始：清空 public schema，让迁移链证明自己能白手起家
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "heads"],
        cwd=SERVER_DIR,
        env={**os.environ, "MEDPLAT_DATABASE_URL": PG_URL},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"迁移在 PG 上失败：\n{result.stderr[-2000:]}"
    yield engine
    engine.dispose()


def test_migrations_reach_both_heads(pg_engine):
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        versions = {v for (v,) in conn.execute(text("SELECT version_num FROM alembic_version"))}
    assert len(versions) == 2, f"应有平台 + spd 两个分支头，实际 {versions}"


def test_spd_tables_created(pg_engine):
    from sqlalchemy import inspect

    tables = set(inspect(pg_engine).get_table_names())
    assert {"spd_enrollments", "spd_tasks", "spd_measurements",
            "spd_report_instances"} <= tables
    assert "patients" in tables and "attachments" in tables


def test_partial_unique_index_only_locks_active(pg_engine):
    """部分唯一索引：同患者同病种两条 active 撞索引；migrated + active 共存。

    用 ORM 建前置数据而不是手写 INSERT：表的非空列会随版本增加，
    手写列清单迟早跟不上模型。
    """
    import sqlalchemy.exc
    from sqlalchemy.orm import sessionmaker

    from app.models import Organization, Patient
    from app.spd.models import SpdEnrollment

    Session = sessionmaker(bind=pg_engine)
    with Session() as db:
        org = Organization(name="PG索引测试院", org_type="township", level="township")
        patient = Patient(name="PG索引患者", id_card="330900199001011234",
                          gender="男", birth_date="1990-01-01",
                          ehc_no="PG-EHC-0001")  # 健康卡号由接口层发号，直连建档要自带
        db.add_all([org, patient])
        db.flush()

        def enrollment(status):
            return SpdEnrollment(
                patient_id=patient.id, program_code="hypertension",
                org_id=org.id, status=status,
            )

        db.add(enrollment("migrated"))
        db.add(enrollment("active"))  # 与 migrated 共存：迁走后目标机构可重建
        db.commit()

        db.add(enrollment("active"))  # 第二条在管档案必须撞索引
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.commit()


def test_boolean_defaults_are_boolean(pg_engine):
    """child_records.high_risk 的默认值必须是布尔字面量（曾配成整数 0，PG 拒收）。"""
    from sqlalchemy import text

    with pg_engine.connect() as conn:
        default = conn.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name = 'child_records' AND column_name = 'high_risk'"
        )).scalar()
    assert default and "false" in default.lower()
