"""仿真造数脚本在生产档拒跑（P2-1280，第三十七批「种子与初始化数据」扫描 AA4-8）。

`scripts/seed_bulk.py` 头注写「禁止对生产库执行」，`main` 里却没有任何环境判断：生产容器里 MEDPLAT_DATABASE_URL 就是生产库，
漏掉头注那句 export 就照灌。修前实测（把 settings 置为生产档后照文档命令跑一次小量）：退出码 0，库里多出仿真机构 001/002、
患者 3、就诊 3，三条 SIM 审方规则启用——全部进统计。同类的演示种子在生产档拒启（config.py 生产守卫，理由「灌入后无法与
真实数据剥离」）。

修后 `main` 开头判 `settings.is_production`：是就打一句原因、退出码 2，连库之前就拦、一行不落；开发档照旧。与
`test_seed_bulk_readmission.py` 一样用独立的临时库跑，不往共用的测试库里灌仿真数据。
"""
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.database import Base
from app.models import DrugRule, Organization, Patient, User

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import seed_bulk  # noqa: E402

ARGV = ["seed_bulk.py", "--orgs", "2", "--patients", "3", "--encounters", "3"]


@pytest.fixture()
def bulk_db(tmp_path, monkeypatch):
    """建好表、只有一个用户（seed_bulk 取它当 created_by）的临时库；脚本的 SessionLocal 换成连它的。"""
    engine = create_engine(f"sqlite:///{tmp_path / 'bulk.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    with factory() as db:
        db.add(User(username="p21280-admin", password_hash="x", role="admin"))
        db.commit()
    monkeypatch.setattr(seed_bulk, "SessionLocal", factory)
    monkeypatch.setattr(sys, "argv", ARGV)
    yield engine
    engine.dispose()


def _row_counts(engine) -> dict[str, int]:
    """库里每张表的行数（全量，不挑表）：「一行不落」就是这张表前后一模一样。"""
    with engine.connect() as conn:
        return {t.name: conn.execute(select(func.count()).select_from(t)).scalar_one()
                for t in Base.metadata.sorted_tables}


def test_生产档拒跑_退出码2_一行不落(bulk_db, monkeypatch, capsys):
    monkeypatch.setattr(settings, "env", "prod")
    assert settings.is_production
    before = _row_counts(bulk_db)
    assert seed_bulk.main() == 2   # 修前 0：照灌
    assert _row_counts(bulk_db) == before
    err = capsys.readouterr().err
    assert "拒绝执行" in err and "生产档" in err, err


def test_MEDPLAT_ENVIRONMENT为prod同样拒跑(bulk_db, monkeypatch):
    monkeypatch.setattr(settings, "environment", "prod")
    before = _row_counts(bulk_db)
    assert seed_bulk.main() == 2
    assert _row_counts(bulk_db) == before


def test_开发档照旧灌入(bulk_db):
    assert not settings.is_production
    assert seed_bulk.main() == 0
    with sessionmaker(bind=bulk_db)() as db:
        assert {o.name for o in db.query(Organization)} == {"仿真机构001", "仿真机构002"}
        assert db.query(Patient).count() == 3
        assert db.query(DrugRule).filter(DrugRule.drug_code.like("SIM-%")).count() == 3
