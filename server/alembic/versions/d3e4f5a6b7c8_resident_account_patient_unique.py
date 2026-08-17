"""居民账户绑定档案的部分唯一索引：一份档案只绑一个账户，约束下沉数据库

应用层查重（bind_realname / _autobind_by_phone 的"先查 taken 再绑"）是
check-then-act，并发下两个账户可同时绑上同一份档案——与全域基金池 D-2
同一个形状。修法同样是把兜底落在数据库：patient_id 上建部分唯一索引，
NULL（未绑定）放行任意多个，非 NULL 唯一。

存量数据可能已经存在"一档多账户"（该缺陷此前无拦截）：迁移前先清重，
保留最早绑定的那个账户（id 最小），其余解绑——解绑账户仍可登录，重新
实名绑定时会得到明确的 409 提示去联系服务机构，比静默留两个绑定安全。

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
"""
import sqlalchemy as sa
from alembic import op

revision = "d3e4f5a6b7c8"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 清重：每个 patient_id 只留 id 最小的绑定，其余解绑
    op.execute(
        """
        UPDATE resident_accounts SET patient_id = NULL
        WHERE patient_id IS NOT NULL
          AND id NOT IN (
            SELECT keep_id FROM (
              SELECT MIN(id) AS keep_id FROM resident_accounts
              WHERE patient_id IS NOT NULL GROUP BY patient_id
            ) AS keepers
          )
        """
    )
    op.create_index(
        "uq_resident_account_patient",
        "resident_accounts",
        ["patient_id"],
        unique=True,
        sqlite_where=sa.text("patient_id IS NOT NULL"),
        postgresql_where=sa.text("patient_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("uq_resident_account_patient", table_name="resident_accounts")
