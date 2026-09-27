"""spd 报告实例区分手工生成（P2-531）

定时推送（`jobs.spd_report_push`）判「当期出过没有」按（任务, 期间标签, 机构）去重（P2-255 加了机构）。「立即执行」
（`POST /report-instances`）按本人机构出一份、期间标签不带机构后缀、也不投递订阅人——没有所属机构的管理员 / 主任
点一下，出的那份正好是机构为空、标签不带后缀，与全域推送任务当期要出的那份一模一样：定时推送当它「已出过」，
当期不再生成，订阅人收不到。

**本迁移属 spd 链**（down_revision 指向 spd head），不得挂到平台链。

## 升级

纯加列：`manual`（布尔，非空）。存量行以库默认 false 补齐后去掉默认（`f2b3c4d5e6fa` 同一写法）——只填本迁移
新加的列。存量一律记为定时推送出的：这正是修前的判重算法，升级不让任何已出过的当期重新推送。存量里手工出的认不
出来；要补标的话按审计日志里 `POST /api/spd/report-instances` 的时刻对：

    SELECT a.created_at, a.username FROM audit_logs a
     WHERE a.method = 'POST' AND a.path = '/api/spd/report-instances' ORDER BY a.created_at;
    -- 对上时刻的那份实例（created_at 相差几秒）人工确认后：
    UPDATE spd_report_instances SET manual = true WHERE id = :instance_id;

补标只影响「那一期还推不推」：标成手工后，若还在那一期的期间内，下一轮定时推送会补出一份并投递订阅人。

## 回退

删列。回退后手工出的实例重新顶替当期推送（修前行为）；实例本身不受影响。回退前要保留的话先导出：

    SELECT id, task_id, period_label, org_id FROM spd_report_instances WHERE manual = true;

Revision ID: c3bfca22dbdd
Revises: fdf0d429aa89
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3bfca22dbdd"
down_revision: Union[str, Sequence[str], None] = "fdf0d429aa89"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "spd_report_instances",
        sa.Column("manual", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    with op.batch_alter_table("spd_report_instances") as batch:
        batch.alter_column("manual", server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("spd_report_instances") as batch:
        batch.drop_column("manual")
