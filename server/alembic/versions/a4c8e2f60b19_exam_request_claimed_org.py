"""exam_requests 记录领取的共享诊断中心机构（claimed_org_id）

Revision ID: a4c8e2f60b19
Revises: c7d8e9f1a2b7
Create Date: 2026-08-20

为什么加这一列：共享诊断中心的核心流程是"基层检查、上级诊断"——甲卫生院开单，
乙中心医师领取并出报告。但申请单只记了 `from_org_id`（开单机构）与 `claimed_by`
（领取人的**展示名**），"乙中心与这位患者有服务关系"在模型里没有任何落点。
后果是把附件/打印接口接上可见性校验之后，中心医师打不开自己刚写的报告。

列名以 `org_id` 结尾是刻意的：`visibility._relation_tables()` 按该后缀推导
患者↔机构关系表，命名对了就自动被算进服务关系。

回填：存量已领取的单子按 `claimed_by` 反查 users（先匹配 full_name、再匹配
username）。这是**尽力而为**——`claimed_by` 是展示名，同名同姓会取到任意一个，
匹配不上的留空。留空的后果仅是那张在途单需要重新领取一次，不影响数据正确性。
"""
import sqlalchemy as sa
from alembic import op

revision = "a4c8e2f60b19"
down_revision = "c7d8e9f1a2b7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "exam_requests", sa.Column("claimed_org_id", sa.Integer(), nullable=True)
    )
    op.create_index(
        "ix_exam_requests_claimed_org_id", "exam_requests", ["claimed_org_id"]
    )
    # 尽力而为回填：展示名优先匹配 full_name，退而匹配 username
    op.execute(
        """
        UPDATE exam_requests
           SET claimed_org_id = (
               SELECT u.org_id FROM users u
                WHERE u.org_id IS NOT NULL
                  AND (u.full_name = exam_requests.claimed_by
                       OR u.username = exam_requests.claimed_by)
                LIMIT 1
           )
         WHERE claimed_by <> '' AND claimed_org_id IS NULL
        """
    )


def downgrade() -> None:
    op.drop_index("ix_exam_requests_claimed_org_id", table_name="exam_requests")
    op.drop_column("exam_requests", "claimed_org_id")
