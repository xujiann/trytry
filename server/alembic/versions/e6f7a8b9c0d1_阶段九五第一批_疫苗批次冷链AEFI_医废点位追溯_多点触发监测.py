"""阶段九·五 第一批：把降到 ⚠️ 的三条主项补回来

第七轮子功能级重审把 ㉕疫苗接种、㉖应急指挥、㊱医疗废弃物 三条从 ✅ 降档，
这一批补的就是它们缺的子功能：

- ㉕ 疫苗批次（批号/厂家/效期）、冷链监测、AEFI 报告；接种记录挂批次
- ㊱ 医废点位（产生点/暂存间）、追溯码、转运人员挂员工档案
- ㉖ 症候群监测、病原监测、应急资源保障台账

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""
import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------- ㉕ 疫苗批次 / 冷链 / AEFI ----------------
    op.create_table(
        "vaccine_batches",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("vaccine_code", sa.String(64), nullable=False),
        sa.Column("vaccine_name", sa.String(128), nullable=False),
        sa.Column("batch_no", sa.String(64), nullable=False),
        sa.Column("manufacturer", sa.String(128), nullable=False, server_default=""),
        sa.Column("expire_date", sa.String(10), nullable=False),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("used_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("frozen_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("vaccine_code", "batch_no", name="uq_vaccine_batch"),
    )
    op.create_index("ix_vaccine_batches_vaccine_code", "vaccine_batches", ["vaccine_code"])
    op.create_index("ix_vaccine_batches_batch_no", "vaccine_batches", ["batch_no"])
    op.create_index("ix_vaccine_batches_expire_date", "vaccine_batches", ["expire_date"])
    op.create_index("ix_vaccine_batches_org_id", "vaccine_batches", ["org_id"])
    op.create_index("ix_vaccine_batches_status", "vaccine_batches", ["status"])

    op.create_table(
        "cold_chain_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("device_name", sa.String(128), nullable=False),
        sa.Column("temperature", sa.Float(), nullable=False),
        sa.Column("min_allowed", sa.Float(), nullable=False, server_default="2.0"),
        sa.Column("max_allowed", sa.Float(), nullable=False, server_default="8.0"),
        sa.Column("exceeded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("recorded_at", sa.String(19), nullable=False),
        sa.Column("handled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("handle_note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_cold_chain_records_org_id", "cold_chain_records", ["org_id"])
    op.create_index("ix_cold_chain_records_exceeded", "cold_chain_records", ["exceeded"])
    op.create_index("ix_cold_chain_records_recorded_at", "cold_chain_records", ["recorded_at"])

    op.create_table(
        "aefi_reports",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column(
            "record_id", sa.Integer(), sa.ForeignKey("vaccination_records.id"), nullable=True
        ),
        sa.Column("vaccine_code", sa.String(64), nullable=False),
        sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
        sa.Column("reaction_type", sa.String(16), nullable=False, server_default="general"),
        sa.Column("symptom", sa.String(512), nullable=False),
        sa.Column("onset_date", sa.String(10), nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("reported_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    for col in ("patient_id", "record_id", "vaccine_code", "batch_no", "reaction_type",
                "onset_date", "outcome", "org_id"):
        op.create_index(f"ix_aefi_reports_{col}", "aefi_reports", [col])

    # 接种记录挂批次。批号冗余存一份：接种史要长期保存，
    # 不该因批次表的任何变动而查不出当年打的是哪一批。
    op.add_column("vaccination_records", sa.Column("batch_id", sa.Integer(), nullable=True))
    op.create_index("ix_vaccination_records_batch_id", "vaccination_records", ["batch_id"])
    op.add_column(
        "vaccination_records",
        sa.Column("batch_no", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_vaccination_records_batch_no", "vaccination_records", ["batch_no"])
    op.add_column(
        "vaccination_records", sa.Column("site", sa.String(32), nullable=False, server_default="")
    )
    op.add_column(
        "vaccination_records",
        sa.Column("vaccinator", sa.String(64), nullable=False, server_default=""),
    )

    # ---------------- ㊱ 医废点位与追溯 ----------------
    op.create_table(
        "waste_locations",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("location_type", sa.String(16), nullable=False, server_default="source"),
        sa.Column("manager_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_waste_locations_org_id", "waste_locations", ["org_id"])
    op.create_index("ix_waste_locations_location_type", "waste_locations", ["location_type"])
    op.create_index("ix_waste_locations_active", "waste_locations", ["active"])

    # 追溯码对存量记录回填：不回填的话唯一索引会被多个空串挤爆，
    # 而且"老数据没有码"会让追溯功能看起来时灵时不灵。
    op.add_column("medical_wastes", sa.Column("trace_code", sa.String(32), nullable=True))
    conn = op.get_bind()
    rows = conn.execute(
        sa.text("SELECT id, collected_date FROM medical_wastes ORDER BY id")
    ).fetchall()
    seq: dict[str, int] = {}
    for row in rows:
        day = (row.collected_date or "").replace("-", "") or "00000000"
        seq[day] = seq.get(day, 0) + 1
        conn.execute(
            sa.text("UPDATE medical_wastes SET trace_code = :code WHERE id = :id"),
            {"code": f"MW-{day}-{seq[day]:04d}", "id": row.id},
        )
    op.create_index("ix_medical_wastes_trace_code", "medical_wastes", ["trace_code"], unique=True)

    op.add_column("medical_wastes", sa.Column("source_location_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_medical_wastes_source_location_id", "medical_wastes", ["source_location_id"]
    )
    op.add_column("medical_wastes", sa.Column("storage_location_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_medical_wastes_storage_location_id", "medical_wastes", ["storage_location_id"]
    )
    op.add_column("medical_wastes", sa.Column("handler_employee_id", sa.Integer(), nullable=True))
    op.create_index(
        "ix_medical_wastes_handler_employee_id", "medical_wastes", ["handler_employee_id"]
    )
    op.add_column("medical_wastes", sa.Column("stored_at", sa.DateTime(), nullable=True))

    # ---------------- ㉖ 多点触发监测与应急资源 ----------------
    op.create_table(
        "syndrome_monitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("syndrome", sa.String(16), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("threshold", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_date", sa.String(10), nullable=False),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("org_id", "syndrome", "record_date", name="uq_syndrome_daily"),
    )
    op.create_index("ix_syndrome_monitors_org_id", "syndrome_monitors", ["org_id"])
    op.create_index("ix_syndrome_monitors_syndrome", "syndrome_monitors", ["syndrome"])
    op.create_index("ix_syndrome_monitors_record_date", "syndrome_monitors", ["record_date"])

    op.create_table(
        "pathogen_monitors",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("pathogen_name", sa.String(128), nullable=False),
        sa.Column("specimen_type", sa.String(64), nullable=False, server_default=""),
        sa.Column("tested_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positive_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("record_date", sa.String(10), nullable=False),
        sa.Column("note", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_pathogen_monitors_org_id", "pathogen_monitors", ["org_id"])
    op.create_index("ix_pathogen_monitors_pathogen_name", "pathogen_monitors", ["pathogen_name"])
    op.create_index("ix_pathogen_monitors_record_date", "pathogen_monitors", ["record_date"])

    op.create_table(
        "emergency_resources",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("resource_type", sa.String(16), nullable=False, server_default="material"),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(16), nullable=False, server_default=""),
        sa.Column("min_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expire_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("contact", sa.String(64), nullable=False, server_default=""),
        sa.Column("location", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_emergency_resources_org_id", "emergency_resources", ["org_id"])
    op.create_index(
        "ix_emergency_resources_resource_type", "emergency_resources", ["resource_type"]
    )
    op.create_index("ix_emergency_resources_expire_date", "emergency_resources", ["expire_date"])


def downgrade() -> None:
    op.drop_table("emergency_resources")
    op.drop_table("pathogen_monitors")
    op.drop_table("syndrome_monitors")

    op.drop_column("medical_wastes", "stored_at")
    op.drop_index("ix_medical_wastes_handler_employee_id", table_name="medical_wastes")
    op.drop_column("medical_wastes", "handler_employee_id")
    op.drop_index("ix_medical_wastes_storage_location_id", table_name="medical_wastes")
    op.drop_column("medical_wastes", "storage_location_id")
    op.drop_index("ix_medical_wastes_source_location_id", table_name="medical_wastes")
    op.drop_column("medical_wastes", "source_location_id")
    op.drop_index("ix_medical_wastes_trace_code", table_name="medical_wastes")
    op.drop_column("medical_wastes", "trace_code")
    op.drop_table("waste_locations")

    op.drop_column("vaccination_records", "vaccinator")
    op.drop_column("vaccination_records", "site")
    op.drop_index("ix_vaccination_records_batch_no", table_name="vaccination_records")
    op.drop_column("vaccination_records", "batch_no")
    op.drop_index("ix_vaccination_records_batch_id", table_name="vaccination_records")
    op.drop_column("vaccination_records", "batch_id")

    op.drop_table("aefi_reports")
    op.drop_table("cold_chain_records")
    op.drop_table("vaccine_batches")
