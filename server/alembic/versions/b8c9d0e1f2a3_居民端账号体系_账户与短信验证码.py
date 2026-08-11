"""居民端账号体系：居民账户与短信验证码两表

手机号验证码 / 微信网页授权登录取代"电子健康卡号+身份证号"直接查档案。

phone 与 wechat_openid 用**可空唯一列**：唯一索引在 SQLite 与 PostgreSQL 下
都允许多个 NULL，因此"只有微信没有手机号"的账户可以共存，同时数据库约束
仍能挡住并发首登产生的重复账户。

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
"""
import sqlalchemy as sa
from alembic import op

revision = "b8c9d0e1f2a3"
down_revision = "a7b8c9d0e1f2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "resident_accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(20), nullable=True, unique=True),
        sa.Column("wechat_openid", sa.String(64), nullable=True, unique=True),
        sa.Column("wechat_unionid", sa.String(64), nullable=False, server_default=""),
        sa.Column("nickname", sa.String(64), nullable=False, server_default=""),
        # 未实名绑定时为空：账户存在但看不到任何档案
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=True, index=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("last_login_at", sa.DateTime(), nullable=True),
    )
    op.create_table(
        "sms_codes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("phone", sa.String(20), nullable=False, index=True),
        sa.Column("purpose", sa.String(16), nullable=False, server_default="login"),
        # 只落散列，明文验证码不入库
        sa.Column("code_hash", sa.String(160), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("consumed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("sms_codes")
    op.drop_table("resident_accounts")
