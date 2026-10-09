"""院感报告的上报日期不得晚于今天（P2-1571，第四十六批扫描 AJ2-9 的上报日期那半）。

修前（scan46 aj2 `r2_infection.py` 第 1 步实测）：`create_infection_report` 的 `report_date` 只校验格式（`OptionalDateStr`）——
填 2027-10-05 照样 201、原样回显。

修法：照签约日期（`contracts.py` 的 P2-1548）、接种日期（P2-1304）同一句——非空且晚于业务日的 422「上报日期（…）不得晚于
今天」；今天与过去照收，留空照旧不判。挂不挂住院、加感染日期、统计分期间要业务拍板，不在本条。
"""
from datetime import date

import pytest

from conftest import freeze_business_date, login

#: 冻住的业务日：判据与被判对象取同一个「今天」，用例不怕跨午夜
TODAY = date(2026, 3, 1)
R = "/api/quality/infection-reports"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P21571 县医院", "org_type": "lead_hospital", "level": "county"})
    assert org.status_code in (200, 201), org.text
    made = client.post("/api/users", headers=admin, json={
        "username": "p21571_doc", "password": "passw0rd1", "role": "doctor", "org_id": org.json()["id"]})
    assert made.status_code in (200, 201), made.text
    patient = client.post("/api/patients", headers=admin, json={"name": "P21571 院感患者", "id_card": "330102197003011571"})
    assert patient.status_code == 201, patient.text
    return {"org": org.json()["id"], "patient": patient.json()["id"], "doctor": login(client, "p21571_doc", "passw0rd1")}


def _report(client, world, report_date):
    return client.post(R, headers=world["doctor"], json={
        "org_id": world["org"], "patient_id": world["patient"], "infection_site": "surgical_site", "pathogen": "MRSA",
        "report_date": report_date})


def test_上报日期晚于今天_422_不落库(client, admin, world):
    with freeze_business_date(TODAY):
        got = _report(client, world, "2026-03-02")
    assert got.status_code == 422, got.text   # 修前 201，原样回显 2026-03-02
    assert got.json() == {"detail": "上报日期（2026-03-02）不得晚于今天"}
    rows = client.get(R, headers=admin, params={"org_id": world["org"]}).json()
    assert [r["report_date"] for r in rows] == []


def test_今天与过去照收_留空照收(client, world):
    with freeze_business_date(TODAY):
        made = {d: _report(client, world, d) for d in ("2026-03-01", "2026-02-28", "")}
    for report_date, got in made.items():
        assert got.status_code == 201 and got.json()["report_date"] == report_date, got.text
