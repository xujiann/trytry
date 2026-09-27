"""审方规则改动记录（P2-578）

审方规则按 drug_code 整条覆盖（`POST /api/prescriptions/rules/import`），停用不删行。审方页与停用接口的说明都写着
「规则改过什么、什么时候不再生效，处方点评复核时要回溯得到」，可导入覆盖之后旧值就没了：`drug_rules` 没有更新时间，
审计日志只记方法、路径与状态码，记不下改了什么。

## 升级

新建 `drug_rule_changes`：新建、导入（新建或整条覆盖）、停用、恢复各记一条，存改动前后的整条规则与改动人。只建表，
不回填——上线前的改动无从得知，不替人补；每条规则上线后第一次改动的「改动前」就是它上线时的样子。

## 回退

删表。回退后改动记录随之丢失，规则本身不受影响。回退前要保留的话先导出：

    SELECT id, drug_code, action, before, after, changed_by, created_at FROM drug_rule_changes ORDER BY id;

Revision ID: a1ef99c3f5fe
Revises: 26b1c3ba4497
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1ef99c3f5fe"
down_revision: Union[str, Sequence[str], None] = "26b1c3ba4497"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "drug_rule_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("drug_code", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=16), nullable=False),
        sa.Column("before", sa.JSON(), nullable=True),
        sa.Column("after", sa.JSON(), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_drug_rule_changes_drug_code"), "drug_rule_changes", ["drug_code"])
    op.create_index(op.f("ix_drug_rule_changes_created_at"), "drug_rule_changes", ["created_at"])


def downgrade() -> None:
    op.drop_index(op.f("ix_drug_rule_changes_created_at"), table_name="drug_rule_changes")
    op.drop_index(op.f("ix_drug_rule_changes_drug_code"), table_name="drug_rule_changes")
    op.drop_table("drug_rule_changes")
