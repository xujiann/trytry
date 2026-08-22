"""护理记录关联住院医嘱（P1-24a 护理执行联动）

Revision ID: d4f6a8c0e2b6
Revises: c2e4a6b8d0f2
Create Date: 2026-08-22

为什么加这一列：医嘱执行登记（order_executions）落的是"执行了"这个动作，
而执行产生的护理观察（皮试后观察、输液巡视）落在 nursing_records——两边此前
互不相认，质控想回答"这条医嘱的执行有没有配套护理记录"只能靠时间猜。
护理记录挂上可空的医嘱 id 后，联动是显式外键而不是口径约定。

可空是语义要求：日常巡视、病情观察类护理记录本来就不对应任何医嘱。
外键约束沿用 a4c8e2f60b19（claimed_org_id）的先例：迁移只加列与索引，
约束由 ORM 元数据在建表路径上声明——SQLite 的 ALTER 加不了表级约束，
两套方言各写一份反而引入漂移面。
"""
import sqlalchemy as sa
from alembic import op

revision = "d4f6a8c0e2b6"
down_revision = "a9c1e3b5d7f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "nursing_records", sa.Column("inpatient_order_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_nursing_records_inpatient_order_id", "nursing_records", ["inpatient_order_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_nursing_records_inpatient_order_id", table_name="nursing_records")
    op.drop_column("nursing_records", "inpatient_order_id")
