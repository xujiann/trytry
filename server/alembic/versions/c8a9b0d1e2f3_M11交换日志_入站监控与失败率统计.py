"""M11交换日志：入站监控与失败率统计

Revision ID: c8a9b0d1e2f3
Revises: b6f7a8c9d0e1
Create Date: 2026-08-11 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8a9b0d1e2f3'
down_revision: Union[str, Sequence[str], None] = 'b6f7a8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "exchange_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("source_system", sa.String(length=64), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=8), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("error_detail", sa.String(length=1024), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_exchange_logs_source_system"), "exchange_logs", ["source_system"], unique=False
    )
    op.create_index(
        op.f("ix_exchange_logs_message_type"), "exchange_logs", ["message_type"], unique=False
    )
    op.create_index(op.f("ix_exchange_logs_success"), "exchange_logs", ["success"], unique=False)
    op.create_index(
        op.f("ix_exchange_logs_created_at"), "exchange_logs", ["created_at"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_exchange_logs_created_at"), table_name="exchange_logs")
    op.drop_index(op.f("ix_exchange_logs_success"), table_name="exchange_logs")
    op.drop_index(op.f("ix_exchange_logs_message_type"), table_name="exchange_logs")
    op.drop_index(op.f("ix_exchange_logs_source_system"), table_name="exchange_logs")
    op.drop_table("exchange_logs")
