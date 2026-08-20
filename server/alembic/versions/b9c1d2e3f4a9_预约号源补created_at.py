"""预约号源补 created_at（appointment_slots）

appointment_slots（机构发布的分时段号源）有 slot_date（就诊业务日期），缺 created_at
（号源发布/建档时间）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1
（39 → 38）。

平台链迁移，范式同 a8b9c1d2e3f7：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: b9c1d2e3f4a9
Revises: a8b9c1d2e3f7
Create Date: 2026-08-19 08:35:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b9c1d2e3f4a9'
down_revision: Union[str, Sequence[str], None] = 'a8b9c1d2e3f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'appointment_slots',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_appointment_slots_created_at'), 'appointment_slots', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('appointment_slots') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_appointment_slots_created_at'), table_name='appointment_slots')
    op.drop_column('appointment_slots', 'created_at')
