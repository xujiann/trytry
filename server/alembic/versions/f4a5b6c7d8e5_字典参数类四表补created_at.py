"""字典/参数类四表补 created_at（tcm_techniques + system_params + code_systems + code_entries）

四张字典/参数类配置表均缺 created_at（建档时间）：中医适宜技术库、系统参数、
统一编码字典的类型表与条目表。同为"管理端/种子维护的配置类小表"，继续打包
（先例 e3f4a5b6c7d4 四表一迁移）。数据模型治理⑧"旧结构逐步迁移"，
created_at 欠账棘轮降 4（32 → 28）。

平台链迁移，范式同 e3f4a5b6c7d4：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: f4a5b6c7d8e5
Revises: e3f4a5b6c7d4
Create Date: 2026-08-20 03:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a5b6c7d8e5'
down_revision: Union[str, Sequence[str], None] = 'e3f4a5b6c7d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("tcm_techniques", "system_params", "code_systems", "code_entries")


def upgrade() -> None:
    """Upgrade schema."""
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                'created_at',
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("'1970-01-01 00:00:00'"),
            ),
        )
        op.create_index(op.f(f'ix_{table}_created_at'), table, ['created_at'])
        # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
        with op.batch_alter_table(table) as batch:
            batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    for table in _TABLES:
        op.drop_index(op.f(f'ix_{table}_created_at'), table_name=table)
        op.drop_column(table, 'created_at')
