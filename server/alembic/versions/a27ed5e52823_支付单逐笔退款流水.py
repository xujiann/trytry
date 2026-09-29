"""支付单逐笔退款流水（P2-730）

支付单退款原先只在 `payment_orders` 上累加 `refunded_amount`、覆写 `refunded_at`：退款原因收下就丢，通道的退款单号只在
回执里回显一次，两名经办先后退 30、70，库里只剩累计 100 和第二笔的时间——患者说「只到账 30」时答不出是哪一笔、谁退的、
为什么退，日终对账也没法与通道的退款流水逐笔对上。

## 升级

新建 `payment_refunds`：每退成一笔追加一行（支付单、金额、通道退款单号、原因、经办、时间）。`refunded_amount` 照旧是
占额快照，不动。只建表，**不回填**——存量只有累计额，拆不回逐笔，也不替人编；本迁移之前退过款的支付单，逐笔流水从
上线后的第一笔退款开始记。

## 回退

删表。回退后逐笔流水随之丢失，支付单上的累计已退金额不受影响。回退前要保留的话先导出：

    SELECT id, order_id, amount, refund_no, reason, operator_id, created_at FROM payment_refunds ORDER BY id;

Revision ID: a27ed5e52823
Revises: a1ef99c3f5fe
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a27ed5e52823"
down_revision: Union[str, Sequence[str], None] = "a1ef99c3f5fe"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "payment_refunds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("order_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("refund_no", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=256), nullable=False),
        sa.Column("operator_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["payment_orders.id"]),
        sa.ForeignKeyConstraint(["operator_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_payment_refunds_order_id"), "payment_refunds", ["order_id"])
    op.create_index(op.f("ix_payment_refunds_refund_no"), "payment_refunds", ["refund_no"])
    op.create_index(op.f("ix_payment_refunds_created_at"), "payment_refunds", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_payment_refunds_created_at"), table_name="payment_refunds")
    op.drop_index(op.f("ix_payment_refunds_refund_no"), table_name="payment_refunds")
    op.drop_index(op.f("ix_payment_refunds_order_id"), table_name="payment_refunds")
    op.drop_table("payment_refunds")
