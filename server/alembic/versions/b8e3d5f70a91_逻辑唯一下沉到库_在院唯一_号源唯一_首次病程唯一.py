"""把三条"业务上唯一、库上无约束"的不变式下沉到数据库（P1-29）

`tests/test_stage14_concurrency.py` 的 `LOGICAL_UNIQUE_TABLES` 登记着一类比撞
`IntegrityError` 更坏的缺陷：**业务上唯一，库上没有约束**——并发下不会报错，
而是**静默写出两条**，事后要靠人从两份记录里判哪份算数。本迁移建三条部分唯一
索引，把其中三条不变式下沉到库里（第四条 `bill_details` 经复核并不成立，见
TECH_DEBT P1-29 的更正说明）：

- `uq_admission_patient_admitted`：**一个患者同时只能有一条在院记录**。
  接口层先查在院再建单是 check-then-act，两路并发入院登记会把同一个人登记进
  两张床，此后计费/医嘱/护理各挂一条 admission，出院结算只结得掉一条。
  部分索引 `WHERE status='admitted'`——出院后的历史记录当然允许多条。
- `uq_slot_with_employee` / `uq_slot_without_employee`：**同机构+医师+资源+日期+
  时段的号源唯一**。重复号源各带 capacity，放号量凭空翻倍，多放的号最终无人可看。
  拆两条是因为 SQL 里 NULL != NULL：检查/检验号源不挂医师（employee_id 为 NULL），
  单一复合唯一索引对这类号源等于不设防。
- `uq_progress_note_first`：**首次病程每次住院唯一**（其余病程类型本就该有多条，
  同一时刻两位医师各记一条也正常）。并发双击"书写首次病程"会写出两份法定文书。

**存量冲突不阻塞升级**（CLAUDE.md §4，范式同 `e5b7c9d1f3a4`/`e7c4b19d02fa`）：
每条索引建之前先探重复，探到就**跳过这一条**并打指名冲突键的 ERROR 日志，
其余照常建。本迁移只做 DDL 与 SELECT 探测，不 UPDATE/DELETE 任何存量业务数据——
"留 id 最小的、其余清掉"在这三类数据上都是业务决定：两条在院记录要由医务科判
哪条是真的（另一条可能已经挂了医嘱与费用），重复号源可能已经被患者预约上了，
两份首次病程要由质控判哪份归档。

## 人工处置 SQL

    -- 在院重复（ERROR 日志给出 patient_id）
    SELECT patient_id, count(*), string_agg(id::text, ',') FROM admissions
     WHERE status = 'admitted' GROUP BY patient_id HAVING count(*) > 1;
    -- 逐条确认后：把不该在院的那条按正常出院流程处理（走接口，留审计），
    -- 而不是直接 UPDATE 库；处理完补建：
    CREATE UNIQUE INDEX uq_admission_patient_admitted ON admissions (patient_id)
      WHERE status = 'admitted';

    -- 重复号源（日志给出五元组）
    SELECT org_id, employee_id, resource_type, resource_name, slot_date, slot_time,
           count(*), string_agg(id::text, ',')
      FROM appointment_slots
     GROUP BY 1,2,3,4,5,6 HAVING count(*) > 1;
    -- 已被预约（booked > 0）的那条留下，其余经业务确认后停用/删除，再补建两条索引。

    -- 双份首次病程（日志给出 admission_id）
    SELECT admission_id, count(*), string_agg(id::text, ',') FROM progress_notes
     WHERE note_type = 'first' GROUP BY admission_id HAVING count(*) > 1;
    -- 由质控判定保留哪份（另一份改 note_type='daily' 或按病历修正流程作废），再补建。

Revision ID: b8e3d5f70a91
Revises: e7c4b19d02fa
Create Date: 2026-09-01 01:05:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8e3d5f70a91'
down_revision: Union[str, Sequence[str], None] = 'e7c4b19d02fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: (索引名, 表, 唯一键列, 部分条件)——与模型 __table_args__ 里的声明逐字对应，
#: 两边任何一处改了而另一处没改，结构漂移棘轮（现基线 0）会当场判红。
_INDEXES: tuple[tuple[str, str, tuple[str, ...], str], ...] = (
    ("uq_admission_patient_admitted", "admissions", ("patient_id",), "status = 'admitted'"),
    (
        "uq_slot_with_employee", "appointment_slots",
        ("org_id", "employee_id", "resource_type", "resource_name", "slot_date", "slot_time"),
        "employee_id IS NOT NULL",
    ),
    (
        "uq_slot_without_employee", "appointment_slots",
        ("org_id", "resource_type", "resource_name", "slot_date", "slot_time"),
        "employee_id IS NULL",
    ),
    ("uq_progress_note_first", "progress_notes", ("admission_id",), "note_type = 'first'"),
)

_PROBE_LIMIT = 20


def _duplicates(bind, table: str, columns: tuple[str, ...], where: str) -> list[tuple]:
    """探重复：返回冲突的键值（不是"有 N 条"——那事后查不回来是谁）。"""
    tbl = sa.table(table, *(sa.column(c) for c in columns))
    keys = [tbl.c[c] for c in columns]
    # 用 Core 构造而不是拼 SQL 串：SELECT/GROUP BY/HAVING/LIMIT 交给 SQLAlchemy
    # 按方言渲染；只有部分索引的条件是文本（它与模型 __table_args__ 里那份
    # sqlite_where/postgresql_where 逐字同源，本就该是同一个字符串）。
    stmt = (
        sa.select(*keys, sa.func.count().label("n"))
        .where(sa.text(where))
        .group_by(*keys)
        .having(sa.func.count() > 1)
        .limit(_PROBE_LIMIT)
    )
    return [tuple(row) for row in bind.execute(stmt).fetchall()]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for name, table, columns, where in _INDEXES:
        conflicts = _duplicates(bind, table, columns, where)
        if conflicts:
            logger.error(
                "%s 存量已有重复的 %s（%s，末位是重复条数）：%s。本次跳过建索引 %s——"
                "这三类重复都要由业务判定保留哪条（在院记录可能各自挂了医嘱与费用、"
                "号源可能已被预约、病程是法定文书），迁移不替人决定。"
                "处置与补建 SQL 见本迁移 docstring。",
                table, "+".join(columns), where, conflicts, name,
            )
            continue
        op.create_index(
            name, table, list(columns), unique=True,
            sqlite_where=sa.text(where), postgresql_where=sa.text(where),
        )


def downgrade() -> None:
    """Downgrade schema.

    升级时可能因存量重复跳过了某条，回退要对付"索引本就不存在"
    （回退往往发生在出事的深夜，不该再卡一次）。
    """
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns, _where in reversed(_INDEXES):
        if name in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
