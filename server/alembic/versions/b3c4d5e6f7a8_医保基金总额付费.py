"""医保基金总额付费：池子 / 预付 / 周期预结 / 年终清算 / 结余分配

预付、预结、清算刻意分三张表：真实资金只在预付与清算两处流动，预结是账面
对冲。混在一张"资金流水"里，年终对不上账时无从分辨"这笔是账面数还是真给了钱"。

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""
import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fund_pools",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("year", sa.Integer(), nullable=False, index=True),
        sa.Column("insurance_type", sa.String(16), nullable=False, server_default="resident",
                  index=True),
        # 可空：有的县全域一个池，有的按片区分池
        sa.Column("org_group_id", sa.Integer(), sa.ForeignKey("org_groups.id"), nullable=True,
                  index=True),
        sa.Column("total_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("prepay_ratio_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="active", index=True),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("year", "insurance_type", "org_group_id", name="uq_fund_pool_scope"),
    )
    op.create_table(
        "fund_prepayments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("fund_pools.id"), nullable=False,
                  index=True),
        sa.Column("batch_no", sa.String(32), nullable=False, server_default=""),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("paid_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "fund_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("fund_pools.id"), nullable=False,
                  index=True),
        sa.Column("period", sa.String(7), nullable=False, index=True),
        sa.Column("actual_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("source", sa.String(16), nullable=False, server_default="auto"),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("pool_id", "period", name="uq_fund_period"),
    )
    op.create_table(
        "fund_settlements",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("pool_id", sa.Integer(), sa.ForeignKey("fund_pools.id"), nullable=False,
                  unique=True, index=True),
        sa.Column("total_income", sa.Float(), nullable=False, server_default="0"),
        sa.Column("total_expense", sa.Float(), nullable=False, server_default="0"),
        # 负值即超支。平台不自动扣减，只记录处置选择。
        sa.Column("balance", sa.Float(), nullable=False, server_default="0"),
        sa.Column("overrun_action", sa.String(16), nullable=False, server_default="none"),
        sa.Column("formula_expr", sa.String(256), nullable=False, server_default="score"),
        sa.Column("score_basis", sa.String(128), nullable=False, server_default=""),
        sa.Column("settled_at", sa.DateTime(), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.UniqueConstraint("pool_id", name="uq_fund_settlement_pool"),
    )
    op.create_table(
        "fund_distributions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("settlement_id", sa.Integer(), sa.ForeignKey("fund_settlements.id"),
                  nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False,
                  index=True),
        # 冻结快照：此后调整绩效指标权重不得影响已分结果
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("score_detail", sa.JSON(), nullable=True),
        sa.Column("weight", sa.Float(), nullable=False, server_default="0"),
        sa.Column("share_pct", sa.Float(), nullable=False, server_default="0"),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("fund_distributions")
    op.drop_table("fund_settlements")
    op.drop_table("fund_periods")
    op.drop_table("fund_prepayments")
    op.drop_table("fund_pools")
