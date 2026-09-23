"""跨机构单据补"处理方机构"列（P1-58）

Revision ID: c3f7a9e1d5b2
Revises: b5d9f3a71c2e
Create Date: 2026-09-23

三张跨机构单据只记了一方，另一方（真正处理它的机构）在模型里没有落点，
于是按 id 推进它们的写接口无从判定归属——任何机构都能推进（P1-57 研判时实测）：

* `emergency_cases.dispatch_org_id`：调度机构。原先只有 `dest_org_id`（接收医院），
  院前推进、体征回传、绿道节点由调度方与接收医院做，调度方却不在单子上；
* `tcm_dispense_orders.pharmacy_org_id`：承接调配煎煮的共享中药房。原先只有下单机构；
* `pathology_specimens.center_org_id`：核收标本的病理中心。原先标本表一个机构列都没有。

写入点全在服务端（建事件时取操作人机构、首个非下单方推进时领取、核收时落库），
不收请求体——收了就又回到"校验调用方自报的值"那条老路（P1-56）。

**不回填。** 三列都找不到可复算的事实来源：急救事件没有建单人列；中药订单没有
推进人列；标本的 `received_by` 是手填的核收人姓名，不是账号。按 `users.org_id`
倒推是猜测，且会被调动/派驻改写（见 `visibility._ORG_COLUMN_MISSING` 的同一条理由）。
存量行留空，含义是"归属未定"：

* 急救：空的照旧不判（与改动前一致），新建的事件起才有调度方；
* 中药：空的由下一个推进它的非下单机构领取，与新订单同一口径；
* 病理：空的退回看申请单的 `claimed_org_id`（诊断中心领取过的，就是那家）。

三列都以 `org_id` 结尾，`visibility._relation_tables()` 会把调度方、中药房算进
与患者的服务关系（标本表无 `patient_id`，不受影响）——120 转运过、药房调配过这位
患者，本就是服务关系。

发现与修复：存量行为空不影响正确性；若事后要补，只能人工逐条核对纸质记录后
`UPDATE ... SET <列> = <机构 id> WHERE id = <行 id> AND <列> IS NULL`。
"""
import sqlalchemy as sa
from alembic import op

revision = "c3f7a9e1d5b2"
down_revision = "b5d9f3a71c2e"
branch_labels = None
depends_on = None

_COLUMNS = (
    ("emergency_cases", "dispatch_org_id"),
    ("tcm_dispense_orders", "pharmacy_org_id"),
    ("pathology_specimens", "center_org_id"),
)


def _fk_name(table: str, column: str) -> str:
    return f"fk_{table}_{column}_organizations"


def upgrade() -> None:
    # 带真外键：`exam_requests.claimed_org_id` 当年只加了裸整数列，外键至今挂在
    # 结构漂移基线里（`tests/snapshots/schema_drift_baseline.json`），这里不再添一笔。
    # SQLite 加外键要重建表，故走 batch（与 `e2f4a6c8b0d2` 同一做法）
    for table, column in _COLUMNS:
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column(column, sa.Integer(), nullable=True))
            batch.create_foreign_key(_fk_name(table, column), "organizations", [column], ["id"])
        op.create_index(f"ix_{table}_{column}", table, [column])


def downgrade() -> None:
    # 回退只丢本迁移新加的三列；它们只在本迁移之后由服务端写入，不承载既有业务数据
    for table, column in reversed(_COLUMNS):
        op.drop_index(f"ix_{table}_{column}", table_name=table)
        with op.batch_alter_table(table) as batch:
            batch.drop_constraint(_fk_name(table, column), type_="foreignkey")
            batch.drop_column(column)
