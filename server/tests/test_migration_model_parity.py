"""迁移 ↔ 模型列级一致性的**快速本地**门禁（SQLite 空库跑真迁移）。

守的失效模式 CLAUDE.md §4 点名过，而且历史上真的上线才炸过：
`Base.metadata.create_all`（`main.py`，开发环境仍开着）会在开发 SQLite 上把
模型里新加的列悄悄建出来，本地一切正常；生产 PG 只走 alembic，
漏写的 `add_column` 要等线上第一次读写那一列才暴露。

**与既有两条门禁的分工**（不是第三套实现，比对逻辑共用 `schema_parity.py`）：

| 门禁 | 底座 | 粒度 | 何时跑 |
|---|---|---|---|
| `test_schema_governance.test_模型表零漂移` | 正则扫迁移文本 | 表 | test-unit |
| **本文件** | 一次性 SQLite 空库跑真迁移 | **表 + 列** | **test-unit（约 7 秒）** |
| `test_postgres_real.test_迁移与模型的列集合零漂移` | 真 PG | 表 + 列 + 方言 | integration（CI 阻断） |

PG 那条更强，但它 `skipif` 掉本地绝大多数人的运行，等于把反馈推到 CI。
CLAUDE.md §7 明说"别把 CI 当第一道防线"——改模型是本地动作，反馈也该在本地。
反过来，本文件**替代不了** PG 那条：它证明的是"列写全了"，
证明不了"PG 认这个类型"（历史上抓到过 boolean 列配整数默认值，PG 直接 DatatypeMismatch）。

**硬门禁，无基线欠账**——写这条时实测 246 张表、0 处漂移。不要往里加豁免名单：
真有非改不可的理由，那属于数据模型顶层决策，走 ADR（CLAUDE.md §9）。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys
import tempfile

import pytest
import sqlalchemy as sa

from schema_parity import diff_schema, format_columns

from app.database import Base
from app.main import app  # noqa: F401  触发全部模型 import（含 spd）

SERVER_ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def drift():
    """在一次性空库上跑 `alembic upgrade heads`，返回与模型的差异。

    走子进程而不是 `alembic.command.upgrade()`：迁移脚本会 import 应用模块，
    在本进程里跑容易和测试会话共用的 engine / 已建好的表相互污染。
    子进程 + 独立 `MEDPLAT_DATABASE_URL` 是最干净的隔离。

    用 `heads`（复数）：本仓库两个 head（平台链 + spd 链），
    单数 `head` 会报错并漏掉 spd 的 59 张表。
    """
    env = {k: v for k, v in os.environ.items() if k != "MEDPLAT_DATABASE_URL"}
    with tempfile.TemporaryDirectory() as tmp:
        db_path = pathlib.Path(tmp) / "parity.db"
        proc = subprocess.run(
            [sys.executable, "-m", "alembic", "upgrade", "heads"],
            cwd=SERVER_ROOT,
            env={**env, "MEDPLAT_DATABASE_URL": f"sqlite:///{db_path}"},
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, (
            "`alembic upgrade heads` 从空库跑不通——迁移链本身就是坏的：\n"
            + proc.stdout[-3000:] + proc.stderr[-3000:]
        )
        engine = sa.create_engine(f"sqlite:///{db_path}")
        try:
            yield diff_schema(sa.inspect(engine), Base.metadata)
        finally:
            engine.dispose()


def test_跑一遍迁移能建出全部模型表(drift):
    """比正则版更强：这是迁移**实际执行**的结果，不是文本里出现过 create_table。"""
    assert not drift["missing_tables"], (
        f"以下模型表跑完全部迁移后在库里不存在（生产 PG 会缺表）：{drift['missing_tables']}"
    )


def test_没有模型里已删除的残留表(drift):
    assert not drift["extra_tables"], (
        f"迁移建出了模型里不存在的表（残留，或删模型忘写 drop_table）：{drift['extra_tables']}"
    )


def test_每张表的列集合与模型一致(drift):
    """列级门禁：给现有表加列却漏写 add_column，就是在这里被拦下的。

    表级那条（正则扫 `create_table(`）对这种改动一声不吭——表早就建过了。
    """
    assert not drift["missing_columns"], (
        "模型里有、迁移没建的列（**漏写迁移，生产 PG 上线才炸**）：\n"
        + format_columns(drift["missing_columns"])
        + "\ncreate_all 只在开发 SQLite 上掩盖此问题，见 CLAUDE.md §4。"
    )
    assert not drift["extra_columns"], (
        "迁移建了、模型里没有的列（残留，或改列名时漏了 drop）：\n"
        + format_columns(drift["extra_columns"])
    )


def test_门禁本身没瞎(drift):
    """一个永远不会红的守卫比没有守卫更糟。

    上面三条全靠迁移真的建出了东西——alembic 若静默失败、或临时库路径写错，
    集合会一起变空，三条断言全部"通过"。这里钉住量级与两条链各自的存在。
    """
    assert drift["table_count"] >= 200, (
        f"只建出 {drift['table_count']} 张表，迁移大概没真跑"
    )
    # 模型侧同样不能是空的：import 失败时 metadata 为空，差异集也会全空
    assert len(Base.metadata.tables) >= 200, "模型没加载全，本比对是空转"
