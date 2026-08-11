"""定时任务基座：任务注册表与执行留痕（T1.1）

Revision ID: d0e1f2a3b4c6
Revises: c9d0e1f2a3b5
"""
import sqlalchemy as sa
from alembic import op

revision = "d0e1f2a3b4c6"
down_revision = "c9d0e1f2a3b5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "scheduled_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(64), nullable=False, unique=True, index=True),
        sa.Column("title", sa.String(128), nullable=False, server_default=""),
        sa.Column("interval_seconds", sa.Integer(), nullable=False, server_default="3600"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true(), index=True),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        # 到期时刻落库：进程重启后不漏跑，也不会把所有任务挤到同一时刻
        sa.Column("next_run_at", sa.DateTime(), nullable=True, index=True),
        sa.Column("last_status", sa.String(16), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "job_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("job_name", sa.String(64), nullable=False, index=True),
        sa.Column("trigger", sa.String(16), nullable=False, server_default="scheduled"),
        sa.Column("status", sa.String(16), nullable=False, server_default="succeeded", index=True),
        sa.Column("message", sa.String(1024), nullable=False, server_default=""),
        sa.Column("affected", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(), nullable=False, index=True),
    )


def downgrade() -> None:
    op.drop_table("job_runs")
    op.drop_table("scheduled_jobs")
