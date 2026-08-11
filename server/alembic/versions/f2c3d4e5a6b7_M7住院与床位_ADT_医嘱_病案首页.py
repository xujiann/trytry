"""M7住院与床位：病区/床位、入出转（ADT）、住院医嘱、病案首页

Revision ID: f2c3d4e5a6b7
Revises: e8b1c2d3f4a5
Create Date: 2026-08-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2c3d4e5a6b7'
down_revision: Union[str, Sequence[str], None] = 'e8b1c2d3f4a5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "wards",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "name", name="uq_ward_org_name"),
    )
    op.create_index(op.f("ix_wards_org_id"), "wards", ["org_id"], unique=False)

    op.create_table(
        "beds",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("ward_id", sa.Integer(), nullable=False),
        sa.Column("bed_no", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["ward_id"], ["wards.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("ward_id", "bed_no", name="uq_bed_ward_no"),
    )
    op.create_index(op.f("ix_beds_ward_id"), "beds", ["ward_id"], unique=False)
    op.create_index(op.f("ix_beds_status"), "beds", ["status"], unique=False)

    op.create_table(
        "admissions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("ward_id", sa.Integer(), nullable=False),
        sa.Column("bed_id", sa.Integer(), nullable=False),
        sa.Column("doctor_name", sa.String(length=64), nullable=False),
        sa.Column("diagnosis_name", sa.String(length=256), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("admitted_at", sa.DateTime(), nullable=False),
        sa.Column("discharged_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["ward_id"], ["wards.id"]),
        sa.ForeignKeyConstraint(["bed_id"], ["beds.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_admissions_patient_id"), "admissions", ["patient_id"], unique=False)
    op.create_index(op.f("ix_admissions_org_id"), "admissions", ["org_id"], unique=False)
    op.create_index(op.f("ix_admissions_status"), "admissions", ["status"], unique=False)

    op.create_table(
        "inpatient_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admission_id", sa.Integer(), nullable=False),
        sa.Column("order_type", sa.String(length=8), nullable=False),
        sa.Column("content", sa.String(length=512), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_by_name", sa.String(length=64), nullable=False),
        sa.Column("stopped_by_name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("stopped_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["admission_id"], ["admissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_inpatient_orders_admission_id"), "inpatient_orders", ["admission_id"], unique=False
    )
    op.create_index(
        op.f("ix_inpatient_orders_status"), "inpatient_orders", ["status"], unique=False
    )

    op.create_table(
        "case_summaries",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admission_id", sa.Integer(), nullable=False),
        sa.Column("discharge_diagnosis", sa.String(length=256), nullable=False),
        sa.Column("operation", sa.String(length=256), nullable=False),
        sa.Column("total_cost", sa.Float(), nullable=False),
        sa.Column("drug_cost", sa.Float(), nullable=False),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("note", sa.String(length=1024), nullable=False),
        sa.Column("created_by_name", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["admission_id"], ["admissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_case_summaries_admission_id"), "case_summaries", ["admission_id"], unique=True
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_case_summaries_admission_id"), table_name="case_summaries")
    op.drop_table("case_summaries")
    op.drop_index(op.f("ix_inpatient_orders_status"), table_name="inpatient_orders")
    op.drop_index(op.f("ix_inpatient_orders_admission_id"), table_name="inpatient_orders")
    op.drop_table("inpatient_orders")
    op.drop_index(op.f("ix_admissions_status"), table_name="admissions")
    op.drop_index(op.f("ix_admissions_org_id"), table_name="admissions")
    op.drop_index(op.f("ix_admissions_patient_id"), table_name="admissions")
    op.drop_table("admissions")
    op.drop_index(op.f("ix_beds_status"), table_name="beds")
    op.drop_index(op.f("ix_beds_ward_id"), table_name="beds")
    op.drop_table("beds")
    op.drop_index(op.f("ix_wards_org_id"), table_name="wards")
    op.drop_table("wards")
