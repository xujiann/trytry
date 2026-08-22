"""等保账户安全（工程包 E1）：账号状态/口令生命周期/TOTP 三组列 + 登录留痕表

- users 加 status（active/disabled，停用即时生效）、password_updated_at 与
  must_change_password（口令生命周期：重置后首登强制改密、90 天超期 428）、
  totp_secret（双因素密钥，"pending:" 前缀=待验证）；
- 存量用户 password_updated_at 回填为**迁移时刻**——90 天超期从升级日起算，
  不惊扰现网（否则升级当天全员 428）；
- 新表 login_logs：登录成功/失败/锁定触发均落库（含居民端通道），username/
  created_at/success 带索引供留痕查询与爆破画像。

Revision ID: d4e5f6a7b8c1
Revises: b7c1d2e3f4a6
Create Date: 2026-08-21 10:00:00.000000

"""
from datetime import datetime, timezone
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c1"
down_revision: Union[str, Sequence[str], None] = "b7c1d2e3f4a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "users",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.create_index(op.f("ix_users_status"), "users", ["status"], unique=False)
    op.add_column("users", sa.Column("password_updated_at", sa.DateTime(), nullable=True))
    op.add_column(
        "users",
        sa.Column(
            "must_change_password", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column("users", sa.Column("totp_secret", sa.String(length=64), nullable=True))

    # 存量用户回填：口令基线记为迁移时刻（naive UTC，与全仓库时间戳口径一致）
    users = sa.table("users", sa.column("password_updated_at", sa.DateTime))
    op.execute(
        users.update().values(
            password_updated_at=datetime.now(timezone.utc).replace(tzinfo=None)
        )
    )

    op.create_table(
        "login_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("ip", sa.String(length=64), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("fail_reason", sa.String(length=32), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_login_logs_username"), "login_logs", ["username"], unique=False)
    op.create_index(op.f("ix_login_logs_success"), "login_logs", ["success"], unique=False)
    op.create_index(
        op.f("ix_login_logs_created_at"), "login_logs", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_login_logs_created_at"), table_name="login_logs")
    op.drop_index(op.f("ix_login_logs_success"), table_name="login_logs")
    op.drop_index(op.f("ix_login_logs_username"), table_name="login_logs")
    op.drop_table("login_logs")
    op.drop_column("users", "totp_secret")
    op.drop_column("users", "must_change_password")
    op.drop_column("users", "password_updated_at")
    op.drop_index(op.f("ix_users_status"), table_name="users")
    op.drop_column("users", "status")
