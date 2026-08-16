"""检查中心/病理室承接机构列

Revision ID: 9ae7abe85c31
Revises: 77e9ad26c428
Create Date: 2026-08-16 03:10:01.653711

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9ae7abe85c31'
down_revision: Union[str, Sequence[str], None] = '77e9ad26c428'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """检查申请补承接诊断中心机构、病理标本补承接病理室机构（横向隔离，自填充列）。"""
    with op.batch_alter_table("exam_requests") as batch:
        batch.add_column(sa.Column("center_org_id", sa.Integer(), nullable=True))
        batch.create_index("ix_exam_requests_center_org_id", ["center_org_id"])
        batch.create_foreign_key(
            "fk_exam_requests_center_org_id", "organizations", ["center_org_id"], ["id"]
        )
    with op.batch_alter_table("pathology_specimens") as batch:
        batch.add_column(sa.Column("lab_org_id", sa.Integer(), nullable=True))
        batch.create_index("ix_pathology_specimens_lab_org_id", ["lab_org_id"])
        batch.create_foreign_key(
            "fk_pathology_specimens_lab_org_id", "organizations", ["lab_org_id"], ["id"]
        )


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table("pathology_specimens") as batch:
        batch.drop_constraint("fk_pathology_specimens_lab_org_id", type_="foreignkey")
        batch.drop_index("ix_pathology_specimens_lab_org_id")
        batch.drop_column("lab_org_id")
    with op.batch_alter_table("exam_requests") as batch:
        batch.drop_constraint("fk_exam_requests_center_org_id", type_="foreignkey")
        batch.drop_index("ix_exam_requests_center_org_id")
        batch.drop_column("center_org_id")
