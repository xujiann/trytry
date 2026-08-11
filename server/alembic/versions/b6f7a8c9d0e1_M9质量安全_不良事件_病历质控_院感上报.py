"""M9质量安全：不良事件、病历质控、院感上报

Revision ID: b6f7a8c9d0e1
Revises: a4d5e6f7b8c9
Create Date: 2026-08-11 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6f7a8c9d0e1'
down_revision: Union[str, Sequence[str], None] = 'a4d5e6f7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "adverse_events",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(length=16), nullable=False),
        sa.Column("level", sa.String(length=4), nullable=False),
        sa.Column("anonymous", sa.Boolean(), nullable=False),
        sa.Column("reporter_name", sa.String(length=64), nullable=False),
        sa.Column("description", sa.String(length=2048), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("review_note", sa.String(length=1024), nullable=False),
        sa.Column("reviewed_by", sa.String(length=64), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("rectify_note", sa.String(length=1024), nullable=False),
        sa.Column("rectified_by", sa.String(length=64), nullable=False),
        sa.Column("rectified_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_adverse_events_org_id"), "adverse_events", ["org_id"], unique=False)
    op.create_index(
        op.f("ix_adverse_events_event_type"), "adverse_events", ["event_type"], unique=False
    )
    op.create_index(op.f("ix_adverse_events_status"), "adverse_events", ["status"], unique=False)

    op.create_table(
        "record_qcs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Integer(), nullable=False),
        sa.Column("grade", sa.String(length=4), nullable=False),
        sa.Column("defects", sa.String(length=1024), nullable=False),
        sa.Column("qc_by", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_record_qcs_target_type"), "record_qcs", ["target_type"], unique=False)
    op.create_index(op.f("ix_record_qcs_target_id"), "record_qcs", ["target_id"], unique=False)

    op.create_table(
        "infection_reports",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("infection_site", sa.String(length=16), nullable=False),
        sa.Column("pathogen", sa.String(length=128), nullable=False),
        sa.Column("note", sa.String(length=1024), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reported_by", sa.String(length=64), nullable=False),
        sa.Column("report_date", sa.String(length=10), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_infection_reports_org_id"), "infection_reports", ["org_id"], unique=False
    )
    op.create_index(
        op.f("ix_infection_reports_patient_id"), "infection_reports", ["patient_id"], unique=False
    )
    op.create_index(
        op.f("ix_infection_reports_infection_site"),
        "infection_reports",
        ["infection_site"],
        unique=False,
    )
    op.create_index(
        op.f("ix_infection_reports_status"), "infection_reports", ["status"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_infection_reports_status"), table_name="infection_reports")
    op.drop_index(op.f("ix_infection_reports_infection_site"), table_name="infection_reports")
    op.drop_index(op.f("ix_infection_reports_patient_id"), table_name="infection_reports")
    op.drop_index(op.f("ix_infection_reports_org_id"), table_name="infection_reports")
    op.drop_table("infection_reports")
    op.drop_index(op.f("ix_record_qcs_target_id"), table_name="record_qcs")
    op.drop_index(op.f("ix_record_qcs_target_type"), table_name="record_qcs")
    op.drop_table("record_qcs")
    op.drop_index(op.f("ix_adverse_events_status"), table_name="adverse_events")
    op.drop_index(op.f("ix_adverse_events_event_type"), table_name="adverse_events")
    op.drop_index(op.f("ix_adverse_events_org_id"), table_name="adverse_events")
    op.drop_table("adverse_events")
