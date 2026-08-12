"""阶段九·五 第二批：既有模块的子功能缺口

补第七轮列出的第二批：病理标本核收与标本管理（④）、会诊费用与统计（⑤）、
老年健康统计（㉓，纯查询无表改动）、缺药登记黑名单与履约（⑮）、
课程录制与直播反馈（⑳）、项目管理（㉞）。

顺带把预约黑名单泛化成通用服务黑名单：指引第 12 与第 15 条各要一个黑名单，
再建一张几乎一样的表不如给现有的加一个业务域——第三个域迟早会来。

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""
import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---- ⑫⑮ 预约黑名单 → 通用服务黑名单 ----
    # 重建而非 rename + add_column：原表的 patient_id 上带着单列唯一约束，
    # 而新语义要求 (domain, patient_id) 才唯一——SQLite 改不了既有约束，
    # 只能新建再搬数据。
    op.create_table(
        "service_blacklists",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("domain", sa.String(16), nullable=False, server_default="appointment"),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("domain", "patient_id", name="uq_service_blacklist"),
    )
    op.create_index("ix_service_blacklists_domain", "service_blacklists", ["domain"])
    op.create_index("ix_service_blacklists_patient_id", "service_blacklists", ["patient_id"])
    op.execute(
        "INSERT INTO service_blacklists (domain, patient_id, reason, created_at) "
        "SELECT 'appointment', patient_id, reason, created_at FROM appointment_blacklist"
    )
    op.drop_table("appointment_blacklist")

    # ---- ④ 病理标本 ----
    op.create_table(
        "pathology_specimens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("request_id", sa.Integer(), sa.ForeignKey("exam_requests.id"), nullable=False),
        sa.Column("specimen_no", sa.String(32), nullable=False),
        sa.Column("site", sa.String(128), nullable=False, server_default=""),
        sa.Column("excised_at", sa.String(19), nullable=False, server_default=""),
        sa.Column("fixed_at", sa.String(19), nullable=False, server_default=""),
        sa.Column("fixative", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reject_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("received_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("block_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("slide_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("specimen_no", name="uq_pathology_specimen_no"),
    )
    op.create_index("ix_pathology_specimens_request_id", "pathology_specimens", ["request_id"])
    op.create_index("ix_pathology_specimens_specimen_no", "pathology_specimens", ["specimen_no"])
    op.create_index("ix_pathology_specimens_status", "pathology_specimens", ["status"])

    # ---- ⑤ 会诊费用 ----
    op.add_column(
        "consultations", sa.Column("fee", sa.Float(), nullable=False, server_default="0")
    )
    op.add_column(
        "consultations",
        sa.Column("fee_settled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("ix_consultations_fee_settled", "consultations", ["fee_settled"])
    op.add_column(
        "consultations", sa.Column("fee_note", sa.String(256), nullable=False, server_default="")
    )

    # ---- ⑮ 缺药登记：患者与履约 ----
    op.add_column("drug_shortages", sa.Column("patient_id", sa.Integer(), nullable=True))
    op.create_index("ix_drug_shortages_patient_id", "drug_shortages", ["patient_id"])
    op.add_column(
        "drug_shortages",
        sa.Column("close_reason", sa.String(256), nullable=False, server_default=""),
    )
    op.add_column("drug_shortages", sa.Column("closed_at", sa.DateTime(), nullable=True))

    # ---- ⑳ 课程录制与直播反馈 ----
    op.add_column(
        "live_sessions",
        sa.Column("recording_url", sa.String(512), nullable=False, server_default=""),
    )
    op.add_column("live_sessions", sa.Column("recorded_at", sa.DateTime(), nullable=True))
    op.create_table(
        "live_feedbacks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("session_id", sa.Integer(), sa.ForeignKey("live_sessions.id"), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("rating", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("comment", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint("session_id", "user_id", name="uq_live_feedback"),
    )
    op.create_index("ix_live_feedbacks_session_id", "live_feedbacks", ["session_id"])
    op.create_index("ix_live_feedbacks_user_id", "live_feedbacks", ["user_id"])

    # ---- ㉞ 项目管理 ----
    op.create_table(
        "admin_projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("category", sa.String(32), nullable=False, server_default="general"),
        sa.Column("owner_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("start_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("due_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="planning"),
        sa.Column("progress_pct", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("budget_amount", sa.Float(), nullable=False, server_default="0"),
        sa.Column("description", sa.String(1024), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_admin_projects_org_id", "admin_projects", ["org_id"])
    op.create_index("ix_admin_projects_category", "admin_projects", ["category"])
    op.create_index("ix_admin_projects_status", "admin_projects", ["status"])
    op.create_index("ix_admin_projects_due_date", "admin_projects", ["due_date"])

    op.create_table(
        "project_milestones",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), sa.ForeignKey("admin_projects.id"), nullable=False),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("due_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("done", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("done_date", sa.String(10), nullable=False, server_default=""),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_project_milestones_project_id", "project_milestones", ["project_id"])
    op.create_index("ix_project_milestones_due_date", "project_milestones", ["due_date"])
    op.create_index("ix_project_milestones_done", "project_milestones", ["done"])


def downgrade() -> None:
    op.drop_table("project_milestones")
    op.drop_table("admin_projects")

    op.drop_table("live_feedbacks")
    op.drop_column("live_sessions", "recorded_at")
    op.drop_column("live_sessions", "recording_url")

    op.drop_column("drug_shortages", "closed_at")
    op.drop_column("drug_shortages", "close_reason")
    op.drop_index("ix_drug_shortages_patient_id", table_name="drug_shortages")
    op.drop_column("drug_shortages", "patient_id")

    op.drop_column("consultations", "fee_note")
    op.drop_index("ix_consultations_fee_settled", table_name="consultations")
    op.drop_column("consultations", "fee_settled")
    op.drop_column("consultations", "fee")

    op.drop_table("pathology_specimens")

    op.create_table(
        "appointment_blacklist",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, unique=True
        ),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.execute(
        "INSERT INTO appointment_blacklist (patient_id, reason, created_at) "
        "SELECT patient_id, reason, created_at FROM service_blacklists "
        "WHERE domain = 'appointment'"
    )
    op.drop_table("service_blacklists")
