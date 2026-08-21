"""个保法落地（工程包 E2）：知情同意采集、同意文本版本、更正/注销申请、档案注销列

为什么是这四件事一起：《个人信息保护法》的"告知-同意可举证"（consent_texts +
consent_records）、"更正权/删除权"（correction_requests）与"删除权的落地形态"
（patients.deactivated_at，注销而非物理删除——医疗记录法定保留）是同一条合规
链路，拆开上线会出现"能提申请却无处执行"的中间态。

- consent_records.guardian_* 三列：未成年人（<14 岁）登记同意的监护人要件；
  列名刻意不叫 id_card——人物身份字段只归 patients（核心数据不可变定义）。
- patients 加列已按核心表冻结流程同步更新 tests/test_schema_governance.py 的
  FROZEN_CORE_COLUMNS 快照。

Revision ID: e2f4a6c8b0d2
Revises: b7c1d2e3f4a6
"""
import sqlalchemy as sa
from alembic import op

revision = "e2f4a6c8b0d2"
down_revision = "b7c1d2e3f4a6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "consent_texts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("scene", sa.String(32), nullable=False, index=True),
        sa.Column("version", sa.String(16), nullable=False),
        sa.Column("content", sa.String(1024), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("scene", "version", name="uq_consent_text_scene_version"),
    )
    op.create_table(
        "consent_records",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("scene", sa.String(32), nullable=False, index=True),
        sa.Column("text_version", sa.String(16), nullable=False, server_default=""),
        sa.Column("method", sa.String(16), nullable=False),
        sa.Column("operator_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "resident_account_id",
            sa.Integer(),
            sa.ForeignKey("resident_accounts.id"),
            nullable=True,
        ),
        sa.Column("evidence", sa.String(256), nullable=False, server_default=""),
        sa.Column("guardian_name", sa.String(64), nullable=False, server_default=""),
        sa.Column("guardian_id_card", sa.String(18), nullable=False, server_default=""),
        sa.Column("guardian_relation", sa.String(16), nullable=False, server_default=""),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    op.create_table(
        "correction_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("patient_id", sa.Integer(), sa.ForeignKey("patients.id"), nullable=False, index=True),
        sa.Column("request_type", sa.String(16), nullable=False, server_default="correction", index=True),
        sa.Column("changes", sa.String(1024), nullable=False, server_default=""),
        sa.Column("reason", sa.String(256), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending", index=True),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column(
            "applicant_account_id",
            sa.Integer(),
            sa.ForeignKey("resident_accounts.id"),
            nullable=True,
        ),
        sa.Column("applicant_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("reviewer_user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
        sa.Column("review_comment", sa.String(256), nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )
    # 注销时间（"删除权"落地）：可空加列，存量档案全部保持在册，无需回填
    with op.batch_alter_table("patients") as batch:
        batch.add_column(sa.Column("deactivated_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("patients") as batch:
        batch.drop_column("deactivated_at")
    op.drop_table("correction_requests")
    op.drop_table("consent_records")
    op.drop_table("consent_texts")
