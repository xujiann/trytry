"""阶段九·五 第三批：⑧一码通、⑨便捷寻医、⑬名老中医传承、㉑模拟诊疗

一码通不建表——动态码是自包含的签名串（健康卡号.过期时刻.签名），
生命周期只有一分钟，落库等于每扫一次写一行还要清理过期行。

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""
import sqlalchemy as sa
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ⑨ 便捷寻医：号源挂医师档案，不靠姓名字符串匹配
    op.add_column("appointment_slots", sa.Column("employee_id", sa.Integer(), nullable=True))
    op.create_index("ix_appointment_slots_employee_id", "appointment_slots", ["employee_id"])

    # ⑬ 名老中医医案：分维度存，不压成一段文本
    op.create_table(
        "tcm_master_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("master_name", sa.String(64), nullable=False),
        sa.Column("successor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("disease", sa.String(128), nullable=False, server_default=""),
        sa.Column("syndrome", sa.String(128), nullable=False, server_default=""),
        sa.Column("four_exams", sa.String(2048), nullable=False, server_default=""),
        sa.Column("treatment_method", sa.String(512), nullable=False, server_default=""),
        sa.Column("prescription", sa.String(1024), nullable=False, server_default=""),
        sa.Column("commentary", sa.String(2048), nullable=False, server_default=""),
        sa.Column("visit_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    for col in ("master_name", "disease", "syndrome", "published"):
        op.create_index(f"ix_tcm_master_cases_{col}", "tcm_master_cases", [col])

    # ㉑ 模拟诊疗
    op.create_table(
        "simulation_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("category", sa.String(16), nullable=False, server_default="clinical"),
        sa.Column("scenario", sa.String(4096), nullable=False, server_default=""),
        sa.Column("decision_points", sa.JSON(), nullable=True),
        sa.Column("pass_score", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_simulation_cases_category", "simulation_cases", ["category"])
    op.create_index("ix_simulation_cases_active", "simulation_cases", ["active"])

    op.create_table(
        "simulation_attempts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("case_id", sa.Integer(), sa.ForeignKey("simulation_cases.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=True),
        sa.Column("score", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_simulation_attempts_case_id", "simulation_attempts", ["case_id"])
    op.create_index("ix_simulation_attempts_user_id", "simulation_attempts", ["user_id"])
    op.create_index("ix_simulation_attempts_passed", "simulation_attempts", ["passed"])


def downgrade() -> None:
    op.drop_table("simulation_attempts")
    op.drop_table("simulation_cases")
    op.drop_table("tcm_master_cases")
    op.drop_index("ix_appointment_slots_employee_id", table_name="appointment_slots")
    op.drop_column("appointment_slots", "employee_id")
