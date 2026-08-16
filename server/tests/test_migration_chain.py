"""迁移链健康度守卫（无需数据库，CI 默认跑）。

背景：运行时 schema 由 `Base.metadata.create_all` 建，迁移链一度不参与建库，
于是分叉成多个 head 也没人发现——`alembic upgrade head` 会因 multiple heads 直接失败，
等到生产库有真实数据、第一次要 ALTER 时才在最坏的时机暴露。

这条守卫把"单一 head"钉成跑不过就红，不依赖 PG。
"""
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

SERVER_DIR = Path(__file__).resolve().parents[1]


def _script_dir() -> ScriptDirectory:
    cfg = Config(str(SERVER_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVER_DIR / "alembic"))
    return ScriptDirectory.from_config(cfg)


def test_single_migration_head():
    """迁移图必须只有一个 head，否则 `alembic upgrade head` 无法执行。

    分叉时用 `alembic merge heads` 收敛为单 head；新增迁移接在当前 head 之后。
    （迁移终态与模型的表级一致性由真 PG 迁移测试 test_postgres_real 覆盖，
    需数据库不在本无库守卫内重复。）
    """
    heads = _script_dir().get_heads()
    assert len(heads) == 1, (
        f"迁移链出现 {len(heads)} 个 head：{heads}。"
        "新增迁移请接在当前 head 之后；分叉时用 `alembic merge heads` 收敛为单 head。"
    )
