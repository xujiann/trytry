"""第九轮：敏感读留痕表 access_logs

Revision ID: a9b8c7d6e5f4
Revises: e2f3a4b5c6d7
"""
import sqlalchemy as sa
from alembic import op

revision = "a9b8c7d6e5f4"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("username", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("org_id", sa.Integer(), nullable=True),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("resource", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("basis", sa.String(length=16), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_access_logs_username", "access_logs", ["username"])
    op.create_index("ix_access_logs_patient_id", "access_logs", ["patient_id"])
    op.create_index("ix_access_logs_resource", "access_logs", ["resource"])
    op.create_index("ix_access_logs_basis", "access_logs", ["basis"])
    op.create_index("ix_access_logs_created_at", "access_logs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_access_logs_created_at", table_name="access_logs")
    op.drop_index("ix_access_logs_basis", table_name="access_logs")
    op.drop_index("ix_access_logs_resource", table_name="access_logs")
    op.drop_index("ix_access_logs_patient_id", table_name="access_logs")
    op.drop_index("ix_access_logs_username", table_name="access_logs")
    op.drop_table("access_logs")
