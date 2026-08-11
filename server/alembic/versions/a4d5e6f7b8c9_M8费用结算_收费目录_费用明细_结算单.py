"""M8费用结算：收费项目目录、费用明细、结算单

Revision ID: a4d5e6f7b8c9
Revises: f2c3d4e5a6b7
Create Date: 2026-08-11 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a4d5e6f7b8c9'
down_revision: Union[str, Sequence[str], None] = 'f2c3d4e5a6b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "charge_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=16), nullable=False),
        sa.Column("price", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_charge_items_code"), "charge_items", ["code"], unique=True)

    op.create_table(
        "settlements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("bill_type", sa.String(length=16), nullable=False),
        sa.Column("admission_id", sa.Integer(), nullable=True),
        sa.Column("encounter_id", sa.Integer(), nullable=True),
        sa.Column("total_amount", sa.Float(), nullable=False),
        sa.Column("insurance_pay", sa.Float(), nullable=False),
        sa.Column("self_pay", sa.Float(), nullable=False),
        sa.Column("insurance_settlement_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.ForeignKeyConstraint(["admission_id"], ["admissions.id"]),
        sa.ForeignKeyConstraint(["encounter_id"], ["encounters.id"]),
        sa.ForeignKeyConstraint(["insurance_settlement_id"], ["insurance_settlements.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_settlements_patient_id"), "settlements", ["patient_id"], unique=False)
    op.create_index(op.f("ix_settlements_org_id"), "settlements", ["org_id"], unique=False)
    op.create_index(
        op.f("ix_settlements_admission_id"), "settlements", ["admission_id"], unique=False
    )

    op.create_table(
        "bill_details",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("patient_id", sa.Integer(), nullable=False),
        sa.Column("admission_id", sa.Integer(), nullable=True),
        sa.Column("encounter_id", sa.Integer(), nullable=True),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("unit_price", sa.Float(), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("amount", sa.Float(), nullable=False),
        sa.Column("settlement_id", sa.Integer(), nullable=True),
        sa.Column("created_by", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"]),
        sa.ForeignKeyConstraint(["admission_id"], ["admissions.id"]),
        sa.ForeignKeyConstraint(["encounter_id"], ["encounters.id"]),
        sa.ForeignKeyConstraint(["settlement_id"], ["settlements.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_bill_details_patient_id"), "bill_details", ["patient_id"], unique=False)
    op.create_index(
        op.f("ix_bill_details_admission_id"), "bill_details", ["admission_id"], unique=False
    )
    op.create_index(
        op.f("ix_bill_details_encounter_id"), "bill_details", ["encounter_id"], unique=False
    )
    op.create_index(op.f("ix_bill_details_item_code"), "bill_details", ["item_code"], unique=False)
    op.create_index(
        op.f("ix_bill_details_settlement_id"), "bill_details", ["settlement_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_bill_details_settlement_id"), table_name="bill_details")
    op.drop_index(op.f("ix_bill_details_item_code"), table_name="bill_details")
    op.drop_index(op.f("ix_bill_details_encounter_id"), table_name="bill_details")
    op.drop_index(op.f("ix_bill_details_admission_id"), table_name="bill_details")
    op.drop_index(op.f("ix_bill_details_patient_id"), table_name="bill_details")
    op.drop_table("bill_details")
    op.drop_index(op.f("ix_settlements_admission_id"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_org_id"), table_name="settlements")
    op.drop_index(op.f("ix_settlements_patient_id"), table_name="settlements")
    op.drop_table("settlements")
    op.drop_index(op.f("ix_charge_items_code"), table_name="charge_items")
    op.drop_table("charge_items")
