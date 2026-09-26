"""一个数据源采集时撞了库，整轮同步跟着回滚、谁也不留同步日志（P2-265）。

`run_source` 接住了采集器的异常（「一个数据源挂掉不该拖垮整轮同步」），可采集器里撞了库——约束、方言、连接——会话随之作废：
紧接着写同步日志那一下就抛 `PendingRollbackError`，异常冒出 `run_due_sources`，整轮同步（别的源已经采到的一起）回滚，
监控页上一个源都没有日志。修法：每个源的采集圈在自己的保存点里，撞了库只退回这个源的半截写入，照常记失败日志。
"""
import pytest

from app.database import SessionLocal
from app.spd import collectors
from app.spd.models import SpdDataSource, SpdMeasurement, SpdSyncLog


def _bad(db, source):
    """半截写入之后撞 NOT NULL 约束：flush 即 IntegrityError，会话随之要求回滚。"""
    db.add(SpdMeasurement(patient_id=None, program_code="x", metric="bp_sys", value=1, source="p2265"))
    db.flush()
    return 1


def _good(db, source):
    return 3


@pytest.fixture()
def sources(client, monkeypatch):
    monkeypatch.setitem(collectors.COLLECTORS, "device", _bad)
    monkeypatch.setitem(collectors.COLLECTORS, "checkup", _good)
    with SessionLocal() as db:
        # 停用着建：只由本用例直接调 run_source，别的用例按到期跑整轮时不会碰到它们
        bad = SpdDataSource(code="p2265_bad", name="P2265 坏源", source_type="device", freq_minutes=5, active=False)
        good = SpdDataSource(code="p2265_good", name="P2265 好源", source_type="checkup", freq_minutes=5, active=False)
        db.add_all([bad, good])
        db.commit()
        return bad.id, good.id


def test_一个源撞了库_只退它自己的半截写入_照常记失败日志_下一个源照跑(sources):
    bad_id, good_id = sources
    with SessionLocal() as db:
        bad_log = collectors.run_source(db, db.get(SpdDataSource, bad_id))   # 修前：抛 PendingRollbackError
        good_log = collectors.run_source(db, db.get(SpdDataSource, good_id))
        db.commit()
        assert (bad_log.success, bad_log.rows) == (False, 0) and bad_log.message   # 撞库的原因写进日志
        assert (good_log.success, good_log.rows) == (True, 3)
    with SessionLocal() as db:
        logs = {s: db.query(SpdSyncLog).filter_by(source_id=s).count() for s in (bad_id, good_id)}
        assert logs == {bad_id: 1, good_id: 1}
        assert db.query(SpdMeasurement).filter_by(source="p2265").count() == 0   # 半截写入退掉了
        assert db.get(SpdDataSource, bad_id).status == "failed"
