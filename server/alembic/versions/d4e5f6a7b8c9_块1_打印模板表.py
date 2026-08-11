"""块1：报告打印——打印模板表 print_templates

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "print_templates",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("doc_type", sa.String(length=32), nullable=False),
        sa.Column("header_org_name", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("footer_note", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("show_qr", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_print_templates_doc_type"), "print_templates", ["doc_type"], unique=True)


def downgrade() -> None:
    op.drop_index(op.f("ix_print_templates_doc_type"), table_name="print_templates")
    op.drop_table("print_templates")
