"""会计分录补 created_at

会计分录（voucher_entries）此前没有创建时间列——审计上是硬伤（记账凭证无法回答
"这条分录何时入账"）。本迁移补上 created_at，属数据模型治理⑧"旧结构逐步迁移"。

跨方言：add_column 用**常量** server_default 回填历史行（SQLite 不允许 ADD COLUMN
带非常量默认如 CURRENT_TIMESTAMP）。历史行拿不到真实创建时间，以纪元时间为
"未知"哨兵；新行由 ORM 的 default=utcnow 写入当时时间。

Revision ID: b7d8e9f0a1c2
Revises: d3e4f5a6b7c8
Create Date: 2026-08-18 06:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b7d8e9f0a1c2'
down_revision: Union[str, Sequence[str], None] = 'd3e4f5a6b7c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 先带常量 server_default 加列，回填历史行（跨方言安全）……
    op.add_column(
        'voucher_entries',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(
        op.f('ix_voucher_entries_created_at'), 'voucher_entries', ['created_at']
    )
    # ……回填完成后去掉 server_default，让列与模型（default=utcnow，无 server_default）一致：
    # 否则非 ORM 插入会在 PG 上默默填 1970，而 SQLite 上又无此默认，两端分叉。
    # batch 模式让 SQLite 也能改列（表重建）。
    with op.batch_alter_table('voucher_entries') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_voucher_entries_created_at'), table_name='voucher_entries')
    op.drop_column('voucher_entries', 'created_at')
