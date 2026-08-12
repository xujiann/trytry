"""门急诊文书补全与手术质量标记（浙江省指南 #3 / #14）

Revision ID: f8a2b3c4d5e6
Revises: e7f8a2b3c4d5
"""
import sqlalchemy as sa
from alembic import op

revision = "f8a2b3c4d5e6"
down_revision = "e7f8a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consent_templates",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("consent_type", sa.String(16), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("body", sa.String(8192), nullable=False),
        sa.Column("version", sa.String(16), nullable=False, server_default="v1"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    # 正文在签署时整段拷贝进来，不引模板外键：患者签的是当时那份措辞，
    # 模板后续修订不得回溯改变已签文书。
    op.create_table(
        "informed_consents",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False,
                  index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False,
                  index=True),
        sa.Column("consent_type", sa.String(16), nullable=False, index=True),
        sa.Column("title", sa.String(128), nullable=False),
        sa.Column("content", sa.String(8192), nullable=False, server_default=""),
        sa.Column("template_version", sa.String(16), nullable=False, server_default=""),
        sa.Column("related_type", sa.String(32), nullable=False, server_default="", index=True),
        sa.Column("related_id", sa.Integer(), nullable=False, server_default="0", index=True),
        sa.Column("doctor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("signer_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("signer_relation", sa.String(16), nullable=False, server_default=""),
        sa.Column("signed_at", sa.DateTime(), nullable=True),
        sa.Column("refuse_reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "treatment_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("encounter_id", sa.Integer(), sa.ForeignKey("encounters.id"), nullable=False,
                  index=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False,
                  index=True),
        sa.Column("org_id", sa.Integer(), sa.ForeignKey("organizations.id"), nullable=False,
                  index=True),
        sa.Column("treatment_name", sa.String(128), nullable=False),
        sa.Column("treatment_code", sa.String(64), nullable=False, server_default=""),
        sa.Column("site", sa.String(64), nullable=False, server_default=""),
        sa.Column("dose", sa.String(64), nullable=False, server_default=""),
        sa.Column("executor_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("performed_at", sa.String(16), nullable=False, server_default=""),
        sa.Column("reaction", sa.String(256), nullable=False, server_default=""),
        sa.Column("note", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )

    # 护理记录放宽到门急诊：admission_id 转可空，新增 encounter_id。
    # SQLite 不支持直接 ALTER COLUMN，用 batch 模式重建表。
    with op.batch_alter_table("nursing_records") as batch:
        batch.alter_column("admission_id", existing_type=sa.Integer(), nullable=True)
        batch.add_column(sa.Column("encounter_id", sa.Integer(), nullable=True))
    op.create_index("ix_nursing_records_encounter_id", "nursing_records", ["encounter_id"])

    # 非计划重返手术室：医师显式标记，不做推断（分期手术、计划内二次探查
    # 都会被推断规则误伤）。
    op.add_column(
        "surgery_requests",
        sa.Column("unplanned_return", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index(
        "ix_surgery_requests_unplanned_return", "surgery_requests", ["unplanned_return"]
    )


def downgrade() -> None:
    op.drop_index("ix_surgery_requests_unplanned_return", table_name="surgery_requests")
    op.drop_column("surgery_requests", "unplanned_return")
    op.drop_index("ix_nursing_records_encounter_id", table_name="nursing_records")
    with op.batch_alter_table("nursing_records") as batch:
        batch.drop_column("encounter_id")
        batch.alter_column("admission_id", existing_type=sa.Integer(), nullable=False)
    op.drop_table("treatment_records")
    op.drop_table("informed_consents")
    op.drop_table("consent_templates")
