"""生产守卫盲区（A2，生产整改包 S）。

凭据强度三重校验（test_prod_credential_guard.py）之外，还有几个"起得来但不该起"
的形态：生产 + 演示种子、生产 + SQLite、生产无 Redis（多实例状态失效）、
以及免登录证件号查询面默认敞开（TECH_DEBT P1-3）。
每个守卫都配一条红（该拦的拦住）一条绿（不该拦的放行），非空洞。
"""
import logging

import pytest

from app.config import Settings

STRONG_SECRET = "9f3c2b7e" * 4 + "a1d4"
STRONG_PASSWORD = "Kx7!mQ2$vLp9"
PG_URL = "postgresql://medplat:pw@db:5432/medplat"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """隔离宿主环境：这些变量任何一个残留都会让红绿判定失真。"""
    monkeypatch.delenv("MEDPLAT_SEED_DEMO", raising=False)
    monkeypatch.delenv("MEDPLAT_REDIS_URL", raising=False)
    monkeypatch.delenv("MEDPLAT_PORTAL_LEGACY_VERIFY", raising=False)


def _prod(**kwargs) -> Settings:
    base = {
        "env": "prod",
        "secret": STRONG_SECRET,
        "admin_password": STRONG_PASSWORD,
        "database_url": PG_URL,
    }
    return Settings(**{**base, **kwargs})


# ================================================================ 演示种子开关


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_生产开演示种子拒绝启动(monkeypatch, value):
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", value)
    with pytest.raises(Exception) as exc:
        _prod()
    assert "MEDPLAT_SEED_DEMO" in str(exc.value)


def test_生产未开演示种子正常启动():
    assert _prod().is_production


def test_非生产开演示种子放行(monkeypatch):
    """演示站/本地就是靠它灌数据的，守卫只在生产收紧。"""
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", "1")
    assert Settings(env="dev").is_production is False


def test_演示种子假值不误伤(monkeypatch):
    """置了变量但值为假（0/false/空）不算开启——shell 里常见的关闭写法。"""
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", "0")
    _prod()
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", "false")
    _prod()


# ================================================================ SQLite


def test_生产用sqlite拒绝启动并指明须PostgreSQL():
    with pytest.raises(Exception) as exc:
        _prod(database_url="sqlite:///./medplat.db")
    message = str(exc.value)
    assert "SQLite" in message and "PostgreSQL" in message


def test_生产用postgres正常启动():
    assert _prod(database_url=PG_URL).is_production


def test_非生产用sqlite放行():
    """开发/测试全程跑在 SQLite 上，不能拦。"""
    s = Settings(env="dev", database_url="sqlite:///./medplat.db")
    assert s.database_url.startswith("sqlite")


def test_生产同时踩演示种子与sqlite时一次报全(monkeypatch):
    """与凭据守卫同一风格：一次列清，别让运维改一个重启一次再撞下一个。"""
    monkeypatch.setenv("MEDPLAT_SEED_DEMO", "1")
    with pytest.raises(Exception) as exc:
        _prod(database_url="sqlite:///./x.db")
    message = str(exc.value)
    assert "MEDPLAT_SEED_DEMO" in message and "SQLite" in message


def test_凭据弱时先报凭据问题():
    """守卫定义顺序钉住：凭据强度校验在前——既有测试断言的就是那条报文。"""
    with pytest.raises(Exception) as exc:
        _prod(secret="x", database_url="sqlite:///./x.db")
    assert "MEDPLAT_SECRET" in str(exc.value)


# ================================================================ Redis 缺配强警告


def test_生产未配redis打印强警告但不拒启(caplog):
    with caplog.at_level(logging.WARNING, logger="medplat.config"):
        s = _prod()
    assert s.is_production, "缺 Redis 只警告，不拒绝启动"
    warnings = [r for r in caplog.records if "MEDPLAT_REDIS_URL" in r.getMessage()]
    assert warnings, "应有一条点名 MEDPLAT_REDIS_URL 的警告"
    text = warnings[0].getMessage()
    assert "黑名单" in text and "多实例" in text, "警告须说明多实例下失效的是什么"


def test_生产配了redis不再警告(monkeypatch, caplog):
    monkeypatch.setenv("MEDPLAT_REDIS_URL", "redis://localhost:6379/0")
    with caplog.at_level(logging.WARNING, logger="medplat.config"):
        _prod()
    assert not [r for r in caplog.records if "MEDPLAT_REDIS_URL" in r.getMessage()]


def test_非生产未配redis不警告(caplog):
    """本地开发没 Redis 是常态，别刷屏。"""
    with caplog.at_level(logging.WARNING, logger="medplat.config"):
        Settings(env="dev")
    assert not [r for r in caplog.records if "MEDPLAT_REDIS_URL" in r.getMessage()]


# ================================================================ portal_legacy_verify 默认关


def test_旧核验接口默认关闭():
    """TECH_DEBT P1-3：免登录的证件号查询面不该默认敞开。"""
    assert Settings(env="dev").portal_legacy_verify is False


def test_旧核验接口可显式开启():
    """过渡期的县显式置 true 仍可用——翻的是默认值，不是拆能力。"""
    assert Settings(env="dev", portal_legacy_verify=True).portal_legacy_verify is True


def test_旧核验接口默认关时返回410():
    """行为面钉住：默认配置下 my-archive 直接 410，而非可查询。"""
    from fastapi.testclient import TestClient

    from app.config import settings as live_settings
    from app.main import app

    assert live_settings.portal_legacy_verify is False, "默认值应为 False"
    from conftest import reset_database

    reset_database()
    with TestClient(app) as client:
        resp = client.get("/api/portal/my-archive?ehc_no=X&id_card=Y")
        assert resp.status_code == 410
