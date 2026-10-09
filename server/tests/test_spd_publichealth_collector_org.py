"""公卫采集器按数据源的机构取数：带机构的源只采该机构管理的慢病档案的随访（P2-1642，第四十八批「慢专病配置域」扫描
AL2-4）。

`collect_publichealth` 原先不看 `source.org_id`——同文件的兄弟 `collect_encounter_probe` 是按它筛的，建源表单也收
「机构」（中心调度手册三节同样这么写）。按乡镇各登记一个公卫源：先跑的东镇源把两镇的随访全采走（本次 4 行），西镇源
永远「正常、0 行」；西镇的源停用之后，西镇新录的收缩压 182 照样被东镇的源采进来，停用挡不住该镇的数据。

修法：`platform.iter_recent_chronic_followups` 加可选机构参数，按档案的管理机构（`chronic_patients.managed_by_org_id`，
随访表自己没有机构列）筛，缺省不筛；采集器把 `source.org_id` 传下去。不带机构的源照采全县。
"""
from datetime import timedelta

import pytest

B = "/api/spd"


def _idcard(body17):
    weights = [7, 9, 10, 5, 8, 4, 2, 1, 6, 3, 7, 9, 10, 5, 8, 4, 2]
    return body17 + "10X98765432"[sum(int(a) * b for a, b in zip(body17, weights)) % 11]


@pytest.fixture(scope="module")
def world(client, admin):
    county = client.post("/api/organizations", headers=admin, json={
        "name": "P21642 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    towns, chronic, sources = {}, {}, {}
    for n, town in enumerate(("east", "west"), start=1):
        towns[town] = client.post("/api/organizations", headers=admin, json={
            "name": f"P21642 {town} 卫生院", "org_type": "township", "level": "township", "parent_id": county}).json()["id"]
        patient = client.post("/api/patients", headers=admin, json={
            "name": f"P21642 {town} 居民", "id_card": _idcard(f"3301061960010{n:04d}"), "gender": "男"})
        assert patient.status_code in (200, 201), patient.text
        made = client.post("/api/chronic", headers=admin, json={
            "patient_id": patient.json()["id"], "disease": "hypertension", "managed_by_org_id": towns[town]})
        assert made.status_code == 201, made.text
        chronic[town] = made.json()["id"]
        source = client.post(f"{B}/data-sources", headers=admin, json={
            "code": f"p21642_{town}", "name": f"P21642 {town} 公卫", "source_type": "publichealth",
            "org_id": towns[town], "freq_minutes": 1})
        assert source.status_code == 201, source.text
        sources[town] = source.json()["id"]
    return {"towns": towns, "chronic": chronic, "sources": sources}


def _followup(chronic_id, sbp):
    from app.database import SessionLocal
    from app.models import FollowUp

    with SessionLocal() as db:
        row = FollowUp(chronic_id=chronic_id, sbp=sbp)
        db.add(row)
        db.commit()
        return row.id


def _synced(followup_id):
    from app.database import SessionLocal
    from app.spd.models import SpdMeasurement

    with SessionLocal() as db:
        return db.query(SpdMeasurement).filter(SpdMeasurement.source_ref == f"chronic_fu:{followup_id}:bp_sys").count()


def _run(source_id):
    from app.database import SessionLocal
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource

    with SessionLocal() as db:
        log = run_source(db, db.get(SpdDataSource, source_id))
        db.commit()
        return log.success, log.rows


def test_两镇各一源_各采各的(world):
    east = _followup(world["chronic"]["east"], 150)
    west = _followup(world["chronic"]["west"], 170)
    assert _run(world["sources"]["east"]) == (True, 1)   # 修前 (True, 2)：西镇那条也被东镇的源采走
    assert (_synced(east), _synced(west)) == (1, 0)
    assert _run(world["sources"]["west"]) == (True, 1)   # 修前 (True, 0)：永远「正常、0 行」
    assert _synced(west) == 1


def test_停掉西镇的源_西镇的随访不再被别的源采进来(world):
    from app.database import SessionLocal
    from app.spd.collectors import run_due_sources_counted
    from app.spd.models import SpdDataSource, SpdSyncLog

    with SessionLocal() as db:
        db.get(SpdDataSource, world["sources"]["west"]).status = "stopped"
        for log in db.query(SpdSyncLog).all():   # 让东镇的源到期
            log.started_at = log.started_at - timedelta(hours=1)
        db.commit()
    east = _followup(world["chronic"]["east"], 155)
    west = _followup(world["chronic"]["west"], 182)
    with SessionLocal() as db:
        ran, failed, _ = run_due_sources_counted(db)
        db.commit()
    assert (ran, failed) == (1, 0)
    assert _synced(east) == 1
    assert _synced(west) == 0   # 修前 1：东镇的源把它采了进来


def test_不带机构的源照采全县(client, admin, world):
    source = client.post(f"{B}/data-sources", headers=admin, json={
        "code": "p21642_county", "name": "P21642 全县公卫", "source_type": "publichealth", "freq_minutes": 1})
    assert source.status_code == 201, source.text
    east = _followup(world["chronic"]["east"], 160)
    west = _followup(world["chronic"]["west"], 175)
    success, _ = _run(source.json()["id"])
    assert success
    assert (_synced(east), _synced(west)) == (1, 1)
