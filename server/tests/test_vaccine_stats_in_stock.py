"""疫苗批次统计的「过期」「30天内到期」只数还有余量的批次，与临期清单同一口径（P2-148）。

临期清单（`/api/vaccine-supply/expiring`）的注释写明：发完的批次不删行、只累加 used_quantity，真实库里大半是零余量，
预警必须只看还有余量的批次，否则最该预警的淹在里面。统计卡片却按全部批次数：
- 早就发完的旧批次年年累加进「过期批次」，这个数只增不减，永远亮着；
- 刚发完最后一支、下月到期的批次也挂着「30天内到期」——卡片上的数与点开的清单对不上。
"""
from datetime import timedelta

import pytest

from app import clock

V = "/api/vaccine-supply"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2148 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    soon = (clock.today() + timedelta(days=10)).isoformat()
    for batch_no, expire, quantity in (("P2148-SOON-LEFT", soon, 5), ("P2148-SOON-EMPTY", soon, 0),
                                       ("P2148-OLD-LEFT", "2020-01-01", 3), ("P2148-OLD-EMPTY", "2020-01-01", 0)):
        created = client.post(f"{V}/batches", headers=admin, json={
            "vaccine_code": "P2148-HEPB", "vaccine_name": "乙肝疫苗(P2148)", "batch_no": batch_no,
            "expire_date": expire, "org_id": org, "quantity": quantity})
        assert created.status_code == 201, created.text
    return {"org": org}


def test_过期与临期只数还有余量的批次(client, admin, world):
    batches = client.get(f"{V}/stats", headers=admin).json()["batches"]
    assert batches["total"] == 4
    assert (batches["expired"], batches["expiring_soon"]) == (1, 1)   # 修前 (2, 2)：零余量的也数进去


def test_卡片上的临期数与临期清单对得上(client, admin, world):
    listed = client.get(f"{V}/expiring", headers=admin, params={"days": 30}).json()["batches"]
    today = clock.today().isoformat()
    not_yet = [b for b in listed if b["expire_date"] >= today]
    assert len(not_yet) == client.get(f"{V}/stats", headers=admin).json()["batches"]["expiring_soon"]
