"""居民端家庭成员代管表

一个账户代管家人档案（老人、儿童往往不自己用手机）。与本人档案
（resident_accounts.patient_id）分开存：本人唯一，代管可多条，
且同一份档案可被多个子女同时代管，故唯一约束落在 (account_id, patient_id)。

Revision ID: c9d0e1f2a3b5
Revises: b8c9d0e1f2a3
"""
import sqlalchemy as sa
from alembic import op

revision = "c9d0e1f2a3b5"
down_revision = "b8c9d0e1f2a3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resident_family_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(), sa.ForeignKey("resident_accounts.id"), nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("relation", sa.String(16), nullable=False, server_default="other"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("account_id", "patient_id", name="uq_family_account_patient"),
    )


def downgrade() -> None:
    op.drop_table("resident_family_members")
