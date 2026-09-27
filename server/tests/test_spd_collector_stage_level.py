"""公卫随访采集进来的监测值，按患者所在管理阶段的目标判级（P2-585，第十二批「批量 vs 单条」扫描 Z4-6）。

管理端录入、居民自报都按在管档案的阶段取管理目标（P2-227「判级取哪份档案的阶段，在管的优先」）；采集器原先一律传空
阶段——稳定期收紧过的目标对采集进来的值不起作用：同一个收缩压 135，医生录入判「偏高」，公卫随访同步过来判「正常」，
异常清单里只见前一条。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdMeasurement

B = "/api/spd"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2585 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2585 稳定期患者", "id_card": "330127196005052585"}).json()["id"]
    program = next(p for p in client.get(f"{B}/programs", headers=admin).json() if p["code"] == "hypertension")
    stable = next(t for t in client.get(f"{B}/programs/{program['id']}/targets", headers=admin).json()
                  if t["metric"] == "bp_sys" and t["stage"] == "stable")
    tightened = client.patch(f"{B}/targets/{stable['id']}", headers=admin, json={"target_high": 130})
    assert tightened.status_code == 200, tightened.text   # 稳定期目标收紧到 ≤130（种子是 ≤140）
    enrolled = client.post(f"{B}/enrollments", headers=admin, json={
        "patient_id": patient, "program_code": "hypertension", "org_id": org, "stage": "stable"})
    assert enrolled.status_code == 201, enrolled.text
    chronic = client.post("/api/chronic", headers=admin, json={
        "patient_id": patient, "disease": "hypertension", "managed_by_org_id": org})
    assert chronic.status_code == 201, chronic.text
    followup = client.post(f"/api/chronic/{chronic.json()['id']}/followups", headers=admin, json={"sbp": 135, "dbp": 85})
    assert followup.status_code == 201, followup.text
    source = client.post(f"{B}/data-sources", headers=admin, json={
        "code": "p2585_ph", "name": "P2585 公卫随访", "source_type": "publichealth", "freq_minutes": 60})
    assert source.status_code == 201, source.text
    return {"patient": patient, "source": source.json()["id"], "followup": followup.json()["followup"]["id"]}


def test_采集进来的值与医生录入按同一阶段判级(client, admin, world):
    from app.spd.collectors import run_source
    from app.spd.models import SpdDataSource

    with SessionLocal() as db:
        log = run_source(db, db.get(SpdDataSource, world["source"]))
        db.commit()
        assert log.success, log.error
        collected = db.query(SpdMeasurement).filter(
            SpdMeasurement.source_ref == f"chronic_fu:{world['followup']}:bp_sys").one()
        assert collected.value == 135 and collected.level == "high"   # 修前 normal：按治疗期的 ≤140 判
    manual = client.post(f"{B}/measurements", headers=admin, json={
        "patient_id": world["patient"], "metric": "bp_sys", "value": 135, "unit": "mmHg"})
    assert manual.status_code == 201 and manual.json()["level"] == "high", manual.text
