"""县外就诊登记与绩效自定义公式（阶段四 T4.1 / T4.3）

Revision ID: a3b4c5d6e7f9
Revises: f2a3b4c5d6e8
"""
import sqlalchemy as sa
from alembic import op

revision = "a3b4c5d6e7f9"
down_revision = "f2a3b4c5d6e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbound_visits",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("visit_date", sa.String(10), nullable=False, index=True),
        sa.Column("external_org_name", sa.String(128), nullable=False),
        sa.Column("external_org_level", sa.String(16), nullable=False, server_default="city", index=True),
        sa.Column("visit_type", sa.String(16), nullable=False, server_default="outpatient", index=True),
        sa.Column("diagnosis_name", sa.String(256), nullable=False, server_default=""),
        sa.Column("total_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("insurance_pay", sa.Float(), nullable=False, server_default="0"),
        # 有值表示经县域内机构规范转出，无值即自行外出就医
        sa.Column("referral_id", sa.Integer(), sa.ForeignKey("referrals.id"), nullable=True),
        sa.Column("source", sa.String(20), nullable=False, server_default="manual", index=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "performance_formulas",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(32), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("expression", sa.String(512), nullable=False),
        sa.Column("unit", sa.String(16), nullable=False, server_default=""),
        sa.Column("higher_is_better", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("performance_formulas")
    op.drop_table("outbound_visits")
