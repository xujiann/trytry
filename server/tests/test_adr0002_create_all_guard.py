"""ADR-0002 守卫：生产环境启动不执行 create_all，开发环境保留零配置起库。

生产结构变更统一走 alembic（部署产物启动前 `alembic upgrade heads`）。create_all
只建"不存在的表"、不改列——漏写迁移时开发 SQLite 看起来正常、生产 PG 上线才炸。
本测试钉住 main.lifespan 的环境分支，防止守卫被顺手删掉回到双轨。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.config import settings
from app.database import Base
from app.main import app


@pytest.fixture()
def create_all_spy(monkeypatch):
    calls = []
    original = Base.metadata.create_all

    def spy(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(Base.metadata, "create_all", spy)
    return calls


def test_production_startup_skips_create_all(monkeypatch, create_all_spy):
    """生产（env=prod）：lifespan 不得调用 create_all——迁移是唯一建表路径。

    库表已由 reset_database 备好，种子化（幂等只增不改）照常运行不受影响。
    """
    reset_database()
    create_all_spy.clear()  # reset_database 自身的 create_all 不计入
    monkeypatch.setattr(settings, "env", "prod")
    # 生产校验拒绝默认口令/密钥（H4），换成非默认值让应用能启动
    monkeypatch.setattr(settings, "secret", "adr0002-test-secret-not-default")
    monkeypatch.setattr(settings, "admin_password", "adr0002-test-password")
    with TestClient(app):
        pass
    assert create_all_spy == [], "生产环境启动不应执行 create_all（ADR-0002）"


def test_dev_startup_keeps_create_all(create_all_spy):
    """开发（默认 env=dev）：保留 create_all 零配置起库，开发体验不变。"""
    reset_database()
    create_all_spy.clear()  # 只看 lifespan 里的那一次
    with TestClient(app):
        pass
    assert create_all_spy, "开发环境启动应仍执行 create_all（零配置起库）"
