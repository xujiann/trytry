"""转诊证明记签发人（P2-524，P0-29 残留）

P0-29 的实测记着两件事：转诊证明任一机构的经办都能签，签证明的人不落库。当时只修了前一件（先判患者可见性并留痕），
`referral_certs` 仍只有转诊号、证明号与签发时间——证明是凭证，出了争议要问「谁签的」，库里答不上来。

## 升级

纯加列（C 档）：`issued_by`（可空、外键指 `users.id`），不动任何存量行。SQLite 加外键要走 batch（整表重建）；
PostgreSQL 上等价于一句 `ADD COLUMN` 加一句 `ADD CONSTRAINT`。

存量证明的签发人本来就没记，**不回填**：审计日志里 `POST /api/insurance/referral-certs/{转诊号}` 的请求路径带着
转诊号，要查可以按转诊号与签发时间去对（复签是幂等的、同一路径可能有多条，最早那条 2xx 才是签发），但把它写回
这张表就是替人下了「谁签的」结论：

    SELECT a.username, a.created_at, a.status_code FROM audit_logs a
     WHERE a.method = 'POST' AND a.path = '/api/insurance/referral-certs/' || :referral_id
     ORDER BY a.created_at;

存量行该列为空。

## 回退

删列（先删外键）。该列只是签发留痕，回退丢的是它、不影响证明本身。回退前要保留的话先导出：

    SELECT id, referral_id, cert_no, issued_by, issued_at FROM referral_certs WHERE issued_by IS NOT NULL;

Revision ID: 0cbfd7d05951
Revises: c7e9a1b3d5f8
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0cbfd7d05951"
down_revision: Union[str, Sequence[str], None] = "c7e9a1b3d5f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "referral_certs"
_FK = "fk_referral_certs_issued_by_users"   # 显式命名：SQLite 的 batch 重建必须有约束名，回退时也才删得掉


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("issued_by", sa.Integer(), nullable=True))
        batch.create_foreign_key(_FK, "users", ["issued_by"], ["id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint(_FK, type_="foreignkey")
        batch.drop_column("issued_by")
