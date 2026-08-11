"""终审轮批4：血液库存与用血申请、患者档案调阅授权、直播申请审核

Revision ID: b7c8d9e0f1a2
Revises: a5b6c7d8e9f0
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, Sequence[str], None] = "a5b6c7d8e9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "blood_stocks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("blood_type", sa.String(length=4), nullable=False),
        sa.Column("component", sa.String(length=16), nullable=False),
        sa.Column("quantity_ml", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("blood_type", "component", name="uq_blood_type_component"),
    )
    op.create_index(op.f("ix_blood_stocks_blood_type"), "blood_stocks", ["blood_type"], unique=False)
    op.create_table(
        "transfusion_requests",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("blood_type", sa.String(length=4), nullable=False),
        sa.Column("component", sa.String(length=16), nullable=False),
        sa.Column("quantity_ml", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("approved_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_transfusion_requests_patient_id"), "transfusion_requests", ["patient_id"], unique=False)
    op.create_index(op.f("ix_transfusion_requests_status"), "transfusion_requests", ["status"], unique=False)
    op.create_table(
        "archive_authorizations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("grantee_org_id", sa.Integer(), nullable=False),
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("expire_date", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["grantee_org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_archive_authorizations_patient_id"), "archive_authorizations", ["patient_id"], unique=False)
    op.create_index(op.f("ix_archive_authorizations_grantee_org_id"), "archive_authorizations", ["grantee_org_id"], unique=False)
    op.create_index(op.f("ix_archive_authorizations_status"), "archive_authorizations", ["status"], unique=False)
    op.create_table(
        "live_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=True),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("speaker", sa.String(length=64), nullable=False),
        sa.Column("planned_at", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_comment", sa.String(length=256), nullable=False),
        sa.Column("requested_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.ForeignKeyConstraint(["requested_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_live_sessions_status"), "live_sessions", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_live_sessions_status"), table_name="live_sessions")
    op.drop_table("live_sessions")
    op.drop_index(op.f("ix_archive_authorizations_status"), table_name="archive_authorizations")
    op.drop_index(op.f("ix_archive_authorizations_grantee_org_id"), table_name="archive_authorizations")
    op.drop_index(op.f("ix_archive_authorizations_patient_id"), table_name="archive_authorizations")
    op.drop_table("archive_authorizations")
    op.drop_index(op.f("ix_transfusion_requests_status"), table_name="transfusion_requests")
    op.drop_index(op.f("ix_transfusion_requests_patient_id"), table_name="transfusion_requests")
    op.drop_table("transfusion_requests")
    op.drop_index(op.f("ix_blood_stocks_blood_type"), table_name="blood_stocks")
    op.drop_table("blood_stocks")
