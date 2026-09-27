"""慢专病数据源同步把「这一轮跑了几个源」当提醒推，成功与失败一字不差（P2-506，第九批「通知承诺」扫描 W2-6）。

`spd_data_source_sync` 每 5 分钟跑一轮，跑了几个到期源就 `broadcast("spd_sync", "慢专病数据源同步", 个数)`；内置页面不开
WebSocket，没配 Redis 时广播恒不达，于是转成运维告警「慢专病数据源同步：N 条（无在线管理端，广播未送达）」——冷却 10 分钟，
一天约 144 条，全部成功与有源失败一模一样，真出了故障反倒淹没在里面。

修法：只把失败的个数推出去（标题写明「失败」），全部成功不推。
"""
import pytest

from app import clock


@pytest.fixture
def pushed(monkeypatch):
    from app.spd import jobs

    captured: list[tuple[str, str, int]] = []
    monkeypatch.setattr(jobs, "broadcast", lambda kind, title, count: captured.append((kind, title, count)))
    return captured


def _only_new_sources_due():
    """把之前用例建的数据源都标成刚同步过：这里只看本用例建的源。"""
    from app.database import SessionLocal
    from app.spd.models import SpdDataSource

    with SessionLocal() as db:
        db.query(SpdDataSource).update({SpdDataSource.last_sync_at: clock.now_naive()}, synchronize_session=False)
        db.commit()


def _sync():
    from app.database import SessionLocal
    from app.spd.jobs import spd_data_source_sync

    with SessionLocal() as db:
        result = spd_data_source_sync(db)
        db.commit()
    return result


def test_全部成功不推_有失败只推失败数(client, admin, pushed):
    _only_new_sources_due()
    ok = client.post("/api/spd/data-sources", headers=admin, json={
        "code": "p2506_ph", "name": "P2506 公卫随访库", "source_type": "publichealth", "freq_minutes": 5})
    assert ok.status_code == 201, ok.text
    count, summary = _sync()
    assert (count, summary) == (1, "到期数据源 1 个，失败 0 个")
    assert pushed == []   # 修前 [("spd_sync", "慢专病数据源同步", 1)]：成功也当提醒推

    bad = client.post("/api/spd/data-sources", headers=admin, json={
        "code": "p2506_lis", "name": "P2506 县医院 LIS", "source_type": "LIS", "freq_minutes": 5})
    assert bad.status_code == 201, bad.text
    count, summary = _sync()
    assert (count, summary) == (1, "到期数据源 1 个，失败 1 个")
    assert pushed == [("spd_sync_failed", "慢专病数据源同步失败", 1)]   # 修前与成功那一轮同一句
