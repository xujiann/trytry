"""号源撮合与统一资源视图的号源：按机构 / 按类别的数在库里全量聚合，过了期的号源不算资源（P2-163 / P2-164）。

撮合说「按最早可用时间排序，同时给出每家机构的余量」，实现先按日期时间取窗口里最早的 500 个号源、再按机构汇总——
全县两周的号源一过 500 个，日期靠后的机构整个不在候选里，在的机构余量也只数到第 500 个。
统一资源视图的号源不看日期、按编号升序取最早建的 500 个：开诊几周后列的全是过了期的，过期没约满的还标「可用」，
卡片上的「可用 / 总数」也跟着封顶在 500 以内。
"""
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app import clock
    from app.database import SessionLocal
    from app.models import AppointmentSlot

    busy, far = (client.post("/api/organizations", headers=admin, json={
        "name": f"P2163 {name}", "org_type": "township", "level": "township"}).json()["id"]
        for name in ("甲卫生院", "乙卫生院"))
    today = clock.today()
    day = lambda n: (today + timedelta(days=n)).isoformat()   # noqa: E731
    with SessionLocal() as db:
        db.add(AppointmentSlot(org_id=busy, resource_type="outpatient", resource_name="P2163 全科",
                               slot_date=day(-1), slot_time="08:00", capacity=2))   # 昨天的，没约满
        db.add_all([AppointmentSlot(org_id=busy, resource_type="outpatient", resource_name="P2163 全科",
                                    slot_date=day(1), slot_time=f"{8 + i // 60:02d}:{i % 60:02d}", capacity=1)
                    for i in range(501)])
        db.add(AppointmentSlot(org_id=far, resource_type="outpatient", resource_name="P2163 全科",
                               slot_date=day(2), slot_time="09:00", capacity=3))
        db.commit()
    return {"busy": busy, "far": far, "day": day}


def test_撮合的余量与候选机构不封顶在500个号源(client, admin, world):
    body = client.get("/api/resources/match/slots", headers=admin, params={"keyword": "P2163"}).json()
    # 修前 [(甲, 500)]：乙的号源排在第 502 个，整个不在候选里；甲的余量也只数到 500
    assert [(c["org_id"], c["remaining_total"]) for c in body["candidates"]] == [(world["busy"], 501), (world["far"], 3)]
    busy = body["candidates"][0]
    assert busy["earliest"] == world["day"](1)
    assert [s["slot_time"] for s in busy["slots"]] == ["08:00", "08:01", "08:02", "08:03", "08:04"]


def test_资源视图的号源只算今天及以后_计数不封顶(client, admin, world):
    body = client.get("/api/resources/catalog", headers=admin,
                      params={"org_id": world["busy"], "resource_kind": "slot"}).json()
    assert body["by_kind"] == {"slot": {"total": 501, "usable": 501}}   # 修前 {total: 500, usable: 500}
    assert body["total"] == 501
    slots = body["items"]
    assert len(slots) == 500
    assert world["day"](-1) not in {s["detail"][:10] for s in slots}   # 修前昨天那个在，还标「可用」
    assert slots[0]["detail"] == f"{world['day'](1)} 08:00"
