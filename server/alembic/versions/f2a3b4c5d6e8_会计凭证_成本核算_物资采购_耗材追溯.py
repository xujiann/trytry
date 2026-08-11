"""会计科目与凭证、成本核算、物资采购、高值耗材追溯（阶段三 T3.1–T3.4）

Revision ID: f2a3b4c5d6e8
Revises: e1f2a3b4c5d7
"""
import sqlalchemy as sa
from alembic import op

revision = "f2a3b4c5d6e8"
down_revision = "e1f2a3b4c5d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------- T3.1 会计科目与凭证 ----------------
    op.create_table(
        "account_subjects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(16), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, index=True),
        # 余额方向：资产/费用借增，负债/净资产/收入贷增
        sa.Column("direction", sa.String(8), nullable=False, server_default="debit"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
    )
    op.create_table(
        "vouchers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("voucher_no", sa.String(32), nullable=False),
        sa.Column("voucher_date", sa.String(10), nullable=False, index=True),
        sa.Column("summary", sa.String(256), nullable=False, server_default=""),
        sa.Column("total_debit", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_credit", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft", index=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("posted_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("posted_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.UniqueConstraint("org_id", "voucher_no", name="uq_voucher_org_no"),
    )
    op.create_table(
        "voucher_entries",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("voucher_id", sa.Integer(), sa.ForeignKey("vouchers.id"), nullable=False, index=True),
        sa.Column("subject_code", sa.String(16), nullable=False, index=True),
        sa.Column("summary", sa.String(256), nullable=False, server_default=""),
        sa.Column("debit", sa.Float(), nullable=False, server_default="0"),
        sa.Column("credit", sa.Float(), nullable=False, server_default="0"),
    )

    # ---------------- T3.2 成本核算 ----------------
    op.create_table(
        "department_costs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("dept_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("cost_type", sa.String(16), nullable=False, index=True),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("dept_id", "period", "cost_type", name="uq_dept_cost_period_type"),
    )
    op.create_table(
        "cost_allocation_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("from_dept_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("to_dept_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=False, index=True),
        sa.Column("ratio_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("from_dept_id", "to_dept_id", name="uq_alloc_from_to"),
    )

    # ---------------- T3.3 物资采购 ----------------
    op.create_table(
        "material_purchases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("dept_id", sa.Integer(), sa.ForeignKey("departments.id"), nullable=True),
        sa.Column("item_name", sa.String(128), nullable=False),
        sa.Column("spec", sa.String(64), nullable=False, server_default=""),
        sa.Column("unit", sa.String(16), nullable=False, server_default="件"),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("estimated_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("reason", sa.String(512), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested", index=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("contract_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("contract_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("received_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("received_note", sa.String(512), nullable=False, server_default=""),
        sa.Column("requested_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )

    # ---------------- T3.4 高值耗材追溯 ----------------
    op.create_table(
        "high_value_consumables",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("barcode", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("spec", sa.String(64), nullable=False, server_default=""),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("supplier_id", sa.Integer(), sa.ForeignKey("suppliers.id"), nullable=True),
        sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("expire_date", sa.String(10), nullable=False, server_default="", index=True),
        sa.Column("unit_price", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="in_stock", index=True),
        sa.Column("used_patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=True, index=True),
        sa.Column("used_surgery_id", sa.Integer(), sa.ForeignKey("surgery_requests.id"), nullable=True, index=True),
        sa.Column("used_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    for table in (
        "high_value_consumables",
        "material_purchases",
        "cost_allocation_rules",
        "department_costs",
        "voucher_entries",
        "vouchers",
        "account_subjects",
    ):
        op.drop_table(table)
