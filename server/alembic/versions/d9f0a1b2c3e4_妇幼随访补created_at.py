"""妇幼随访补 created_at（maternal_visits + child_visits）

妇幼随访记录（产前/产后访视、新生儿/儿童体检）此前只有 visit_date（业务访视日），
没有 created_at（记录写入时间）——审计点名的缺 created_at 高风险台账之一。两表同域
同结构，一并补。属数据模型治理⑧"旧结构逐步迁移"。

迁移范式同 b7d8e9f0a1c2：常量 server_default 回填历史行后 batch 去掉默认，
使列与模型一致（新行走 ORM default=utcnow）。

Revision ID: d9f0a1b2c3e4
Revises: c8e9f0a1b2d3
Create Date: 2026-08-18 07:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd9f0a1b2c3e4'
down_revision: Union[str, Sequence[str], None] = 'c8e9f0a1b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("maternal_visits", "child_visits")


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
        op.create_index(op.f(f'ix_{table}_created_at'), table, ['created_at'])
        # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
        with op.batch_alter_table(table) as batch:
            batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for table in _TABLES:
        op.drop_index(op.f(f'ix_{table}_created_at'), table_name=table)
        op.drop_column(table, 'created_at')
