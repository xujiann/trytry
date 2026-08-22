"""迁移 ↔ 模型 schema 比对的**唯一**一份实现。

两个用例共用它，跑在两种底座上：

- `test_migration_model_parity.py`：一次性 SQLite 空库，**跑在 test-unit 里**，
  约 7 秒，改模型的人本地 `make verify` 就能拿到反馈；
- `test_postgres_real.py::test_迁移与模型的列集合零漂移`：真 PG（integration，
  CI 阻断），额外守 SQLite 测不出的方言问题。

比对逻辑只此一份：这个仓库已经有"同一概念三套并行实现"的历史债
（CLAUDE.md §5），不给它再添一笔。SQLite 那条不能替代 PG 那条——
它证明的是"列写全了"，证明不了"PG 认这个类型"。
"""
from __future__ import annotations


def diff_schema(inspector, metadata) -> dict[str, dict]:
    """比对 inspector 反映的真实 schema 与 `Base.metadata`。

    返回四类差异，空 dict/list 即一致：

    - `missing_tables`：模型有、库里没有 → 漏写迁移，生产缺表
    - `extra_tables`：库里有、模型没有 → 残留，或删模型忘写 drop
    - `missing_columns`：模型有、库里没有 → 漏写 add_column，生产 UndefinedColumn
    - `extra_columns`：库里有、模型没有 → 删列/改列名时漏写迁移
    """
    in_db = {t for t in inspector.get_table_names() if t != "alembic_version"}
    in_model = set(metadata.tables)

    missing_columns: dict[str, list[str]] = {}
    extra_columns: dict[str, list[str]] = {}
    for table in sorted(in_db & in_model):
        db_cols = {c["name"] for c in inspector.get_columns(table)}
        model_cols = set(metadata.tables[table].columns.keys())
        if model_cols - db_cols:
            missing_columns[table] = sorted(model_cols - db_cols)
        if db_cols - model_cols:
            extra_columns[table] = sorted(db_cols - model_cols)

    return {
        "missing_tables": sorted(in_model - in_db),
        "extra_tables": sorted(in_db - in_model),
        "missing_columns": missing_columns,
        "extra_columns": extra_columns,
        "table_count": len(in_db),
    }


def format_columns(drift: dict[str, list[str]]) -> str:
    return "\n".join(f"  {table}: {cols}" for table, cols in sorted(drift.items()))
