"""慢专病 P1：spd_enrollments.migrated_from_id + spd_measurements.source_ref

Revision ID: d2b3c4d5e6f7
Revises: d1a2b3c4e5f6

- migrated_from_id：跨机构迁入的新档案指向原档案。迁移只建关系不搬历史，
  原机构的工作留痕与考核数字保持原样。
- source_ref：采集器同步的溯源键（如 chronic_fu:123），兼作幂等判重。
- 唯一约束换形：uq_spd_enroll_patient_program（全量）→ 部分唯一索引（仅 active）。
  全量唯一让迁出/排除后的患者永远无法重新纳管；正确语义是
  "同病种同一时刻只有一条在管档案"。
"""
import sqlalchemy as sa
from alembic import op

revision = "d2b3c4d5e6f7"
down_revision = "d1a2b3c4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # SQLite 删表级唯一约束要走 batch（重建表）；PG 上等价于 DROP CONSTRAINT
    with op.batch_alter_table("spd_enrollments") as batch:
        batch.drop_constraint("uq_spd_enroll_patient_program", type_="unique")
        batch.add_column(sa.Column("migrated_from_id", sa.Integer(), nullable=True))
    op.create_index(
        "uq_spd_enroll_active_patient_program",
        "spd_enrollments",
        ["patient_id", "program_code"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )
    op.add_column(
        "spd_measurements",
        sa.Column("source_ref", sa.String(64), nullable=False, server_default=""),
    )
    op.create_index("ix_spd_measurements_source_ref", "spd_measurements", ["source_ref"])


def downgrade() -> None:
    op.drop_index("ix_spd_measurements_source_ref", table_name="spd_measurements")
    op.drop_column("spd_measurements", "source_ref")
    op.drop_index("uq_spd_enroll_active_patient_program", table_name="spd_enrollments")
    with op.batch_alter_table("spd_enrollments") as batch:
        batch.drop_column("migrated_from_id")
        batch.create_unique_constraint(
            "uq_spd_enroll_patient_program", ["patient_id", "program_code"]
        )
