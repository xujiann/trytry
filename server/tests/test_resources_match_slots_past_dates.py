"""号源撮合的起始日期填过去时，过去没约满的号不算「最早可约」（P2-1301 跟进）。

撮合 `/api/resources/match/slots` 原先直接拿 `from_date` 当下界：起始日期填过去，过去没约满的号也算候选、排在最前，
「最早可约」报的是已经过去的那天，照着给的号去约只得 409「该号源日期已过」（P2-64）。寻医（P2-1301）与资源视图（P2-164）
早已只算今天及以后的。修后候选号源的下界取 max(from_date, 今天)；窗口照请求的报。
"""
from datetime import timedelta

import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app import clock
    from app.database import SessionLocal
    from app.models import AppointmentSlot

    mixed, stale = (client.post("/api/organizations", headers=admin, json={
        "name": f"P21301撮合 {name}", "org_type": "township", "level": "township"}).json()["id"]
        for name in ("甲卫生院", "乙卫生院"))
    today = clock.today()
    day = lambda n: (today + timedelta(days=n)).isoformat()   # noqa: E731
    with SessionLocal() as db:   # 过去的号源只能直接落库：放号接口不收早于业务日的日期（P2-1301）
        past = AppointmentSlot(org_id=mixed, resource_type="outpatient", resource_name="P21301撮合 全科",
                               slot_date=day(-3), slot_time="08:00", capacity=4)
        db.add_all([
            past,
            AppointmentSlot(org_id=mixed, resource_type="outpatient", resource_name="P21301撮合 全科",
                            slot_date=day(1), slot_time="09:00", capacity=2),
            AppointmentSlot(org_id=stale, resource_type="outpatient", resource_name="P21301撮合 全科",
                            slot_date=day(-2), slot_time="10:00", capacity=5),   # 这家只有过去的号
        ])
        db.commit()
    return {"mixed": mixed, "stale": stale, "day": day}


def _match(client, admin, **params):
    resp = client.get("/api/resources/match/slots", headers=admin, params={"keyword": "P21301撮合", **params})
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_起始日期填过去_过去的号不算候选_窗口照请求的报(client, admin, world):
    body = _match(client, admin, from_date=world["day"](-10), days=14)
    assert body["window"] == {"start": world["day"](-10), "end": world["day"](3)}
    # 修前 [(乙, -2 天, 5), (甲, -3 天, 6)]：过去的号排在最前、余量也算进去，只有过去号源的乙也成了候选
    assert [(c["org_id"], c["earliest"], c["remaining_total"]) for c in body["candidates"]] == [
        (world["mixed"], world["day"](1), 2)]
    assert [s["slot_date"] for s in body["candidates"][0]["slots"]] == [world["day"](1)]


def test_起始日期在今天以后_照旧从那天起(client, admin, world):
    assert _match(client, admin, from_date=world["day"](2), days=7)["candidates"] == []
    body = _match(client, admin, from_date=world["day"](1), days=1)
    assert [(c["org_id"], c["earliest"]) for c in body["candidates"]] == [(world["mixed"], world["day"](1))]


def test_窗口整段在过去_一个候选也没有(client, admin, world):
    body = _match(client, admin, from_date=world["day"](-5), days=4)   # -5 ~ -2 天
    assert body["candidates"] == []   # 修前甲、乙各有过去的号在候选里
