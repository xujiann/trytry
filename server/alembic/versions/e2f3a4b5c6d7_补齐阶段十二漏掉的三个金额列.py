"""补齐阶段十二漏掉的三个金额列

阶段十二按 amount/price/cost 这批命名族批量改类型，漏掉了三个不含这些词的
金额列：`voucher_entries.debit`、`voucher_entries.credit`、
`payroll_records.perf_bonus`。

前两个尤其要紧——整个复式记账、试算平衡表与合并报表都靠它们求和，
**平台最核心的两个金额列反而漏在了第一遍之外**。教训：按命名批量处理之后
必须回头核对剩下的清单，不能只看匹配到的那一批。
`test_stage12_dialect.py` 的命名族已补上这三个词，并配了豁免清单防腐用例。

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""
import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("voucher_entries") as batch:
        batch.alter_column("debit", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("credit", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("payroll_records") as batch:
        batch.alter_column("perf_bonus", type_=sa.Numeric(14, 2), existing_type=sa.Float())


def downgrade() -> None:
    with op.batch_alter_table("payroll_records") as batch:
        batch.alter_column("perf_bonus", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("voucher_entries") as batch:
        batch.alter_column("credit", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("debit", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
