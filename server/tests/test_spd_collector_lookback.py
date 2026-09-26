"""公卫随访采集的回溯窗口只有 `freq_minutes × 4`：1 分钟一次的源每轮漏掉约 20%，漏跑、失败过的补不回来（P2-254）。

`collect_publichealth` 的 docstring 写着「回溯窗口取 freq_minutes × 4：比同步周期宽一倍以上，一次漏跑补得回来」——可同步
周期不是 `freq_minutes`：定时任务 `spd_data_source_sync` 按 5 分钟唤醒，1 分钟一次的源实际 5 分钟才跑一回，4 分钟的窗口
每轮漏掉头 1 分钟里录的随访；某一轮采集失败、定时任务停过，窗口移过去就再也补不回来。不报错，同步日志照写「成功」。

修法：窗口从上一次**成功**同步的开始时刻再往前放一个周期（至少 `× 4`），幂等键保证补跑不重复。
"""
from datetime import timedelta

import pytest

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2254 公卫中心", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2254 患者", "id_card": "330127196909092254"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    return {"chronic": chronic.json()["id"]}


def _source(client, admin, code):
    resp = client.post(f"{B}/data-sources", headers=admin, json={
        "code": code, "name": f"{code} 公卫随访", "source_type": "publichealth", "freq_minutes": 1})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _followup(world, minutes_ago):
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.models import FollowUp

    with SessionLocal() as db:
        row = FollowUp(chronic_id=world["chronic"], sbp=132, created_at=now_naive() - timedelta(minutes=minutes_ago))
        db.add(row)
        db.commit()
        return row.id


def _sync_log(source_id, minutes_ago, success):
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.spd.models import SpdSyncLog

    with SessionLocal() as db:
        db.add(SpdSyncLog(source_id=source_id, started_at=now_naive() - timedelta(minutes=minutes_ago),
                          rows=0, latency_ms=1, success=success, message="" if success else "采集失败"))
        db.commit()


def _run_and_synced(source_id, followup_id):
    from app.database import SessionLocal
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource, SpdMeasurement

    with SessionLocal() as db:
        log = run_source(db, db.get(SpdDataSource, source_id))
        db.commit()
        assert log.success, log.message
        return db.query(SpdMeasurement).filter_by(source_ref=f"chronic_fu:{followup_id}:bp_sys").count()


def test_一分钟一次的源按五分钟跑_上一轮之后录的都同步到(client, admin, world):
    source = _source(client, admin, "p2254_a")
    _sync_log(source, minutes_ago=5, success=True)          # 上一轮：定时任务 5 分钟前唤醒时跑的
    followup = _followup(world, minutes_ago=4.5)            # 上一轮之后、4 分钟窗口之外录的
    assert _run_and_synced(source, followup) == 1           # 修前 0：窗口只回溯 4 分钟


def test_中间一轮失败了_从上一次成功算起补得回来(client, admin, world):
    source = _source(client, admin, "p2254_b")
    _sync_log(source, minutes_ago=30, success=True)
    _sync_log(source, minutes_ago=5, success=False)         # 这一轮挂了，该它同步的没进来
    followup = _followup(world, minutes_ago=20)
    assert _run_and_synced(source, followup) == 1           # 修前 0：窗口移过去就补不回来
    assert _run_and_synced(source, followup) == 1           # 再跑一轮不重复落数


def test_从没成功过的新源照旧只看四个周期(client, admin, world):
    from app.clock import now_naive
    from app.database import SessionLocal
    from app.spd.collectors import lookback_since
    from app.spd.models import SpdDataSource

    source = _source(client, admin, "p2254_c")
    stale = _followup(world, minutes_ago=30)
    assert _run_and_synced(source, stale) == 0              # 新源不回头扫历史
    with SessionLocal() as db:
        since = lookback_since(db, db.get(SpdDataSource, source))
    assert timedelta(minutes=3) < now_naive() - since < timedelta(minutes=6)   # 刚成功过一轮：窗口仍是四个周期
