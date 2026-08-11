"""M12 DRG分组目录与病案首页入组字段

Revision ID: d0b1c2e3f4a6
Revises: c8a9b0d1e2f3
Create Date: 2026-08-11 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd0b1c2e3f4a6'
down_revision: Union[str, Sequence[str], None] = 'c8a9b0d1e2f3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "drg_groups",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=16), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("base_weight", sa.Float(), nullable=False),
        sa.Column("keywords", sa.String(length=256), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_drg_groups_code"), "drg_groups", ["code"], unique=True)

    op.add_column(
        "case_summaries",
        sa.Column("drg_code", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "case_summaries",
        sa.Column("drg_weight", sa.Float(), nullable=False, server_default="0"),
    )
    op.create_index(
        op.f("ix_case_summaries_drg_code"), "case_summaries", ["drg_code"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_case_summaries_drg_code"), table_name="case_summaries")
    op.drop_column("case_summaries", "drg_weight")
    op.drop_column("case_summaries", "drg_code")
    op.drop_index(op.f("ix_drg_groups_code"), table_name="drg_groups")
    op.drop_table("drg_groups")
