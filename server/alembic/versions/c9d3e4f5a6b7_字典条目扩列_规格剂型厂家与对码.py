"""字典条目扩列：规格/剂型/厂家/单位/医保对码/本位码/备用扩展

Revision ID: c9d3e4f5a6b7
Revises: b7c1d2e3f4a6
Create Date: 2026-08-21

为什么扩列：药品/耗材/收费目录要真正落地，只有 code+name 撑不起
业务——同名药不同规格/剂型/厂家是不同条目，医保结算要对医保编码，
跨系统对照要本位码（YPID）。这些属性放进统一字典而不是另建目录表，
是为了守住"四统一"一处权威（CLAUDE.md §4 核心数据不可变定义）。

全部可空：诊断（ICD-10）等字典用不上这些列，强填默认值只会造假数据。
insurance_code / national_code 建索引：对码查询是结算对账的高频路径。
downgrade 对称删列删索引。
"""
import sqlalchemy as sa
from alembic import op

revision = "c9d3e4f5a6b7"
down_revision = "b7c1d2e3f4a6"
branch_labels = None
depends_on = None

COLUMNS = [
    sa.Column("spec", sa.String(64), nullable=True),
    sa.Column("dosage_form", sa.String(32), nullable=True),
    sa.Column("manufacturer", sa.String(128), nullable=True),
    sa.Column("unit", sa.String(16), nullable=True),
    sa.Column("insurance_code", sa.String(64), nullable=True),
    sa.Column("national_code", sa.String(64), nullable=True),
    sa.Column("extra", sa.String(1024), nullable=True),
]

INDEXES = [
    ("ix_code_entries_insurance_code", "insurance_code"),
    ("ix_code_entries_national_code", "national_code"),
]


def upgrade() -> None:
    for column in COLUMNS:
        op.add_column("code_entries", column)
    for name, column in INDEXES:
        op.create_index(name, "code_entries", [column])


def downgrade() -> None:
    for name, _column in reversed(INDEXES):
        op.drop_index(name, table_name="code_entries")
    for column in reversed(COLUMNS):
        op.drop_column("code_entries", column.name)
