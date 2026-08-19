"""spd 监测数据补 created_at（spd_measurements）

spd_measurements 有 measured_at（采集当时的业务时间，可由录入方指定为历史时刻），
但缺 created_at（记录写入库的时间）——两者语义不同：补录一条上个月的血压，
measured_at 是上月、created_at 是今天。数据模型治理⑧"旧结构逐步迁移"，
created_at 欠账棘轮再降 1（46 → 45）。

**本迁移属 spd 链**（down_revision 指向 spd head，branch_labels=None 沿用 spd 血统），
不得挂到平台链——tests/test_spd_boundary.py 盯着。范式同 f1a2b3c4d5e6：常量
server_default 回填历史行后 batch 去掉默认，两端方言一致。

Revision ID: a1b2c3d4e5f7
Revises: d2b3c4d5e6f7
Create Date: 2026-08-19 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f7'
down_revision: Union[str, Sequence[str], None] = 'd2b3c4d5e6f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'spd_measurements',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_spd_measurements_created_at'), 'spd_measurements', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('spd_measurements') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_spd_measurements_created_at'), table_name='spd_measurements')
    op.drop_column('spd_measurements', 'created_at')
