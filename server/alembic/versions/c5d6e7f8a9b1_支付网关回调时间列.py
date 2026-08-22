"""工程包 I2：payment_orders 补回调确认时间列（callback_at）

真通道（HTTP 支付网关）是异步语义：下单只受理，订单停在 pending，
网关回调验签通过后才转 paid。回调确认时间与本地 paid_at 分列记录——
对账排差时"什么时候收到网关确认"是独立于"本地何时置为已支付"的事实。
渠道单号（trade_no）与渠道名（channel）既有列已够用，不再新增。

Revision ID: c5d6e7f8a9b1
Revises: b1f2a3c4d5e6
Create Date: 2026-08-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5d6e7f8a9b1"
down_revision: Union[str, Sequence[str], None] = "b1f2a3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("payment_orders", sa.Column("callback_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("payment_orders", "callback_at")
