"""M1中危修复：存量危急值回填、改密令牌基线、报告修订留痕、诊断领取人

Revision ID: e8b1c2d3f4a5
Revises: d7f8a9b0c1e2
Create Date: 2026-08-11 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e8b1c2d3f4a5'
down_revision: Union[str, Sequence[str], None] = 'd7f8a9b0c1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # M-1：存量危急报告（迁移引入 critical_status 时填了空串）回填为"已通知"，
    # 使其可正常进入确认/处置闭环并出现在催办清单
    exam_reports = sa.table(
        "exam_reports",
        sa.column("critical", sa.Boolean),
        sa.column("critical_status", sa.String),
    )
    op.execute(
        exam_reports.update()
        .where(
            sa.and_(
                exam_reports.c.critical == sa.true(),
                sa.or_(
                    exam_reports.c.critical_status == "",
                    exam_reports.c.critical_status.is_(None),
                ),
            )
        )
        .values(critical_status="notified")
    )

    # M-4：改密令牌基线（iat 早于该时刻的令牌拒绝）
    op.add_column("users", sa.Column("token_valid_from", sa.DateTime(), nullable=True))

    # L-6：诊断领取人
    op.add_column(
        "exam_requests",
        sa.Column("claimed_by", sa.String(length=64), nullable=False, server_default=""),
    )

    # M-6：报告修订历史表
    op.create_table(
        "report_revisions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("report_id", sa.Integer(), nullable=False),
        sa.Column("prev_conclusion", sa.String(length=1024), nullable=False),
        sa.Column("prev_finding", sa.String(length=2048), nullable=False),
        sa.Column("prev_critical", sa.Boolean(), nullable=False),
        sa.Column("revised_by", sa.String(length=64), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["report_id"], ["exam_reports.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_report_revisions_report_id"), "report_revisions", ["report_id"], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_report_revisions_report_id"), table_name="report_revisions")
    op.drop_table("report_revisions")
    op.drop_column("exam_requests", "claimed_by")
    op.drop_column("users", "token_valid_from")
    # critical_status 回填不回退（数据迁移不可逆且无害）
