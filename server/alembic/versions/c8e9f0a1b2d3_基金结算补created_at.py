"""基金结算补 created_at

年终清算单（fund_settlements）此前没有创建时间列——与会计分录同为审计硬伤
（settled_at 是业务清算时点，不等于"这行记录何时写入"）。补 created_at，属数据
模型治理⑧"旧结构逐步迁移"。迁移范式同 b7d8e9f0a1c2（会计分录补 created_at）：
常量 server_default 回填历史行后，batch 去掉默认，使列与模型一致（新行走
ORM default=utcnow）。

Revision ID: c8e9f0a1b2d3
Revises: b7d8e9f0a1c2
Create Date: 2026-08-18 06:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8e9f0a1b2d3'
down_revision: Union[str, Sequence[str], None] = 'b7d8e9f0a1c2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'fund_settlements',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(
        op.f('ix_fund_settlements_created_at'), 'fund_settlements', ['created_at']
    )
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入在 PG/SQLite 上分叉）。
    with op.batch_alter_table('fund_settlements') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_fund_settlements_created_at'), table_name='fund_settlements')
    op.drop_column('fund_settlements', 'created_at')
