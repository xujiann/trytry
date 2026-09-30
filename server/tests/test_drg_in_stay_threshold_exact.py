"""DRG 事中预警拿浮点「均值 × 倍数」比门槛：在院天数恰好等于门槛也报「已明显超出」（P2-989，第二十八批「取整与精度发生
在哪一步」扫描 F2-3）。

同组 6 例已出院，住院日 5、6、6、6、6、6（合计 35），倍数 1.2 时门槛精确是 35 × 1.2 ÷ 6 = 7 天；判据是严格大于，在院
恰好 7 天不该报。浮点 35/6 × 1.2 = 6.999999999999999，于是报了、页面同时显示超出倍数 1.2×，而设定倍数正是 1.2（倍数 1.5
不报）。P2-156 定过压在线上的值不能被二进制浮点判成超线。

修法：在院天数与样本住院日都是整数，按 stayed × n > Σ样本 × Decimal(倍数) 精确比。
"""
from datetime import datetime, timedelta

import pytest

from app.database import SessionLocal

TODAY = "2026-09-20"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import Admission, Bed, CaseSummary, DrgGroup, User, Ward

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2989 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    patients = []
    for i in range(8):
        made = client.post("/api/patients", headers=admin, json={
            "name": f"P2989 住院{i}", "id_card": f"3301061980010{2989 + i:05d}"})
        assert made.status_code in (200, 201), made.text
        patients.append(made.json()["id"])
    with SessionLocal() as db:
        creator = db.query(User.id).filter(User.username == "admin").scalar()
        group = DrgGroup(code="P2989", name="P2989 测试组", base_weight=1.0, keywords="P2989测试病", mdc="MDCZ",
                         mdc_name="测试")
        ward = Ward(org_id=org, name="P2989 病区")
        db.add_all([group, ward])
        db.flush()
        beds = [Bed(ward_id=ward.id, bed_no=f"P2989-{i}") for i in range(8)]
        db.add_all(beds)
        db.flush()
        noon = datetime(2026, 6, 1, 4, 0)
        for i, days in enumerate([5, 6, 6, 6, 6, 6]):
            adm = Admission(patient_id=patients[i], org_id=org, ward_id=ward.id, bed_id=beds[i].id,
                            status="discharged", admitted_at=noon + timedelta(days=10 * i),
                            discharged_at=noon + timedelta(days=10 * i + days), created_by=creator)
            db.add(adm)
            db.flush()
            db.add(CaseSummary(admission_id=adm.id, discharge_diagnosis="P2989测试病", drg_code="P2989",
                               drg_weight=1.0))
        stays = {}
        for i, admitted in ((6, datetime(2026, 9, 13, 4, 0)), (7, datetime(2026, 9, 12, 4, 0))):   # 在院 7 天、8 天
            adm = Admission(patient_id=patients[i], org_id=org, ward_id=ward.id, bed_id=beds[i].id, status="admitted",
                            admitted_at=admitted, created_by=creator)
            db.add(adm)
            db.flush()
            db.add(CaseSummary(admission_id=adm.id, discharge_diagnosis="P2989测试病", drg_code="P2989",
                               drg_weight=1.0))
            stays[i] = adm.id
        db.commit()
    return {"org": org, "seven": stays[6], "eight": stays[7]}


def _alerts(client, admin, world, multiplier):
    resp = client.get("/api/drgs/in-stay-alerts", headers=admin, params={
        "los_multiplier": multiplier, "today": TODAY, "org_id": world["org"]})
    assert resp.status_code == 200, resp.text
    return {a["admission_id"]: a["stayed_days"] for a in resp.json()["alerts"]}


def test_恰好等于门槛不报_超过才报(client, admin, world):
    alerts = _alerts(client, admin, world, 1.2)
    assert world["seven"] not in alerts   # 修前报：6.999999999999999 < 7
    assert alerts.get(world["eight"]) == 8


def test_倍数1点5照旧都不报(client, admin, world):
    alerts = _alerts(client, admin, world, 1.5)
    assert world["seven"] not in alerts and world["eight"] not in alerts   # 门槛 8.75
