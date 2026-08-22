"""药品批次记"退回但已不可发"的量（药事域对账不变式收口）

Revision ID: e1b3c5d7f9a2
Revises: d4f6a8c0e2b6
Create Date: 2026-08-22

为什么加这一列：退药冲销原先无条件把发出的量补回 `drug_stocks.quantity`，
不看目标批次还能不能发。批次若已召回或已过效期，药确实回到了库房，却一片也
发不出去——实测一次冲销让可用汇总从 160 涨到 180，而实际可发仍是 50，
缺药预警与采购建议看的都是汇总，于是既发不出药、也不提示采购。

只把汇总那边跳过是不够的：那样"库房里有多少"与"账上有多少"之间就出现了
一个谁也算不出来的差额，对账不变式从此无法校验。把这笔量显式记在批次上，
可发余量就有了确定的算法（quantity - used_quantity - blocked_quantity），
汇总与批次两侧任何时刻都能对上，且与时钟无关。

不可空、默认 0：存量批次一片"退回后不可发"的量都没有，0 是事实而不是占位。
"""
import sqlalchemy as sa
from alembic import op

revision = "e1b3c5d7f9a2"
down_revision = "d4f6a8c0e2b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "drug_batches",
        sa.Column(
            "blocked_quantity", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("drug_batches", "blocked_quantity")
