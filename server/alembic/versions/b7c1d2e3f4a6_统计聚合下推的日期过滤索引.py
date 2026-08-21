"""统计聚合下推（A3）的日期过滤索引

Revision ID: b7c1d2e3f4a6
Revises: a4c8e2f60b19
Create Date: 2026-08-21

为什么加这批索引：/api/analytics/efficiency 与 /api/analytics/drug-use 从
"把历史全量明细拉进内存逐行算"改为数据库内按日期范围过滤 + 分组聚合之后，
过滤谓词全部落在这些时间列上——它们此前一个索引都没有，聚合下推等于把
全表扫描从 Python 挪进数据库。补上索引，日期范围过滤才真正走 range scan：

- admissions.admitted_at / discharged_at：占用床日（与统计期重叠）、
  出院人次/平均住院日、收治人天三组聚合的过滤列；
- encounters.created_at：期间诊疗人次（医师担负的分子）；
- prescriptions.created_at：期间处方（抗菌药使用强度的分子）；
- bill_details.created_at：期间费用明细（门诊药占比）；
- case_summaries.created_at：期间病案首页（住院药占比）。

只建索引不动数据，downgrade 对称删除。
"""
from alembic import op

revision = "b7c1d2e3f4a6"
down_revision = "a4c8e2f60b19"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_admissions_admitted_at", "admissions", "admitted_at"),
    ("ix_admissions_discharged_at", "admissions", "discharged_at"),
    ("ix_encounters_created_at", "encounters", "created_at"),
    ("ix_prescriptions_created_at", "prescriptions", "created_at"),
    ("ix_bill_details_created_at", "bill_details", "created_at"),
    ("ix_case_summaries_created_at", "case_summaries", "created_at"),
]


def upgrade() -> None:
    for name, table, column in INDEXES:
        op.create_index(name, table, [column])


def downgrade() -> None:
    for name, table, _column in reversed(INDEXES):
        op.drop_index(name, table_name=table)
