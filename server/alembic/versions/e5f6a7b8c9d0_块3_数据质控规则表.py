"""块3：数据质控规则引擎——规则表 qc_rules

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5f6a7b8c9d0"
down_revision: Union[str, Sequence[str], None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qc_rules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("target_table", sa.String(length=64), nullable=False),
        sa.Column("rule_type", sa.String(length=16), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("severity", sa.String(length=8), nullable=False, server_default="error"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qc_rules_code"), "qc_rules", ["code"], unique=True)
    op.create_index(op.f("ix_qc_rules_target_table"), "qc_rules", ["target_table"], unique=False)
    op.create_index(op.f("ix_qc_rules_rule_type"), "qc_rules", ["rule_type"], unique=False)
    op.create_index(op.f("ix_qc_rules_severity"), "qc_rules", ["severity"], unique=False)
    op.create_index(op.f("ix_qc_rules_active"), "qc_rules", ["active"], unique=False)


def downgrade() -> None:
    for index in ("active", "severity", "rule_type", "target_table", "code"):
        op.drop_index(op.f(f"ix_qc_rules_{index}"), table_name="qc_rules")
    op.drop_table("qc_rules")
