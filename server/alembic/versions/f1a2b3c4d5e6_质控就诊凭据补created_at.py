"""质控记录与就诊凭据补 created_at（qc_records + visit_credentials）

两张审计点名的缺 created_at 台账：qc_records 只有业务 record_date、visit_credentials
只有 issued_at（发放时间），都缺"记录写入时间"。属数据模型治理⑧"旧结构逐步迁移"，
created_at 欠账棘轮再降 2（48 → 46）。

迁移范式同 d9f0a1b2c3e4：常量 server_default 回填历史行后 batch 去掉默认，
使列与模型一致（新行走 ORM default=utcnow）。SQLite 不能 ADD COLUMN 带非常量默认
（如 CURRENT_TIMESTAMP），故先用常量回填再撤默认，两端方言一致。

Revision ID: f1a2b3c4d5e6
Revises: d9f0a1b2c3e4
Create Date: 2026-08-19 02:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'd9f0a1b2c3e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("qc_records", "visit_credentials")


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
