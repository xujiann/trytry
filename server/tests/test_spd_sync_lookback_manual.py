"""采集的回溯窗口不从手工登记的「成功」算起（P2-530，第十批「只做一次的承诺」扫描 X1-2）。

`collectors.lookback_since` 从「上一次成功同步」往前放一个周期起采（P2-254：「从上一次成功同步算起，漏跑、失败都补得
回来」）。数据源页的「记一次同步」（接口方回报 / 手工补录）同样写一条成功日志，原先也被当成上一次成功同步：采集器
9 小时前最后一次成功，7 小时前录了一条公卫随访（血压 168/102），运维在监控页补登一条「成功」把状态翻回正常——之后
采集器从那一刻起采，这条血压永远进不了慢专病监测数据。

修后同步日志记下是不是手工登记（`manual`），回溯窗口只认采集器自己写的成功日志。
"""
from datetime import timedelta

import pytest

from app.clock import now_naive
from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import FollowUp
    from app.spd.models import SpdSyncLog

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2530 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    src = client.post(f"{B}/data-sources", headers=admin, json={
        "code": "P2530_PH", "name": "P2530 公卫随访", "source_type": "publichealth", "org_id": org,
        "freq_minutes": 60})
    assert src.status_code == 201, src.text
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2530 患者", "id_card": "330127196001012530", "birth_date": "1960-01-01"}).json()["id"]
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code in (200, 201), chronic.text
    followup = client.post(f"/api/chronic/{chronic.json()['id']}/followups", headers=admin, json={"sbp": 168, "dbp": 102})
    assert followup.status_code in (200, 201), followup.text
    now = now_naive()
    with SessionLocal() as db:
        # 采集器最后一次成功在 9 小时前；之后调度停了，7 小时前录了这条随访
        db.add(SpdSyncLog(source_id=src.json()["id"], started_at=now - timedelta(hours=9), rows=0, latency_ms=10,
                          success=True, message="P2530 采集器"))
        db.query(FollowUp).filter(FollowUp.chronic_id == chronic.json()["id"]).update(
            {FollowUp.created_at: now - timedelta(hours=7)}, synchronize_session=False)
        db.commit()
    return {"source": src.json()["id"], "patient": patient}


def test_手工补登一条成功之后_采集器照样补回此前漏采的随访(client, admin, world):
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource, SpdMeasurement, SpdSyncLog

    manual = client.post(f"{B}/data-sources/{world['source']}/sync-logs", headers=admin, json={
        "rows": 0, "latency_ms": 0, "success": True, "message": "P2530 手工补录"})
    assert manual.status_code == 201, manual.text
    with SessionLocal() as db:
        assert db.get(SpdSyncLog, manual.json()["id"]).manual is True
        run_source(db, db.get(SpdDataSource, world["source"]))
        db.commit()
        collected = db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == world["patient"]).count()
    assert collected == 2, "7 小时前那条随访的收缩压 / 舒张压没采到"   # 修前 0：窗口从手工补登那一刻算起
