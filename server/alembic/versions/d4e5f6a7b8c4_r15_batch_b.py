"""第十五轮 Batch B：临床路径医嘱物化表 patient_pathway_orders

Revision ID: d4e5f6a7b8c4
Revises: c3d4e5f6a7b3
Create Date: 2026-08-17 02:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'd4e5f6a7b8c4'
down_revision: Union[str, Sequence[str], None] = 'c3d4e5f6a7b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'patient_pathway_orders',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_pathway_id', sa.Integer(), sa.ForeignKey('patient_pathways.id'), nullable=False),
        sa.Column('node_id', sa.Integer(), sa.ForeignKey('pathway_nodes.id'), nullable=False),
        sa.Column('day_no', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('order_type', sa.String(length=24), nullable=False),
        sa.Column('detail', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_patient_pathway_orders_node_id', 'patient_pathway_orders', ['node_id'], unique=False)
    op.create_index('ix_patient_pathway_orders_patient_pathway_id', 'patient_pathway_orders', ['patient_pathway_id'], unique=False)


def downgrade() -> None:
    op.drop_table('patient_pathway_orders')
