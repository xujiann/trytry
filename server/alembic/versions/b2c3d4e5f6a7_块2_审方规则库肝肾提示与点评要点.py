"""块2：审方规则库新增肝肾功能提示 renal_hepatic_note 与处方点评要点 review_points

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "drug_rules",
        sa.Column("renal_hepatic_note", sa.String(length=512), nullable=False, server_default=""),
    )
    op.add_column(
        "drug_rules",
        sa.Column("review_points", sa.String(length=512), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("drug_rules", "review_points")
    op.drop_column("drug_rules", "renal_hepatic_note")
