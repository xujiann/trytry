"""通用附件能力：attachments 表（检查报告/不良事件附件，本地磁盘存储元数据）

Revision ID: c9d0e1f2a3b4
Revises: b7c8d9e0f1a2
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c9d0e1f2a3b4"
down_revision: Union[str, Sequence[str], None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("owner_type", sa.String(length=32), nullable=False),
        sa.Column("owner_id", sa.Integer(), nullable=False),
        sa.Column("uploaded_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_attachments_sha256"), "attachments", ["sha256"], unique=False)
    op.create_index(op.f("ix_attachments_owner_type"), "attachments", ["owner_type"], unique=False)
    op.create_index(op.f("ix_attachments_owner_id"), "attachments", ["owner_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_attachments_owner_id"), table_name="attachments")
    op.drop_index(op.f("ix_attachments_owner_type"), table_name="attachments")
    op.drop_index(op.f("ix_attachments_sha256"), table_name="attachments")
    op.drop_table("attachments")
