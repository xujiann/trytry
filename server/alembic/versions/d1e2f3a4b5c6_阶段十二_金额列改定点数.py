"""阶段十二：43 个金额列 Float → Numeric(14,2)

浮点存金额的误差在阶段七就露头了——`test_fund.py` 里不得不写
`abs(distributed - 200000) < 1.0` 的容差断言才能通过。国产库（达梦/人大金仓）
对浮点的处理与 SQLite 又不相同，信创适配时这批列首当其冲，故两件事一次做完。

`asdecimal=False`：Python 侧仍收发 float。这一步拿到的是**存储与库侧聚合**的
精确性（PostgreSQL/国产库的 SUM/AVG 对 NUMERIC 是精确的），Python 侧逐笔运算
仍是浮点。端到端 Decimal 的代价与计划见 `docs/信创适配与部署.md`，不含糊带过。

SQLite 不支持 ALTER COLUMN，故用 `batch_alter_table` 重建表；
数值本身无需转换，REAL 值写进 NUMERIC 亲和列按原值保留。

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""
import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("consultations") as batch:
        batch.alter_column("fee", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("admin_projects") as batch:
        batch.alter_column("budget_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("insurance_settlements") as batch:
        batch.alter_column("total_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("insurance_pay", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("self_pay", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("finance_entries") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("case_summaries") as batch:
        batch.alter_column("total_cost", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("drug_cost", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("charge_items") as batch:
        batch.alter_column("price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("bill_details") as batch:
        batch.alter_column("unit_price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("settlements") as batch:
        batch.alter_column("total_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("insurance_pay", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("self_pay", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("exam_resources") as batch:
        batch.alter_column("price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("payroll_records") as batch:
        batch.alter_column("base_salary", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("total", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("budgets") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("cssd_cost_items") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("payment_orders") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("refunded_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("reconciliation_batches") as batch:
        batch.alter_column("total_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("diff_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("reconciliation_diffs") as batch:
        batch.alter_column("local_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("remote_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("vouchers") as batch:
        batch.alter_column("total_debit", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("total_credit", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("department_costs") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("material_purchases") as batch:
        batch.alter_column("estimated_price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("contract_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("high_value_consumables") as batch:
        batch.alter_column("unit_price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("outbound_visits") as batch:
        batch.alter_column("total_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("insurance_pay", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("charge_price_changes") as batch:
        batch.alter_column("old_price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("new_price", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("fund_pools") as batch:
        batch.alter_column("total_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("prepay_ratio_pct", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("fund_prepayments") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("fund_periods") as batch:
        batch.alter_column("actual_amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("fund_settlements") as batch:
        batch.alter_column("total_income", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("total_expense", type_=sa.Numeric(14, 2), existing_type=sa.Float())
        batch.alter_column("balance", type_=sa.Numeric(14, 2), existing_type=sa.Float())
    with op.batch_alter_table("fund_distributions") as batch:
        batch.alter_column("amount", type_=sa.Numeric(14, 2), existing_type=sa.Float())


def downgrade() -> None:
    with op.batch_alter_table("consultations") as batch:
        batch.alter_column("fee", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("admin_projects") as batch:
        batch.alter_column("budget_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("insurance_settlements") as batch:
        batch.alter_column("total_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("insurance_pay", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("self_pay", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("finance_entries") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("case_summaries") as batch:
        batch.alter_column("total_cost", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("drug_cost", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("charge_items") as batch:
        batch.alter_column("price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("bill_details") as batch:
        batch.alter_column("unit_price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("settlements") as batch:
        batch.alter_column("total_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("insurance_pay", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("self_pay", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("exam_resources") as batch:
        batch.alter_column("price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("payroll_records") as batch:
        batch.alter_column("base_salary", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("total", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("budgets") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("cssd_cost_items") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("payment_orders") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("refunded_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("reconciliation_batches") as batch:
        batch.alter_column("total_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("diff_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("reconciliation_diffs") as batch:
        batch.alter_column("local_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("remote_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("vouchers") as batch:
        batch.alter_column("total_debit", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("total_credit", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("department_costs") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("material_purchases") as batch:
        batch.alter_column("estimated_price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("contract_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("high_value_consumables") as batch:
        batch.alter_column("unit_price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("outbound_visits") as batch:
        batch.alter_column("total_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("insurance_pay", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("charge_price_changes") as batch:
        batch.alter_column("old_price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("new_price", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("fund_pools") as batch:
        batch.alter_column("total_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("prepay_ratio_pct", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("fund_prepayments") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("fund_periods") as batch:
        batch.alter_column("actual_amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("fund_settlements") as batch:
        batch.alter_column("total_income", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("total_expense", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
        batch.alter_column("balance", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
    with op.batch_alter_table("fund_distributions") as batch:
        batch.alter_column("amount", type_=sa.Float(), existing_type=sa.Numeric(14, 2))
