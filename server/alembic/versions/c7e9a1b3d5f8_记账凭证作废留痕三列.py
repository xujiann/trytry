"""记账凭证作废留痕：作废人、作废时间、作废原因（P2-522）

`vouchers` 只有 `posted_by` / `posted_at`，作废只翻一个状态：模块注释写着「冲销走作废……账务留痕的意义就在于错了
也要看得见」，作废却连谁作废、何时、为什么都不落——凭证作废之后试算平衡表里就少了这笔账，查账时只看得到一张
「已作废」的凭证。

## 升级

纯加列（C 档）：`voided_by`（可空、外键指 `users.id`）、`voided_at`（可空）、`void_reason`（非空、库默认空串），
不动任何存量行。SQLite 加外键要走 batch（整表重建）；PostgreSQL 上等价于三句 `ADD COLUMN` 加一句 `ADD CONSTRAINT`。

存量里已作废的凭证作废人、时间本来就没记，**不回填**：审计日志里 `POST /api/accounting/vouchers/{id}/void`
的请求路径带着凭证编号，要查可以按编号去对，但把它写回这张表就是替人下了「谁作废的」结论。存量行两列为空、
原因为空串，页面显示「—」与「（未写明）」。

## 回退

删三列（先删外键）。三列只是作废留痕，回退丢的是它们、不影响凭证与分录本身。回退前要保留的话先导出：

    SELECT id, voided_by, voided_at, void_reason FROM vouchers WHERE status = 'void';

Revision ID: c7e9a1b3d5f8
Revises: d8f2a6c4b1e3
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c7e9a1b3d5f8"
down_revision: Union[str, Sequence[str], None] = "d8f2a6c4b1e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "vouchers"
_FK = "fk_vouchers_voided_by_users"   # 显式命名：SQLite 的 batch 重建必须有约束名，回退时也才删得掉


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("voided_by", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("voided_at", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("void_reason", sa.String(length=256), nullable=False, server_default=""))
        batch.create_foreign_key(_FK, "users", ["voided_by"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_column("void_reason")
        batch.drop_column("voided_at")
        batch.drop_column("voided_by")
