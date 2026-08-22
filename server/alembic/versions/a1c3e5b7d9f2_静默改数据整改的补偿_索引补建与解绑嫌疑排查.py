"""静默改数据整改的补偿：跳过的唯一索引自动补建 + 静默解绑嫌疑记录点名

为什么需要一条单独的补偿迁移
----------------------------

`d3e4f5a6b7c8`（居民账户绑定唯一）与 `d5e6f7a8b9c0`（全域基金池唯一）已改成
不阻塞路径：探到冲突就跳过建索引、打 ERROR 日志、把处置 SQL 写进各自 docstring。
但 alembic 的版本表只记"这条 revision 跑过了"，**不记它当时跳没跳**——DBA 按日志
人工处置完冲突之后，再跑多少次 `alembic upgrade heads` 也不会回头补建那两个索引，
库会长期停在"没有兜底约束"的状态而没人发现。

所以补一条只做收尾的迁移：每次升级链走到这里都重新探一次，

* 索引已在 → 什么都不做；
* 索引不在、冲突也没了 → 建上（这正是"人工处置完再跑一次升级"的那一步）；
* 索引不在、冲突还在 → 再打一条指名冲突记录的 ERROR 日志，**不改任何数据**。

**它只补这一次，不是常驻巡检。** alembic 不会重跑已记录的 revision：库一旦升到链尾，
这条迁移就不再执行了。它接住的是"当时停在链中途、处置完再继续升级"的那批库
（`d3e4f5a6b7c8` 与本迁移之间还隔着十几条 revision），已经在链尾的库请按各自
docstring 手工 CREATE INDEX。真要常驻巡检，那是运维监控的事，不是迁移的事。

它同时是一条**重放安全**的收口：先于本次整改跑过旧版迁移的库，索引已经建好，
这条迁移在那里是纯 no-op，不会二次损坏任何东西。

已经被静默解绑的账户：查得出嫌疑，恢复不了
------------------------------------------

旧版 `d3e4f5a6b7c8` 的 `UPDATE resident_accounts SET patient_id = NULL` 已经在跑过
它的库上执行完毕。`patient_id` 是这条绑定关系的唯一载体，置空即信息丢失——平台
没有影子表、没有审计（解绑走的是迁移里的裸 SQL，不经过应用层的 AuditLog），
**这些绑定无法自动恢复，本迁移也不尝试恢复**。

能做的只有点名嫌疑：账户当前未绑定，但存在一份手机号与它相同的档案——正常流程下
这种账户早该自动绑上（`_autobind_by_phone`），现在没绑，多半就是被静默解绑的那批。
本迁移只**统计条数并打一条 WARNING**（不打手机号，PII 不进日志，见 CLAUDE.md §8），
明细查询与人工处置流程写在 `docs/运维手册.md`"静默解绑的人工排查"一节。

Revision ID: a1c3e5b7d9f2
Revises: e5b7c9d1f3a4
Create Date: 2026-08-22
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "a1c3e5b7d9f2"
down_revision = "e5b7c9d1f3a4"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_ACCOUNT_INDEX = "uq_resident_account_patient"
_POOL_INDEX = "uq_fund_pool_global"

#: 未绑定、但存在同手机号档案的账户条数——静默解绑的嫌疑面。
#: 明文态比 phone，密文态（工程包 E3 开启后）比 phone_idx，两条都算进去：
#: 同一个库里两态的行可能并存（回填脚本分批推进），只比一边会漏。
_SUSPECT_SQL = """
SELECT COUNT(*) FROM resident_accounts ra
 WHERE ra.patient_id IS NULL
   AND EXISTS (
     SELECT 1 FROM patients p
      WHERE (ra.phone IS NOT NULL AND ra.phone <> '' AND p.phone = ra.phone)
         OR (ra.phone_idx IS NOT NULL AND ra.phone_idx <> '' AND p.phone_idx = ra.phone_idx))
"""


def _indexes(bind: sa.engine.Connection, table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(table)}


def _retry_index(bind: sa.engine.Connection, *, name: str, table: str, columns: list[str],
                 where: str, conflict_sql: str, human: str) -> None:
    """索引缺失且已无冲突就补建；仍有冲突只报告，绝不改数据。"""
    if name in _indexes(bind, table):
        return
    rows = bind.execute(sa.text(conflict_sql)).fetchall()
    if rows:
        logger.error(
            "%s 仍有存量冲突，%s 依旧建不上（冲突记录：%s）。%s "
            "本迁移**不会**替人删改数据；人工处置完请手工 CREATE INDEX（SQL 见上述 docstring）——"
            "本迁移是链尾一次性补建点，跑过就不会再跑。",
            table, name, [tuple(r) for r in rows[:50]], human,
        )
        return
    op.create_index(
        name, table, columns, unique=True,
        sqlite_where=sa.text(where), postgresql_where=sa.text(where),
    )
    logger.warning("存量冲突已清空，补建 %s（%s）。", name, table)


def _report_unbind_suspects(bind: sa.engine.Connection) -> None:
    """点名"疑似被旧版迁移静默解绑"的账户条数——只读、不打 PII。"""
    count = bind.execute(sa.text(_SUSPECT_SQL)).scalar() or 0
    if not count:
        return
    logger.warning(
        "有 %s 个居民账户未绑定档案、却存在同手机号的档案。若本库曾跑过旧版 "
        "d3e4f5a6b7c8（会静默 `SET patient_id = NULL`），这批很可能就是被解绑的那些——"
        "**绑定关系已永久丢失，无法自动恢复**。人工排查与请居民重新实名绑定的流程见 "
        "docs/运维手册.md“静默解绑的人工排查”。",
        count,
    )


def upgrade() -> None:
    bind = op.get_bind()

    _retry_index(
        bind,
        name=_ACCOUNT_INDEX,
        table="resident_accounts",
        columns=["patient_id"],
        where="patient_id IS NOT NULL",
        conflict_sql=(
            "SELECT patient_id, id AS account_id FROM resident_accounts "
            "WHERE patient_id IN ("
            "  SELECT patient_id FROM resident_accounts WHERE patient_id IS NOT NULL"
            "  GROUP BY patient_id HAVING COUNT(*) > 1) "
            "ORDER BY patient_id, id"
        ),
        human="一份档案挂了多个账户，须人工确认哪个是本人（处置 SQL 见 d3e4f5a6b7c8 的 docstring）。",
    )

    _retry_index(
        bind,
        name=_POOL_INDEX,
        table="fund_pools",
        columns=["year", "insurance_type"],
        where="org_group_id IS NULL",
        conflict_sql=(
            "SELECT a.year, a.insurance_type, a.id FROM fund_pools a "
            "WHERE a.org_group_id IS NULL AND EXISTS ("
            "  SELECT 1 FROM fund_pools b WHERE b.org_group_id IS NULL"
            "    AND b.year = a.year AND b.insurance_type = a.insurance_type AND b.id <> a.id) "
            "ORDER BY a.year, a.insurance_type, a.id"
        ),
        human="同一年度险种有多个全域池，哪本账作数须由财务核对（处置 SQL 见 d5e6f7a8b9c0 的 docstring）。",
    )

    _report_unbind_suspects(bind)


def downgrade() -> None:
    """降级不删索引。

    这两个索引的归属是 `d3e4f5a6b7c8` / `d5e6f7a8b9c0`，各自的 downgrade 已经带了
    "索引不存在就跳过"的判断；本迁移只是替它们补建过一次，退回来时把索引留给正主去删，
    免得出现"补偿迁移退了、正主还没退，兜底约束却已经没了"的空窗。
    """
