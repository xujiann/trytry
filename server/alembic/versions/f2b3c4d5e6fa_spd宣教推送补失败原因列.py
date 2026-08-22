"""spd 宣教推送补 fail_reason：把"为什么没发出去"记下来

`spd_edu_pushes.status=failed` 此前不带原因，而失败至少有三种、处置完全不同：
运营没配微信模板、患者没绑微信/没有手机号、通道接口未受理。都显示成"发送失败"，
实施期只能挨个去猜该找谁。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

Revision ID: f2b3c4d5e6fa
Revises: e1a2b3c4d5e9
Create Date: 2026-08-22 12:40:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2b3c4d5e6fa'
down_revision: Union[str, Sequence[str], None] = 'e1a2b3c4d5e9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spd_edu_pushes",
        sa.Column("fail_reason", sa.String(length=200), nullable=False, server_default=""),
    )
    # 回填历史行之后去掉默认：默认值是为了让存量行有值，不是列的语义
    with op.batch_alter_table("spd_edu_pushes") as batch:
        batch.alter_column("fail_reason", server_default=None)


def downgrade() -> None:
    op.drop_column("spd_edu_pushes", "fail_reason")
