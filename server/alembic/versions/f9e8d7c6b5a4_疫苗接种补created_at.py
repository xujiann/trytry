"""疫苗接种记录补 created_at（vaccination_records）

vaccination_records 有 vaccinated_date（接种业务日期，String(10)），缺 created_at
（记录写入库时间）——补录历史接种史时二者不同，AEFI 归因/召回按批号追溯也需要
写入时间维度。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1（45 → 44）。

平台链迁移，范式同 f1a2b3c4d5e6：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: f9e8d7c6b5a4
Revises: f1a2b3c4d5e6
Create Date: 2026-08-19 03:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f9e8d7c6b5a4'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'vaccination_records',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_vaccination_records_created_at'), 'vaccination_records', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('vaccination_records') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_vaccination_records_created_at'), table_name='vaccination_records')
    op.drop_column('vaccination_records', 'created_at')
