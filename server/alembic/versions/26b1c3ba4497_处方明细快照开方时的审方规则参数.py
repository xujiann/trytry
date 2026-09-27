"""处方明细快照开方时的审方规则参数（P2-577）

处方点评要点（`GET /api/prescriptions/{id}/review-points`）、抗菌药物使用强度（`analytics.drug_use`）、规则可审张数
（`performance`）原先都拿**现行**规则去判读历史处方：规则纠正单位（g → mg）后，一张按 g 开的历史处方显示成「2.0mg」、
当月 DDDs 从 5.0 变成 0.01；收紧上限后，当时系统审通过的方被标「日剂量超限」；停用后这张方成了「规则未维护」、DDDs
与未覆盖数双双归零（药占比页写的是「不悄悄丢掉」）。审方页写着「规则改过什么、什么时候不再生效，处方点评复核时要
回溯得到」，停用确认框写的是「此后」。

## 升级

纯加列，全部在 `prescription_items` 上：`rule_max_daily_dose`（可空）、`rule_dose_unit`、`rule_antibiotic`、`rule_ddd`
（非空，存量以库默认 ''/false/0 补齐后去掉默认，`c3bfca22dbdd` 同一写法）——只填本迁移新加的列。
`rule_max_daily_dose` 为空即「没有快照」：存量一律为空，照旧按现行生效规则判读（修前口径），不替人推断开方时是哪一版
规则——规则表没有历史，推不出来。上线后新开的处方逐行记下当时用的那一版。

存量要补的话只能人工逐条核对（例如确知某药的规则自某日后没改过）：

    UPDATE prescription_items i
       SET rule_max_daily_dose = r.max_daily_dose, rule_dose_unit = r.dose_unit,
           rule_antibiotic = r.antibiotic, rule_ddd = r.ddd
      FROM drug_rules r, prescriptions p
     WHERE r.drug_code = i.drug_code AND p.id = i.prescription_id AND r.active
       AND i.rule_max_daily_dose IS NULL AND i.drug_code = :drug_code AND p.created_at >= :since;

补错了把这几列改回空（`rule_max_daily_dose = NULL`）即回到按现行规则判读。

## 回退

删列。回退后历史处方重新按现行规则判读（修前行为），处方本身不受影响。回退前要保留快照的话先导出：

    SELECT id, rule_max_daily_dose, rule_dose_unit, rule_antibiotic, rule_ddd
      FROM prescription_items WHERE rule_max_daily_dose IS NOT NULL;

Revision ID: 26b1c3ba4497
Revises: 0cbfd7d05951
Create Date: 2026-09-27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "26b1c3ba4497"
down_revision: Union[str, Sequence[str], None] = "0cbfd7d05951"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_NOT_NULL = ("rule_dose_unit", "rule_antibiotic", "rule_ddd")


def upgrade() -> None:
    op.add_column("prescription_items", sa.Column("rule_max_daily_dose", sa.Float(), nullable=True))
    op.add_column(
        "prescription_items",
        sa.Column("rule_dose_unit", sa.String(length=16), nullable=False, server_default=""),
    )
    op.add_column(
        "prescription_items",
        sa.Column("rule_antibiotic", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "prescription_items",
        sa.Column("rule_ddd", sa.Float(), nullable=False, server_default="0"),
    )
    with op.batch_alter_table("prescription_items") as batch:
        for column in _NOT_NULL:
            batch.alter_column(column, server_default=None)


def downgrade() -> None:
    with op.batch_alter_table("prescription_items") as batch:
        for column in ("rule_ddd", "rule_antibiotic", "rule_dose_unit", "rule_max_daily_dose"):
            batch.drop_column(column)
