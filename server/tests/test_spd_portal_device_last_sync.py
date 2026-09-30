"""居民 App 回传的设备读数不刷新设备台账的「最近同步」，同一类设备走医护端录入会刷新（P2-975，第二十七批「冗余的汇总列 /
派生字段与明细不同步」扫描 G3-4）。

医护端 `care._record_measurement`（单条录入、设备批量都走它）带了设备号就刷新 `SpdDevice.last_sync_at`；居民端
`portal.add_measurement` 收同样的 `source="device"` 与 `device_sn`，只写监测值。两台都绑好的血压计，居民家里回传的那台
「最近同步」一直是空，台账看不出哪台还在用。

修法：抽成 `service.touch_device_sync`，两端共用；只认台账里有的序列号，不比对绑给了谁（随 P2-746 定）。
"""
import pytest

from app.database import SessionLocal

B = "/api/spd"
PHONE = "13900029750"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.spd.models import SpdDevice

    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2975 居民", "id_card": "330106196707070075", "birth_date": "1967-07-07", "phone": PHONE})
    assert patient.status_code in (200, 201), patient.text
    with SessionLocal() as db:
        db.add(SpdDevice(sn="P2975-BP-HOME", device_type="bp", bound_patient_id=patient.json()["id"], status="bound"))
        db.commit()
    code = client.post("/api/portal/auth/sms/code", json={"phone": PHONE}).json()["debug_code"]
    token = client.post("/api/portal/auth/sms/login", json={"phone": PHONE, "code": code})
    assert token.status_code == 200, token.text
    return {"Authorization": f"Bearer {token.json()['access_token']}"}


def _last_sync(sn):
    from app.spd.models import SpdDevice

    with SessionLocal() as db:
        return db.query(SpdDevice).filter_by(sn=sn).one().last_sync_at


def test_居民端带设备号回传_刷新最近同步(client, world):
    assert _last_sync("P2975-BP-HOME") is None
    resp = client.post("/api/portal/spd/measurements", headers=world, json={
        "metric": "sbp", "value": 132, "unit": "mmHg", "source": "device", "device_sn": "P2975-BP-HOME"})
    assert resp.status_code == 201, resp.text
    assert _last_sync("P2975-BP-HOME") is not None   # 修前一直是空


def test_不带设备号的不动_台账外的序列号照收不建台账(client, world):
    from app.spd.models import SpdDevice

    before = _last_sync("P2975-BP-HOME")
    manual = client.post("/api/portal/spd/measurements", headers=world, json={"metric": "sbp", "value": 128})
    assert manual.status_code == 201, manual.text
    assert _last_sync("P2975-BP-HOME") == before
    unknown = client.post("/api/portal/spd/measurements", headers=world, json={
        "metric": "sbp", "value": 130, "source": "device", "device_sn": "P2975-NOT-IN-LEDGER"})
    assert unknown.status_code == 201, unknown.text
    with SessionLocal() as db:
        assert db.query(SpdDevice).filter_by(sn="P2975-NOT-IN-LEDGER").count() == 0


def test_两端共用一处():
    import inspect

    from app.spd.routers import care, portal

    assert "touch_device_sync(" in inspect.getsource(care._record_measurement)
    assert "touch_device_sync(" in inspect.getsource(portal.add_measurement)
