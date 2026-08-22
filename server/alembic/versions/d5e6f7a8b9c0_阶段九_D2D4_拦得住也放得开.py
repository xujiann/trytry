"""阶段九 D-2/D-4：拦得住，也要放得开

三处改动同源：业务上"拦住"了，却没留"放开"的路。

- D-2 全域基金池：唯一约束含可空列在 SQL 里形同虚设（NULL != NULL），
  并发实测建出过两个池，同一笔结余会被分两次。加部分唯一索引兜底。
- D-4 接种禁忌：登记后永久硬拦截，无解除接口，退了热也再打不了这支疫苗。
  加状态、有效期与解除留痕。
- 用药规则：录错只能靠 import 覆盖，删不掉也停不掉。加停用标记。

D-2 的存量重复：不阻塞，也不替人改账
------------------------------------

**本迁移的第一版会静默改账。** 它在建索引前把重复的全域池

    UPDATE fund_pools SET status = 'closed', note = note || ' [迁移…已归档]'
     WHERE org_group_id IS NULL AND year = :y AND insurance_type = :t AND id != :keep

一律改成 closed、只留 id 最小的那个。虽然留了 note 痕迹、也没删行，但它仍然是
**程序替人决定哪本账作数**：基金池下面挂着预付与结算单，被关掉的那个池上的
金额去向就此悬空，而"id 最小"跟"哪本账是对的"没有任何关系。这与
`e5b7c9d1f3a4`（住院结算单）的判断——财务凭证不能由程序替人决定——是同一件事，
只是当时没有一致执行。

改成不阻塞路径：探到重复就跳过建 `uq_fund_pool_global`，打一条指名
(year, insurance_type, 池 id) 的 ERROR 日志，由财务人工核对归并后处置。
应用层的查重此刻仍在，重复只会存量不会新增。

人工处置 SQL（DBA + 财务共同确认后执行）::

    -- ① 列出重复的全域池，以及各自挂了多少钱
    SELECT a.id, a.year, a.insurance_type, a.total_amount, a.status, a.note
      FROM fund_pools a
     WHERE a.org_group_id IS NULL
       AND EXISTS (SELECT 1 FROM fund_pools b
                    WHERE b.org_group_id IS NULL AND b.year = a.year
                      AND b.insurance_type = a.insurance_type AND b.id <> a.id)
     ORDER BY a.year, a.insurance_type, a.id;
    -- ② 查各池下挂的预付/结算，确认把哪本账并到哪本（**不要**直接删池，删了对不上账）
    SELECT pool_id, COUNT(*) FROM fund_settlements GROUP BY pool_id;
    -- ③ 归并完成后补建索引
    CREATE UNIQUE INDEX uq_fund_pool_global ON fund_pools (year, insurance_type)
        WHERE org_group_id IS NULL;

第 ③ 步还有一条自动路径：补偿迁移 `a1c3e5b7d9f2`（平台链末尾）在它自己那一次执行时
会重探一遍，无重复就补建。本库若已升到链尾，alembic 不会重跑已记录的 revision，
请按第 ③ 步手工 CREATE INDEX。

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
import logging

import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")

_POOL_INDEX = "uq_fund_pool_global"

#: 重复的全域池明细（year, insurance_type, 池 id）。
#: 用 EXISTS 自连接而不是行值 IN——行值子查询要 SQLite 3.15+，EXISTS 两方言都老得多。
POOL_CONFLICT_SQL = (
    "SELECT a.year, a.insurance_type, a.id FROM fund_pools a "
    "WHERE a.org_group_id IS NULL AND EXISTS ("
    "  SELECT 1 FROM fund_pools b WHERE b.org_group_id IS NULL"
    "    AND b.year = a.year AND b.insurance_type = a.insurance_type AND b.id <> a.id) "
    "ORDER BY a.year, a.insurance_type, a.id"
)


def _pool_conflicts(bind: sa.engine.Connection) -> dict[str, list[int]]:
    """{"2026/职工": [池 id, …]}——只读，不改任何一行。"""
    grouped: dict[str, list[int]] = {}
    for year, ins_type, pool_id in bind.execute(sa.text(POOL_CONFLICT_SQL)).fetchall():
        grouped.setdefault(f"{year}/{ins_type}", []).append(pool_id)
    return grouped


def upgrade() -> None:
    # ---- D-2 全域基金池的部分唯一索引 ----
    # 存量重复不阻塞、也不替人改账：探到就跳过建索引 + ERROR 日志指名池子，
    # 由财务人工归并（理由与处置 SQL 见本迁移 docstring）。
    conn = op.get_bind()
    conflicts = _pool_conflicts(conn)
    if conflicts:
        logger.error(
            "fund_pools 存量存在重复的全域池（年度/险种: 池 id 列表 = %s），本次跳过建 %s。"
            "**不自动归档**：池子下面挂着预付与结算单，哪本账作数要由财务核对，"
            "“留 id 最小的”与“哪本对”无关。人工归并后按 docstring 第 ③ 步手工 CREATE INDEX"
            "（本库若还没升到链尾 a1c3e5b7d9f2，下一次 upgrade heads 会自动补建）。",
            dict(sorted(conflicts.items())),
            _POOL_INDEX,
        )
    elif _POOL_INDEX not in {i["name"] for i in sa.inspect(conn).get_indexes("fund_pools")}:
        op.create_index(
            _POOL_INDEX,
            "fund_pools",
            ["year", "insurance_type"],
            unique=True,
            sqlite_where=sa.text("org_group_id IS NULL"),
            postgresql_where=sa.text("org_group_id IS NULL"),
        )

    # ---- D-4 接种禁忌可解除 ----
    op.add_column(
        "vaccine_contraindications",
        sa.Column("contra_type", sa.String(16), nullable=False, server_default="permanent"),
    )
    op.create_index(
        "ix_vaccine_contraindications_contra_type", "vaccine_contraindications", ["contra_type"]
    )
    op.add_column(
        "vaccine_contraindications",
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
    )
    op.create_index(
        "ix_vaccine_contraindications_status", "vaccine_contraindications", ["status"]
    )
    op.add_column(
        "vaccine_contraindications",
        sa.Column("valid_until", sa.String(10), nullable=False, server_default=""),
    )
    op.add_column(
        "vaccine_contraindications", sa.Column("lifted_by", sa.Integer(), nullable=True)
    )
    op.add_column(
        "vaccine_contraindications", sa.Column("lifted_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "vaccine_contraindications",
        sa.Column("lift_reason", sa.String(256), nullable=False, server_default=""),
    )

    # ---- 用药规则停用标记 ----
    op.add_column(
        "drug_rules",
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index("ix_drug_rules_active", "drug_rules", ["active"])


def downgrade() -> None:
    op.drop_index("ix_drug_rules_active", table_name="drug_rules")
    op.drop_column("drug_rules", "active")

    op.drop_column("vaccine_contraindications", "lift_reason")
    op.drop_column("vaccine_contraindications", "lifted_at")
    op.drop_column("vaccine_contraindications", "lifted_by")
    op.drop_column("vaccine_contraindications", "valid_until")
    op.drop_index("ix_vaccine_contraindications_status", table_name="vaccine_contraindications")
    op.drop_column("vaccine_contraindications", "status")
    op.drop_index(
        "ix_vaccine_contraindications_contra_type", table_name="vaccine_contraindications"
    )
    op.drop_column("vaccine_contraindications", "contra_type")

    # upgrade 可能因存量重复而跳过建索引，降级要能对付"索引不存在"
    conn = op.get_bind()
    if _POOL_INDEX in {i["name"] for i in sa.inspect(conn).get_indexes("fund_pools")}:
        op.drop_index(_POOL_INDEX, table_name="fund_pools")
