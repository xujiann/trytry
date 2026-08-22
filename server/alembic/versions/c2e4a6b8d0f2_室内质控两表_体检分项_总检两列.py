"""工程包 B2：检验室内质控（IQC）两表、体检分项结果、体检总检两列

为什么另立 qc_lots/qc_measurements 而不是复用 qc_records：qc_records 是
①-④共享中心的运行质量台账（人工登记合格/不合格），法域不同；室内质控是
"靶值/SD 定基线 → 测定值录入即按 Westgard 规则数值判定"的闭环，两者的
数据形状与判定方式都不一样，硬塞进一张表只会让两边都难用。

体检分项单独成表（checkup_items）：physical_exams 的 summary/abnormal_items
是汇总字符串，逐项测值塞长串等于放弃"按项目查历史"的能力；分项落行后
总检（physical_exams 新增 final_conclusion/final_doctor 两列）才有依据。

Revision ID: c2e4a6b8d0f2
Revises: b1f2a3c4d5e6
Create Date: 2026-08-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f2"
down_revision: Union[str, Sequence[str], None] = "b1f2a3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "qc_lots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Integer(), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("lot_no", sa.String(length=64), nullable=False),
        sa.Column("target_value", sa.Float(), nullable=False),
        sa.Column("sd", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["org_id"], ["organizations.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "item_code", "lot_no", name="uq_qc_lot_org_item_lot"),
    )
    op.create_index(op.f("ix_qc_lots_org_id"), "qc_lots", ["org_id"], unique=False)
    op.create_index(op.f("ix_qc_lots_item_code"), "qc_lots", ["item_code"], unique=False)
    op.create_index(op.f("ix_qc_lots_lot_no"), "qc_lots", ["lot_no"], unique=False)
    op.create_index(op.f("ix_qc_lots_active"), "qc_lots", ["active"], unique=False)
    op.create_index(op.f("ix_qc_lots_created_at"), "qc_lots", ["created_at"], unique=False)

    op.create_table(
        "qc_measurements",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("lot_id", sa.Integer(), nullable=False),
        sa.Column("value", sa.Float(), nullable=False),
        sa.Column("measured_at", sa.String(length=16), nullable=False),
        sa.Column("operator", sa.String(length=64), nullable=False),
        sa.Column("warning", sa.Boolean(), nullable=False),
        sa.Column("out_of_control", sa.Boolean(), nullable=False),
        sa.Column("violated_rules", sa.String(length=64), nullable=False),
        sa.Column("handled", sa.Boolean(), nullable=False),
        sa.Column("handle_reason", sa.String(length=512), nullable=False),
        sa.Column("corrective_action", sa.String(length=512), nullable=False),
        sa.Column("handled_by", sa.String(length=64), nullable=False),
        sa.Column("handled_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["lot_id"], ["qc_lots.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_qc_measurements_lot_id"), "qc_measurements", ["lot_id"], unique=False)
    op.create_index(
        op.f("ix_qc_measurements_out_of_control"), "qc_measurements", ["out_of_control"], unique=False
    )
    op.create_index(op.f("ix_qc_measurements_handled"), "qc_measurements", ["handled"], unique=False)
    op.create_index(
        op.f("ix_qc_measurements_created_at"), "qc_measurements", ["created_at"], unique=False
    )

    op.create_table(
        "checkup_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("checkup_id", sa.Integer(), nullable=False),
        sa.Column("item_code", sa.String(length=64), nullable=False),
        sa.Column("item_name", sa.String(length=128), nullable=False),
        sa.Column("result_value", sa.String(length=64), nullable=False),
        sa.Column("unit", sa.String(length=16), nullable=False),
        sa.Column("ref_range", sa.String(length=64), nullable=False),
        sa.Column("abnormal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["checkup_id"], ["physical_exams.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_checkup_items_checkup_id"), "checkup_items", ["checkup_id"], unique=False)
    op.create_index(op.f("ix_checkup_items_item_code"), "checkup_items", ["item_code"], unique=False)
    op.create_index(op.f("ix_checkup_items_abnormal"), "checkup_items", ["abnormal"], unique=False)

    # 体检总检两列：存量记录未总检，回填空串（与模型 default 一致）
    op.add_column(
        "physical_exams",
        sa.Column("final_conclusion", sa.String(length=1024), nullable=False, server_default=""),
    )
    op.add_column(
        "physical_exams",
        sa.Column("final_doctor", sa.String(length=64), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("physical_exams", "final_doctor")
    op.drop_column("physical_exams", "final_conclusion")
    op.drop_index(op.f("ix_checkup_items_abnormal"), table_name="checkup_items")
    op.drop_index(op.f("ix_checkup_items_item_code"), table_name="checkup_items")
    op.drop_index(op.f("ix_checkup_items_checkup_id"), table_name="checkup_items")
    op.drop_table("checkup_items")
    op.drop_index(op.f("ix_qc_measurements_created_at"), table_name="qc_measurements")
    op.drop_index(op.f("ix_qc_measurements_handled"), table_name="qc_measurements")
    op.drop_index(op.f("ix_qc_measurements_out_of_control"), table_name="qc_measurements")
    op.drop_index(op.f("ix_qc_measurements_lot_id"), table_name="qc_measurements")
    op.drop_table("qc_measurements")
    op.drop_index(op.f("ix_qc_lots_created_at"), table_name="qc_lots")
    op.drop_index(op.f("ix_qc_lots_active"), table_name="qc_lots")
    op.drop_index(op.f("ix_qc_lots_lot_no"), table_name="qc_lots")
    op.drop_index(op.f("ix_qc_lots_item_code"), table_name="qc_lots")
    op.drop_index(op.f("ix_qc_lots_org_id"), table_name="qc_lots")
    op.drop_table("qc_lots")
