"""处方明细补 created_at（prescription_items）

prescription_items（处方内逐条药品明细）缺 created_at（记录写入库时间）——处方主表
有时间维度，明细行没有。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1
（41 → 40）。

平台链迁移，范式同 e6f7a8b9c1d3：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: f7a8b9c1d2e5
Revises: e6f7a8b9c1d3
Create Date: 2026-08-19 07:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f7a8b9c1d2e5'
down_revision: Union[str, Sequence[str], None] = 'e6f7a8b9c1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'prescription_items',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_prescription_items_created_at'), 'prescription_items', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('prescription_items') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_prescription_items_created_at'), table_name='prescription_items')
    op.drop_column('prescription_items', 'created_at')
