"""迁移遇存量冲突走不阻塞路径：跳过该项 + 指名冲突记录，其余照常升级。

CLAUDE.md §4 与 `test_migration_data_safety.py` 把这条纪律写成了规矩，但规矩只
拦住"迁移里不许出现 UPDATE/DELETE 的形状"——**跳过分支本身有没有真的跳过、
日志有没有真的指名**，静态扫描看不出来。e7c4b19d02fa 一次性补 14 个外键、收紧
25 列 NOT NULL，是全仓最依赖这条分支的一次迁移：真实生产库上只要有一行孤儿数据
或一个 NULL，升级要么被它挡死（运维半夜卡在升级上），要么替人删数据（不可逆）。
两条都不可接受，所以第三条路必须有用例咬住。

本档在真的库上验三件事（不是读源码猜）：

1. **有冲突的那一项被跳过**：孤儿外键不建、有 NULL 的列不收紧；
2. **升级不被挡死，且没冲突的项照常生效**——同一张表上有冲突项与无冲突项时，
   不能一起放弃（按表聚合的 batch 很容易写成"一处冲突整表跳过"）；
3. **日志指名到主键**：只说"有 N 条冲突"等于事后查不回来是谁（CLAUDE.md §4
   点名要求），故断言 stderr 里出现那一行的 id。

做法：空库升到本迁移的**前一版**，塞进冲突行，再升一格，看结果与日志。
"""
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

SERVER_DIR = Path(__file__).resolve().parent.parent
_PREV_REVISION = "c1d2e3f4a5b6"
_REVISION = "e7c4b19d02fa"

# 冲突项：孤儿外键（子表, 列, 目标表）与 NULL 列（表, 列）
_ORPHAN_FK = ("elderly_assessments", "org_id", "organizations")
_NULL_COLUMN = ("waste_locations", "created_at")
# 对照项：同一次迁移里无冲突的项，必须照常生效
_CLEAN_FK = ("visit_credentials", "org_id", "organizations")
_CLEAN_NOT_NULL = ("roles", "created_at")


def _alembic(db_path: Path, target: str) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=SERVER_DIR,
        env={**os.environ, "MEDPLAT_DATABASE_URL": f"sqlite:///{db_path}"},
        capture_output=True,
        text=True,
    )


def _insert_row(conn: sqlite3.Connection, table: str, **overrides) -> int:
    """按表结构造一行最小可插入数据：NOT NULL 列填哑值，其余留空。

    显式给出的列（含要求为 NULL 的那列）覆盖哑值——这样用例不必跟着建表 DDL
    的字段增删改，表变了也不会假红。
    """
    columns = conn.execute(f"PRAGMA table_info({table})").fetchall()
    values: dict[str, object] = {}
    for _cid, name, decl_type, notnull, _default, pk in columns:
        if pk and name not in overrides:
            continue
        if name in overrides:
            values[name] = overrides[name]
        elif notnull:
            upper = (decl_type or "").upper()
            if "INT" in upper:
                values[name] = 0
            elif "DATETIME" in upper or "DATE" in upper:
                values[name] = "1970-01-01 00:00:00"
            elif "NUMERIC" in upper or "REAL" in upper or "FLOAT" in upper:
                values[name] = 0
            elif "JSON" in upper:
                values[name] = "{}"
            else:
                values[name] = "x"
    cols = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    cur = conn.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(values.values()))
    conn.commit()
    return cur.lastrowid


def _fk_targets(conn: sqlite3.Connection, table: str, column: str) -> set[str]:
    return {
        row[2]
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall()
        if row[3] == column
    }


def _is_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
    for _cid, name, _decl, notnull, _default, _pk in conn.execute(
        f"PRAGMA table_info({table})"
    ).fetchall():
        if name == column:
            return bool(notnull)
    raise AssertionError(f"{table}.{column} 不存在——用例清单该跟着迁移一起更新")


