"""created_at 欠账收官：blood_stocks 与 admissions 补行写入时间（ADR-0018）

52 张表的补列长征在这两张收尾，棘轮 BASELINE_MISSING_CREATED_AT 2 → 0：

- blood_stocks：当年按"小型 upsert 台账、价值低"降级留置——豁免留着就是
  别人效仿的口子，顺路补齐；
- admissions：核心冻结表，改列先过了 ADR-0018。它有 admitted_at（入院业务
  时间）但没有行写入时间，补录历史住院时二者不同。历史行回填**常量哨兵**
  1970-01-01 而不是抄 admitted_at：真值已不可考，用业务时间冒充写入时间是
  伪造精度（取舍详见 ADR-0018），与其余 52 表同一口径——哨兵值一眼可辨
  "此行早于该列存在"。

平台链迁移，范式同 f9e8d7c6b5a4：常量 server_default 回填历史行后 batch
去掉默认（SQLite 不能 ADD COLUMN 带非常量默认，两端方言一致）。纯加列，
不 UPDATE/DELETE 任何存量业务数据。

Revision ID: c1d2e3f4a5b6
Revises: b5d9f3a71c2e
Create Date: 2026-08-31 09:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, Sequence[str], None] = 'b5d9f3a71c2e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ('blood_stocks', 'admissions')


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                'created_at',
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("'1970-01-01 00:00:00'"),
            ),
        )
        # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
        with op.batch_alter_table(table) as batch:
            batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for table in reversed(_TABLES):
        op.drop_column(table, 'created_at')
