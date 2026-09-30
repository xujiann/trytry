"""数据源页「记一次同步」（手工登记）把定时采集推后一个周期（P2-938，第二十六批「同一业务动作的多个入口」扫描 H4-5）。

P2-530 定的规矩是手工登记不算采集器的同步，但只改了回溯窗口（`lookback_since`）；「到没到期」（`run_due_sources_counted`）
仍按 `last_sync_at` 判，而手工登记也写它。公卫源每 60 分钟一次、上次自动采集在 2 小时前，运维点一次「记一次同步」，
定时任务就报「没有到期的数据源」、期间录的公卫随访血压一条不采；接口方每个周期回报一次，采集器就永远不跑，
监控页照显示「正常」。

修法：到期判定与回溯窗口同一取法——只认采集器自己写的日志（`manual=False`），不管成败。
"""
from datetime import timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdDataSource, SpdSyncLog

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2938 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    ids = {}
    now = now_naive()
    for key, minutes_ago in (("stale", 120), ("fresh", 10)):
        src = client.post(f"{B}/data-sources", headers=admin, json={
            "code": f"P2938_{key.upper()}", "name": f"P2938 公卫随访 {key}", "source_type": "publichealth",
            "org_id": org, "freq_minutes": 60})
        assert src.status_code == 201, src.text
        ids[key] = src.json()["id"]
        with SessionLocal() as db:   # 采集器上一次自己跑：stale 两小时前（该到期了）、fresh 十分钟前（没到期）
            started = now - timedelta(minutes=minutes_ago)
            db.add(SpdSyncLog(source_id=ids[key], started_at=started, rows=0, latency_ms=10, success=True,
                              message="P2938 采集器"))
            db.get(SpdDataSource, ids[key]).last_sync_at = started
            db.commit()
    with SessionLocal() as db:   # 别的源都挪开：只看这两个
        db.query(SpdDataSource).filter(SpdDataSource.id.notin_(list(ids.values()))).update(
            {SpdDataSource.active: False}, synchronize_session=False)
        db.commit()
    return ids


def test_手工登记一次同步之后_定时采集照样按采集器自己的时刻到期(client, admin, world):
    from app.spd.collectors import run_due_sources_counted
    from app.spd.models import SpdSyncLog

    manual = client.post(f"{B}/data-sources/{world['stale']}/sync-logs", headers=admin, json={
        "rows": 3, "latency_ms": 0, "success": True, "message": "P2938 接口方回报"})
    assert manual.status_code == 201, manual.text
    with SessionLocal() as db:
        count, failed, summary = run_due_sources_counted(db)
        db.commit()
        ran = {sid for (sid,) in db.query(SpdSyncLog.source_id).filter(
            SpdSyncLog.manual.is_(False), SpdSyncLog.message != "P2938 采集器")}
    assert (count, failed) == (1, 0), summary   # 修前 (0, 0)「没有到期的数据源」
    assert ran == {world["stale"]}   # 十分钟前才跑过的照旧不到期
