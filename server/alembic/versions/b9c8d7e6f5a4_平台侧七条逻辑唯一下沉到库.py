"""平台侧七条"业务上唯一、库上无约束"的不变式下沉为唯一索引（P1-30）

P1-30 把并发闸门看不到的 136 张表逐张审了一遍（判据与理由见
`tests/test_stage14_concurrency.py` 的三份审计清单）。判为"业务上确实唯一"的表里，
平台侧这七张的键能直接用列表达，故下沉到库；其余"唯一性长在父行状态跃迁上"的表
走条件 UPDATE，不在本迁移。

这类缺陷比撞 `IntegrityError` 更坏：库上没约束，并发下不报错而是**静默写出两条**，
事后要靠人从两条记录里判哪条算数。七条：

- `uq_disease_enrollment_program_patient_enrolled`：同患者同专病**在管唯一**
  （`WHERE status='enrolled'`）。完成/退出后复发再入组是常态，全量唯一会拒掉它。
  两条在管会让专病统计双计，出组只翻掉一条，剩下那条永远挡着再入组。
- `uq_dual_channel_pending` / `uq_special_disease_app_applied`：双通道药品申报、
  特殊病种申报**待审唯一**。批准/驳回后可再申报，故只锁待审那一态。
- `uq_fund_distribution_settlement_org`：一次清算里一家机构只分一条钱。分配明细是
  "分完即冻结的快照"，两条同键等于同一次清算给同一家机构分了两次。
- `uq_reconciliation_batch_date`：一个自然日一张日终对账单。跑两次会写出两张口径不同
  的单子，差异明细各挂各的 batch，事后没人知道哪张算数。
- `uq_inpatient_order_active_long`：同一次住院里内容相同的**长期医嘱在执行唯一**
  （`WHERE order_type='long' AND status='active'`）。临时医嘱按次开立、停用后重开都合法。
- `uq_delivery_record`：一本孕产妇档案只登记一次分娩。多胎由 `newborn_count` 表达，
  不是多行；查询侧 `.first()` 无序，两条之后取到哪条全看运气。

**存量冲突不阻塞升级**（CLAUDE.md §4，范式同 `b8e3d5f70a91`/`e5b7c9d1f3a4`）：
每条索引建之前先探重复，探到就**跳过这一条**并打**指名冲突键**的 ERROR 日志，其余照常建。
本迁移只做 DDL 与 SELECT 探测，不 UPDATE/DELETE 任何存量业务数据——这七类重复
都是业务决定：两条在管专病记录要由医生判哪条是真的（各自可能已经走了路径节点），
重复的分配明细牵涉已打款的金额，两条长期医嘱可能已经各自产生了执行记录。

## 人工处置 SQL

    -- 1. 专病在管重复（ERROR 日志给出 program_id, patient_id）
    SELECT program_id, patient_id, count(*), string_agg(id::text, ',')
      FROM disease_enrollments WHERE status = 'enrolled'
     GROUP BY program_id, patient_id HAVING count(*) > 1;
    -- 逐条确认后：把多余那条按正常出组流程处理（走接口 POST /api/disease-programs/
    -- enrollments/{id}/exit，留审计），不要直接 UPDATE 库；处理完补建：
    CREATE UNIQUE INDEX uq_disease_enrollment_program_patient_enrolled
      ON disease_enrollments (program_id, patient_id) WHERE status = 'enrolled';

    -- 2/3. 双通道 / 特病申报待审重复（日志给出 patient_id + 药品名/病种名）
    SELECT patient_id, drug_name, count(*), string_agg(id::text, ',')
      FROM dual_channel_apps WHERE status = 'pending'
     GROUP BY patient_id, drug_name HAVING count(*) > 1;
    SELECT patient_id, disease_name, count(*), string_agg(id::text, ',')
      FROM special_disease_apps WHERE status = 'applied'
     GROUP BY patient_id, disease_name HAVING count(*) > 1;
    -- 由审核人经接口驳回多余的那条（留审计），再补建对应索引。

    -- 4. 分配明细重复（日志给出 settlement_id, org_id）
    SELECT settlement_id, org_id, count(*), string_agg(id::text, ','), sum(amount)
      FROM fund_distributions GROUP BY settlement_id, org_id HAVING count(*) > 1;
    -- 涉及已分配金额：先与财务核对该次清算的实际拨付额，确认哪条已入账、
    -- 另一条是否已重复打款，处置方案由财务出，再补建索引。

    -- 5. 同日多张对账单（日志给出 date）
    SELECT date, count(*), string_agg(id::text, ',') FROM reconciliation_batches
     GROUP BY date HAVING count(*) > 1;
    -- 保留哪张由财务判（差异明细挂在各自 batch 下，删错会丢失差异记录）；
    -- 作废的那张连同其 reconciliation_diffs 一并归档后再补建索引。

    -- 6. 同内容长期医嘱重复在执行（日志给出 admission_id + content）
    SELECT admission_id, content, count(*), string_agg(id::text, ',')
      FROM inpatient_orders WHERE order_type = 'long' AND status = 'active'
     GROUP BY admission_id, content HAVING count(*) > 1;
    -- 由主管医师判定停哪条（走接口停医嘱，两条可能各自已有执行记录），再补建索引。

    -- 7. 一档两条分娩记录（日志给出 record_id）
    SELECT record_id, count(*), string_agg(id::text, ',') FROM delivery_records
     GROUP BY record_id HAVING count(*) > 1;
    -- 由产科判定哪条是真实分娩（分娩方式/新生儿数可能不同），作废另一条后补建索引。

Revision ID: b9c8d7e6f5a4
Revises: b8e3d5f70a91
Create Date: 2026-09-04 05:40:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b9c8d7e6f5a4'
down_revision: Union[str, Sequence[str], None] = 'b8e3d5f70a91'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: (索引名, 表, 唯一键列, 部分条件或 None)——与模型 `__table_args__` 里的声明逐字对应，
#: 两边任何一处改了而另一处没改，结构漂移棘轮（现基线 0）会当场判红。
_INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    (
        "uq_disease_enrollment_program_patient_enrolled", "disease_enrollments",
        ("program_id", "patient_id"), "status = 'enrolled'",
    ),
    ("uq_dual_channel_pending", "dual_channel_apps", ("patient_id", "drug_name"), "status = 'pending'"),
    (
        "uq_special_disease_app_applied", "special_disease_apps",
        ("patient_id", "disease_name"), "status = 'applied'",
    ),
    ("uq_fund_distribution_settlement_org", "fund_distributions", ("settlement_id", "org_id"), None),
    ("uq_reconciliation_batch_date", "reconciliation_batches", ("date",), None),
    (
        "uq_inpatient_order_active_long", "inpatient_orders", ("admission_id", "content"),
        "order_type = 'long' AND status = 'active'",
    ),
    ("uq_delivery_record", "delivery_records", ("record_id",), None),
)

_PROBE_LIMIT = 20


def _duplicates(bind, table: str, columns: tuple[str, ...], where: str | None) -> list[tuple]:
    """探重复：返回冲突的键值（不是"有 N 条"——那事后查不回来是谁）。"""
    tbl = sa.table(table, *(sa.column(c) for c in columns))
    keys = [tbl.c[c] for c in columns]
    # 用 Core 构造而不是拼 SQL 串：SELECT/GROUP BY/HAVING/LIMIT 交给 SQLAlchemy
    # 按方言渲染；只有部分索引的条件是文本（它与模型 `__table_args__` 里那份
    # sqlite_where/postgresql_where 逐字同源，本就该是同一个字符串）。
    stmt = sa.select(*keys, sa.func.count().label("n"))
    if where is not None:
        stmt = stmt.where(sa.text(where))
    stmt = stmt.group_by(*keys).having(sa.func.count() > 1).limit(_PROBE_LIMIT)
    return [tuple(row) for row in bind.execute(stmt).fetchall()]


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    for name, table, columns, where in _INDEXES:
        conflicts = _duplicates(bind, table, columns, where)
        if conflicts:
            logger.error(
                "%s 存量已有重复的 %s（%s，末位是重复条数）：%s。本次跳过建索引 %s——"
                "这几类重复都要由业务判定保留哪条（在管专病记录各自可能已走了路径节点、"
                "分配明细牵涉已打款金额、长期医嘱可能各自已有执行记录），迁移不替人决定。"
                "处置与补建 SQL 见本迁移 docstring。",
                table, "+".join(columns), where or "全表", conflicts, name,
            )
            continue
        kwargs = {}
        if where is not None:
            kwargs = {"sqlite_where": sa.text(where), "postgresql_where": sa.text(where)}
        op.create_index(name, table, list(columns), unique=True, **kwargs)


def downgrade() -> None:
    """Downgrade schema.

    升级时可能因存量重复跳过了某条，回退要对付"索引本就不存在"
    （回退往往发生在出事的深夜，不该再卡一次）。
    """
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns, _where in reversed(_INDEXES):
        if name in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
