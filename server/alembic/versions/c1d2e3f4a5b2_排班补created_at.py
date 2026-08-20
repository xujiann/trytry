"""共享中心排班补 created_at（duty_rosters）

duty_rosters（①-④共享中心排班）有 duty_date（值班业务日期），缺 created_at（排班
录入库时间）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1（38 → 37）。

平台链迁移，范式同 b9c1d2e3f4a9：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: c1d2e3f4a5b2
Revises: b9c1d2e3f4a9
Create Date: 2026-08-19 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c1d2e3f4a5b2'
down_revision: Union[str, Sequence[str], None] = 'b9c1d2e3f4a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'duty_rosters',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_duty_rosters_created_at'), 'duty_rosters', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('duty_rosters') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_duty_rosters_created_at'), table_name='duty_rosters')
    op.drop_column('duty_rosters', 'created_at')
