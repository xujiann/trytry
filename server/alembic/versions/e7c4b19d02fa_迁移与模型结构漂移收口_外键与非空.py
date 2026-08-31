"""迁移-模型结构漂移收口：补 14 处外键 + 25 列 NOT NULL + 去 2 处冗余唯一约束（P1-27）

**这是一批"模型说了、库没做"的差异**，全部由 `test_schema_governance` 的真实结构
diff（alembic `compare_metadata`，不是只比表名的老守卫）点名，逐条清偿：

- **add_fk 14 处**：模型上声明了外键、迁移里没建。性质是**生产库不做参照完整性
  校验**——指向已删除主数据的孤儿行可以静默落库（比如 `medical_wastes` 的处置人
  指向一个已离职删除的 employee），ORM join 时才现形，且现形的地点离写入现场很远。
- **modify_nullable 25 列**：迁移建成可空、模型是 NOT NULL。开发 SQLite 上被
  `create_all` 掩盖（它按模型建，天然 NOT NULL），生产 PG 走迁移建成可空——
  非 ORM 写入（DBA 手工补数据、外部 ETL）能把 NULL 塞进 `created_at` 与 JSON 列，
  读出来时才在应用层炸。
- **remove_constraint 2 处（仅 PG）**：`roles(key)`/`permissions(code)` 的建表 DDL
  同时留下了一条**无名唯一约束**和模型声明的唯一索引 `ix_*`——同一条唯一性被两个
  DB 对象各守一遍，写入要维护两份索引。模型是权威，去掉库里多出来的那份。

**存量冲突一律不阻塞升级**（CLAUDE.md §4 的既定路径，范式同 `e5b7c9d1f3a4`）：
每一项升级前先探一遍，探到冲突就**跳过这一项**并打一条**指名冲突记录**的 ERROR
日志（给出主键 id，不是"有 N 条冲突"），其余项照常升级。库结构本来就已经这样运行
了很久，缺的这道闸门晚几天补上不会更坏；而迁移替人删数据、改数据是不可逆的。

## 人工处置 SQL

**孤儿外键行**（ERROR 日志里会给出 `<表>.<列>` 与冲突的 id）——先查：

    SELECT c.id, c.<列> FROM <表> c
      LEFT JOIN <目标表> p ON c.<列> = p.id
     WHERE c.<列> IS NOT NULL AND p.id IS NULL;

再由业务决定逐条处置：确认父行是误删的 → 补回父行；确认关联本就无意义 →
`UPDATE <表> SET <列> = NULL WHERE id = <id>;`（这 14 列在模型上都是可空列，
置 NULL 是合法终态）。**不要**用"一把置 NULL"了事——那正是本仓库禁止迁移替人做的
业务决定（见 `d3e4f5a6b7c8` 的教训）。处置完补建：

    ALTER TABLE <表> ADD CONSTRAINT fk_<表>_<列>_<目标表>
      FOREIGN KEY (<列>) REFERENCES <目标表> (id);

**NULL 值行**（同样会指名 id）——先查 `SELECT id FROM <表> WHERE <列> IS NULL;`，
再按列的性质回填：`created_at` 用与全仓 54 张表同口径的常量哨兵
（`UPDATE <表> SET created_at = '1970-01-01 00:00:00' WHERE created_at IS NULL;`
——真值不可考，哨兵一眼可辨"此行早于该列被管起来"）；JSON 列回填 `'{}'`/`'[]'`
（与模型 `default=dict`/`default=list` 同值）；`medical_wastes.trace_code` 是
**业务唯一码**，必须由业务补真码，不能程序编。回填后补紧：

    ALTER TABLE <表> ALTER COLUMN <列> SET NOT NULL;

本迁移只做 DDL 与 SELECT 探测，**不 UPDATE/DELETE 任何存量业务数据**。

## 锁与执行窗口（如实写清，别等生产上才发现）

PG 上这两类 DDL 都要**扫全表且持锁**：`ADD CONSTRAINT ... FOREIGN KEY` 取
SHARE ROW EXCLUSIVE（阻塞写），`SET NOT NULL` 取 ACCESS EXCLUSIVE（连读一起阻塞），
持锁时间随表行数增长。本迁移是**割接前**的结构收口，届时这些表基本是空的，一次
执行即可；但若日后在**已装满数据的库**上重放（灾备重建、克隆环境补迁移），大表
（`vaccination_records`/`nursing_records` 量级）应改为低锁配方分两步手工做：

    -- 外键：先建不校验（秒级、只取 SHARE ROW EXCLUSIVE 的短锁），再单独校验
    ALTER TABLE <表> ADD CONSTRAINT fk_<表>_<列>_<目标表>
      FOREIGN KEY (<列>) REFERENCES <目标表> (id) NOT VALID;
    ALTER TABLE <表> VALIDATE CONSTRAINT fk_<表>_<列>_<目标表>;  -- 只取 SHARE UPDATE EXCLUSIVE，不阻塞读写

    -- NOT NULL：PG 12+ 可先加等价 CHECK（可 NOT VALID + VALIDATE），
    -- 再 SET NOT NULL 时规划器凭该 CHECK 跳过全表扫描，ACCESS EXCLUSIVE 只持一瞬
    ALTER TABLE <表> ADD CONSTRAINT <表>_<列>_nn CHECK (<列> IS NOT NULL) NOT VALID;
    ALTER TABLE <表> VALIDATE CONSTRAINT <表>_<列>_nn;
    ALTER TABLE <表> ALTER COLUMN <列> SET NOT NULL;
    ALTER TABLE <表> DROP CONSTRAINT <表>_<列>_nn;

迁移正文没有直接用这套配方：它要按方言分叉（SQLite 只能整表重建，没有 NOT VALID），
换来的复杂度只在"满数据的大库"这一种情形下有价值，而那一次本就该由 DBA 盯着分步做。

Revision ID: e7c4b19d02fa
Revises: c1d2e3f4a5b6
Create Date: 2026-08-31 16:10:00.000000

"""
import logging
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e7c4b19d02fa'
down_revision: Union[str, Sequence[str], None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

#: (子表, 列, 目标表, 目标列)——模型上有、迁移里没建的外键。
_FKS: tuple[tuple[str, str, str, str], ...] = (
    ('appointment_slots', 'employee_id', 'employees', 'id'),
    ('drug_shortages', 'patient_id', 'patients', 'id'),
    ('elderly_assessments', 'org_id', 'organizations', 'id'),
    ('employees', 'dept_id', 'departments', 'id'),
    ('exam_requests', 'claimed_org_id', 'organizations', 'id'),
    ('medical_wastes', 'handler_employee_id', 'employees', 'id'),
    ('medical_wastes', 'source_location_id', 'waste_locations', 'id'),
    ('medical_wastes', 'storage_location_id', 'waste_locations', 'id'),
    ('nursing_records', 'encounter_id', 'encounters', 'id'),
    ('nursing_records', 'inpatient_order_id', 'inpatient_orders', 'id'),
    ('vaccination_records', 'batch_id', 'vaccine_batches', 'id'),
    ('vaccine_contraindications', 'lifted_by', 'users', 'id'),
    ('visit_credentials', 'org_id', 'organizations', 'id'),
    ('women_health_records', 'org_id', 'organizations', 'id'),
)

#: (表, 列, 列类型)——迁移建成可空、模型是 NOT NULL 的列。
#: 类型照模型现状写死（batch 模式下 alter_column 需要 existing_type）。
_NOT_NULLS: tuple[tuple[str, str, object], ...] = (
    ('access_logs', 'created_at', sa.DateTime()),
    ('admin_projects', 'created_at', sa.DateTime()),
    ('aefi_reports', 'created_at', sa.DateTime()),
    ('cold_chain_records', 'created_at', sa.DateTime()),
    ('disease_programs', 'path_nodes', sa.JSON()),
    ('emergency_resources', 'created_at', sa.DateTime()),
    ('fund_distributions', 'score_detail', sa.JSON()),
    ('live_feedbacks', 'created_at', sa.DateTime()),
    ('medical_wastes', 'trace_code', sa.String(length=32)),
    ('pathogen_monitors', 'created_at', sa.DateTime()),
    ('pathology_specimens', 'created_at', sa.DateTime()),
    ('permissions', 'created_at', sa.DateTime()),
    ('project_milestones', 'created_at', sa.DateTime()),
    ('resources', 'created_at', sa.DateTime()),
    ('role_permissions', 'created_at', sa.DateTime()),
    ('roles', 'created_at', sa.DateTime()),
    ('service_blacklists', 'created_at', sa.DateTime()),
    ('simulation_attempts', 'answers', sa.JSON()),
    ('simulation_attempts', 'created_at', sa.DateTime()),
    ('simulation_cases', 'created_at', sa.DateTime()),
    ('simulation_cases', 'decision_points', sa.JSON()),
    ('syndrome_monitors', 'created_at', sa.DateTime()),
    ('tcm_master_cases', 'created_at', sa.DateTime()),
    ('vaccine_batches', 'created_at', sa.DateTime()),
    ('waste_locations', 'created_at', sa.DateTime()),
)

#: (表, 列)——建表 DDL 留下的无名唯一约束，与模型声明的唯一索引重复（仅 PG 反射得出）。
_REDUNDANT_UNIQUES: tuple[tuple[str, str], ...] = (
    ('permissions', 'code'),
    ('roles', 'key'),
)

_PROBE_LIMIT = 20


def _fk_name(table: str, column: str, target: str) -> str:
    """显式命名：SQLite 的 batch 重建必须有约束名，回退时也才删得掉。

    名字不参与 alembic 的外键比对（比对看的是"哪些列指向哪张表的哪些列"），
    所以模型侧的匿名外键与这里的具名外键是同一处，补上即销账。
    """
    return f"fk_{table}_{column}_{target}"


def _orphans(bind, table: str, column: str, target: str, target_col: str) -> list:
    rows = bind.execute(sa.text(
        f"SELECT c.id FROM {table} c "  # noqa: S608 - 表列名全部来自本文件字面量
        f"LEFT JOIN {target} p ON c.{column} = p.{target_col} "
        f"WHERE c.{column} IS NOT NULL AND p.{target_col} IS NULL "
        f"LIMIT {_PROBE_LIMIT + 1}"
    )).fetchall()
    return [row[0] for row in rows]


def _null_rows(bind, table: str, column: str) -> list:
    rows = bind.execute(sa.text(
        f"SELECT id FROM {table} WHERE {column} IS NULL "  # noqa: S608 - 同上
        f"LIMIT {_PROBE_LIMIT + 1}"
    )).fetchall()
    return [row[0] for row in rows]


def _named(ids: list) -> str:
    """指名冲突记录：给主键，不给"有 N 条"——后者查不回来是谁。"""
    head = ", ".join(str(i) for i in ids[:_PROBE_LIMIT])
    return f"{head} …（还有更多，已截断）" if len(ids) > _PROBE_LIMIT else head


def upgrade() -> None:
    """Upgrade schema."""
    bind = op.get_bind()
    plan: dict[str, dict[str, list]] = {}

    def _slot(table: str) -> dict[str, list]:
        return plan.setdefault(table, {"fks": [], "not_nulls": []})

    for table, column, target, target_col in _FKS:
        conflicts = _orphans(bind, table, column, target, target_col)
        if conflicts:
            logger.error(
                "%s.%s 存量有指向不存在 %s 的孤儿行（id: %s），本次跳过补建外键 %s。"
                "请按本迁移 docstring 的处置 SQL 逐条确认后补建——迁移不替人决定"
                "该补父行还是该置 NULL。",
                table, column, target, _named(conflicts), _fk_name(table, column, target),
            )
            continue
        _slot(table)["fks"].append((column, target, target_col))

    for table, column, type_ in _NOT_NULLS:
        conflicts = _null_rows(bind, table, column)
        if conflicts:
            logger.error(
                "%s.%s 存量有 NULL 行（id: %s），本次跳过收紧为 NOT NULL。"
                "请按本迁移 docstring 的处置 SQL 回填后手工收紧。",
                table, column, _named(conflicts),
            )
            continue
        _slot(table)["not_nulls"].append((column, type_))

    # 按表聚合成一个 batch：SQLite 的 batch 是整表重建，一表一次比逐项四次便宜得多。
    for table in sorted(plan):
        items = plan[table]
        if not items["fks"] and not items["not_nulls"]:
            continue
        with op.batch_alter_table(table) as batch:
            for column, target, target_col in items["fks"]:
                batch.create_foreign_key(
                    _fk_name(table, column, target), target, [column], [target_col],
                )
            for column, type_ in items["not_nulls"]:
                batch.alter_column(column, existing_type=type_, nullable=False)

    _drop_redundant_uniques(bind)


def _drop_redundant_uniques(bind) -> None:
    """去掉与模型唯一索引重复的无名唯一约束（仅 PG——SQLite 反射不出无名约束）。

    **先确认唯一性还有人守**：只有当同列上另有一条不属于该约束的唯一索引时才删。
    约束自己的背后索引也会出现在 `get_indexes()` 里，不排掉它这道闸门就是空的。
    """
    if bind.dialect.name != "postgresql":
        return
    inspector = sa.inspect(bind)
    for table, column in _REDUNDANT_UNIQUES:
        uniques = [
            u for u in inspector.get_unique_constraints(table)
            if list(u["column_names"]) == [column]
        ]
        if not uniques:
            continue
        constraint_names = {u["name"] for u in uniques}
        others = [
            i for i in inspector.get_indexes(table)
            if i.get("unique") and list(i["column_names"]) == [column]
            and i["name"] not in constraint_names
        ]
        if not others:
            logger.error(
                "%s.%s 的唯一性只由约束 %s 一个对象把关，跳过删除——"
                "删了就没人守唯一了（模型声明的 ix_%s_%s 索引本应存在，请先查为何缺失）。",
                table, column, [u["name"] for u in uniques], table, column,
            )
            continue
        for unique in uniques:
            op.drop_constraint(unique["name"], table, type_="unique")


def downgrade() -> None:
    """Downgrade schema.

    回退只做"放松"：删外键、把列改回可空、把无名唯一约束加回来——不动任何行。
    升级时按存量冲突跳过的项在这里可能并不存在，故每一步都先探再动
    （回退往往发生在出事的深夜，"对象不存在"不该再让人卡一次）。
    """
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    plan: dict[str, dict[str, list]] = {}

    def _slot(table: str) -> dict[str, list]:
        return plan.setdefault(table, {"fks": [], "not_nulls": []})

    for table, column, target, _target_col in _FKS:
        name = _fk_name(table, column, target)
        if name in {fk["name"] for fk in inspector.get_foreign_keys(table)}:
            _slot(table)["fks"].append(name)

    for table, column, type_ in _NOT_NULLS:
        col = next(
            (c for c in inspector.get_columns(table) if c["name"] == column), None
        )
        if col is not None and not col["nullable"]:
            _slot(table)["not_nulls"].append((column, type_))

    for table in sorted(plan):
        items = plan[table]
        with op.batch_alter_table(table) as batch:
            for name in items["fks"]:
                batch.drop_constraint(name, type_="foreignkey")
            for column, type_ in items["not_nulls"]:
                batch.alter_column(column, existing_type=type_, nullable=True)

    if bind.dialect.name == "postgresql":
        for table, column in _REDUNDANT_UNIQUES:
            existing = [
                u for u in inspector.get_unique_constraints(table)
                if list(u["column_names"]) == [column]
            ]
            if not existing:
                op.create_unique_constraint(f"{table}_{column}_key", table, [column])
