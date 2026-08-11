"""终审轮批1：分娩记录、新生儿疾病筛查、高危儿、妇女保健、法定医学证明、成人体检

Revision ID: e1f2a3b4c5d6
Revises: d0b1c2e3f4a6
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "d0b1c2e3f4a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "delivery_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("delivery_date", sa.String(length=10), nullable=False),
        sa.Column("delivery_mode", sa.String(length=16), nullable=False),
        sa.Column("newborn_count", sa.Integer(), nullable=False),
        sa.Column("outcome", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["maternal_records.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_delivery_records_record_id"), "delivery_records", ["record_id"], unique=False
    )
    op.create_table(
        "newborn_screenings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("child_id", sa.Integer(), nullable=False),
        sa.Column("item", sa.String(length=16), nullable=False),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("screen_date", sa.String(length=10), nullable=False),
        sa.Column("note", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["child_id"], ["child_records.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_newborn_screenings_child_id"), "newborn_screenings", ["child_id"], unique=False
    )
    op.create_table(
        "women_health_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("record_type", sa.String(length=16), nullable=False),
        sa.Column("exam_date", sa.String(length=10), nullable=False),
        sa.Column("result", sa.String(length=512), nullable=False),
        sa.Column("advice", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_women_health_records_patient_id"),
        "women_health_records",
        ["patient_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_women_health_records_record_type"),
        "women_health_records",
        ["record_type"],
        unique=False,
    )
    op.create_table(
        "medical_certs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("cert_type", sa.String(length=8), nullable=False),
        sa.Column("cert_no", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("gender", sa.String(length=8), nullable=False),
        sa.Column("event_date", sa.String(length=10), nullable=False),
        sa.Column("detail", sa.String(length=512), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=True),
        sa.Column("child_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["child_id"], ["child_records.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_medical_certs_cert_type"), "medical_certs", ["cert_type"], unique=False)
    op.create_index(op.f("ix_medical_certs_cert_no"), "medical_certs", ["cert_no"], unique=True)
    op.create_table(
        "physical_exams",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("package_name", sa.String(length=128), nullable=False),
        sa.Column("exam_date", sa.String(length=10), nullable=False),
        sa.Column("summary", sa.String(length=1024), nullable=False),
        sa.Column("abnormal_items", sa.String(length=512), nullable=False),
        sa.Column("has_abnormal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_physical_exams_patient_id"), "physical_exams", ["patient_id"], unique=False)
    op.create_index(op.f("ix_physical_exams_has_abnormal"), "physical_exams", ["has_abnormal"], unique=False)

    # 高危儿管理：存量儿童档案默认非高危
    op.add_column(
        "child_records",
        sa.Column("high_risk", sa.Boolean(), nullable=False, server_default=sa.text("0")),
    )
    op.add_column(
        "child_records",
        sa.Column("risk_note", sa.String(length=256), nullable=False, server_default=""),
    )
    op.create_index(op.f("ix_child_records_high_risk"), "child_records", ["high_risk"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_child_records_high_risk"), table_name="child_records")
    op.drop_column("child_records", "risk_note")
    op.drop_column("child_records", "high_risk")
    op.drop_index(op.f("ix_physical_exams_has_abnormal"), table_name="physical_exams")
    op.drop_index(op.f("ix_physical_exams_patient_id"), table_name="physical_exams")
    op.drop_table("physical_exams")
    op.drop_index(op.f("ix_medical_certs_cert_no"), table_name="medical_certs")
    op.drop_index(op.f("ix_medical_certs_cert_type"), table_name="medical_certs")
    op.drop_table("medical_certs")
    op.drop_index(op.f("ix_women_health_records_record_type"), table_name="women_health_records")
    op.drop_index(op.f("ix_women_health_records_patient_id"), table_name="women_health_records")
    op.drop_table("women_health_records")
    op.drop_index(op.f("ix_newborn_screenings_child_id"), table_name="newborn_screenings")
    op.drop_table("newborn_screenings")
    op.drop_index(op.f("ix_delivery_records_record_id"), table_name="delivery_records")
    op.drop_table("delivery_records")
