"""传染病病例报告补 created_at（infectious_cases）

infectious_cases 有 reported_at（上报时间，业务语义）与 onset_date（发病日），缺
created_at（记录写入库时间）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮
再降 1（42 → 41）。

平台链迁移，范式同 d5e6f7a8b9c1：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: e6f7a8b9c1d3
Revises: d5e6f7a8b9c1
Create Date: 2026-08-19 07:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c1d3'
down_revision: Union[str, Sequence[str], None] = 'd5e6f7a8b9c1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'infectious_cases',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_infectious_cases_created_at'), 'infectious_cases', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('infectious_cases') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_infectious_cases_created_at'), table_name='infectious_cases')
    op.drop_column('infectious_cases', 'created_at')
