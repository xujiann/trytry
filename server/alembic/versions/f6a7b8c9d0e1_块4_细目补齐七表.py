"""块4：细目补齐——中药制剂/消毒成本/课件资源/实训/产前筛查/绩效整改/上门服务

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f6a7b8c9d0e1"
down_revision: Union[str, Sequence[str], None] = "e5f6a7b8c9d0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "tcm_formulas",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("dosage_form", sa.String(length=16), nullable=False, server_default="decoction"),
        sa.Column("composition", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("process", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("indication", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("shelf_life_months", sa.Integer(), nullable=False, server_default="12"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_tcm_formulas_code"), "tcm_formulas", ["code"], unique=True)
    op.create_index(op.f("ix_tcm_formulas_dosage_form"), "tcm_formulas", ["dosage_form"])
    op.create_index(op.f("ix_tcm_formulas_active"), "tcm_formulas", ["active"])

    op.create_table(
        "tcm_preparation_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("formula_id", sa.Integer(), nullable=False),
        sa.Column("batch_no", sa.String(length=32), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(length=16), nullable=False, server_default="剂"),
        sa.Column("produced_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("expire_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="produced"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["formula_id"], ["tcm_formulas.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_tcm_preparation_batches_batch_no"), "tcm_preparation_batches", ["batch_no"], unique=True
    )
    for column in ("formula_id", "org_id", "expire_date", "status"):
        op.create_index(
            op.f(f"ix_tcm_preparation_batches_{column}"), "tcm_preparation_batches", [column]
        )

    op.create_table(
        "cssd_cost_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("cost_type", sa.String(length=16), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["sterilization_batches.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_cssd_cost_items_batch_id"), "cssd_cost_items", ["batch_id"])
    op.create_index(op.f("ix_cssd_cost_items_cost_type"), "cssd_cost_items", ["cost_type"])

    op.create_table(
        "course_materials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("course_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("material_type", sa.String(length=16), nullable=False, server_default="slide"),
        sa.Column("url", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("play_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["course_id"], ["courses.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_course_materials_course_id"), "course_materials", ["course_id"])
    op.create_index(op.f("ix_course_materials_material_type"), "course_materials", ["material_type"])

    op.create_table(
        "training_plans",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("technique_id", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("plan_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("capacity", sa.Integer(), nullable=False, server_default="30"),
        sa.Column("trainer", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["technique_id"], ["tcm_techniques.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_training_plans_org_id"), "training_plans", ["org_id"])
    op.create_index(op.f("ix_training_plans_status"), "training_plans", ["status"])

    op.create_table(
        "training_enrollments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="enrolled"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["training_plans.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "user_id", name="uq_enroll_plan_user"),
    )
    op.create_index(op.f("ix_training_enrollments_plan_id"), "training_enrollments", ["plan_id"])
    op.create_index(op.f("ix_training_enrollments_user_id"), "training_enrollments", ["user_id"])
    op.create_index(op.f("ix_training_enrollments_status"), "training_enrollments", ["status"])

    op.create_table(
        "training_assessments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("comment", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("assessor", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["plan_id"], ["training_plans.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("plan_id", "user_id", name="uq_assess_plan_user"),
    )
    op.create_index(op.f("ix_training_assessments_plan_id"), "training_assessments", ["plan_id"])
    op.create_index(op.f("ix_training_assessments_user_id"), "training_assessments", ["user_id"])
    op.create_index(op.f("ix_training_assessments_passed"), "training_assessments", ["passed"])

    op.create_table(
        "prenatal_screenings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.Integer(), nullable=False),
        sa.Column("screen_type", sa.String(length=16), nullable=False),
        sa.Column("screen_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("gest_week", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False, server_default="low_risk"),
        sa.Column("indicator", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("conclusion", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("flagged_high_risk", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["record_id"], ["maternal_records.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("record_id", "screen_type", "result", "flagged_high_risk"):
        op.create_index(op.f(f"ix_prenatal_screenings_{column}"), "prenatal_screenings", [column])

    op.create_table(
        "improvement_tasks",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("indicator_key", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("problem", sa.String(length=512), nullable=False),
        sa.Column("measures", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("owner_name", sa.String(length=64), nullable=False),
        sa.Column("due_date", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="open"),
        sa.Column("completion_note", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("verify_comment", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("verified_by", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("org_id", "indicator_key", "due_date", "status"):
        op.create_index(op.f(f"ix_improvement_tasks_{column}"), "improvement_tasks", [column])

    op.create_table(
        "home_visit_orders",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("contract_id", sa.Integer(), nullable=True),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("service_type", sa.String(length=16), nullable=False),
        sa.Column("demand", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("address", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("expect_date", sa.String(length=10), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=16), nullable=False, server_default="applied"),
        sa.Column("assignee_name", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("dispatched_at", sa.DateTime(), nullable=True),
        sa.Column("service_note", sa.String(length=512), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["contract_id"], ["fd_contracts.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("patient_id", "org_id", "service_type", "status"):
        op.create_index(op.f(f"ix_home_visit_orders_{column}"), "home_visit_orders", [column])


def downgrade() -> None:
    for table in (
        "home_visit_orders",
        "improvement_tasks",
        "prenatal_screenings",
        "training_assessments",
        "training_enrollments",
        "training_plans",
        "course_materials",
        "cssd_cost_items",
        "tcm_preparation_batches",
        "tcm_formulas",
    ):
        op.drop_table(table)
