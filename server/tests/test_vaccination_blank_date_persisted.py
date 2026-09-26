"""接种日期留空按今天落库，与查禁忌、查批次效期用的是同一个日期（P2-196）。

`vaccinate` 在日期留空时拿今天去查禁忌、查批次效期，落库的却是空串：AEFI 发生率的分母（同期接种剂次）按日期区间数，
这一针不在任何区间里；接种证明的接种日期印成「—」。页面上的接种日期是选填的，`formJson` 又把空格子整个丢掉。
"""
from datetime import date

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2196 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2196 受种者", "id_card": "330106201905051536"}).json()["id"]
    return {"org": org, "patient": patient}


def test_日期留空落今天_计入当期剂次(client, admin, world):
    today = date.today().isoformat()
    resp = client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "vaccine_code": "P2196-DTAP", "vaccine_name": "百白破"})
    assert resp.status_code == 201, resp.text
    assert resp.json()["vaccinated_date"] == today                      # 修前 ""
    stats = client.get(f"/api/vaccine-supply/stats?start_date={today}&end_date={today}", headers=admin).json()
    assert stats["doses"] == 1                                         # 修前 0：这一针不在任何区间里


def test_填了日期照填的存(client, admin, world):
    resp = client.post("/api/vaccination/records", headers=admin, json={
        "patient_id": world["patient"], "org_id": world["org"], "vaccine_code": "P2196-MMR", "vaccine_name": "麻腮风",
        "vaccinated_date": "2026-03-01"})
    assert resp.status_code == 201 and resp.json()["vaccinated_date"] == "2026-03-01", resp.text
