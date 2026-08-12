"""医疗质量三维指标与抗菌药物使用强度所需字段

指南 #14/#48/#49/#52：术前术后诊断符合率、抢救成功率、抗菌药物使用强度。
这三项的差距不是"没算"，是**没地方记**——本迁移补的就是那几个记录位。

Revision ID: d6e7f8a2b3c4
Revises: c5d6e7f8a2b3
"""
import sqlalchemy as sa
from alembic import op

revision = "d6e7f8a2b3c4"
down_revision = "c5d6e7f8a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 术前/术后诊断：诊断符合率的唯一来源，术式名不能替代
    op.add_column(
        "surgery_records",
        sa.Column("preop_diagnosis", sa.String(256), nullable=False, server_default=""),
    )
    op.add_column(
        "surgery_records",
        sa.Column("postop_diagnosis", sa.String(256), nullable=False, server_default=""),
    )
    # 抢救转归：""=未判定，与 failed 严格区分，否则成功率被系统性算低
    op.add_column(
        "emergency_cases",
        sa.Column("rescue_outcome", sa.String(16), nullable=False, server_default=""),
    )
    op.create_index(
        "ix_emergency_cases_rescue_outcome", "emergency_cases", ["rescue_outcome"]
    )
    # 抗菌药物标记与 DDD；ddd=0 表示未维护，统计侧按"未覆盖"计而非按 0 计算
    op.add_column(
        "drug_rules",
        sa.Column("antibiotic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_drug_rules_antibiotic", "drug_rules", ["antibiotic"])
    op.add_column("drug_rules", sa.Column("ddd", sa.Float(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("drug_rules", "ddd")
    op.drop_index("ix_drug_rules_antibiotic", table_name="drug_rules")
    op.drop_column("drug_rules", "antibiotic")
    op.drop_index("ix_emergency_cases_rescue_outcome", table_name="emergency_cases")
    op.drop_column("emergency_cases", "rescue_outcome")
    op.drop_column("surgery_records", "postop_diagnosis")
    op.drop_column("surgery_records", "preop_diagnosis")
