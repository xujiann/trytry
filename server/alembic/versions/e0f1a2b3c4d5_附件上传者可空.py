"""附件 uploaded_by 改可空：居民端（慢专病任务佐证）上传没有工作人员账号。

Revision ID: e0f1a2b3c4d5
Revises: c2d3e4f5a6b7

伪造一个工作人员 id 会在 PostgreSQL 上撞外键、在审计口径上撒谎——NULL 是
"非工作人员上传"的诚实表示。挂在平台分支（不是 spd 分支）：attachments 是平台表。
"""
import sqlalchemy as sa
from alembic import op

revision = "e0f1a2b3c4d5"
down_revision = "c2d3e4f5a6b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("attachments") as batch:
        batch.alter_column("uploaded_by", existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    # 回滚前须先清掉 NULL 行，否则 NOT NULL 加不回去——这是刻意留给操作者的决定
    with op.batch_alter_table("attachments") as batch:
        batch.alter_column("uploaded_by", existing_type=sa.Integer(), nullable=False)
