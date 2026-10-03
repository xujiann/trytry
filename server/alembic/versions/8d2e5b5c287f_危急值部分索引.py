"""危急值部分索引：exam_reports(critical) 只收危急值（P2-1156）

待办铃铛每人每 30 s 轮询一次，医生、院长、管理员各有一节危急值；驾驶舱的危急值指标、运营报表的「危急值未闭环例数」、
危急值清单与超时未确认催办也都按同一个判据取：`critical 为真 AND critical_status IN ('notified', …, '')`。
`critical` 这一列原先没有索引；`critical_status` 有索引，但普通报告的缺省值就是 ''，恰在 IN 列表里，这个索引一条也
筛不掉——每次都按报告总量逐条回表判断（报告 4.4 万、危急值 30 条时每条语句约 5 ms，第三十三批扫描 A4-11）。
危急值在报告里是极少数，只收它们的部分索引很小。

判据同时改写成 `critical == true()`：ORM 的 `.is_(True)` 编译成 `IS 1`（SQLite）/ `IS true`（PG），两个库都证明不了它
蕴含索引谓词，建了索引也用不上（2026-09-30 在 SQLite 3.45 与 PG 16 上实测）。

## 升级

纯加索引（C 档），不动任何存量行。两库都支持部分索引，谓词各写一份：SQLite 没有布尔类型、存 0 / 1；PG 写布尔列本身
（查询里的 `critical = true` 规划时规范成同一个样子）。

## 回退

删索引，不碰数据。判据的新写法与索引无关，回退后结果照旧，只是又回到按报告总量扫。

Revision ID: 8d2e5b5c287f
Revises: c2792b9b55b3
Create Date: 2026-09-30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "8d2e5b5c287f"
down_revision: Union[str, Sequence[str], None] = "c2792b9b55b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_index(
        "ix_exam_reports_critical",
        "exam_reports",
        ["critical"],
        sqlite_where=sa.text("critical = 1"),
        postgresql_where=sa.text("critical"),
    )


def downgrade() -> None:
    op.drop_index("ix_exam_reports_critical", table_name="exam_reports")
