"""测试库关掉 fsync、外键照开（P2-1015）：`conftest._test_sqlite_no_fsync` 被误删时，单元档不会红、只会悄悄慢回去——
CI run 770 分到刷盘慢的 runner，就从七十分钟拖到四小时十分钟。外键开着是 P2-71 的约定（夹具先建父行），两个开关一起钉住。"""


def test_测试库不刷盘_外键照开(client):
    from app.database import engine

    with engine.connect() as conn:
        synchronous = conn.exec_driver_sql("PRAGMA synchronous").scalar()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar()
    assert (synchronous, foreign_keys) == (0, 1), (synchronous, foreign_keys)
