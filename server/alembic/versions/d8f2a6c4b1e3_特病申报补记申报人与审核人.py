"""特病申报补记申报人与审核人：申报人不得自审（P2-399）

`special_disease_apps` 原先只有患者、病种、状态、理由——谁报的、谁批的都不落。审核接口的注释写着「申报
（operator/doctor）与审核（director）职责分离，杜绝自报自批」，可角色守卫只分得开这两类角色，同时带两种
权限的账号（管理员、复制了两类权限点的自定义角色）照样自报自批；表上连申报人都没有，接口想比也没处比。
双通道申报（`dual_channel_apps`）早就记着 `created_by` / `reviewed_by`，P2-398 已据此拦住自审。

## 升级

纯加列（C 档）：`created_by`、`reviewed_by` 两列都可空、外键指 `users.id`，不动任何存量行。SQLite 加外键
要走 batch（整表重建）；PostgreSQL 上等价于两句 `ADD COLUMN` 加两句 `ADD CONSTRAINT`。

存量申报的申报人本来就没记，**不回填**：审计日志里的请求虽然对得上用户，但要按接口与时间去配，配错了就是
替人下了「谁报的」这个结论。存量行 `created_by` 为空，审核时比不出申报人、照原样放行；新申报一律记下。

## 回退

删两列（先删外键）。两列只是「谁报的 / 谁批的」两条留痕，回退丢的是它们、不影响申报本身。回退前要保留的话
先导出：

    SELECT id, created_by, reviewed_by FROM special_disease_apps
     WHERE created_by IS NOT NULL OR reviewed_by IS NOT NULL;

Revision ID: d8f2a6c4b1e3
Revises: 9f6e90d1f6b5
Create Date: 2026-09-26
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d8f2a6c4b1e3"
down_revision: Union[str, Sequence[str], None] = "9f6e90d1f6b5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "special_disease_apps"
_COLUMNS = ("created_by", "reviewed_by")


def _fk_name(column: str) -> str:
    """显式命名：SQLite 的 batch 重建必须有约束名，回退时也才删得掉（同 `e7c4b19d02fa`）。"""
    return f"fk_{_TABLE}_{column}_users"


def upgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        for column in _COLUMNS:
            batch.add_column(sa.Column(column, sa.Integer(), nullable=True))
            batch.create_foreign_key(_fk_name(column), "users", [column], ["id"])


def downgrade() -> None:
    with op.batch_alter_table(_TABLE) as batch:
        for column in reversed(_COLUMNS):
            batch.drop_constraint(_fk_name(column), type_="foreignkey")
            batch.drop_column(column)
