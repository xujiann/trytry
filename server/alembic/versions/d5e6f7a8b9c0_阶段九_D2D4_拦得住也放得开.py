"""阶段九 D-2/D-4：拦得住，也要放得开

三处改动同源：业务上"拦住"了，却没留"放开"的路。

- D-2 全域基金池：唯一约束含可空列在 SQL 里形同虚设（NULL != NULL），
  并发实测建出过两个池，同一笔结余会被分两次。加部分唯一索引兜底。
- D-4 接种禁忌：登记后永久硬拦截，无解除接口，退了热也再打不了这支疫苗。
  加状态、有效期与解除留痕。
- 用药规则：录错只能靠 import 覆盖，删不掉也停不掉。加停用标记。

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""
import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- D-2 全域基金池的部分唯一索引 ----
    # 建索引前先清理历史重复：并发窗口在生产上可能已经建出过重复的全域池。
    # 保留 id 最小的一条，其余标为 closed 并注明——**不删行**，
    # 池子下面挂着预付与结算单，删了对不上账。
    conn = op.get_bind()
    duplicates = conn.execute(
        sa.text(
            "SELECT year, insurance_type, MIN(id) AS keep_id, COUNT(*) AS n "
            "FROM fund_pools WHERE org_group_id IS NULL "
            "GROUP BY year, insurance_type HAVING COUNT(*) > 1"
        )
    ).fetchall()
    for row in duplicates:
        conn.execute(
            sa.text(
                "UPDATE fund_pools SET status = 'closed', "
                "note = note || ' [迁移d5e6f7a8b9c0：并发建出的重复全域池，已归档]' "
                "WHERE org_group_id IS NULL AND year = :y AND insurance_type = :t "
                "AND id != :keep"
            ),
            {"y": row.year, "t": row.insurance_type, "keep": row.keep_id},
        )

    op.create_index(
        "uq_fund_pool_global",
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

    op.drop_index("uq_fund_pool_global", table_name="fund_pools")
