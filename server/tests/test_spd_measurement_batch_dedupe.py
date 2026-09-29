"""设备批量回传按天然键判重，网关重推同一批不再落第二份（P2-728，第十九批「导入 / 入站对接 vs 界面录入」扫描 K3-9）。

蓝牙网关断线重连按退避重试，把缓存里的同一批再推一遍：原先同一台血压计的两条读数推 3 次，监测清单里 6 行——考核达标率
按行数算、异常计数与趋势都按 3 倍算。`source_ref` 的注释写明「兼作幂等判重」，公卫采集器就是这么用的；设备回传有现成的
天然键（患者、指标、设备号、测定时刻）。修法：带设备号和测定时刻的按天然键判重，重复的跳过、回执给出 `skipped`
（没有跳过的不出这个键，原有回执逐字节不变）；同一批里重复的同样只落一条。没带设备号或测定时刻的照旧逐条落。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdMeasurement

B = "/api/spd"


@pytest.fixture(scope="module")
def patient(client, admin):
    resp = client.post("/api/patients", headers=admin, json={"name": "P2728 赵六", "id_card": "330102196001012728"})
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


def _item(patient, value, measured_at, device_sn="P2728-BP-0001", **extra):
    return {"patient_id": patient, "metric": "bp_sys", "value": value, "unit": "mmHg",
            "program_code": "hypertension", "source": "device", "device_sn": device_sn,
            "measured_at": measured_at, **extra}


def _rows(patient, device_sn="P2728-BP-0001"):
    with SessionLocal() as db:
        return sorted((m.value, m.measured_at.isoformat()) for m in db.query(SpdMeasurement).filter(
            SpdMeasurement.patient_id == patient, SpdMeasurement.device_sn == device_sn))


def test_同一批推三次只落一份(client, admin, patient):
    batch = {"items": [_item(patient, 128, "2026-09-28T07:30:00"), _item(patient, 186, "2026-09-28T19:30:00")]}
    first = client.post(f"{B}/measurements/batch", headers=admin, json=batch)
    assert first.json() == {"created": 2, "abnormal": 1}, first.text   # 没有跳过的，回执照旧
    for _ in range(2):
        again = client.post(f"{B}/measurements/batch", headers=admin, json=batch)
        assert again.json() == {"created": 0, "abnormal": 0, "skipped": 2}, again.text   # 修前每次再落 2 条
    assert _rows(patient) == [(128.0, "2026-09-28T07:30:00"), (186.0, "2026-09-28T19:30:00")]


def test_一批里有新有旧_只落新的_同批重复只落一条(client, admin, patient):
    batch = {"items": [
        _item(patient, 128, "2026-09-28T07:30:00"),     # 上一轮已落
        _item(patient, 132, "2026-09-29T07:30:00"),     # 新的
        _item(patient, 132, "2026-09-29T07:30:00"),     # 同一批里重复
    ]}
    resp = client.post(f"{B}/measurements/batch", headers=admin, json=batch)
    assert resp.json() == {"created": 1, "abnormal": 0, "skipped": 2}, resp.text
    assert len(_rows(patient)) == 3


def test_另一台设备或没带设备号_测定时刻相同也照落(client, admin, patient):
    other = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
        _item(patient, 128, "2026-09-28T07:30:00", device_sn="P2728-BP-0002")]})
    assert other.json() == {"created": 1, "abnormal": 0}, other.text
    manual = {"items": [_item(patient, 126, "2026-09-28T07:30:00", device_sn="", source="manual")] * 2}
    resp = client.post(f"{B}/measurements/batch", headers=admin, json=manual)
    assert resp.json() == {"created": 2, "abnormal": 0}, resp.text   # 没有天然键的不判重（手工补录两次是两次）
