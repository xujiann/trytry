"""绩效指标 remote_exam 的名称已过时：只报告，不替人改

同一轮把 `remote_exam` 维度从"只按申请方计"改成"申请方 + 共享诊断中心两侧都计"，
原名「远程诊断服务量」因此名不副实——它听起来是诊断工作量，实际两侧都含，
新名应为「共享诊断协同量」。`DEFAULT_INDICATORS` 已改，**新库自动是新名**。

**存量库本迁移不动它。** 起初这里写的是
`UPDATE performance_indicators SET name = ... WHERE key = 'remote_exam' AND name = '旧名'`，
被 `tests/test_migration_data_safety.py` 判为 A 档（改写不是本迁移新加的业务列）而拦下。
那道闸门拦得对，理由不止"规则如此"：

* `performance_indicators.name` 是**管理端可编辑**的
  （`PATCH /api/performance/indicators/{key}`），属现场配置而非平台常量；
* 它会出现在各县自己的考核文件、报表标题、对上汇报材料里。迁移把它悄悄改掉，
  现场看到的是"报表列名自己变了"，而库里没有任何记录说明是谁改的；
* 加 `AND name = '旧名'` 只能挡住"已经改过名"的库，挡不住"没改过名但引用了这个名字"的库。

所以按平台通则（CLAUDE.md §4，与 `a1c3e5b7d9f2` / `e5b7c9d1f3a4` 同一形状）：
**探到就报告，处置交人工。** 升级时若发现仍是旧名，打一条 WARNING 指名该行，
并给出可直接执行的 SQL；改不改、什么时候改，由现场决定。

人工处置（二选一）：

    -- 方式一：SQL
    UPDATE performance_indicators
       SET name = '共享诊断协同量'
     WHERE key = 'remote_exam' AND name = '远程诊断服务量';

    -- 方式二：管理端「绩效考核 → 指标目录」直接改，与上面等价

不改也不影响计分：计分只认 `key`，`name` 纯展示。

Revision ID: b5d9f3a71c2e
Revises: a1c3e5b7d9f2
Create Date: 2026-08-22
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "b5d9f3a71c2e"
down_revision = "a1c3e5b7d9f2"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

OLD_NAME = "远程诊断服务量"
NEW_NAME = "共享诊断协同量"

#: 只读探测：仍叫旧名的那一行。不 SELECT *，只取定位所需的两列。
_STALE_SQL = sa.text(
    "SELECT id, name FROM performance_indicators "
    "WHERE key = 'remote_exam' AND name = :old"
)


def _report(direction: str, frm: str, to: str) -> None:
    bind = op.get_bind()
    rows = bind.execute(_STALE_SQL, {"old": frm}).fetchall()
    if not rows:
        return
    # 处置 SQL **只写在本文件的 docstring 里**，不进这条日志：
    # `tests/test_migration_data_safety.py` 是正则扫 SQL 形状的 fail-closed 闸门，
    # 分不出"执行的 SQL"和"写给人看的 SQL"——把 UPDATE 语句放进日志字符串会被
    # 判成本迁移在静默改数据。这不是闸门的毛病：宁可误报也不漏报。
    logger.warning(
        "绩效指标 remote_exam 仍名为「%s」（%s）。该维度自本轮起两侧都计"
        "（申请方 + 共享诊断中心），旧名名不副实，建议改为「%s」。"
        "**本迁移不替人改**：这是管理端可编辑的现场配置，可能已被写进各县的考核"
        "文件与报表标题。处置办法（SQL 与界面路径）见本迁移文件 %s 的 docstring。"
        "不改不影响计分——计分只认 key。%s",
        frm, [tuple(r) for r in rows], to, revision,
        "" if direction == "upgrade" else "（回退方向：改回旧名）",
    )


def upgrade() -> None:
    _report("upgrade", OLD_NAME, NEW_NAME)


def downgrade() -> None:
    # 升级没改过数据，回退自然也无从改起；这里同样只报告，方向相反。
    _report("downgrade", NEW_NAME, OLD_NAME)
