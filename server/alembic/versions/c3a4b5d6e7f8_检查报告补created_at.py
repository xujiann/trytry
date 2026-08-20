"""检查检验报告补 created_at（exam_reports）

exam_reports 有 reported_at（报告出具/危急值上报时间，业务语义），缺 created_at
（记录写入库时间）。数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮再降 1（44 → 43）。

平台链迁移，范式同 f9e8d7c6b5a4：常量 server_default 回填历史行后 batch 去掉默认，
两端方言一致（SQLite 不能 ADD COLUMN 带非常量默认）。

Revision ID: c3a4b5d6e7f8
Revises: f9e8d7c6b5a4
Create Date: 2026-08-19 06:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c3a4b5d6e7f8'
down_revision: Union[str, Sequence[str], None] = 'f9e8d7c6b5a4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'exam_reports',
        sa.Column(
            'created_at',
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("'1970-01-01 00:00:00'"),
        ),
    )
    op.create_index(op.f('ix_exam_reports_created_at'), 'exam_reports', ['created_at'])
    # 回填后去掉 server_default，让列与模型一致（避免非 ORM 插入两端分叉）。
    with op.batch_alter_table('exam_reports') as batch:
        batch.alter_column('created_at', server_default=None)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_exam_reports_created_at'), table_name='exam_reports')
    op.drop_column('exam_reports', 'created_at')
