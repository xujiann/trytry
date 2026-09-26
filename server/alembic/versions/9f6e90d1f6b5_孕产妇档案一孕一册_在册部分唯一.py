"""孕产妇档案一孕一册：patient_id 全量唯一 → 「在册」部分唯一（P1-140）

原先 `uq_maternal_patient` 是 patient_id 全量唯一，建册接口又是「查到这位妇女的任何一本就原样返回」：
一位妇女一生只能建一本。上一胎产后访视结案（closed）之后再孕，建册拿回的是那本已结案的旧档案——
页面上已结案的档案没有访视、分娩入口，这一胎的产检、分娩、高危标记都无处可记，审方也认不出她在孕期。
孕次 / 产次（gravidity / parity）两列本来就是为「不止一胎」准备的。

正确语义是「同一位妇女同一时刻只有一本在册（未结案）的档案」：结案的历次档案可以多本，未结案的只能
一本。并发建册仍由库兜底——抢输的一路撞部分唯一索引，拿回既有那本（与原先全量唯一时同一道闸门）。

## 升级

纯结构变更（C 档），不动任何存量行：删全量唯一约束、建部分唯一索引。存量里每位妇女至多一本（旧约束
保证的），新索引约束的行是旧约束的子集，**不可能有冲突**，无需探测。SQLite 删表级唯一约束要走 batch
（重建表）；PostgreSQL 上等价于 `ALTER TABLE maternal_records DROP CONSTRAINT uq_maternal_patient`。

## 回退

回退要把全量唯一建回来。升级之后同一位妇女可能已有多本档案（结案的上一胎 + 在册的这一胎），全量唯一
建不上。**先探存量**：只要有一位妇女名下多于一本，就拒绝回退并指名这些档案——每一本都是真实的孕产
记录，挂着产检、分娩、产前筛查，删哪本、并哪本不能由程序替人决定。回退前的人工处置：

    -- 查：哪些妇女名下不止一本（逐本列出，连同状态与建册时间）
    SELECT patient_id, id, status, lmp, edc, created_at FROM maternal_records
     WHERE patient_id IN (SELECT patient_id FROM maternal_records GROUP BY patient_id HAVING COUNT(*) > 1)
     ORDER BY patient_id, id;
    -- 这些都是真实记录，不要删除，也不要改写 patient_id。要回退只能先把本版本之后新建的档案
    -- （连同挂在它上面的 maternal_visits / delivery_records / prenatal_screenings）导出归档、与业务方
    -- 确认处置之后再回退；否则放弃回退——保留部分唯一索引不妨碍旧版本代码读写，只是旧代码按
    -- patient_id 取 `.first()`，取到的是哪一本不确定。

Revision ID: 9f6e90d1f6b5
Revises: c3e4f5a6b7d9
Create Date: 2026-09-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "9f6e90d1f6b5"
down_revision: Union[str, Sequence[str], None] = "c3e4f5a6b7d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX_NAME = "uq_maternal_patient_open"
_WHERE = "status <> 'closed'"


def upgrade() -> None:
    # SQLite 删表级唯一约束要走 batch（重建表）；PG 上等价于 DROP CONSTRAINT
    with op.batch_alter_table("maternal_records") as batch:
        batch.drop_constraint("uq_maternal_patient", type_="unique")
    op.create_index(
        _INDEX_NAME,
        "maternal_records",
        ["patient_id"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    duplicated = op.get_bind().execute(sa.text(
        "SELECT patient_id, id FROM maternal_records WHERE patient_id IN "
        "(SELECT patient_id FROM maternal_records GROUP BY patient_id HAVING COUNT(*) > 1) "
        "ORDER BY patient_id, id"
    )).fetchall()
    if duplicated:
        raise RuntimeError(
            "拒绝回退：maternal_records 里有妇女名下不止一本孕产妇档案，全量唯一建不回去。"
            f"（patient_id, 档案 id）：{[tuple(row) for row in duplicated]}。处置办法见本迁移 docstring 的「回退」一节。"
        )
    op.drop_index(_INDEX_NAME, table_name="maternal_records")
    with op.batch_alter_table("maternal_records") as batch:
        batch.create_unique_constraint("uq_maternal_patient", ["patient_id"])
