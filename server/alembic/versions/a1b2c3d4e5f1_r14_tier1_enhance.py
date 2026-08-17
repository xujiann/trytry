"""第十四轮 E1/E3/E4/E5/E6：家医签约内容化、转诊闭环、公卫短板、统一采购配送、数据智能层

Revision ID: a1b2c3d4e5f1
Revises: 9ae7abe85c31
Create Date: 2026-08-17 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f1'
down_revision: Union[str, Sequence[str], None] = '9ae7abe85c31'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'service_packages',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('annual_fee', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('target_population', sa.String(length=32), nullable=False),
        sa.Column('description', sa.String(length=512), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_service_packages_code', 'service_packages', ['code'], unique=True)
    op.create_table(
        'service_package_items',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('package_id', sa.Integer(), sa.ForeignKey('service_packages.id'), nullable=False),
        sa.Column('service_type', sa.String(length=16), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('annual_times', sa.Integer(), nullable=False),
        sa.Column('billable', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_service_package_items_package_id', 'service_package_items', ['package_id'], unique=False)
    op.create_table(
        'sign_fee_pools',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('org_group_id', sa.Integer(), sa.ForeignKey('org_groups.id'), nullable=True),
        sa.Column('per_capita', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('total_amount', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('year', 'org_group_id', name='uq_signfee_year_group'),
    )
    op.create_index('ix_sign_fee_pools_org_group_id', 'sign_fee_pools', ['org_group_id'], unique=False)
    op.create_index('ix_sign_fee_pools_year', 'sign_fee_pools', ['year'], unique=False)
    op.create_table(
        'sign_fee_distributions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('pool_id', sa.Integer(), sa.ForeignKey('sign_fee_pools.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('headcount', sa.Integer(), nullable=False),
        sa.Column('fulfillment_rate', sa.Float(), nullable=False),
        sa.Column('key_pop_factor', sa.Float(), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('share_pct', sa.Float(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_sign_fee_distributions_pool_id', 'sign_fee_distributions', ['pool_id'], unique=False)
    op.create_index('ix_sign_fee_distributions_org_id', 'sign_fee_distributions', ['org_id'], unique=False)
    op.create_table(
        'referral_clinical_refs',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('referral_id', sa.Integer(), sa.ForeignKey('referrals.id'), nullable=False),
        sa.Column('ref_type', sa.String(length=24), nullable=False),
        sa.Column('ref_id', sa.Integer(), nullable=False),
        sa.Column('summary', sa.String(length=1024), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_referral_clinical_refs_referral_id', 'referral_clinical_refs', ['referral_id'], unique=False)
    op.create_table(
        'referral_plans',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('referral_id', sa.Integer(), sa.ForeignKey('referrals.id'), nullable=False),
        sa.Column('plan_type', sa.String(length=16), nullable=False),
        sa.Column('content', sa.String(length=2048), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('created_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_referral_plans_referral_id', 'referral_plans', ['referral_id'], unique=False)
    op.create_index('ix_referral_plans_org_id', 'referral_plans', ['org_id'], unique=False)
    op.create_table(
        'referral_slot_reservations',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('referral_id', sa.Integer(), sa.ForeignKey('referrals.id'), nullable=False),
        sa.Column('slot_id', sa.Integer(), sa.ForeignKey('appointment_slots.id'), nullable=False),
        sa.Column('appointment_id', sa.Integer(), sa.ForeignKey('appointments.id'), nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('reserved_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('referral_id', name='uq_referral_slot_reservation'),
    )
    op.create_index('ix_referral_slot_reservations_referral_id', 'referral_slot_reservations', ['referral_id'], unique=False)
    op.create_index('ix_referral_slot_reservations_slot_id', 'referral_slot_reservations', ['slot_id'], unique=False)
    op.create_index('ix_referral_slot_reservations_patient_id', 'referral_slot_reservations', ['patient_id'], unique=False)
    op.create_table(
        'psych_patients',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('diagnosis', sa.String(length=128), nullable=False),
        sa.Column('danger_level', sa.String(length=2), nullable=False),
        sa.Column('guardian', sa.String(length=64), nullable=False),
        sa.Column('guardian_phone', sa.String(length=32), nullable=False),
        sa.Column('locked', sa.Boolean(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_psych_patients_org_id', 'psych_patients', ['org_id'], unique=False)
    op.create_index('ix_psych_patients_patient_id', 'psych_patients', ['patient_id'], unique=False)
    op.create_table(
        'psych_followups',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('psych_id', sa.Integer(), sa.ForeignKey('psych_patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('followup_date', sa.String(length=10), nullable=False),
        sa.Column('medication_adherence', sa.String(length=8), nullable=False),
        sa.Column('stability', sa.String(length=16), nullable=False),
        sa.Column('note', sa.String(length=512), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_psych_followups_psych_id', 'psych_followups', ['psych_id'], unique=False)
    op.create_index('ix_psych_followups_org_id', 'psych_followups', ['org_id'], unique=False)
    op.create_table(
        'tb_patients',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('tb_type', sa.String(length=32), nullable=False),
        sa.Column('treatment_start', sa.String(length=10), nullable=False),
        sa.Column('regimen', sa.String(length=64), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_tb_patients_org_id', 'tb_patients', ['org_id'], unique=False)
    op.create_index('ix_tb_patients_patient_id', 'tb_patients', ['patient_id'], unique=False)
    op.create_table(
        'tb_followups',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('tb_id', sa.Integer(), sa.ForeignKey('tb_patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('followup_date', sa.String(length=10), nullable=False),
        sa.Column('taken', sa.Boolean(), nullable=False),
        sa.Column('sputum_result', sa.String(length=16), nullable=False),
        sa.Column('note', sa.String(length=512), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_tb_followups_org_id', 'tb_followups', ['org_id'], unique=False)
    op.create_index('ix_tb_followups_tb_id', 'tb_followups', ['tb_id'], unique=False)
    op.create_table(
        'tb_contacts',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('tb_id', sa.Integer(), sa.ForeignKey('tb_patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('relation', sa.String(length=32), nullable=False),
        sa.Column('screened', sa.Boolean(), nullable=False),
        sa.Column('result', sa.String(length=64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_tb_contacts_tb_id', 'tb_contacts', ['tb_id'], unique=False)
    op.create_index('ix_tb_contacts_org_id', 'tb_contacts', ['org_id'], unique=False)
    op.create_table(
        'health_supervisions',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('domain', sa.String(length=16), nullable=False),
        sa.Column('target', sa.String(length=128), nullable=False),
        sa.Column('inspect_date', sa.String(length=10), nullable=False),
        sa.Column('findings', sa.String(length=512), nullable=False),
        sa.Column('passed', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_health_supervisions_org_id', 'health_supervisions', ['org_id'], unique=False)
    op.create_table(
        'pubhealth_projects',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pubhealth_projects_code', 'pubhealth_projects', ['code'], unique=True)
    op.create_table(
        'pubhealth_project_stats',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('pubhealth_projects.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('target_count', sa.Integer(), nullable=False),
        sa.Column('done_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('project_id', 'org_id', 'year', name='uq_ph_project_stat'),
    )
    op.create_index('ix_pubhealth_project_stats_project_id', 'pubhealth_project_stats', ['project_id'], unique=False)
    op.create_index('ix_pubhealth_project_stats_org_id', 'pubhealth_project_stats', ['org_id'], unique=False)
    op.create_table(
        'consortium_drug_catalog',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('drug_code', sa.String(length=64), nullable=False),
        sa.Column('drug_name', sa.String(length=128), nullable=False),
        sa.Column('spec', sa.String(length=64), nullable=False),
        sa.Column('unit', sa.String(length=16), nullable=False),
        sa.Column('selected', sa.Boolean(), nullable=False),
        sa.Column('volume_agreed', sa.Integer(), nullable=False),
        sa.Column('unit_price', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_consortium_drug_catalog_drug_code', 'consortium_drug_catalog', ['drug_code'], unique=True)
    op.create_table(
        'volume_purchases',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('catalog_id', sa.Integer(), sa.ForeignKey('consortium_drug_catalog.id'), nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('total_volume', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_volume_purchases_catalog_id', 'volume_purchases', ['catalog_id'], unique=False)
    op.create_table(
        'volume_purchase_allocations',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('purchase_id', sa.Integer(), sa.ForeignKey('volume_purchases.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('allocated_volume', sa.Integer(), nullable=False),
        sa.Column('fulfilled_volume', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('purchase_id', 'org_id', name='uq_volume_alloc_purchase_org'),
    )
    op.create_index('ix_volume_purchase_allocations_purchase_id', 'volume_purchase_allocations', ['purchase_id'], unique=False)
    op.create_index('ix_volume_purchase_allocations_org_id', 'volume_purchase_allocations', ['org_id'], unique=False)
    op.create_table(
        'distribution_orders',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('from_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('to_org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('drug_code', sa.String(length=64), nullable=False),
        sa.Column('qty', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_distribution_orders_from_org_id', 'distribution_orders', ['from_org_id'], unique=False)
    op.create_index('ix_distribution_orders_to_org_id', 'distribution_orders', ['to_org_id'], unique=False)
    op.create_table(
        'drug_trace_codes',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('trace_code', sa.String(length=64), nullable=False),
        sa.Column('drug_code', sa.String(length=64), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('direction', sa.String(length=8), nullable=False),
        sa.Column('ref_type', sa.String(length=24), nullable=False),
        sa.Column('ref_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_drug_trace_codes_org_id', 'drug_trace_codes', ['org_id'], unique=False)
    op.create_index('ix_drug_trace_codes_trace_code', 'drug_trace_codes', ['trace_code'], unique=False)
    op.create_table(
        'risk_model_weights',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('factor', sa.String(length=32), nullable=False),
        sa.Column('weight', sa.Float(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_risk_model_weights_factor', 'risk_model_weights', ['factor'], unique=True)
    # E1：签约挂内容化服务包 + 重点人群 + 到期日；履约核销到条目
    # SQLite 不支持 ALTER 加约束，统一走 batch 复制-搬移策略（与 77e9ad26c428 一致）
    with op.batch_alter_table('fd_contracts') as batch:
        batch.add_column(sa.Column('package_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('key_population', sa.String(length=32), nullable=False, server_default=''))
        batch.add_column(sa.Column('expire_date', sa.String(length=10), nullable=False, server_default=''))
        batch.create_index('ix_fd_contracts_package_id', ['package_id'])
        batch.create_foreign_key('fk_fd_contracts_package_id', 'service_packages', ['package_id'], ['id'])
    with op.batch_alter_table('fd_contract_services') as batch:
        batch.add_column(sa.Column('item_id', sa.Integer(), nullable=True))
        batch.create_index('ix_fd_contract_services_item_id', ['item_id'])
        batch.create_foreign_key('fk_fd_contract_services_item_id', 'service_package_items', ['item_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('fd_contract_services') as batch:
        batch.drop_constraint('fk_fd_contract_services_item_id', type_='foreignkey')
        batch.drop_index('ix_fd_contract_services_item_id')
        batch.drop_column('item_id')
    with op.batch_alter_table('fd_contracts') as batch:
        batch.drop_constraint('fk_fd_contracts_package_id', type_='foreignkey')
        batch.drop_index('ix_fd_contracts_package_id')
        batch.drop_column('expire_date')
        batch.drop_column('key_population')
        batch.drop_column('package_id')
    op.drop_table('risk_model_weights')
    op.drop_table('drug_trace_codes')
    op.drop_table('distribution_orders')
    op.drop_table('volume_purchase_allocations')
    op.drop_table('volume_purchases')
    op.drop_table('consortium_drug_catalog')
    op.drop_table('pubhealth_project_stats')
    op.drop_table('pubhealth_projects')
    op.drop_table('health_supervisions')
    op.drop_table('tb_contacts')
    op.drop_table('tb_followups')
    op.drop_table('tb_patients')
    op.drop_table('psych_followups')
    op.drop_table('psych_patients')
    op.drop_table('referral_slot_reservations')
    op.drop_table('referral_plans')
    op.drop_table('referral_clinical_refs')
    op.drop_table('sign_fee_distributions')
    op.drop_table('sign_fee_pools')
    op.drop_table('service_package_items')
    op.drop_table('service_packages')
