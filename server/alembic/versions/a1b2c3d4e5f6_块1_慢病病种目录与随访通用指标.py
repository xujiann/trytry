"""块1：慢病病种目录 chronic_disease_types + 随访通用指标 followups.metrics

Revision ID: a1b2c3d4e5f6
Revises: c9d0e1f2a3b4
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "c9d0e1f2a3b4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "chronic_disease_types",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("level_rules", sa.JSON(), nullable=False),
        sa.Column("guidance", sa.String(length=512), nullable=False),
        sa.Column("followup_interval_days", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_chronic_disease_types_code"), "chronic_disease_types", ["code"], unique=True
    )
    # 存量随访记录的通用指标默认空对象
    op.add_column(
        "followups",
        sa.Column("metrics", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("followups", "metrics")
    op.drop_index(op.f("ix_chronic_disease_types_code"), table_name="chronic_disease_types")
    op.drop_table("chronic_disease_types")
