"""站内消息表：与 WebSocket 瞬时广播互补的可靠触达

Revision ID: c5d6e7f8a2b3
Revises: b4c5d6e7f8a1
"""
import sqlalchemy as sa
from alembic import op

revision = "c5d6e7f8a2b3"
down_revision = "b4c5d6e7f8a1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), primary_key=True),
        # 收件人二选一：工作人员 or 居民账户。两类身份鉴权路径不同，分开存
        # 反而让越权更难写错，故不合并成统一的"主体"外键。
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True, index=True),
        sa.Column(
            "resident_account_id",
            sa.Integer(),
            sa.ForeignKey("resident_accounts.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("category", sa.String(24), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("body", sa.String(1024), nullable=False, server_default=""),
        # 关联业务对象，供客户端跳转；不做外键（可指向任意业务表）
        sa.Column("link_type", sa.String(32), nullable=False, server_default=""),
        sa.Column("link_id", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("notifications")
