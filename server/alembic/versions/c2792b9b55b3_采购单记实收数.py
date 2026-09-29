"""采购单记实收数（P2-852）

药品采购「到货验收」原先不收实收数，一律按申请量整单入库：到了 60 盒，库存汇总与兜底批次各加 100，少到的 40 盒成了
账上能发、实际不存在的库存，要等盘点才以「盘亏」冒出来，采购单上也看不出只到了 60。兄弟路径物资采购早就按实收记
（`materials.ReceiveIn.received_quantity`，不超过采购量，记到单上并入库）。

## 升级

纯加列（C 档）：`purchase_orders.received_quantity`（整数、可空），不动任何存量行。之后的验收把实收数记在这一列。

**不回填**：本迁移之前验收的单是按申请量整单入库的（入库量就是 `quantity`），接口出参对这些行照样给空值，页面按
「采购量」显示；要核对存量验收单与库存流水的，按单号去对：

    SELECT id, org_id, item_code, quantity, status FROM purchase_orders WHERE status = 'received' ORDER BY id;

## 回退

删列。回退后实收数丢失、验收退回按申请量入库的旧口径；已入库的库存不受影响。回退前要保留的话先导出：

    SELECT id, item_code, quantity, received_quantity FROM purchase_orders WHERE received_quantity IS NOT NULL;

Revision ID: c2792b9b55b3
Revises: a27ed5e52823
Create Date: 2026-09-29
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2792b9b55b3"
down_revision: Union[str, Sequence[str], None] = "a27ed5e52823"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("purchase_orders", sa.Column("received_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("purchase_orders") as batch_op:
        batch_op.drop_column("received_quantity")
