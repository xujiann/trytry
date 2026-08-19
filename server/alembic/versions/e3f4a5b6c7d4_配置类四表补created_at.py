"""配置/目录类四表补 created_at（consult_experts + drg_groups + report_templates + print_templates)

四张配置/目录台账表均缺 created_at（建档时间）：会诊专家、DRG 分组目录、共享中心
报告模板、打印模板。同为"管理端维护的配置类小表"，一并补齐（先例 d9f0a1b2c3e4
两表一迁移）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮降 4（36 → 32）。

平台链迁移，范式同 d2e3f4a5b6c3：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: e3f4a5b6c7d4
Revises: d2e3f4a5b6c3
Create Date: 2026-08-20 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e3f4a5b6c7d4'
down_revision: Union[str, Sequence[str], None] = 'd2e3f4a5b6c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("consult_experts", "drg_groups", "report_templates", "print_templates")


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