@pytest.fixture(scope="module")
def conflicted(tmp_path_factory):
    """升到前一版 → 塞一行孤儿外键 + 一行 NULL → 再升一格，返回（连接, 日志）。"""
    db_path = tmp_path_factory.mktemp("conflict") / "drift.db"
    prepared = _alembic(db_path, _PREV_REVISION)
    assert prepared.returncode == 0, prepared.stderr[-2000:]

    conn = sqlite3.connect(db_path)
    # 主键取**可辨识的大数**：id=1 时"共 1 条"这类退化文案里也有个 1，
    # 断言"日志指名了主键"会假绿——变异验证当场抓到过这一处（故意留此注记）。
    table, column, _target = _ORPHAN_FK
    orphan_id = _insert_row(conn, table, id=474701, **{column: 999999})  # 指向不存在的机构
    null_table, null_column = _NULL_COLUMN
    null_id = _insert_row(conn, null_table, id=474702, **{null_column: None})
    conn.close()

    result = _alembic(db_path, _REVISION)
    conn = sqlite3.connect(db_path)
    yield {
        "conn": conn, "result": result,
        "orphan_id": orphan_id, "null_id": null_id, "db": db_path,
    }
    conn.close()


def test_有冲突不挡死升级(conflicted):
    """升级必须成功返回——挡死等于把运维锁在半夜的升级窗口里。"""
    result = conflicted["result"]
    assert result.returncode == 0, result.stderr[-3000:]


def test_孤儿外键那一项被跳过(conflicted):
    table, column, target = _ORPHAN_FK
    assert target not in _fk_targets(conflicted["conn"], table, column), (
        f"{table}.{column} 有孤儿行却仍建了外键——真 PG 上这条 ALTER 会直接失败，"
        f"整个升级被挡死"
    )


def test_有NULL的列那一项被跳过(conflicted):
    table, column = _NULL_COLUMN
    assert not _is_not_null(conflicted["conn"], table, column), (
        f"{table}.{column} 有 NULL 行却仍收紧为 NOT NULL——真 PG 上会直接失败"
    )


def test_没冲突的项照常生效(conflicted):
    """按表聚合的 batch 最容易写成"一处冲突就整批放弃"，这里两类各钉一条。"""
    conn = conflicted["conn"]
    table, column, target = _CLEAN_FK
    assert target in _fk_targets(conn, table, column), f"{table}.{column} 的外键没补上"
    nn_table, nn_column = _CLEAN_NOT_NULL
    assert _is_not_null(conn, nn_table, nn_column), f"{nn_table}.{nn_column} 没收紧"


def test_同一张表上的其余项不受牵连(conflicted):
    """`waste_locations.created_at` 有 NULL 被跳过，但同表被 medical_wastes 指向的
    外键关系不受影响——冲突的粒度是"项"，不是"表"。"""
    conn = conflicted["conn"]
    assert "waste_locations" in _fk_targets(conn, "medical_wastes", "source_location_id")
    assert "waste_locations" in _fk_targets(conn, "medical_wastes", "storage_location_id")


def test_日志指名到冲突记录的主键(conflicted):
    """"有 N 条冲突"事后查不回来是谁——CLAUDE.md §4 要求指名。"""
    log = conflicted["result"].stderr
    fk_table, fk_column, _target = _ORPHAN_FK
    null_table, null_column = _NULL_COLUMN
    assert f"{fk_table}.{fk_column}" in log, "日志没说是哪张表哪一列的外键被跳过"
    assert f"{null_table}.{null_column}" in log, "日志没说是哪张表哪一列没能收紧"
    assert re.search(rf"id: .*\b{conflicted['orphan_id']}\b", log), (
        f"日志里没有孤儿行的 id={conflicted['orphan_id']}：\n{log[-2000:]}"
    )
    assert re.search(rf"id: .*\b{conflicted['null_id']}\b", log), (
        f"日志里没有 NULL 行的 id={conflicted['null_id']}：\n{log[-2000:]}"
    )
    assert "docstring" in log, "日志要指向人工处置 SQL 的所在，否则运维只知道出事不知道怎么办"
