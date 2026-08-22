"""生产 Redis 口径升级（P1-26）：多实例特征 + 无 Redis = 拒启。

单实例没 Redis 是合法形态（进程内存实现语义完整），保持既有强警告；
多实例（MEDPLAT_WORKERS>1 或 MEDPLAT_MIGRATE_ON_START=0——多实例部署的
两个信号，见 start.sh 与 docs/发布流程.md）下会话黑名单/防爆破锁定/限流/
任务锁全部退化为各进程各一份，属安全事故而非降级，必须拒启。
每个守卫一红一绿，非空洞。
"""
import logging

import pytest

from app.config import Settings

STRONG_SECRET = "9f3c2b7e" * 4 + "a1d4"
STRONG_PASSWORD = "Kx7!mQ2$vLp9"
PG_URL = "postgresql://medplat:pw@db:5432/medplat"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离宿主环境：任何一个残留都会让红绿判定失真。"""
    for var in (
        "MEDPLAT_SEED_DEMO",
        "MEDPLAT_REDIS_URL",
        "MEDPLAT_WORKERS",
        "MEDPLAT_MIGRATE_ON_START",
    ):
        monkeypatch.delenv(var, raising=False)


def _prod(**kwargs) -> Settings:
    base = {
        "env": "prod",
        "secret": STRONG_SECRET,
        "admin_password": STRONG_PASSWORD,
        "database_url": PG_URL,
    }
    return Settings(**{**base, **kwargs})


# ================================================================ 拒启（红）


def test_生产多worker无redis拒绝启动(monkeypatch):
    monkeypatch.setenv("MEDPLAT_WORKERS", "2")
    with pytest.raises(Exception) as exc:
        _prod()
    message = str(exc.value)
    assert "MEDPLAT_REDIS_URL" in message and "MEDPLAT_WORKERS=2" in message
    # 报错要说明多实例下失效的是什么，不是光喊"配 Redis"
    assert "黑名单" in message and "锁" in message


def test_生产迁移关闭信号无redis拒绝启动(monkeypatch):
    """MEDPLAT_MIGRATE_ON_START=0 是多实例部署的规定动作（迁移由发布流程单跑）。"""
    monkeypatch.setenv("MEDPLAT_MIGRATE_ON_START", "0")
    with pytest.raises(Exception) as exc:
        _prod()
    assert "MEDPLAT_MIGRATE_ON_START=0" in str(exc.value)


def test_两个多实例信号同时报全(monkeypatch):
    monkeypatch.setenv("MEDPLAT_WORKERS", "4")
    monkeypatch.setenv("MEDPLAT_MIGRATE_ON_START", "0")
    with pytest.raises(Exception) as exc:
        _prod()
    message = str(exc.value)
    assert "MEDPLAT_WORKERS=4" in message and "MEDPLAT_MIGRATE_ON_START=0" in message


def test_与其他生产守卫一次报全(monkeypatch):
    """同一风格：一次列清，别让运维改一个重启一次再撞下一个。"""
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", "1")
    monkeypatch.setenv("MEDPLAT_WORKERS", "2")
    with pytest.raises(Exception) as exc:
        _prod()
    message = str(exc.value)
    assert "MEDPLAT_SEED_DEMO" in message and "MEDPLAT_REDIS_URL" in message


# ================================================================ 放行（绿）


def test_生产多worker配了redis放行(monkeypatch):
    monkeypatch.setenv("MEDPLAT_WORKERS", "4")
    monkeypatch.setenv("MEDPLAT_MIGRATE_ON_START", "0")
    monkeypatch.setenv("MEDPLAT_REDIS_URL", "redis://localhost:6379/0")
    assert _prod().is_production


def test_生产单实例无redis仍只警告不拒启(caplog, monkeypatch):
    """既有口径保持：MEDPLAT_WORKERS=1（或未设）只警告。"""
    monkeypatch.setenv("MEDPLAT_WORKERS", "1")
    monkeypatch.setenv("MEDPLAT_MIGRATE_ON_START", "1")
    with caplog.at_level(logging.WARNING, logger="medplat.config"):
        s = _prod()
    assert s.is_production
    assert [r for r in caplog.records if "MEDPLAT_REDIS_URL" in r.getMessage()]


def test_workers非法值不误判为多实例(monkeypatch):
    monkeypatch.setenv("MEDPLAT_WORKERS", "abc")
    assert _prod().is_production


def test_非生产多实例特征放行(monkeypatch):
    """本地/CI 随便设 worker 数，守卫只在生产收紧。"""
    monkeypatch.setenv("MEDPLAT_WORKERS", "8")
    monkeypatch.setenv("MEDPLAT_MIGRATE_ON_START", "0")
    assert Settings(env="dev").is_production is False
