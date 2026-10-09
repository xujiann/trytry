"""慢专病监测的收缩压 / 舒张压有生理上界 300 / 200（P2-1602，第四十七批扫描 AK2-4 血压上界那一半）。

`service.measure_value_problem` 原先只拦 ≤0 与百分数 >100。2026-10-09 实测（修前代码）：多敲一个 0 的收缩压 1900、
舒张压 850 都 201 判「偏高」，各派一条「指标异常处置」紧急任务（3 天到期），趋势当日均值被拉到 1014、latest=1900，
还会进规则事实的最近值与考核达标率的分母。平台慢病随访早有 `sbp le=300, dbp le=200`（`app/schemas.py::FollowUpCreate`，
P1-101，与住院体征同口径）。

修法：两项血压按同值上界拦下，文案照同函数的既有句式；医护单条录入、设备批量上传、居民自测三处写入都过这一句，
越界的 422 且一行不写（批量里一条越界整批不落），恰好 300 / 200 照收。其余指标的上界与录错作废的口径待裁定，这里不碰。
"""
import pytest

from app.database import SessionLocal
from app.spd.models import SpdMeasurement
from app.spd.service import measure_value_problem

B = "/api/spd"
PHONE = "13900216020"
ID_CARD = "330281199106061602"
NAME = "P21602 居民"


@pytest.fixture(scope="module")
def world(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": NAME, "id_card": ID_CARD, "gender": "男", "birth_date": "1991-06-06", "phone": PHONE})
    assert patient.status_code == 201, patient.text
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code}).json()["access_token"]
    ph = {"Authorization": f"Bearer {token}"}
    bound = client.post("/api/portal/auth/realname", json={"name": NAME, "id_card": ID_CARD}, headers=ph)
    assert bound.status_code in (200, 409), bound.text
    return {"patient": patient.json()["id"], "ph": ph}


def _count(patient_id):
    with SessionLocal() as db:
        return db.query(SpdMeasurement).filter(SpdMeasurement.patient_id == patient_id).count()


def test_判据_300与200照收_越过一点即说出来():
    assert measure_value_problem("bp_sys", 300) is None and measure_value_problem("bp_dia", 200) is None
    assert measure_value_problem("bp_sys", 301) == "收缩压不得超过 300（收到 301）"   # 修前 None
    assert measure_value_problem("bp_dia", 201) == "舒张压不得超过 200（收到 201）"


def test_医护单条录入_越界422一行不写(client, admin, world):
    pid = world["patient"]
    before = _count(pid)
    for metric, value in (("bp_sys", 301), ("bp_dia", 201), ("bp_sys", 1900)):
        resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": metric, "value": value})
        assert resp.status_code == 422, (metric, value, resp.text)   # 修前 201 判偏高、派紧急处置任务
    assert _count(pid) == before
    for metric, value in (("bp_sys", 300), ("bp_dia", 200)):
        resp = client.post(f"{B}/measurements", headers=admin, json={"patient_id": pid, "metric": metric, "value": value})
        assert resp.status_code == 201, (metric, value, resp.text)
    assert _count(pid) == before + 2


def test_设备批量上传_一条越界整批不落(client, admin, world):
    pid = world["patient"]
    before = _count(pid)
    for bad in ({"metric": "bp_sys", "value": 301}, {"metric": "bp_dia", "value": 201}):
        resp = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
            {"patient_id": pid, "metric": "bp_sys", "value": 130}, {"patient_id": pid, **bad}]})
        assert resp.status_code == 422, (bad, resp.text)
    assert _count(pid) == before   # 前一条正常值也没落
    ok = client.post(f"{B}/measurements/batch", headers=admin, json={"items": [
        {"patient_id": pid, "metric": "bp_sys", "value": 300}, {"patient_id": pid, "metric": "bp_dia", "value": 200}]})
    assert ok.status_code == 200 and ok.json()["created"] == 2, ok.text
    assert _count(pid) == before + 2


def test_居民自测_越界422一行不写(client, world):
    pid = world["patient"]
    before = _count(pid)
    for metric, value in (("bp_sys", 301), ("bp_dia", 201)):
        resp = client.post("/api/portal/spd/measurements", headers=world["ph"], json={"metric": metric, "value": value})
        assert resp.status_code == 422, (metric, value, resp.text)
    assert _count(pid) == before
    for metric, value in (("bp_sys", 300), ("bp_dia", 200)):
        resp = client.post("/api/portal/spd/measurements", headers=world["ph"], json={"metric": metric, "value": value})
        assert resp.status_code == 201, (metric, value, resp.text)
    assert _count(pid) == before + 2
