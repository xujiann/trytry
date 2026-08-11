"""住院临床文书、手术麻醉、统一随访中心（阶段二 T2.1–T2.4）

Revision ID: e1f2a3b4c5d7
Revises: d0e1f2a3b4c6
"""
import sqlalchemy as sa
from alembic import op

revision = "e1f2a3b4c5d7"
down_revision = "d0e1f2a3b4c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------- T2.1/T2.2 住院临床文书 ----------------
    op.create_table(
        "progress_notes",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("admissions.id"), nullable=False, index=True),
        sa.Column("note_type", sa.String(16), nullable=False, index=True),
        sa.Column("content", sa.String(4096), nullable=False),
        sa.Column("doctor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("recorded_at", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "nursing_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("admissions.id"), nullable=False, index=True),
        sa.Column("nursing_level", sa.String(16), nullable=False, server_default="level2", index=True),
        sa.Column("content", sa.String(2048), nullable=False, server_default=""),
        sa.Column("nurse_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("recorded_at", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "vital_sign_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("admissions.id"), nullable=False, index=True),
        sa.Column("measured_at", sa.String(16), nullable=False, index=True),
        # 各项可空：一次测量未必测全，用 0 冒充"未测"会污染趋势曲线
        sa.Column("temperature", sa.Float(), nullable=True),
        sa.Column("pulse", sa.Integer(), nullable=True),
        sa.Column("respiration", sa.Integer(), nullable=True),
        sa.Column("sbp", sa.Integer(), nullable=True),
        sa.Column("dbp", sa.Integer(), nullable=True),
        sa.Column("intake_ml", sa.Integer(), nullable=True),
        sa.Column("output_ml", sa.Integer(), nullable=True),
        sa.Column("weight_kg", sa.Float(), nullable=True),
        sa.Column("recorder", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "shift_handovers",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("ward_id", sa.Integer(), sa.ForeignKey("wards.id"), nullable=False, index=True),
        sa.Column("shift", sa.String(16), nullable=False, index=True),
        sa.Column("handover_date", sa.String(10), nullable=False, index=True),
        sa.Column("from_staff", sa.String(64), nullable=False, server_default=""),
        sa.Column("to_staff", sa.String(64), nullable=False, server_default=""),
        sa.Column("patient_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("critical_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content", sa.String(2048), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )

    # ---------------- T2.3 手术麻醉 ----------------
    op.create_table(
        "operating_rooms",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("org_id", "name", name="uq_or_org_name"),
    )
    op.create_table(
        "surgery_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("admission_id", sa.Integer(), sa.ForeignKey("admissions.id"), nullable=False, index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("surgery_name", sa.String(256), nullable=False),
        sa.Column("surgery_code", sa.String(32), nullable=False, server_default=""),
        sa.Column("incision_level", sa.String(4), nullable=False, server_default="II"),
        sa.Column("anesthesia_type", sa.String(16), nullable=False, server_default="general"),
        sa.Column("surgeon_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("urgency", sa.String(16), nullable=False, server_default="elective", index=True),
        sa.Column("planned_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="requested", index=True),
        sa.Column("approved_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "surgery_schedules",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("surgery_requests.id"), nullable=False, unique=True, index=True),
        sa.Column("room_id", sa.Integer(), sa.ForeignKey("operating_rooms.id"), nullable=False, index=True),
        sa.Column("scheduled_date", sa.String(10), nullable=False, index=True),
        sa.Column("start_time", sa.String(5), nullable=False),
        sa.Column("end_time", sa.String(5), nullable=False),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        # 起点相同的重排由此挡住；真正的区间重叠在应用层判定
        sa.UniqueConstraint("room_id", "scheduled_date", "start_time", name="uq_schedule_room_slot"),
    )
    op.create_table(
        "surgery_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("surgery_requests.id"), nullable=False, unique=True, index=True),
        sa.Column("actual_surgery_name", sa.String(256), nullable=False),
        sa.Column("surgeon_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("assistants", sa.String(256), nullable=False, server_default=""),
        sa.Column("anesthetist_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("anesthesia_type", sa.String(16), nullable=False, server_default="general"),
        sa.Column("incision_level", sa.String(4), nullable=False, server_default="II"),
        sa.Column("start_at", sa.String(16), nullable=False, server_default=""),
        sa.Column("end_at", sa.String(16), nullable=False, server_default=""),
        sa.Column("blood_loss_ml", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("findings", sa.String(2048), nullable=False, server_default=""),
        sa.Column("procedure", sa.String(4096), nullable=False, server_default=""),
        sa.Column("complications", sa.String(1024), nullable=False, server_default=""),
        sa.Column("outcome", sa.String(16), nullable=False, server_default="好转"),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )

    # ---------------- T2.4 统一随访中心 ----------------
    op.create_table(
        "followup_tasks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False, index=True),
        sa.Column("category", sa.String(16), nullable=False, index=True),
        # 来源单据 id，不做外键：四类来源表不同，用外键得开四个可空列反而更难查
        sa.Column("source_id", sa.Integer(), nullable=False, server_default="0", index=True),
        sa.Column("title", sa.String(128), nullable=False, server_default=""),
        sa.Column("due_date", sa.String(10), nullable=False, index=True),
        sa.Column("assigned_to", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("result", sa.String(1024), nullable=False, server_default=""),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    for table in (
        "followup_tasks",
        "surgery_records",
        "surgery_schedules",
        "surgery_requests",
        "operating_rooms",
        "shift_handovers",
        "vital_sign_records",
        "nursing_records",
        "progress_notes",
    ):
        op.drop_table(table)
