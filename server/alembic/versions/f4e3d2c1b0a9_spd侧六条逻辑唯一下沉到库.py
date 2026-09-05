"""慢专病侧六条"业务上唯一、库上无约束"的不变式下沉为唯一索引（P1-30）

**本迁移属 spd 链**（`down_revision` 指向 spd head），不得挂到平台链；
`tests/test_spd_boundary.py` 盯着这条边界。平台侧那七条在同批次的 `b9c8d7e6f5a4`。

判据与逐表理由见 `tests/test_stage14_concurrency.py` 的三份审计清单。六条：

- `uq_spd_program_version`：同病种同版本号只留一份规则快照。两份同版本快照会让
  "这批病人当初按哪版规则纳的管"失去意义（快照存在的全部理由就是事后可追溯）。
- `uq_spd_qc_sample_record_batch`：同一质控记录同一抽样批次只抽一次；重复抽样行
  会让质控合格率的分母虚高。
- `uq_spd_pkg_binding_enroll_pkg_bound`：同一档案同一服务包**绑定中唯一**
  （`WHERE status='bound'`）。解绑保留台账、解绑后可再绑，故只锁 bound——
  两条 bound 会让剩余次数分裂在两条台账上，扣哪条全看查询顺序。
- `uq_spd_consult_open_patient_program`：同患者同病种**开放会话唯一**
  （`WHERE status='open'`，患者端 #18 明写"进行中会话会继续，不会重复新开"）。
  两条 open 会让消息分叉在两条会话线里，医生端与工作台各显一条。
- `uq_spd_apply_pending_patient_program`：同患者同病种**待受理申请唯一**
  （接口 docstring 原话："同一病种已有待受理申请时不重复提交"）。被拒后可再申请。
- `uq_spd_call_task_pending_ref`：同一个被引用对象（某条随访/复诊）**待呼叫唯一**
  （`WHERE status='pending' AND ref_id IS NOT NULL`）。呼叫失败后重派是先前那条
  已置 failed 之后的新一行，天然在范围外；无 ref_id 的患者级外呼显式排除——
  SQL 里 NULL != NULL，不排除等于对这类任务不设防且键含义不清。

**`spd_lifecycle_events` 审出同样的不变式却故意不建索引**："一份档案同时只有一次待确认
跨机构迁出"在业务上成立，但这张表**没有"撤回待确认迁出"的通道**（只有
`POST /lifecycle-events/{id}/confirm`，没有拒绝/撤回）。今天迁错目标机构还能再发一条指向
正确机构的迁出，加了索引就变成"发不出第二条、也撤不掉第一条"——把一条良性的多余行换成
一份卡死的档案。补上撤回通道再建索引，登记在 `AUDITED_UNDECIDED_TABLES` 与 TECH_DEBT。

**存量冲突不阻塞升级**（CLAUDE.md §4，范式同 `b8e3d5f70a91`）：每条索引建之前先探重复，
探到就**跳过这一条**并打指名冲突键的 ERROR 日志，其余照常建。只做 DDL 与 SELECT 探测，
不 UPDATE/DELETE 任何存量业务数据——保留哪条都是业务决定（两条开放会话各自挂着居民
发过的消息，两条绑定各自扣过次数）。

## 人工处置 SQL

    -- 1. 同版本快照重复（ERROR 日志给出 program_id, version）
    SELECT program_id, version, count(*), string_agg(id::text, ',')
      FROM spd_program_versions GROUP BY program_id, version HAVING count(*) > 1;
    -- 快照是只读留痕：与病种维护人核对哪份对应真实发布，另一份改 version 加后缀
    -- （如 '2.1-dup'）保留证据，不要删行；处理完补建：
    CREATE UNIQUE INDEX uq_spd_program_version ON spd_program_versions (program_id, version);

    -- 2. 同批次质控抽样重复（日志给出 record_id, batch）
    SELECT record_id, batch, count(*), string_agg(id::text, ',')
      FROM spd_qc_samples GROUP BY record_id, batch HAVING count(*) > 1;
    -- 由质控员判定保留哪条（两条的 result 可能相反），另一条按质控流程作废后补建索引。

    -- 3. 同档案同服务包两条绑定中（日志给出 enrollment_id, package_id）
    SELECT enrollment_id, package_id, count(*), string_agg(id::text, ','), string_agg(items::text, ' | ')
      FROM spd_package_bindings WHERE status = 'bound'
     GROUP BY enrollment_id, package_id HAVING count(*) > 1;
    -- 两条 items 里的 used 次数要**先合并**再解绑多余那条（走接口解绑，留台账），
    -- 否则患者已用的次数会凭空回滚；合并口径由服务包管理员定，再补建索引。

    -- 4. 同患者同病种两条开放会话（日志给出 patient_id, program_code）
    SELECT patient_id, program_code, count(*), string_agg(id::text, ',')
      FROM spd_consults WHERE status = 'open'
     GROUP BY patient_id, program_code HAVING count(*) > 1;
    -- 两条各自挂着 spd_consult_messages：由医生经接口关闭多余那条（消息保留可回看），
    -- 不要删会话行，再补建索引。

    -- 5. 同患者同病种两条待受理申请（日志给出 patient_id, program_code）
    SELECT patient_id, program_code, count(*), string_agg(id::text, ',')
      FROM spd_service_applies WHERE status = 'pending'
     GROUP BY patient_id, program_code HAVING count(*) > 1;
    -- 由团队经接口受理/拒绝多余那条（留 handle_note），再补建索引。

    -- 6. 同一被引对象两条待呼叫（日志给出 patient_id, ref_type, ref_id）
    SELECT patient_id, ref_type, ref_id, count(*), string_agg(id::text, ',')
      FROM spd_call_tasks WHERE status = 'pending' AND ref_id IS NOT NULL
     GROUP BY patient_id, ref_type, ref_id HAVING count(*) > 1;
    -- 由调度台取消多余那条（走接口，留操作人），再补建索引。

Revision ID: f4e3d2c1b0a9
Revises: f2b3c4d5e6fa
Create Date: 2026-09-04 05:45:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f4e3d2c1b0a9'
down_revision: Union[str, Sequence[str], None] = 'f2b3c4d5e6fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: (索引名, 表, 唯一键列, 部分条件或 None)——与模型 `__table_args__` 里的声明逐字对应。
_INDEXES: tuple[tuple[str, str, tuple[str, ...], str | None], ...] = (
    ("uq_spd_program_version", "spd_program_versions", ("program_id", "version"), None),
    ("uq_spd_qc_sample_record_batch", "spd_qc_samples", ("record_id", "batch"), None),
    (
        "uq_spd_pkg_binding_enroll_pkg_bound", "spd_package_bindings",
        ("enrollment_id", "package_id"), "status = 'bound'",
    ),
    (
        "uq_spd_consult_open_patient_program", "spd_consults",
        ("patient_id", "program_code"), "status = 'open'",
    ),
    (
        "uq_spd_apply_pending_patient_program", "spd_service_applies",
        ("patient_id", "program_code"), "status = 'pending'",
    ),
    (
        "uq_spd_call_task_pending_ref", "spd_call_tasks",
        ("patient_id", "ref_type", "ref_id"), "status = 'pending' AND ref_id IS NOT NULL",
    ),
)

_PROBE_LIMIT = 20


def _duplicates(bind, table: str, columns: tuple[str, ...], where: str | None) -> list[tuple]:
    """探重复：返回冲突的键值（不是"有 N 条"——那事后查不回来是谁）。"""
    tbl = sa.table(table, *(sa.column(c) for c in columns))
    keys = [tbl.c[c] for c in columns]
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
                "保留哪条是业务决定（两条开放会话各自挂着居民发过的消息、两条服务包绑定"
                "各自扣过次数、两条同版本快照对应的发布记录不同），迁移不替人决定。"
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

    升级时可能因存量重复跳过了某条，回退要对付"索引本就不存在"。
    """
    inspector = sa.inspect(op.get_bind())
    for name, table, _columns, _where in reversed(_INDEXES):
        if name in {i["name"] for i in inspector.get_indexes(table)}:
            op.drop_index(name, table_name=table)
