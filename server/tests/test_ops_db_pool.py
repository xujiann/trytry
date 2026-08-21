"""连接池配置接线（生产整改 A11）：engine_kwargs 按 MEDPLAT_DB_POOL_* 生效。

不实连数据库——engine_kwargs 是纯函数，settings 用 monkeypatch 打值。
默认值（全 0）下必须与历史输出完全一致（沿用 QueuePool 默认），
由 test_config_matrix.py 的既有矩阵继续钉住。
"""
import pytest

from app.config import settings
from app.database import build_engine, engine_kwargs

PG_URL = "postgresql+psycopg2://medplat:secret@db.example:5432/medplat"


@pytest.fixture()
def pool_settings(monkeypatch):
    monkeypatch.setattr(settings, "db_pool_size", 20)
    monkeypatch.setattr(settings, "db_max_overflow", 30)
    monkeypatch.setattr(settings, "db_pool_timeout_seconds", 5)
    monkeypatch.setattr(settings, "db_pool_recycle_seconds", 280)


def test_pg_pool_kwargs_applied(pool_settings):
    assert engine_kwargs(PG_URL) == {
        "connect_args": {},
        "pool_size": 20,
        "max_overflow": 30,
        "pool_timeout": 5,
        "pool_recycle": 280,
    }


def test_pg_pool_kwargs_zero_means_default(monkeypatch):
    for field in ("db_pool_size", "db_max_overflow",
                  "db_pool_timeout_seconds", "db_pool_recycle_seconds"):
        monkeypatch.setattr(settings, field, 0)
    assert engine_kwargs(PG_URL) == {"connect_args": {}}


def test_sqlite_ignores_pool_settings(pool_settings):
    """SQLite 无连接池语义：即便配了池参数也不得往 create_engine 传。"""
    assert engine_kwargs("sqlite:///./medplat.db") == {
        "connect_args": {"check_same_thread": False}
    }


def test_pg_engine_carries_pool_settings(pool_settings):
    """create_engine 惰性建连：仅装配连接池，不触达网络。"""
    eng = build_engine(PG_URL)
    try:
        assert eng.pool.size() == 20
        assert eng.pool._max_overflow == 30
        assert eng.pool._timeout == 5
        assert eng.pool._recycle == 280
    finally:
        eng.dispose()
