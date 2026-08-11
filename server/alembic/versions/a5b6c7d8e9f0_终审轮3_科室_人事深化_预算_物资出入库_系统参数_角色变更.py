"""终审轮批3：科室库、人员变动/合同/薪酬、预算、物资出入库、系统参数、角色变更记录

Revision ID: a5b6c7d8e9f0
Revises: f3a4b5c6d7e8
Create Date: 2026-08-11
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: Union[str, Sequence[str], None] = "f3a4b5c6d7e8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "departments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "code", name="uq_dept_org_code"),
    )
    op.create_index(op.f("ix_departments_org_id"), "departments", ["org_id"], unique=False)
    op.create_table(
        "employee_changes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("change_type", sa.String(length=16), nullable=False),
        sa.Column("to_org_id", sa.Integer(), nullable=True),
        sa.Column("detail", sa.String(length=256), nullable=False),
        sa.Column("effective_date", sa.String(length=10), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.ForeignKeyConstraint(["to_org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_employee_changes_employee_id"), "employee_changes", ["employee_id"], unique=False)
    op.create_index(op.f("ix_employee_changes_change_type"), "employee_changes", ["change_type"], unique=False)
    op.create_table(
        "staff_contracts",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("contract_no", sa.String(length=64), nullable=False),
        sa.Column("start_date", sa.String(length=10), nullable=False),
        sa.Column("end_date", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("contract_no"),
    )
    op.create_index(op.f("ix_staff_contracts_employee_id"), "staff_contracts", ["employee_id"], unique=False)
    op.create_index(op.f("ix_staff_contracts_status"), "staff_contracts", ["status"], unique=False)
    op.create_table(
        "payroll_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.Integer(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("base_salary", sa.Float(), nullable=False),
        sa.Column("perf_bonus", sa.Float(), nullable=False),
        sa.Column("perf_coefficient", sa.Float(), nullable=False),
        sa.Column("total", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["employee_id"], ["employees.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "period", name="uq_payroll_emp_period"),
    )
    op.create_index(op.f("ix_payroll_records_employee_id"), "payroll_records", ["employee_id"], unique=False)
    op.create_index(op.f("ix_payroll_records_period"), "payroll_records", ["period"], unique=False)
    op.create_table(
        "budgets",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("year", sa.String(length=4), nullable=False),
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "year", "category", name="uq_budget_org_year_cat"),
    )
    op.create_index(op.f("ix_budgets_org_id"), "budgets", ["org_id"], unique=False)
    op.create_index(op.f("ix_budgets_year"), "budgets", ["year"], unique=False)
    op.create_table(
        "asset_movements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("movement_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("note", sa.String(length=256), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["asset_id"], ["assets.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_asset_movements_asset_id"), "asset_movements", ["asset_id"], unique=False)
    op.create_table(
        "system_params",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.String(length=256), nullable=False),
        sa.Column("description", sa.String(length=256), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_system_params_key"), "system_params", ["key"], unique=True)
    op.create_table(
        "role_change_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("old_role", sa.String(length=16), nullable=False),
        sa.Column("new_role", sa.String(length=16), nullable=False),
        sa.Column("changed_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_role_change_logs_user_id"), "role_change_logs", ["user_id"], unique=False)

    # 员工科室挂接（存量员工为空）
    op.add_column("employees", sa.Column("dept_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("employees", "dept_id")
    op.drop_index(op.f("ix_role_change_logs_user_id"), table_name="role_change_logs")
    op.drop_table("role_change_logs")
    op.drop_index(op.f("ix_system_params_key"), table_name="system_params")
    op.drop_table("system_params")
    op.drop_index(op.f("ix_asset_movements_asset_id"), table_name="asset_movements")
    op.drop_table("asset_movements")
    op.drop_index(op.f("ix_budgets_year"), table_name="budgets")
    op.drop_index(op.f("ix_budgets_org_id"), table_name="budgets")
    op.drop_table("budgets")
    op.drop_index(op.f("ix_payroll_records_period"), table_name="payroll_records")
    op.drop_index(op.f("ix_payroll_records_employee_id"), table_name="payroll_records")
    op.drop_table("payroll_records")
    op.drop_index(op.f("ix_staff_contracts_status"), table_name="staff_contracts")
    op.drop_index(op.f("ix_staff_contracts_employee_id"), table_name="staff_contracts")
    op.drop_table("staff_contracts")
    op.drop_index(op.f("ix_employee_changes_change_type"), table_name="employee_changes")
    op.drop_index(op.f("ix_employee_changes_employee_id"), table_name="employee_changes")
    op.drop_table("employee_changes")
    op.drop_index(op.f("ix_departments_org_id"), table_name="departments")
    op.drop_table("departments")
