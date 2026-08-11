"""终审轮批2：处方点评、供应商与采购验收、存货盘点、统一知识库、检查资源要素、双通道申报

Revision ID: f3a4b5c6d7e8
Revises: e1f2a3b4c5d6
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3a4b5c6d7e8"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prescription_comments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prescription_id", sa.Integer(), nullable=False),
        sa.Column("grade", sa.String(length=16), nullable=False),
        sa.Column("issues", sa.String(length=256), nullable=False),
        sa.Column("comment", sa.String(length=1024), nullable=False),
        sa.Column("reviewer_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["prescription_id"], ["prescriptions.id"]),
        sa.ForeignKeyConstraint(["reviewer_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prescription_id", name="uq_rx_comment_prescription"),
    )
    op.create_index(
        op.f("ix_prescription_comments_prescription_id"),
        "prescription_comments",
        ["prescription_id"],
        unique=False,
    )
    op.create_index(op.f("ix_prescription_comments_grade"), "prescription_comments", ["grade"], unique=False)
    op.create_table(
        "suppliers",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("contact", sa.String(length=64), nullable=False),
        sa.Column("license_no", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "purchase_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("supplier_id", sa.Integer(), nullable=False),
        sa.Column("item_type", sa.String(length=16), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("note", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["supplier_id"], ["suppliers.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_purchase_orders_org_id"), "purchase_orders", ["org_id"], unique=False)
    op.create_index(op.f("ix_purchase_orders_status"), "purchase_orders", ["status"], unique=False)
    op.create_table(
        "stock_takes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("drug_code", sa.String(length=64), nullable=False),
        sa.Column("book_qty", sa.Integer(), nullable=False),
        sa.Column("actual_qty", sa.Integer(), nullable=False),
        sa.Column("diff", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=256), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_stock_takes_org_id"), "stock_takes", ["org_id"], unique=False)
    op.create_table(
        "knowledge_entries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("body", sa.String(length=4096), nullable=False),
        sa.Column("expire_date", sa.String(length=10), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_knowledge_entries_category"), "knowledge_entries", ["category"], unique=False)
    op.create_index(op.f("ix_knowledge_entries_title"), "knowledge_entries", ["title"], unique=False)
    op.create_table(
        "exam_resources",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("center_type", sa.String(length=16), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("device", sa.String(length=128), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("duration_min", sa.Integer(), nullable=False),
        sa.Column("notes", sa.String(length=512), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_exam_resources_org_id"), "exam_resources", ["org_id"], unique=False)
    op.create_index(op.f("ix_exam_resources_center_type"), "exam_resources", ["center_type"], unique=False)
    op.create_table(
        "dual_channel_apps",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("drug_name", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_comment", sa.String(length=512), nullable=False),
        sa.Column("reviewed_by", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dual_channel_apps_patient_id"), "dual_channel_apps", ["patient_id"], unique=False)
    op.create_index(op.f("ix_dual_channel_apps_status"), "dual_channel_apps", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_dual_channel_apps_status"), table_name="dual_channel_apps")
    op.drop_index(op.f("ix_dual_channel_apps_patient_id"), table_name="dual_channel_apps")
    op.drop_table("dual_channel_apps")
    op.drop_index(op.f("ix_exam_resources_center_type"), table_name="exam_resources")
    op.drop_index(op.f("ix_exam_resources_org_id"), table_name="exam_resources")
    op.drop_table("exam_resources")
    op.drop_index(op.f("ix_knowledge_entries_title"), table_name="knowledge_entries")
    op.drop_index(op.f("ix_knowledge_entries_category"), table_name="knowledge_entries")
    op.drop_table("knowledge_entries")
    op.drop_index(op.f("ix_stock_takes_org_id"), table_name="stock_takes")
    op.drop_table("stock_takes")
    op.drop_index(op.f("ix_purchase_orders_status"), table_name="purchase_orders")
    op.drop_index(op.f("ix_purchase_orders_org_id"), table_name="purchase_orders")
    op.drop_table("purchase_orders")
    op.drop_table("suppliers")
    op.drop_index(op.f("ix_prescription_comments_grade"), table_name="prescription_comments")
    op.drop_index(
        op.f("ix_prescription_comments_prescription_id"), table_name="prescription_comments"
    )
    op.drop_table("prescription_comments")
