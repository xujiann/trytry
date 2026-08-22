"""ESB 端点补出站投递地址与签名密钥

Revision ID: c9d3e4f5a6b8
Revises: b7c1d2e3f4a6
Create Date: 2026-08-21

为什么加这两列：集成层工程包 I1 把 ESB 出站从"仅登记"补成真实投递闭环——
消费出站消息时把转换后的报文经 HTTP POST 投递到接入方的 `endpoint_url`，
报文体用 `secret` 做 HMAC-SHA256 加签供对方验签。两列均可空：

- `endpoint_url` 为空 → 端点保持既有"仅登记"语义，消费成功但不投递（响应说明）；
- `secret` 为空 → 投递时不带签名头（对接方未约定验签时的最小配置）。

水位（FHIR 批量导出）复用既有 `system_params` 表，不新建表。
downgrade 对称删列。
"""
import sqlalchemy as sa
from alembic import op

revision = "c9d3e4f5a6b8"
down_revision = "c9d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("esb_endpoints", sa.Column("endpoint_url", sa.String(length=512), nullable=True))
    op.add_column("esb_endpoints", sa.Column("secret", sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column("esb_endpoints", "secret")
    op.drop_column("esb_endpoints", "endpoint_url")
