"""收费调价历史与就诊凭据（浙江省指南 #55 / #27）

Revision ID: e7f8a2b3c4d5
Revises: d6e7f8a2b3c4
"""
import sqlalchemy as sa
from alembic import op

revision = "e7f8a2b3c4d5"
down_revision = "d6e7f8a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 调价历史：价格要对外公示，公示的前提是改过什么能说清楚。
    # 不参与任何金额计算（已计费明细存的是价格快照），纯粹对外解释用。
    op.create_table(
        "charge_price_changes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("item_id", sa.Integer(), sa.ForeignKey("charge_items.id"), nullable=False,
                  index=True),
        sa.Column("old_price", sa.Float(), nullable=False),
        sa.Column("new_price", sa.Float(), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("effective_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("changed_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    # 就诊凭据：与电子健康卡号并存。卡号是身份（终身唯一），凭据是介质
    # （会丢、要挂失重发）。合成一个字段会让患者一丢卡就得换身份。
    op.create_table(
        "visit_credentials",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False,
                  index=True),
        sa.Column("credential_no", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("credential_type", sa.String(16), nullable=False, server_default="card",
                  index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", index=True),
        sa.Column("issued_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("issued_at", sa.DateTime(), nullable=False),
        sa.Column("closed_at", sa.DateTime(), nullable=True),
        sa.Column("close_reason", sa.String(128), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_table("visit_credentials")
    op.drop_table("charge_price_changes")
