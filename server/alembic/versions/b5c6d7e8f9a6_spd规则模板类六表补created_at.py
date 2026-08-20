"""spd 规则/模板类六表补 created_at

spd_followup_rules / spd_intervention_templates / spd_point_rules / spd_questionnaires /
spd_referral_rules / spd_tags 六张慢专病配置表（随访规则、干预模板、积分规则、问卷、
转诊规则、标签）均缺 created_at（建档时间）。同为"管理端/种子维护的配置类小表"，
打包补齐（先例 f4a5b6c7d8e5）。数据模型治理⑧，created_at 欠账棘轮降 6（28 → 22）。

**本迁移属 spd 链**（down_revision 指向 spd head，branch_labels=None 沿用 spd 血统），
不得挂到平台链——tests/test_spd_boundary.py 盯着。范式同 a1b2c3d4e5f7：常量
server_default 回填历史行后 batch 去掉默认，两端方言一致。

Revision ID: b5c6d7e8f9a6
Revises: a1b2c3d4e5f7
Create Date: 2026-08-20 03:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b5c6d7e8f9a6'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "spd_followup_rules",
    "spd_intervention_templates",
    "spd_point_rules",
    "spd_questionnaires",
    "spd_referral_rules",
    "spd_tags",
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
