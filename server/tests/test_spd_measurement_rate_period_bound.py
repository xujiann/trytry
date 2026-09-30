"""慢专病考核「监测」取数源的在管患者与期末对齐（P2-1073，第三十一批「统计与报表按哪个日期归期」扫描 C1-1）。

变量字典写的是「期内在管患者的监测次数」（`service.py`），纳管 / 建档 / 评估三个取数源早给档案加了期末上界
（`through_day(SpdEnrollment.created_at, end)`，P2-688「分母与分子同一个期末」），监测这一支却取「现在在管」的全部档案，
再数期内读数。监测值不要求纳管——公卫随访同步、设备、手录都照收——于是 9 月才纳管的人，他 8 月的读数在补跑 8 月时进了
8 月的「指标控制达标率」：纳管前跑 8 月 1 次 / 1 正常，纳管后重跑 3 次 / 1 正常，同一期越晚跑分越低、永远复现不出来。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.spd.models import SpdEnrollment, SpdIndicator, SpdMeasurement
from app.spd.routers.assess import collect_metrics_batch

PROGRAM = "P21073X"
ID_CARDS = ["330106196808080011", "33010619680808002X"]


@pytest.fixture(scope="module")
def org(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21073 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patients = []
    for i, id_card in enumerate(ID_CARDS):
        resp = client.post("/api/patients", headers=admin, json={"name": f"P21073 居民{i}", "id_card": id_card})
        assert resp.status_code in (200, 201), resp.text
        patients.append(resp.json()["id"])
    early, late = patients
    with SessionLocal() as db:
        # 甲 7 月纳管；乙 8 月只有读数（未纳管），9 月底才纳管
        db.add(SpdEnrollment(patient_id=early, program_code=PROGRAM, org_id=org, status="active",
                             created_at=datetime(2026, 7, 1, 9)))
        db.add(SpdEnrollment(patient_id=late, program_code=PROGRAM, org_id=org, status="active",
                             created_at=datetime(2026, 9, 29, 9)))
        for patient, value, level in ((early, 128, "normal"), (late, 172, "high"), (late, 168, "high")):
            db.add(SpdMeasurement(patient_id=patient, program_code=PROGRAM, metric="bp_sys", value=value,
                                  unit="mmHg", level=level, measured_at=datetime(2026, 8, 20, 9)))
        db.add(SpdMeasurement(patient_id=late, program_code=PROGRAM, metric="bp_sys", value=150, unit="mmHg",
                              level="high", measured_at=datetime(2026, 9, 29, 10)))
        db.commit()
    return org


def _measure_inputs(org, period):
    indicator = SpdIndicator(code="P21073", name="指标控制达标率", data_source="measurement", object_type="org")
    with SessionLocal() as db:
        return collect_metrics_batch(db, indicator, "org", [org], period, PROGRAM)[org]


def test_补跑往期_期末之后才纳管的人_纳管前的读数不进往期(org):
    assert _measure_inputs(org, "2026-08") == {"total": 1.0, "normal": 1.0, "abnormal": 0.0}   # 修前 3 / 1 / 2


def test_当期照旧_期内纳管的人的读数照数(org):
    # 9 月在管的两人：乙 9 月纳管，他 9 月的读数照数（8 月的读数不在 9 月期内）
    assert _measure_inputs(org, "2026-09") == {"total": 1.0, "normal": 0.0, "abnormal": 1.0}
