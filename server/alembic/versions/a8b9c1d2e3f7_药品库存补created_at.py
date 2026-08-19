"""药品库存补 created_at（drug_stocks）

drug_stocks（中心药房库存，org_id+drug_code 唯一）缺 created_at（记录建档时间）。
库存量本身走 upsert 增减（add_amount），但"这条库存档何时建的"仍应有写入时间。
数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1（40 → 39）。

平台链迁移，范式同 f7a8b9c1d2e5：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: a8b9c1d2e3f7
Revises: f7a8b9c1d2e5
Create Date: 2026-08-19 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a8b9c1d2e3f7'
down_revision: Union[str, Sequence[str], None] = 'f7a8b9c1d2e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'drug_stocks',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_drug_stocks_created_at'), 'drug_stocks', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('drug_stocks') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_drug_stocks_created_at'), table_name='drug_stocks')
    op.drop_column('drug_stocks', 'created_at')
