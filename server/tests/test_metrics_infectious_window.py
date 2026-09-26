"""驾驶舱「近7日传染病报告」按含今天共 7 个日历日数（P2-195）。

原先从今天往前减 7 天、两头都含——实际 8 天，发病日期填成将来的也算「近 7 日」；传染病多点预警的「7 天窗口」
早已是含今天共 7 天（P2-159），两处同名的窗口差一天，驾驶舱横幅与下钻就比预警多数一天的病例。
"""
from datetime import date, timedelta

import pytest


@pytest.fixture(scope="module")
def seeded(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2195 县疾控", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    today = date.today()
    for offset in (0, 6, 7, -1):   # 今天、第 7 天（在窗口里）、第 8 天、明天（都不在）
        resp = client.post("/api/infectious/cases", headers=admin, json={
            "org_id": org, "disease_code": "J11", "disease_name": "流行性感冒",
            "onset_date": (today - timedelta(days=offset)).isoformat()})
        assert resp.status_code in (200, 201), resp.text
    return today


def test_下钻只列含今天共7天内发病的个案(client, admin, seeded):
    body = client.get("/api/metrics/drilldown?metric=infectious_recent", headers=admin).json()
    onsets = sorted(row["onset_date"] for row in body["items"])
    assert onsets == [(seeded - timedelta(days=6)).isoformat(), seeded.isoformat()]   # 修前 4 条
    assert body["total"] == 2


def test_预警横幅与下钻同一个数(client, admin, seeded):
    items = client.get("/api/metrics/alerts", headers=admin).json()["items"]
    counts = {i["type"]: i["count"] for i in items}
    assert counts["infectious_recent"] == 2
