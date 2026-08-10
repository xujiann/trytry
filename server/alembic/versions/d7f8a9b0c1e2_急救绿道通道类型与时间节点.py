"""急救绿道通道类型与时间节点

Revision ID: d7f8a9b0c1e2
Revises: c5e6f7a8b9d0
Create Date: 2026-08-10 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7f8a9b0c1e2'
down_revision: Union[str, Sequence[str], None] = 'c5e6f7a8b9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 绿色通道类型（存量行以空串填充 = 普通急救）
    op.add_column(
        'emergency_cases',
        sa.Column('channel_type', sa.String(length=16), nullable=False, server_default=''),
    )
    op.create_index(
        op.f('ix_emergency_cases_channel_type'), 'emergency_cases', ['channel_type'], unique=False
    )

    # 绿道时间节点
    op.create_table(
        'emergency_milestones',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('case_id', sa.Integer(), nullable=False),
        sa.Column('milestone', sa.String(length=16), nullable=False),
        sa.Column('occurred_at', sa.String(length=32), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['case_id'], ['emergency_cases.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('case_id', 'milestone', name='uq_emergency_milestone_case'),
    )
    op.create_index(
        op.f('ix_emergency_milestones_case_id'), 'emergency_milestones', ['case_id'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_emergency_milestones_case_id'), table_name='emergency_milestones')
    op.drop_table('emergency_milestones')
    op.drop_index(op.f('ix_emergency_cases_channel_type'), table_name='emergency_cases')
    op.drop_column('emergency_cases', 'channel_type')
