"""工程包 B1：药品批号效期台账、西药发药（含明细）、医嘱执行记录、住院押金流水

为什么是五张表而不是四张：发药明细单独成表（dispense_items）——召回按批号
反查"这批发给了谁"只有按批次落行才答得出来，塞进发药记录的长字符串等于放弃追溯。

Revision ID: b1f2a3c4d5e6
Revises: b7c1d2e3f4a6
Create Date: 2026-08-21
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1f2a3c4d5e6"
down_revision: Union[str, Sequence[str], None] = "b7c1d2e3f4a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "drug_batches",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("drug_code", sa.String(length=64), nullable=False),
        sa.Column("batch_no", sa.String(length=64), nullable=False),
        sa.Column("expire_date", sa.String(length=10), nullable=False),
        sa.Column("supplier", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("used_quantity", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("recall_reason", sa.String(length=256), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "drug_code", "batch_no", name="uq_drug_batch"),
    )
    op.create_index(op.f("ix_drug_batches_org_id"), "drug_batches", ["org_id"], unique=False)
    op.create_index(op.f("ix_drug_batches_drug_code"), "drug_batches", ["drug_code"], unique=False)
    op.create_index(op.f("ix_drug_batches_batch_no"), "drug_batches", ["batch_no"], unique=False)
    op.create_index(op.f("ix_drug_batches_expire_date"), "drug_batches", ["expire_date"], unique=False)
    op.create_index(op.f("ix_drug_batches_status"), "drug_batches", ["status"], unique=False)
    op.create_index(op.f("ix_drug_batches_created_at"), "drug_batches", ["created_at"], unique=False)

    op.create_table(
        "dispense_records",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("prescription_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("dispensed_by", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reverse_reason", sa.String(length=256), nullable=False),
        sa.Column("reversed_by", sa.Integer(), nullable=True),
        sa.Column("reversed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["prescription_id"], ["prescriptions.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["dispensed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["reversed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prescription_id", name="uq_dispense_prescription"),
    )
    op.create_index(
        op.f("ix_dispense_records_prescription_id"), "dispense_records", ["prescription_id"], unique=False
    )
    op.create_index(op.f("ix_dispense_records_org_id"), "dispense_records", ["org_id"], unique=False)
    op.create_index(op.f("ix_dispense_records_status"), "dispense_records", ["status"], unique=False)
    op.create_index(op.f("ix_dispense_records_created_at"), "dispense_records", ["created_at"], unique=False)

    op.create_table(
        "dispense_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("dispense_id", sa.Integer(), nullable=False),
        sa.Column("batch_id", sa.Integer(), nullable=False),
        sa.Column("drug_code", sa.String(length=64), nullable=False),
        sa.Column("drug_name", sa.String(length=128), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["dispense_id"], ["dispense_records.id"]),
        sa.ForeignKeyConstraint(["batch_id"], ["drug_batches.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_dispense_items_dispense_id"), "dispense_items", ["dispense_id"], unique=False)
    op.create_index(op.f("ix_dispense_items_batch_id"), "dispense_items", ["batch_id"], unique=False)
    op.create_index(op.f("ix_dispense_items_drug_code"), "dispense_items", ["drug_code"], unique=False)

    op.create_table(
        "order_executions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("inpatient_order_id", sa.Integer(), nullable=False),
        sa.Column("executed_by", sa.Integer(), nullable=False),
        sa.Column("executed_at", sa.DateTime(), nullable=False),
        sa.Column("note", sa.String(length=512), nullable=False),
        sa.Column("skin_test_result", sa.String(length=16), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["inpatient_order_id"], ["inpatient_orders.id"]),
        sa.ForeignKeyConstraint(["executed_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_order_executions_inpatient_order_id"),
        "order_executions",
        ["inpatient_order_id"],
        unique=False,
    )
    op.create_index(op.f("ix_order_executions_executed_at"), "order_executions", ["executed_at"], unique=False)

    op.create_table(
        "deposits",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("admission_id", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("deposit_type", sa.String(length=16), nullable=False),
        sa.Column("method", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["admission_id"], ["admissions.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_deposits_admission_id"), "deposits", ["admission_id"], unique=False)
    op.create_index(op.f("ix_deposits_deposit_type"), "deposits", ["deposit_type"], unique=False)
    op.create_index(op.f("ix_deposits_created_at"), "deposits", ["created_at"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_deposits_created_at"), table_name="deposits")
    op.drop_index(op.f("ix_deposits_deposit_type"), table_name="deposits")
    op.drop_index(op.f("ix_deposits_admission_id"), table_name="deposits")
    op.drop_table("deposits")
    op.drop_index(op.f("ix_order_executions_executed_at"), table_name="order_executions")
    op.drop_index(op.f("ix_order_executions_inpatient_order_id"), table_name="order_executions")
    op.drop_table("order_executions")
    op.drop_index(op.f("ix_dispense_items_drug_code"), table_name="dispense_items")
    op.drop_index(op.f("ix_dispense_items_batch_id"), table_name="dispense_items")
    op.drop_index(op.f("ix_dispense_items_dispense_id"), table_name="dispense_items")
    op.drop_table("dispense_items")
    op.drop_index(op.f("ix_dispense_records_created_at"), table_name="dispense_records")
    op.drop_index(op.f("ix_dispense_records_status"), table_name="dispense_records")
    op.drop_index(op.f("ix_dispense_records_org_id"), table_name="dispense_records")
    op.drop_index(op.f("ix_dispense_records_prescription_id"), table_name="dispense_records")
    op.drop_table("dispense_records")
    op.drop_index(op.f("ix_drug_batches_created_at"), table_name="drug_batches")
    op.drop_index(op.f("ix_drug_batches_status"), table_name="drug_batches")
    op.drop_index(op.f("ix_drug_batches_expire_date"), table_name="drug_batches")
    op.drop_index(op.f("ix_drug_batches_batch_no"), table_name="drug_batches")
    op.drop_index(op.f("ix_drug_batches_drug_code"), table_name="drug_batches")
    op.drop_index(op.f("ix_drug_batches_org_id"), table_name="drug_batches")
    op.drop_table("drug_batches")
