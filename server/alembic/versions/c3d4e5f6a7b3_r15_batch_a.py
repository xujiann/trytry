"""第十五轮 Batch A：绩效二次分配/EMPI/电子健康卡/治理架构/预算编制 + 结算回写·续方库存列

Revision ID: c3d4e5f6a7b3
Revises: b2c3d4e5f6a2
Create Date: 2026-08-17 01:00:00.000000

"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = 'c3d4e5f6a7b3'
down_revision: Union[str, Sequence[str], None] = 'b2c3d4e5f6a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'perf_distribution_pools',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('org_group_id', sa.Integer(), sa.ForeignKey('org_groups.id'), nullable=True),
        sa.Column('source', sa.String(length=24), nullable=False),
        sa.Column('total_amount', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('year', 'org_group_id', 'source', name='uq_perf_pool_year_group_source'),
    )
    op.create_index('ix_perf_distribution_pools_org_group_id', 'perf_distribution_pools', ['org_group_id'], unique=False)
    op.create_index('ix_perf_distribution_pools_year', 'perf_distribution_pools', ['year'], unique=False)
    op.create_table(
        'perf_distributions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('pool_id', sa.Integer(), sa.ForeignKey('perf_distribution_pools.id'), nullable=False),
        sa.Column('target_type', sa.String(length=12), nullable=False),
        sa.Column('target_id', sa.Integer(), nullable=False),
        sa.Column('target_name', sa.String(length=64), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('share_pct', sa.Float(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_perf_distributions_pool_id', 'perf_distributions', ['pool_id'], unique=False)
    op.create_table(
        'patient_merge_links',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('primary_patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('duplicate_patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('reason', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('duplicate_patient_id', name='uq_merge_duplicate'),
    )
    op.create_index('ix_patient_merge_links_primary_patient_id', 'patient_merge_links', ['primary_patient_id'], unique=False)
    op.create_index('ix_patient_merge_links_duplicate_patient_id', 'patient_merge_links', ['duplicate_patient_id'], unique=False)
    op.create_table(
        'ehealth_cards',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('card_no', sa.String(length=32), nullable=False),
        sa.Column('qr_token', sa.String(length=64), nullable=False),
        sa.Column('medical_voucher', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('issued_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_ehealth_cards_patient_id', 'ehealth_cards', ['patient_id'], unique=True)
    op.create_index('ix_ehealth_cards_card_no', 'ehealth_cards', ['card_no'], unique=True)
    op.create_index('ix_ehealth_cards_qr_token', 'ehealth_cards', ['qr_token'], unique=True)
    op.create_table(
        'consortium_charters',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('title', sa.String(length=128), nullable=False),
        sa.Column('version', sa.String(length=16), nullable=False),
        sa.Column('content', sa.String(length=4000), nullable=False),
        sa.Column('effective_date', sa.String(length=10), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('title', 'version', name='uq_charter_title_version'),
    )
    op.create_table(
        'governance_bodies',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('body_type', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_table(
        'governance_members',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('body_id', sa.Integer(), sa.ForeignKey('governance_bodies.id'), nullable=False),
        sa.Column('member_name', sa.String(length=64), nullable=False),
        sa.Column('title', sa.String(length=32), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_governance_members_body_id', 'governance_members', ['body_id'], unique=False)
    op.create_index('ix_governance_members_org_id', 'governance_members', ['org_id'], unique=False)
    op.create_table(
        'staff_pools',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('employees.id'), nullable=True),
        sa.Column('member_name', sa.String(length=64), nullable=False),
        sa.Column('from_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('to_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('assign_type', sa.String(length=24), nullable=False),
        sa.Column('start_date', sa.String(length=10), nullable=False),
        sa.Column('end_date', sa.String(length=10), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_staff_pools_employee_id', 'staff_pools', ['employee_id'], unique=False)
    op.create_index('ix_staff_pools_to_org_id', 'staff_pools', ['to_org_id'], unique=False)
    op.create_index('ix_staff_pools_from_org_id', 'staff_pools', ['from_org_id'], unique=False)
    op.create_table(
        'fund_budget_plans',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('org_group_id', sa.Integer(), sa.ForeignKey('org_groups.id'), nullable=True),
        sa.Column('insurance_type', sa.String(length=16), nullable=False),
        sa.Column('base_amount', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('growth_pct', sa.Float(), nullable=False),
        sa.Column('per_capita', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('headcount', sa.Integer(), nullable=False),
        sa.Column('computed_total', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('applied_pool_id', sa.Integer(), nullable=True),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('year', 'org_group_id', 'insurance_type', name='uq_budget_year_group_type'),
    )
    op.create_index('ix_fund_budget_plans_year', 'fund_budget_plans', ['year'], unique=False)
    op.create_index('ix_fund_budget_plans_org_group_id', 'fund_budget_plans', ['org_group_id'], unique=False)
    with op.batch_alter_table('payment_cases') as b:
        b.add_column(sa.Column('insurance_settlement_id', sa.Integer(), nullable=True))
        b.create_index('ix_payment_cases_insurance_settlement_id', ['insurance_settlement_id'])
    with op.batch_alter_table('refill_requests') as b:
        b.add_column(sa.Column('drug_code', sa.String(length=64), nullable=False, server_default=''))
        b.add_column(sa.Column('qty', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    with op.batch_alter_table('refill_requests') as b:
        b.drop_column('qty')
        b.drop_column('drug_code')
    with op.batch_alter_table('payment_cases') as b:
        b.drop_index('ix_payment_cases_insurance_settlement_id')
        b.drop_column('insurance_settlement_id')
    op.drop_table('fund_budget_plans')
    op.drop_table('staff_pools')
    op.drop_table('governance_members')
    op.drop_table('governance_bodies')
    op.drop_table('consortium_charters')
    op.drop_table('ehealth_cards')
    op.drop_table('patient_merge_links')
    op.drop_table('perf_distributions')
    op.drop_table('perf_distribution_pools')
