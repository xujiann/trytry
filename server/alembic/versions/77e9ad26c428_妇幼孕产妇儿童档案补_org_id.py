"""妇幼孕产妇儿童档案补 org_id

Revision ID: 77e9ad26c428
Revises: 07029964f9bf
Create Date: 2026-08-16 02:46:09.103374

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77e9ad26c428'
down_revision: Union[str, Sequence[str], None] = '07029964f9bf'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """孕产妇/儿童档案补建册管理机构 org_id（横向隔离根治）。

    列可空以容纳历史数据；随后按患者最近一次就诊机构做**尽力回填**——回填不中
    的历史档案 org_id 保持 NULL，仅全域角色可见（安全：不泄露给非全域），
    可后续由业务侧补登管理机构。
    """
    with op.batch_alter_table("maternal_records") as batch:
        batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
        batch.create_index("ix_maternal_records_org_id", ["org_id"])
        batch.create_foreign_key(
            "fk_maternal_records_org_id", "organizations", ["org_id"], ["id"]
        )
    with op.batch_alter_table("child_records") as batch:
        batch.add_column(sa.Column("org_id", sa.Integer(), nullable=True))
        batch.create_index("ix_child_records_org_id", ["org_id"])
        batch.create_foreign_key(
            "fk_child_records_org_id", "organizations", ["org_id"], ["id"]
        )
    # 尽力回填：孕产妇按本人、儿童按监护人的最近一次就诊机构（两方言通用相关子查询）
    op.execute(
        "UPDATE maternal_records SET org_id = ("
        " SELECT e.org_id FROM encounters e"
        " WHERE e.patient_id = maternal_records.patient_id"
        " ORDER BY e.id DESC LIMIT 1) WHERE org_id IS NULL"
    )
    op.execute(
        "UPDATE child_records SET org_id = ("
        " SELECT e.org_id FROM encounters e"
        " WHERE e.patient_id = child_records.guardian_patient_id"
        " ORDER BY e.id DESC LIMIT 1)"
        " WHERE org_id IS NULL AND guardian_patient_id IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("child_records") as batch:
        batch.drop_constraint("fk_child_records_org_id", type_="foreignkey")
        batch.drop_index("ix_child_records_org_id")
        batch.drop_column("org_id")
    with op.batch_alter_table("maternal_records") as batch:
        batch.drop_constraint("fk_maternal_records_org_id", type_="foreignkey")
        batch.drop_index("ix_maternal_records_org_id")
        batch.drop_column("org_id")
