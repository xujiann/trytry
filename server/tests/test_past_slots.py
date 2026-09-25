"""日期已过的号源不再列为「可约」、也约不上（P2-64）。

居民端「可约号源」（`GET /api/portal/me/slots`）原先只看有没有余号、不看日期，按日期正序排——头几页全是已经过去
的号；点进去照样约得上（`book_slot` 也不看日期）。2026-09-25 开发库实测（修前代码）：放一个昨天的号，居民端列表
第一条就是它，居民预约 201、状态 `booked`——约上的是一个已经过去的时段。业务端的寻医列表早就按 `slot_date >= 今天`
过滤，两头口径不一。

修法：`/me/slots` 只列业务日期当天及以后的；`book_slot`（居民自助与窗口代约共用）对日期已过的号源 409。
"""
from datetime import timedelta

from conftest import business_today
from test_portal_services import login, me, org  # noqa: F401


def _slot(client, admin, org_id, day, name):
    resp = client.post("/api/appointments/slots", headers=admin, json={
        "org_id": org_id, "resource_type": "outpatient", "resource_name": name,
        "slot_date": day.isoformat(), "slot_time": "09:00-10:00", "capacity": 5})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_过期号源不列为可约_居民与窗口都约不上(client, admin, org, me):
    past = _slot(client, admin, org["id"], business_today() - timedelta(days=1), "P264 昨日门诊")
    listed = {r["id"] for r in client.get("/api/portal/me/slots", headers=me["headers"]).json()}
    assert past["id"] not in listed   # 修前在列表里，而且排在最前

    resident = client.post("/api/portal/me/appointments", json={"slot_id": past["id"]}, headers=me["headers"])
    assert resident.status_code == 409, resident.text   # 修前 201 booked
    window = client.post("/api/appointments", headers=admin,
                         json={"slot_id": past["id"], "patient_id": me["patient"]["id"]})
    assert window.status_code == 409, window.text


def test_当天的号源照常列出照常约得上(client, admin, org, me):
    today = _slot(client, admin, org["id"], business_today(), "P264 今日门诊")
    listed = {r["id"] for r in client.get("/api/portal/me/slots", headers=me["headers"]).json()}
    assert today["id"] in listed
    resp = client.post("/api/portal/me/appointments", json={"slot_id": today["id"]}, headers=me["headers"])
    assert resp.status_code == 201, resp.text
