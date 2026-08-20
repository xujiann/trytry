"""科室信息基础库补 created_at（departments）

departments（浙#9 科室信息基础库，org_id+code 唯一）缺 created_at（建档时间）。
数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1（37 → 36）。

平台链迁移，范式同 c1d2e3f4a5b2：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: d2e3f4a5b6c3
Revises: c1d2e3f4a5b2
Create Date: 2026-08-19 09:22:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd2e3f4a5b6c3'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'departments',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_departments_created_at'), 'departments', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('departments') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_departments_created_at'), table_name='departments')
    op.drop_column('departments', 'created_at')
