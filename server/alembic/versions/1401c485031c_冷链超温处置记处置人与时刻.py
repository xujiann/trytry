"""冷链超温处置记处置人与处置时刻（P2-1503）

`cold_chain_records` 的超温处置只写 `handled` 与 `handle_note`：谁在什么时候处置的，库里答不上来，只能去翻审计日志的请求
路径（第四十四批扫描 AH1-7）。兄弟路径室内质控的失控处理早就记处置人与处置时刻（`qc_measurements.handled_by` /
`handled_at`，P2-492 / P2-1472）。

## 升级

纯加列（C 档）：`handled_by`（`String(64)`，非空、库默认空串）、`handled_at`（`DateTime`，可空），不动任何存量行。之后的
处置在判「未处置」的同一条条件 UPDATE 里写这两列（处置人记 full_name 或 username）。

**不回填存量**：本迁移之前已处置的超温记录，处置人本来就没记、处置时刻也只在审计日志里。审计日志
`POST /api/vaccine-supply/cold-chain/{id}/handle` 的请求路径带着记录号，要查可以按记录号去对（同一条只能处置一次，
P2-308，最早那条 2xx 才是处置），但把它写回这张表就是替人下了「谁处置的」结论：

    SELECT a.username, a.created_at, a.status_code FROM audit_logs a
     WHERE a.method = 'POST' AND a.path = '/api/vaccine-supply/cold-chain/' || :record_id || '/handle'
     ORDER BY a.created_at;

存量行 `handled_by` 为空串、`handled_at` 为空：接口照原样给出，页面只写处置说明。

## 回退

删两列。两列只是处置留痕，回退丢的是它们、不影响超温记录与处置说明本身。回退前要保留的话先导出：

    SELECT id, handled_by, handled_at FROM cold_chain_records WHERE handled_by <> '' OR handled_at IS NOT NULL;

Revision ID: 1401c485031c
Revises: 8d2e5b5c287f
Create Date: 2026-10-05
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1401c485031c"
down_revision: Union[str, Sequence[str], None] = "8d2e5b5c287f"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "cold_chain_records"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("handled_by", sa.String(length=64), nullable=False, server_default=""))
    op.add_column(_TABLE, sa.Column("handled_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("handled_at")
        batch_op.drop_column("handled_by")
