"""第十四轮 E2/E7/E8/E9：DRG/DIP结算+智能审核、CDSS、居民端可办、互操作标准化

Revision ID: b2c3d4e5f6a2
Revises: a1b2c3d4e5f1
Create Date: 2026-08-17 00:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2c3d4e5f6a2'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'drg_rates',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('region', sa.String(length=16), nullable=False),
        sa.Column('rate_per_weight', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('note', sa.String(length=128), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('year', 'region', name='uq_drg_rate_year_region'),
    )
    op.create_table(
        'dip_catalog',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('disease_code', sa.String(length=64), nullable=False),
        sa.Column('disease_name', sa.String(length=128), nullable=False),
        sa.Column('score', sa.Float(), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_dip_catalog_disease_code', 'dip_catalog', ['disease_code'], unique=True)
    op.create_table(
        'dip_rates',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('point_value', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_dip_rates_year', 'dip_rates', ['year'], unique=True)
    op.create_table(
        'payment_cases',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('admission_id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('pay_mode', sa.String(length=8), nullable=False),
        sa.Column('group_code', sa.String(length=64), nullable=False),
        sa.Column('weight_or_score', sa.Float(), nullable=False),
        sa.Column('standard_payment', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('actual_cost', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('profit', sa.Numeric(precision=14, scale=2, asdecimal=False), nullable=False),
        sa.Column('year', sa.String(length=4), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('admission_id', 'pay_mode', name='uq_payment_case_adm_mode'),
    )
    op.create_index('ix_payment_cases_admission_id', 'payment_cases', ['admission_id'], unique=False)
    op.create_index('ix_payment_cases_org_id', 'payment_cases', ['org_id'], unique=False)
    op.create_table(
        'insurance_audit_rules',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('rule_type', sa.String(length=24), nullable=False),
        sa.Column('params', sa.JSON(), nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_insurance_audit_rules_code', 'insurance_audit_rules', ['code'], unique=True)
    op.create_table(
        'insurance_audit_flags',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('settlement_id', sa.Integer(), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('rule_code', sa.String(length=32), nullable=False),
        sa.Column('detail', sa.String(length=512), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_insurance_audit_flags_settlement_id', 'insurance_audit_flags', ['settlement_id'], unique=False)
    op.create_index('ix_insurance_audit_flags_org_id', 'insurance_audit_flags', ['org_id'], unique=False)
    op.create_table(
        'patient_allergies',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('allergen', sa.String(length=64), nullable=False),
        sa.Column('allergen_type', sa.String(length=16), nullable=False),
        sa.Column('severity', sa.String(length=8), nullable=False),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_patient_allergies_patient_id', 'patient_allergies', ['patient_id'], unique=False)
    op.create_index('ix_patient_allergies_org_id', 'patient_allergies', ['org_id'], unique=False)
    op.create_table(
        'clinical_pathways',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('code', sa.String(length=32), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('disease', sa.String(length=64), nullable=False),
        sa.Column('active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_clinical_pathways_code', 'clinical_pathways', ['code'], unique=True)
    op.create_table(
        'pathway_nodes',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('pathway_id', sa.Integer(), sa.ForeignKey('clinical_pathways.id'), nullable=False),
        sa.Column('day_no', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('order_type', sa.String(length=24), nullable=False),
        sa.Column('detail', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pathway_nodes_pathway_id', 'pathway_nodes', ['pathway_id'], unique=False)
    op.create_table(
        'patient_pathways',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('pathway_id', sa.Integer(), sa.ForeignKey('clinical_pathways.id'), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('enrolled_date', sa.String(length=10), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_patient_pathways_pathway_id', 'patient_pathways', ['pathway_id'], unique=False)
    op.create_index('ix_patient_pathways_org_id', 'patient_pathways', ['org_id'], unique=False)
    op.create_index('ix_patient_pathways_patient_id', 'patient_pathways', ['patient_id'], unique=False)
    op.create_table(
        'pathway_variances',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_pathway_id', sa.Integer(), sa.ForeignKey('patient_pathways.id'), nullable=False),
        sa.Column('node_id', sa.Integer(), nullable=False),
        sa.Column('reason', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_pathway_variances_patient_pathway_id', 'pathway_variances', ['patient_pathway_id'], unique=False)
    op.create_table(
        'home_monitor_readings',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('reading_type', sa.String(length=16), nullable=False),
        sa.Column('value1', sa.Float(), nullable=False),
        sa.Column('value2', sa.Float(), nullable=True),
        sa.Column('unit', sa.String(length=16), nullable=False),
        sa.Column('measured_at', sa.String(length=16), nullable=False),
        sa.Column('abnormal', sa.Boolean(), nullable=False),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_home_monitor_readings_reading_type', 'home_monitor_readings', ['reading_type'], unique=False)
    op.create_index('ix_home_monitor_readings_patient_id', 'home_monitor_readings', ['patient_id'], unique=False)
    op.create_table(
        'refill_requests',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('patient_id', sa.Integer(), sa.ForeignKey('patients.id'), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('prescription_id', sa.Integer(), sa.ForeignKey('prescriptions.id'), nullable=True),
        sa.Column('drug_summary', sa.String(length=256), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('reviewed_by', sa.String(length=64), nullable=False),
        sa.Column('note', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_refill_requests_status', 'refill_requests', ['status'], unique=False)
    op.create_index('ix_refill_requests_org_id', 'refill_requests', ['org_id'], unique=False)
    op.create_index('ix_refill_requests_patient_id', 'refill_requests', ['patient_id'], unique=False)
    op.create_table(
        'provincial_reports',
        sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
        sa.Column('report_type', sa.String(length=32), nullable=False),
        sa.Column('org_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False),
        sa.Column('target', sa.String(length=32), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('ack', sa.String(length=256), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_provincial_reports_report_type', 'provincial_reports', ['report_type'], unique=False)
    op.create_index('ix_provincial_reports_org_id', 'provincial_reports', ['org_id'], unique=False)
    op.create_index('ix_provincial_reports_status', 'provincial_reports', ['status'], unique=False)


def downgrade() -> None:
    op.drop_table('provincial_reports')
    op.drop_table('refill_requests')
    op.drop_table('home_monitor_readings')
    op.drop_table('pathway_variances')
    op.drop_table('patient_pathways')
    op.drop_table('pathway_nodes')
    op.drop_table('clinical_pathways')
    op.drop_table('patient_allergies')
    op.drop_table('insurance_audit_flags')
    op.drop_table('insurance_audit_rules')
    op.drop_table('payment_cases')
    op.drop_table('dip_rates')
    op.drop_table('dip_catalog')
    op.drop_table('drg_rates')
