"""spd 其余配置/关联十二表补 created_at（created_at 欠账收官批）

spd_data_sources / spd_goods / spd_group_members / spd_package_bindings /
spd_package_usages / spd_path_instances / spd_path_nodes / spd_point_accounts /
spd_service_packages / spd_sync_logs / spd_targets / spd_team_members
十二张慢专病配置/关联表均缺 created_at（建档时间）。打包补齐（先例 b5c6d7e8f9a6）。
数据模型治理⑧"旧结构逐步迁移"，created_at 欠账棘轮降 12（14 → 2）——至此欠账仅剩
blood_stocks（小型 upsert 表，降级）与 admissions（核心表，改列需先 ADR）。

**本迁移属 spd 链**（down_revision 指向 spd head，branch_labels=None 沿用 spd 血统），
不得挂到平台链——tests/test_spd_boundary.py 盯着。范式同 b5c6d7e8f9a6：常量
server_default 回填历史行后 batch 去掉默认，两端方言一致。

Revision ID: d8e9f1a2b3c8
Revises: b5c6d7e8f9a6
Create Date: 2026-08-20 04:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd8e9f1a2b3c8'
down_revision: Union[str, Sequence[str], None] = 'b5c6d7e8f9a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = (
    "spd_data_sources",
    "spd_goods",
    "spd_group_members",
    "spd_package_bindings",
    "spd_package_usages",
    "spd_path_instances",
    "spd_path_nodes",
    "spd_point_accounts",
    "spd_service_packages",
    "spd_sync_logs",
    "spd_targets",
    "spd_team_members",
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
