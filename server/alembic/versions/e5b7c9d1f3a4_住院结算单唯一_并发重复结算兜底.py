"""住院结算单部分唯一索引：把"一次住院一张结算单"下沉到数据库

为什么加这条索引：`create_settlement` 原本是 check-then-act——先查未结明细、
再建结算单、再回填明细的 settlement_id，三步之间没有任何闸门。真 PG 上四路
并发出院结算实测建出**四张结算单、四条医保结算记录**（医保基金支出重复计入）、
押金被多冲 1500，而且前三张单挂着金额却一条明细都没有。SQLite 的库级写锁把
这个缺陷完全掩盖了，只有生产的 PostgreSQL 会现形。

应用层已改成 `UPDATE bill_details SET settlement_id=... WHERE settlement_id IS NULL`
按 rowcount 认领（判定与写入同一条 SQL），这条索引是第二道闸门——与全域基金池
D-2（uq_fund_pool_global）、居民账户绑定（uq_resident_account_patient）同一个
先例：应用层判定再怎么写，兜底也该落在库里。

部分索引而不是全量唯一：门诊结算的 admission_id 恒为 NULL，而 SQL 里
NULL != NULL 本来就不冲突，写成部分索引是把"只管住院结算"这层意思显式写出来。

**存量重复不阻塞升级**：结算单是财务凭证，不能像居民账户那样"留最早的、
其余清掉"——多出来的那几张单可能已经关联了支付单与医保结算记录，删除或改写
都属于财务冲正，必须由人来做。所以升级时先探一遍：有重复就跳过建索引并打一条
明确的 ERROR 日志（应用层的认领闸门此时已经生效，不会再产生新的重复），
人工冲正后由 DBA 手工补建：

    CREATE UNIQUE INDEX uq_settlement_inpatient_admission ON settlements (admission_id)
      WHERE bill_type = 'inpatient' AND admission_id IS NOT NULL;

Revision ID: e5b7c9d1f3a4
Revises: d4f6a8c0e2b6
Create Date: 2026-08-22
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "e5b7c9d1f3a4"
down_revision = "e1b3c5d7f9a2"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_INDEX_NAME = "uq_settlement_inpatient_admission"
_WHERE = "bill_type = 'inpatient' AND admission_id IS NOT NULL"


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            "SELECT admission_id, COUNT(*) AS n FROM settlements "
            f"WHERE {_WHERE} GROUP BY admission_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        logger.error(
            "settlements 存量已有重复的住院结算单（admission_id: %s），"
            "本次跳过建 %s。这些是财务凭证，需人工冲正后由 DBA 手工补建索引"
            "（SQL 见本迁移 docstring）。应用层的明细认领闸门已生效，不会再新增重复。",
            [row[0] for row in duplicates],
            _INDEX_NAME,
        )
        return
    op.create_index(
        _INDEX_NAME,
        "settlements",
        ["admission_id"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    # upgrade 可能因存量重复而跳过建索引，降级要能对付"索引不存在"
    inspector = sa.inspect(op.get_bind())
    if _INDEX_NAME in {i["name"] for i in inspector.get_indexes("settlements")}:
        op.drop_index(_INDEX_NAME, table_name="settlements")
