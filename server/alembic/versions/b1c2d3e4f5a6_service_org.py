"""第九轮：就诊凭据/妇女保健/老年评估补记服务机构

这三张表原先只有 patient_id，答不出"这条服务是哪家机构做的"。
横向隔离按"本机构服务过的患者"判可见性，缺了机构列的后果不只是统计少一维——
发卡机构反而看不到自己发的卡。

可空：存量行没有机构信息，不编造。

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
"""
import sqlalchemy as sa
from alembic import op

revision = "b1c2d3e4f5a6"
down_revision = "a9b8c7d6e5f4"
branch_labels = None
depends_on = None

_TABLES = ["visit_credentials", "women_health_records", "elderly_assessments"]


def upgrade() -> None:
    for table in _TABLES:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
        op.create_index(f"ix_{table}_org_id", table, ["org_id"])


def downgrade() -> None:
    for table in _TABLES:
        op.drop_index(f"ix_{table}_org_id", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_column("org_id")
