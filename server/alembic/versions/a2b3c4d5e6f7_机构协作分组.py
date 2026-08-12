"""机构协作分组（片区 / 专科联盟 / 网格）

机构之间的横向分组，与 organizations.parent_id 的纵向隶属正交。
刻意不做成第二棵树——现实里一家卫生院的行政上级与它所属的专科联盟牵头单位
常常不是同一家，做成树会立刻遇到归属冲突。

Revision ID: a2b3c4d5e6f7
Revises: f8a2b3c4d5e6
"""
import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f8a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "org_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True, index=True),
        # 仅作标签用途，平台不为某一种类型写特殊分支
        sa.Column("group_type", sa.String(16), nullable=False, server_default="zone", index=True),
        # 可空：网格化管理常常没有"牵头单位"这一说
        sa.Column("lead_org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=True),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    # 多对多：一家机构可以同时属于多个分组（既在某片区，又在某专科联盟）
    op.create_table(
        "org_group_members",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("org_groups.id"), nullable=False,
                  index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False,
                  index=True),
        sa.Column("joined_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("group_id", "org_id", name="uq_org_group_member"),
    )


def downgrade() -> None:
    op.drop_table("org_group_members")
    op.drop_table("org_groups")
