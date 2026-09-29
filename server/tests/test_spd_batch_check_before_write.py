"""批量干预 / 设备批量上传先全部判完（并留痕）再写（P2-731，第十九批「批量 vs 单条」扫描 K1-7）。

原先一边判可见性一边写库：第一位患者落库后请求事务持着写锁，后面每位的调阅留痕在独立会话里等锁（SQLite 5 秒）后写失败、
被吞掉——4 位患者的批量干预 15 秒、留痕只剩 1 条，500 人约 41 分钟、丢 499 条（开发库、CI、公网演示都是 SQLite）。
同子系统的宣教推送（P0-34）、加分组（P0-50）早就先判后写。修法：两处都先查存在（404 点名，同 P2-726）、再逐个判可见性
并留痕，最后才写。
"""
import pytest

from app.database import SessionLocal
from app.models import AccessLog
from app.spd.models import SpdIntervention, SpdMeasurement

B = "/api/spd"


@pytest.fixture(scope="module")
def patients(client, admin):
    ids = []
    for i in range(4):
        resp = client.post("/api/patients", headers=admin, json={
            "name": f"P2731 患者{i}", "id_card": f"33010619800101{2731 + i:04d}"})
        assert resp.status_code in (200, 201), resp.text
        ids.append(resp.json()["id"])
    return ids


def _logs(resource, patient_ids):
    with SessionLocal() as db:
        return db.query(AccessLog).filter(AccessLog.resource == resource, AccessLog.patient_id.in_(patient_ids)).count()


def test_批量干预_每位患者都留下调阅留痕(client, admin, patients):
    resp = client.post(f"{B}/interventions", headers=admin, json={"patient_ids": patients, "content": "P2731 低盐饮食"})
    assert resp.status_code == 201 and resp.json()["created"] == 4, resp.text
    assert _logs("spd_intervention", patients) == 4   # 修前 1：后三位的留痕等锁超时丢了


def test_设备批量上传_每位患者都留下调阅留痕(client, admin, patients):
    items = [{"patient_id": pid, "metric": "bp_sys", "value": 128, "unit": "mmHg", "program_code": "hypertension",
              "source": "device", "device_sn": "P2731-BP", "measured_at": f"2026-09-2{i}T07:30:00"}
             for i, pid in enumerate(patients)]
    resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": items})
    assert resp.status_code == 200 and resp.json()["created"] == 4, resp.text
    assert _logs("spd_measurement", patients) == 4   # 修前 1


@pytest.mark.parametrize("kind", ["批量干预", "设备批量上传"])
def test_混进不存在的患者号_404点名_一条不落(client, admin, patients, kind):
    missing = 987654321
    if kind == "批量干预":
        resp = client.post(f"{B}/interventions", headers=admin,
                           json={"patient_ids": [patients[0], missing], "content": "P2731 错号批次"})
    else:
        resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
            {"patient_id": pid, "metric": "bp_sys", "value": 130, "unit": "mmHg", "program_code": "hypertension",
             "source": "device", "device_sn": "P2731-BAD", "measured_at": "2026-09-28T08:00:00"}
            for pid in (patients[0], missing)]})
    assert resp.status_code == 404 and str(missing) in resp.json()["detail"], resp.text   # 修前 500
    with SessionLocal() as db:
        assert db.query(SpdIntervention).filter(SpdIntervention.content == "P2731 错号批次").count() == 0
        assert db.query(SpdMeasurement).filter(SpdMeasurement.device_sn == "P2731-BAD").count() == 0
