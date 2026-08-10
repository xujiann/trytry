"""绩效指标目录、法定传染病目录

Revision ID: c5e6f7a8b9d0
Revises: b3d9e0f1a2c4
Create Date: 2026-08-10 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c5e6f7a8b9d0'
down_revision: Union[str, Sequence[str], None] = 'b3d9e0f1a2c4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 绩效考核指标目录
    op.create_table(
        'performance_indicators',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('key', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_performance_indicators_key'), 'performance_indicators', ['key'], unique=True
    )

    # 法定传染病目录
    op.create_table(
        'infectious_diseases',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('code', sa.String(length=64), nullable=False),
        sa.Column('name', sa.String(length=128), nullable=False),
        sa.Column('category', sa.String(length=4), nullable=False),
        sa.Column('report_hours', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_infectious_diseases_code'), 'infectious_diseases', ['code'], unique=True
    )
    op.create_index(
        op.f('ix_infectious_diseases_category'), 'infectious_diseases', ['category'], unique=False
    )

    # 病例回填分类（存量行以空串填充 = 目录外/未分类）
    op.add_column(
        'infectious_cases',
        sa.Column('category', sa.String(length=4), nullable=False, server_default=''),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('infectious_cases', 'category')
    op.drop_index(op.f('ix_infectious_diseases_category'), table_name='infectious_diseases')
    op.drop_index(op.f('ix_infectious_diseases_code'), table_name='infectious_diseases')
    op.drop_table('infectious_diseases')
    op.drop_index(op.f('ix_performance_indicators_key'), table_name='performance_indicators')
    op.drop_table('performance_indicators')
