"""平台零散八表补 created_at

account_subjects / chronic_disease_types / drug_rules / exam_resources /
infectious_diseases / org_group_members / performance_indicators / referral_certs
八张平台侧配置/目录/关联表均缺 created_at（建档时间）。打包补齐（先例
f4a5b6c7d8e5）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮降 8（22 → 14）。

平台链迁移，范式同 f4a5b6c7d8e5：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: c7d8e9f1a2b7
Revises: f4a5b6c7d8e5
Create Date: 2026-08-20 03:50:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f1a2b7'
down_revision: Union[str, Sequence[str], None] = 'f4a5b6c7d8e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "account_subjects",
    "chronic_disease_types",
    "drug_rules",
    "exam_resources",
    "infectious_diseases",
    "org_group_members",
    "performance_indicators",
    "referral_certs",
)


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
