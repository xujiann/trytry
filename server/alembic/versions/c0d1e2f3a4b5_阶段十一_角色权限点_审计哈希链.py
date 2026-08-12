"""阶段十一：角色/权限点/关联 + 审计防篡改哈希链

内置六角色作为预置数据保留（由启动时 seed_builtin_roles 幂等写入），
权限点由启动时从路由表自动登记——手工维护的权限点清单与真实接口的偏差，
是这类系统最常见也最难查的问题。

审计哈希链对**存量记录不回填**：回填等于用今天的密钥给历史记录背书，
反而制造了"这些记录经过校验"的假象。存量记录 entry_hash 留空，
校验接口把它们单列为 legacy_unchained。

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""
import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(32), nullable=False, unique=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("description", sa.String(256), nullable=False, server_default=""),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_roles_key", "roles", ["key"], unique=True)
    op.create_index("ix_roles_builtin", "roles", ["builtin"])
    op.create_index("ix_roles_active", "roles", ["active"])

    op.create_table(
        "permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("code", sa.String(320), nullable=False, unique=True),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("module", sa.String(64), nullable=False, server_default=""),
        sa.Column("builtin_roles", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_permissions_code", "permissions", ["code"], unique=True)
    op.create_index("ix_permissions_path", "permissions", ["path"])
    op.create_index("ix_permissions_module", "permissions", ["module"])

    op.create_table(
        "role_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("role_id", sa.Integer(), sa.ForeignKey("roles.id"), nullable=False),
        sa.Column(
            "permission_id", sa.Integer(), sa.ForeignKey("permissions.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("role_id", "permission_id", name="uq_role_permission"),
    )
    op.create_index("ix_role_permissions_role_id", "role_permissions", ["role_id"])
    op.create_index(
        "ix_role_permissions_permission_id", "role_permissions", ["permission_id"]
    )

    op.add_column(
        "audit_logs", sa.Column("prev_hash", sa.String(64), nullable=False, server_default="")
    )
    op.add_column(
        "audit_logs", sa.Column("entry_hash", sa.String(64), nullable=False, server_default="")
    )
    op.create_index("ix_audit_logs_entry_hash", "audit_logs", ["entry_hash"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_entry_hash", table_name="audit_logs")
    op.drop_column("audit_logs", "entry_hash")
    op.drop_column("audit_logs", "prev_hash")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
