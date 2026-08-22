"""工程包 G2（P1-22）：附件表加病毒扫描旁路两列 scan_status / scan_detail

上传不再是"完全没扫"：落库即 pending，由 attachment_av_scan 定时任务经
clamd（MEDPLAT_CLAMD_ADDRESS）异步补扫；下载只拦已确证 infected（410）。
旁路定位=可用性优先，扫描器故障不阻塞上传下载，详见 app/avscan.py。

存量行回填口径：统一 "pending"。clamd 未配置的存量部署，语义上历史附件
就是**没扫过**——回填 pending 如实表达这一点，待开启扫描后由补扫任务
逐批消化；回填 clean/skipped 都是在冒充一个没发生过的结论。

Revision ID: a9c1e3b5d7f9
Revises: c2e4a6b8d0f2
Create Date: 2026-08-22
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3b5d7f9"
down_revision: Union[str, Sequence[str], None] = "c2e4a6b8d0f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default="pending" 同时完成存量回填（回填口径见模块 docstring）
    op.add_column(
        "attachments",
        sa.Column("scan_status", sa.String(length=16), nullable=False, server_default="pending"),
    )
    op.add_column(
        "attachments",
        sa.Column("scan_detail", sa.String(length=256), nullable=False, server_default=""),
    )
    # 补扫任务按 scan_status='pending' 轮询，附件表生产为大表，走索引
    op.create_index(op.f("ix_attachments_scan_status"), "attachments", ["scan_status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_attachments_scan_status"), table_name="attachments")
    op.drop_column("attachments", "scan_detail")
    op.drop_column("attachments", "scan_status")
