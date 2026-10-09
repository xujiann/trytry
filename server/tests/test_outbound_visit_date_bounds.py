"""县外就诊日期不得晚于今天、不得早于患者出生（P2-1704，第五十批扫描 AN3-2）。

`OutboundIn.visit_date` 原先只查格式：把 2026 敲成 2062 照收 201——本月就医流向里看不到这次县外住院，要到 2062 年才
冒出来，全期累计里倒一直算着；早于患者出生的日期（1990 年生、1980 年就诊）也照收。记的是已经发生的就诊，与签约日期
（P2-1548）、出生 / 死亡 / 缺陷证明日期（P2-1538，并以出生日期作下界）同一句。患者没有出生日期就不判下界；存量不动。
"""
from datetime import timedelta

import pytest

from app.database import SessionLocal
from app.models import Patient
from conftest import business_today


@pytest.fixture(scope="module")
def patients(client, admin):
    born = client.post("/api/patients", headers=admin, json={
        "name": "P21704 患者", "id_card": "330106199001011704", "birth_date": "1990-01-01"})
    assert born.status_code in (200, 201), born.text
    with SessionLocal() as db:   # 出生日期没填的存量档案：不判下界
        unknown = Patient(ehc_no="EHC-P21704-NB", name="P21704 无生日", id_card="330106199001021704", birth_date="")
        db.add(unknown)
        db.commit()
        unknown_id = unknown.id
    return {"born": born.json()["id"], "unknown": unknown_id}


def _visit(client, admin, patient_id, visit_date):
    return client.post("/api/analytics/outbound-visits", headers=admin, json={
        "patient_id": patient_id, "visit_date": visit_date, "external_org_name": "省人民医院",
        "external_org_level": "province", "visit_type": "inpatient", "total_amount": 100})


def test_今天照收_明天422(client, admin, patients):
    today = business_today()
    assert _visit(client, admin, patients["born"], today.isoformat()).status_code == 201
    tomorrow = (today + timedelta(days=1)).isoformat()
    got = _visit(client, admin, patients["born"], tomorrow)
    assert got.status_code == 422, got.text   # 修前 201
    assert got.json()["detail"] == f"就诊日期（{tomorrow}）不得晚于今天"
    far = _visit(client, admin, patients["born"], today.replace(year=2062).isoformat())
    assert far.status_code == 422, far.text   # 修前 201：2026 敲成 2062


def test_早于出生422_出生当天照收(client, admin, patients):
    got = _visit(client, admin, patients["born"], "1980-05-01")
    assert got.status_code == 422, got.text   # 修前 201
    assert got.json()["detail"] == "就诊日期（1980-05-01）早于出生日期（1990-01-01）"
    assert _visit(client, admin, patients["born"], "1990-01-01").status_code == 201


def test_没有出生日期的不判下界(client, admin, patients):
    got = _visit(client, admin, patients["unknown"], "1980-05-01")
    assert got.status_code == 201, got.text
