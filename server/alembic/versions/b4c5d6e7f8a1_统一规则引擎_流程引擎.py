"""统一规则引擎与业务流程引擎（阶段五 T5.1 / T5.2）

T5.3 统一申请单中心是既有五类单据的聚合视图，不新增表——刻意不建第六张
通用单据表，那只会得到一张没人写入的空表。

Revision ID: b4c5d6e7f8a1
Revises: a3b4c5d6e7f9
"""
import sqlalchemy as sa
from alembic import op

revision = "b4c5d6e7f8a1"
down_revision = "a3b4c5d6e7f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rule_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(48), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("domain", sa.String(24), nullable=False, index=True),
        sa.Column("condition", sa.String(512), nullable=False),
        sa.Column("message", sa.String(256), nullable=False, server_default=""),
        sa.Column("severity", sa.String(16), nullable=False, server_default="warning", index=True),
        sa.Column("deduct_points", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "workflow_definitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(48), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("nodes", sa.JSON(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "workflow_instances",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("definition_key", sa.String(48), nullable=False, index=True),
        # 业务单据类型+id，不做外键：流程可挂到任何业务对象上
        sa.Column("business_type", sa.String(32), nullable=False, index=True),
        sa.Column("business_id", sa.Integer(), nullable=False, server_default="0", index=True),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True, index=True),
        sa.Column("current_node", sa.String(48), nullable=False, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="running", index=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "workflow_transitions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("instance_id", sa.Integer(), sa.ForeignKey("workflow_instances.id"), nullable=False, index=True),
        sa.Column("from_node", sa.String(48), nullable=False, server_default=""),
        sa.Column("to_node", sa.String(48), nullable=False, server_default=""),
        sa.Column("action", sa.String(16), nullable=False, server_default="advance"),
        sa.Column("comment", sa.String(512), nullable=False, server_default=""),
        sa.Column("actor_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("workflow_transitions")
    op.drop_table("workflow_instances")
    op.drop_table("workflow_definitions")
    op.drop_table("rule_definitions")
