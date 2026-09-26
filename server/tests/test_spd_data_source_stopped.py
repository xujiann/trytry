"""慢专病数据源设成「停用」（status=stopped）照样到点同步，同步完又被翻回「正常 / 异常」（P2-315）。

改档（`PATCH /api/spd/data-sources/{id}`）收 `status=stopped`，列注释写「stopped=停用」；可定时采集（`run_due_sources`）
只看 `active`，停了的数据源到点照跑，跑完 `run_source` 按结果把状态改成 running / delayed / failed——停用这一档设了等于
没设；手工登记同步结果（`record_sync`）同理。

修法：定时采集跳过 stopped；两处按同步结果改状态时，手工停的保持停用。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"


@pytest.fixture(scope="module")
def source(client, admin):
    created = client.post(f"{B}/data-sources", headers=admin, json={
        "code": "P2315_HIS", "name": "P2315 HIS", "source_type": "HIS", "freq_minutes": 1})
    assert created.status_code == 201, created.text
    stopped = client.patch(f"{B}/data-sources/{created.json()['id']}", headers=admin, json={"status": "stopped"})
    assert stopped.status_code == 200 and stopped.json()["status"] == "stopped", stopped.text
    return created.json()["id"]


def _state(source_id):
    from app.spd.models import SpdDataSource, SpdSyncLog

    with SessionLocal() as db:
        logs = db.query(SpdSyncLog).filter(SpdSyncLog.source_id == source_id).count()
        return db.get(SpdDataSource, source_id).status, logs


def test_停用的数据源定时采集跳过(source):
    from app.spd.collectors import run_due_sources

    before = _state(source)
    with SessionLocal() as db:
        run_due_sources(db)
        db.commit()
    assert _state(source) == ("stopped", before[1])   # 修前照跑：多一条同步日志、状态翻成 failed（HIS 采集器未注册）


def test_手工登记同步结果_停用的保持停用(client, admin, source):
    got = client.post(f"{B}/data-sources/{source}/sync-logs", headers=admin, json={"rows": 10, "latency_ms": 5})
    assert got.status_code == 201, got.text
    assert got.json()["source"]["status"] == "stopped"   # 修前翻成 running
