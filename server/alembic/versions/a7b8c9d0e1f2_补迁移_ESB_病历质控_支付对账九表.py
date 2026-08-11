"""补迁移：ESB / 电子病历环节质控 / 统一支付对账 共九表

集成平台 ESB、病历环节质控、统一支付与对账三个开发块只改了模型未补迁移，
导致 alembic 链落后于 models.py——SQLite 开发库靠 create_all 建表看不出来，
但生产 PostgreSQL 走 `alembic upgrade head` 会缺这九张表。此处一次补齐。

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
"""
import sqlalchemy as sa
from alembic import op

revision = "a7b8c9d0e1f2"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------- 集成平台 ESB ----------------
    op.create_table(
        "esb_endpoints",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("system_type", sa.String(16), nullable=False, index=True),
        sa.Column("direction", sa.String(8), nullable=False, server_default="inbound", index=True),
        sa.Column("auth_token_hash", sa.String(200), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("rate_limit_per_min", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "esb_messages",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("endpoint_id", sa.Integer(), sa.ForeignKey("esb_endpoints.id"), nullable=False, index=True),
        sa.Column("msg_type", sa.String(32), nullable=False, index=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued", index=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("last_error", sa.String(1024), nullable=False, server_default=""),
        sa.Column("next_retry_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "esb_flows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "esb_flow_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("flow_id", sa.Integer(), sa.ForeignKey("esb_flows.id"), nullable=False, index=True),
        sa.Column("message_id", sa.Integer(), sa.ForeignKey("esb_messages.id"), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="succeeded", index=True),
        sa.Column("step_results", sa.JSON(), nullable=False),
        sa.Column("error", sa.String(1024), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )

    # ---------------- 电子病历与环节质控 ----------------
    op.create_table(
        "medical_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encounter_id", sa.Integer(), sa.ForeignKey("encounters.id"), nullable=False, unique=True, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("doctor_name", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("chief_complaint", sa.String(256), nullable=False, server_default=""),
        sa.Column("present_illness", sa.String(2048), nullable=False, server_default=""),
        sa.Column("past_history", sa.String(1024), nullable=False, server_default=""),
        sa.Column("physical_exam", sa.String(1024), nullable=False, server_default=""),
        sa.Column("diagnosis_basis", sa.String(1024), nullable=False, server_default=""),
        sa.Column("treatment_plan", sa.String(1024), nullable=False, server_default=""),
        sa.Column("qc_score", sa.Integer(), nullable=False, server_default="100", index=True),
        sa.Column("qc_grade", sa.String(4), nullable=False, server_default="甲", index=True),
        sa.Column("qc_defects", sa.JSON(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "record_qc_rules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("check_field", sa.String(32), nullable=False, index=True),
        sa.Column("rule", sa.String(24), nullable=False, index=True),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("deduct_points", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # ---------------- 统一支付与日终对账 ----------------
    op.create_table(
        "payment_orders",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("settlement_id", sa.Integer(), sa.ForeignKey("settlements.id"), nullable=False, index=True),
        sa.Column("channel", sa.String(16), nullable=False, index=True),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("trade_no", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("refunded_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("fail_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("paid_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("refunded_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "reconciliation_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("date", sa.String(10), nullable=False, index=True),
        sa.Column("total_orders", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("matched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unmatched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("diff_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "reconciliation_diffs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("batch_id", sa.Integer(), sa.ForeignKey("reconciliation_batches.id"), nullable=False, index=True),
        sa.Column("order_id", sa.Integer(), sa.ForeignKey("payment_orders.id"), nullable=True),
        sa.Column("trade_no", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("diff_type", sa.String(20), nullable=False, index=True),
        sa.Column("local_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("remote_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("detail", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    # 外键依赖顺序：先删引用方
    for table in (
        "reconciliation_diffs",
        "reconciliation_batches",
        "payment_orders",
        "record_qc_rules",
        "medical_records",
        "esb_flow_runs",
        "esb_flows",
        "esb_messages",
        "esb_endpoints",
    ):
        op.drop_table(table)
