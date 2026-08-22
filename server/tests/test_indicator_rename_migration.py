"""指标改名迁移 b5d9f3a71c2e 的守卫：**不得覆盖现场改过的名字**。

`performance_indicators.name` 是管理端可编辑的（`PATCH /api/performance/indicators/{key}`），
而种子逻辑是"只增不改"（`main.py`：`if key not in existing_keys`）。所以给存量库改名
只能靠迁移。无条件 `UPDATE` 会把某个县自己改过的名字冲掉——迁移的 WHERE 里带了
旧默认值做判据，这里把那个判据钉住。
"""
import pathlib
import re

import pytest
import sqlalchemy as sa

MIGRATION = (
    pathlib.Path(__file__).resolve().parent.parent
    / "alembic" / "versions" / "b5d9f3a71c2e_rename_remote_exam_indicator.py"
).read_text(encoding="utf-8")

OLD_NAME = "远程诊断服务量"
NEW_NAME = "共享诊断协同量"


def _sql(direction: str) -> str:
    """把迁移里 upgrade/downgrade 实际执行的 SQL 取出来（求值 f-string）。"""
    body = MIGRATION[MIGRATION.index(f"def {direction}()"):]
    body = body[: body.index("\n\n\n")] if "\n\n\n" in body else body
    parts = re.findall(r'f?"([^"]*)"', body)
    joined = "".join(p.replace("{NEW_NAME}", NEW_NAME).replace("{OLD_NAME}", OLD_NAME)
                     for p in parts)
    return joined


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")   # 内存库，与测试主库无关
    with engine.begin() as conn:
        conn.execute(sa.text(
            "CREATE TABLE performance_indicators "
            "(id INTEGER PRIMARY KEY, key TEXT, name TEXT, weight REAL, active BOOLEAN)"
        ))
    yield engine
    engine.dispose()


def _rows(engine):
    with engine.begin() as conn:
        return dict(conn.execute(sa.text(
            "SELECT key, name FROM performance_indicators")).all())


def _seed(engine, rows):
    with engine.begin() as conn:
        for i, (key, name) in enumerate(rows, start=1):
            conn.execute(
                sa.text("INSERT INTO performance_indicators VALUES (:i,:k,:n,20,1)"),
                {"i": i, "k": key, "n": name},
            )


def test_默认名会被改掉(db):
    _seed(db, [("remote_exam", OLD_NAME), ("referral", "转诊结案率")])
    with db.begin() as conn:
        conn.execute(sa.text(_sql("upgrade")))
    got = _rows(db)
    assert got["remote_exam"] == NEW_NAME
    assert got["referral"] == "转诊结案率", "改名迁移碰了别的指标"


def test_现场改过的名字不动(db):
    """这是本迁移最要紧的一条：无条件 UPDATE 会把它冲掉。"""
    _seed(db, [("remote_exam", "本县自定义：影像协同")])
    with db.begin() as conn:
        conn.execute(sa.text(_sql("upgrade")))
    assert _rows(db)["remote_exam"] == "本县自定义：影像协同", (
        "现场改过的指标名被迁移覆盖了"
    )


def test_downgrade改得回去且同样只改新默认名(db):
    _seed(db, [("remote_exam", NEW_NAME), ("rx", "本县自定义：处方")])
    with db.begin() as conn:
        conn.execute(sa.text(_sql("downgrade")))
    got = _rows(db)
    assert got["remote_exam"] == OLD_NAME
    assert got["rx"] == "本县自定义：处方"


def test_迁移的where里确实带了名字判据():
    """防呆：上面几条都靠 `_sql()` 把 SQL 抽对了才成立。"""
    up = _sql("upgrade")
    assert "UPDATE performance_indicators" in up
    assert "WHERE" in up and OLD_NAME in up and NEW_NAME in up, up
    assert "key = 'remote_exam'" in up
