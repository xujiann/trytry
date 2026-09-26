"""孕产妇档案：结案要有产后访视（P2-211）；日期在这一胎结束之后的产前筛查不收，结案后补录孕期里的照收（P2-233）。

①结案只看「已分娩」状态，而分娩登记也把档案推到 delivered：分娩当天就能结案，一次产后访视都没有——结案之后产后访视
反被「档案已结案」挡在外面，产妇还从审方的孕产妇人群里提前掉出去（P2-120）。结案接口自己写的是「须完成产后访视」。
②产前筛查不看是哪一胎的事：一孕一册之后，按上一胎旧档案号录进来的本次妊娠的高风险结果把上一胎标成高危，这一胎仍是
「正常」。P2-211 原先按「已结案」一刀切 409，与 `test_closed_parent_writes.py` 按设计名单里写明的「筛查结果晚到、结案后
才补录照样入档」相冲（第五十七轮全量验证在那条闸门上红了）。改按日期判（P2-233）：日期晚于这一胎的分娩日期（没登记
分娩的按最早一次产后访视）的不可能是这一胎的产前筛查，409；孕期里做的，结案之后补录照收。
"""
import itertools

import pytest

_CARDS = itertools.count(1)


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2211 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _record(client, admin):
    patient = client.post("/api/patients", headers=admin, json={
        "name": "P2211 孕妇", "id_card": f"33010619930303{next(_CARDS) + 1630:04d}", "gender": "女"}).json()["id"]
    resp = client.post("/api/maternal/records", headers=admin, json={"patient_id": patient})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_分娩登记后没有产后访视不能结案(client, admin, org):
    record = _record(client, admin)
    delivery = client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                           json={"org_id": org, "delivery_date": "2026-09-20"})
    assert delivery.status_code == 201, delivery.text
    early = client.post(f"/api/maternal/records/{record}/close", headers=admin)
    assert early.status_code == 409 and "产后访视" in early.json()["detail"]      # 修前 200
    assert client.post(f"/api/maternal/records/{record}/visits", headers=admin,
                       json={"visit_type": "postpartum", "visit_date": "2026-09-27"}).status_code == 201
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).json()["status"] == "closed"


def test_日期在分娩之后的筛查不是这一胎的_409_结案后补录孕期里的照收(client, admin, org):
    record = _record(client, admin)
    assert client.post(f"/api/maternal/records/{record}/delivery", headers=admin,
                       json={"org_id": org, "delivery_date": "2026-09-20"}).status_code == 201
    assert client.post(f"/api/maternal/records/{record}/visits", headers=admin,
                       json={"visit_type": "postpartum", "visit_date": "2026-09-27"}).status_code == 201
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 200
    misfiled = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "nipt", "screen_date": "2026-10-15", "result": "high_risk"})
    assert misfiled.status_code == 409, misfiled.text                             # 修前 201，上一胎被标成高危
    assert misfiled.json() == {"detail": "筛查日期 2026-10-15 晚于这一胎的分娩日期 2026-09-20，"
                                         "不是这一胎的产前筛查：本次妊娠请先建册，按新档案登记"}
    rows = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in rows if r["id"] == record)["high_risk"] is False
    # 这一胎孕期里做的、结案之后才补录的：照收（按设计名单那一条），高危结论就是这一胎的事实
    late = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "down", "screen_date": "2026-05-10", "result": "high_risk"})
    assert late.status_code == 201, late.text
    rows = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in rows if r["id"] == record)["high_risk"] is True


def test_没登记分娩的按最早一次产后访视判(client, admin, org):
    record = _record(client, admin)
    for day in ("2026-09-27", "2026-10-20"):
        assert client.post(f"/api/maternal/records/{record}/visits", headers=admin,
                           json={"visit_type": "postpartum", "visit_date": day}).status_code == 201
    after = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "ultrasound", "screen_date": "2026-10-01"})
    assert after.status_code == 409 and "产后访视日期 2026-09-27" in after.json()["detail"], after.text
    before = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "ultrasound", "screen_date": "2026-09-27"})
    assert before.status_code == 201, before.text   # 当天的照收


def test_还在孕期的照常收(client, admin, org):
    record = _record(client, admin)
    resp = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "nipt", "screen_date": "2026-12-01"})
    assert resp.status_code == 201, resp.text
