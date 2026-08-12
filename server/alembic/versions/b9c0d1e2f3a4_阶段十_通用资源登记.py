"""阶段十：通用资源登记

只建一张表。号源、检查资源、手术间、血制品各有自己的表与状态机，
统一资源视图靠聚合实现——这条原则统一申请单中心当初就定过，
没有理由到了资源这里反过来。

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""
import sqlalchemy as sa
from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("unit", sa.String(16), nullable=False, server_default=""),
        sa.Column("location", sa.String(256), nullable=False, server_default=""),
        sa.Column("contact", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        sa.Column("withdraw_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "code", name="uq_resource_org_code"),
    )
    op.create_index("ix_resources_org_id", "resources", ["org_id"])
    op.create_index("ix_resources_resource_type", "resources", ["resource_type"])
    op.create_index("ix_resources_code", "resources", ["code"])
    op.create_index("ix_resources_status", "resources", ["status"])


def downgrade() -> None:
    op.drop_table("resources")
