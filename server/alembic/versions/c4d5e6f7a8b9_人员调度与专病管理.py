"""人员下沉调度台账与专病路径管理

派驻侧补的是"算不出监测指标"的那几个记录位：职称等级（不从职称文本推断）
与派驻类型（巡诊/短期支援不是下沉）。专病侧另立三张表，不复用慢病——
慢病是长期随访分级，专病是有始有终的诊疗路径。

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""
import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 职称等级：监测指标要区分"中级及以上"，而 title 是自由文本，各院写法不一
    op.add_column(
        "employees",
        sa.Column("title_level", sa.String(16), nullable=False, server_default="none"),
    )
    op.create_index("ix_employees_title_level", "employees", ["title_level"])

    # 派驻类型：只有 long_term 计入下沉指标；巡诊与短期支援混进去会把指标做虚
    op.add_column(
        "secondments",
        sa.Column("assignment_type", sa.String(16), nullable=False, server_default="long_term"),
    )
    op.create_index("ix_secondments_assignment_type", "secondments", ["assignment_type"])
    op.create_index("ix_secondments_to_org_id", "secondments", ["to_org_id"])
    op.add_column("secondments",
                  sa.Column("position", sa.String(64), nullable=False, server_default=""))
    op.add_column("secondments",
                  sa.Column("note", sa.String(256), nullable=False, server_default=""))

    op.create_table(
        "disease_programs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(32), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(512), nullable=False, server_default=""),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True,
                  index=True),
        # 路径节点可配置，不预置任何具体病种——各地管什么病、分几步差异极大
        sa.Column("path_nodes", sa.JSON(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "disease_enrollments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("program_id", sa.Integer(), sa.ForeignKey("disease_programs.id"),
                  nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False,
                  index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False,
                  index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="enrolled", index=True),
        sa.Column("enrolled_at", sa.String(10), nullable=False, server_default=""),
        sa.Column("exited_at", sa.String(10), nullable=False, server_default=""),
        # 留空表示未评价，不等于"无效"
        sa.Column("outcome", sa.String(16), nullable=False, server_default=""),
        sa.Column("outcome_note", sa.String(512), nullable=False, server_default=""),
        sa.Column("exit_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "disease_path_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enrollment_id", sa.Integer(), sa.ForeignKey("disease_enrollments.id"),
                  nullable=False, index=True),
        sa.Column("node_key", sa.String(32), nullable=False, index=True),
        sa.Column("performed_at", sa.String(10), nullable=False, server_default=""),
        sa.Column("operator_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("result", sa.String(256), nullable=False, server_default=""),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("disease_path_records")
    op.drop_table("disease_enrollments")
    op.drop_table("disease_programs")
    op.drop_column("secondments", "note")
    op.drop_column("secondments", "position")
    op.drop_index("ix_secondments_to_org_id", table_name="secondments")
    op.drop_index("ix_secondments_assignment_type", table_name="secondments")
    op.drop_column("secondments", "assignment_type")
    op.drop_index("ix_employees_title_level", table_name="employees")
    op.drop_column("employees", "title_level")
