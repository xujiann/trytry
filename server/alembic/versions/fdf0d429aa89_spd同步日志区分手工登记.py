"""spd 同步日志区分手工登记（P2-530）

`spd_sync_logs` 一张表记两种日志：采集器（`collectors.run_source`）每跑一轮写一条，数据源页的「记一次同步」
（接口方回报 / 手工补录，`devices.record_sync`）也写一条。采集的回溯窗口从「上一次成功同步」算起（P2-254），
原先两种不分——运维手工补登一条「成功」，窗口就挪到那一刻，此前没采回来的那段公卫随访血压再也补不回来。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

## 升级

纯加列：`manual`（布尔，非空）。存量行以库默认 false 补齐后去掉默认（默认值只为让存量行有值，不是列的语义，
与 `f2b3c4d5e6fa` 同一写法）——这是**本迁移新加的列**的补值，不动任何既有列。

存量行一律记为「不是手工登记」：这正是修前的算法（所有成功日志都当采集器的），升级不改变任何数据源的回溯窗口。
存量里的手工登记认不出来（修前没有来源列）；要补标的话，按审计日志里「记一次同步」的请求逐条对：

    SELECT a.created_at, a.path, a.username FROM audit_logs a
     WHERE a.method = 'POST' AND a.path LIKE '/api/spd/data-sources/%/sync-logs' ORDER BY a.created_at;
    -- 对上时间的那条日志（同一数据源、started_at 与审计时刻相差几秒）人工确认后：
    UPDATE spd_sync_logs SET manual = true WHERE id = :log_id;

标错了（把采集器的日志标成手工）只会让回溯窗口从更早的采集器成功日志算起、多补采一段（幂等键保证不重复入库），
不会漏数据；改回 `manual = false` 即可。

## 回退

删列。该列只是区分日志来源，回退丢的是它、不影响日志本身；回退后回溯窗口重新把手工登记算进去（修前行为）。
回退前要保留的话先导出：

    SELECT id, source_id, started_at FROM spd_sync_logs WHERE manual = true;

Revision ID: fdf0d429aa89
Revises: f4e3d2c1b0a9
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "fdf0d429aa89"
down_revision: Union[str, Sequence[str], None] = "f4e3d2c1b0a9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spd_sync_logs",
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("spd_sync_logs") as batch:
        batch.alter_column("manual", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("spd_sync_logs") as batch:
        batch.drop_column("manual")
