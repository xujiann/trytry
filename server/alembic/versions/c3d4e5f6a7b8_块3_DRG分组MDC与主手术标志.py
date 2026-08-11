"""块3：DRG 分组目录新增 MDC、主手术关键词与兜底组标志

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("drg_groups", sa.Column("mdc", sa.String(length=8), nullable=False, server_default=""))
    op.add_column("drg_groups", sa.Column("mdc_name", sa.String(length=64), nullable=False, server_default=""))
    op.add_column(
        "drg_groups",
        sa.Column("procedure_keywords", sa.String(length=256), nullable=False, server_default=""),
    )
    op.add_column(
        "drg_groups",
        sa.Column("require_procedure", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "drg_groups",
        sa.Column("is_fallback", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(op.f("ix_drg_groups_mdc"), "drg_groups", ["mdc"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_drg_groups_mdc"), table_name="drg_groups")
    op.drop_column("drg_groups", "is_fallback")
    op.drop_column("drg_groups", "require_procedure")
    op.drop_column("drg_groups", "procedure_keywords")
    op.drop_column("drg_groups", "mdc_name")
    op.drop_column("drg_groups", "mdc")
