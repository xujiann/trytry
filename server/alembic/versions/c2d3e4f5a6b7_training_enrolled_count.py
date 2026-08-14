"""第十轮：实训计划补已占名额计数列 enrolled_count

原先报名用 COUNT(*) >= capacity 判满，是 check-then-act，并发下超额
（实测容量 2 报上 3 人）。加计数列，报名/退报名走原子增减。
回填：按现有 status='enrolled' 的报名数补齐存量计划。

Revision ID: c2d3e4f5a6b7
Revises: b1c2d3e4f5a6
"""
import sqlalchemy as sa
from alembic import op

revision = "c2d3e4f5a6b7"
down_revision = "b1c2d3e4f5a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("training_plans") as batch:
        batch.add_column(sa.Column("enrolled_count", sa.Integer(), nullable=False,
                                   server_default="0"))
    # 回填：按当前在册报名数
    op.execute(
        "UPDATE training_plans SET enrolled_count = ("
        "  SELECT COUNT(*) FROM training_enrollments te"
        "  WHERE te.plan_id = training_plans.id AND te.status = 'enrolled')"
    )


def downgrade() -> None:
    with op.batch_alter_table("training_plans") as batch:
        batch.drop_column("enrolled_count")
