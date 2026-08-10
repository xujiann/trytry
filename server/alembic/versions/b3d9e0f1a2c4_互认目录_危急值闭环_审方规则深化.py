"""互认目录、危急值闭环、审方规则深化

Revision ID: b3d9e0f1a2c4
Revises: a95862fe8c3f
Create Date: 2026-08-10 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b3d9e0f1a2c4'
down_revision: Union[str, Sequence[str], None] = 'a95862fe8c3f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # 互认项目目录
    op.create_table(
        'recognition_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('item_code', sa.String(length=64), nullable=False),
        sa.Column('item_name', sa.String(length=128), nullable=False),
        sa.Column('center_type', sa.String(length=16), nullable=False),
        sa.Column('mutual_scope', sa.String(length=16), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_recognition_items_item_code'), 'recognition_items', ['item_code'], unique=True
    )
    op.create_index(
        op.f('ix_recognition_items_center_type'), 'recognition_items', ['center_type'], unique=False
    )

    # 危急值闭环：报告状态字段（存量行以空串填充 = 非危急值/未进入闭环）
    op.add_column(
        'exam_reports',
        sa.Column('critical_status', sa.String(length=16), nullable=False, server_default=''),
    )
    op.create_index(
        op.f('ix_exam_reports_critical_status'), 'exam_reports', ['critical_status'], unique=False
    )

    # 危急值处置留痕
    op.create_table(
        'critical_actions',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('report_id', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(length=512), nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['report_id'], ['exam_reports.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(
        op.f('ix_critical_actions_report_id'), 'critical_actions', ['report_id'], unique=False
    )

    # 审方规则深化：禁忌诊断关键词 + 特殊人群（存量行以空串填充）
    op.add_column(
        'drug_rules',
        sa.Column(
            'contraindicated_diagnoses', sa.String(length=512), nullable=False, server_default=''
        ),
    )
    op.add_column(
        'drug_rules',
        sa.Column('special_groups', sa.String(length=64), nullable=False, server_default=''),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('drug_rules', 'special_groups')
    op.drop_column('drug_rules', 'contraindicated_diagnoses')
    op.drop_index(op.f('ix_critical_actions_report_id'), table_name='critical_actions')
    op.drop_table('critical_actions')
    op.drop_index(op.f('ix_exam_reports_critical_status'), table_name='exam_reports')
    op.drop_column('exam_reports', 'critical_status')
    op.drop_index(op.f('ix_recognition_items_center_type'), table_name='recognition_items')
    op.drop_index(op.f('ix_recognition_items_item_code'), table_name='recognition_items')
    op.drop_table('recognition_items')
