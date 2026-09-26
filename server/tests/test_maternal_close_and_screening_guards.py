"""孕产妇档案：结案要有产后访视；已结案的档案不收产前筛查（P2-211）。

①结案只看「已分娩」状态，而分娩登记也把档案推到 delivered：分娩当天就能结案，一次产后访视都没有——结案之后产后访视
反被「档案已结案」挡在外面，产妇还从审方的孕产妇人群里提前掉出去（P2-120）。结案接口自己写的是「须完成产后访视」。
②产前筛查不看档案状态：一孕一册之后，按上一胎已结案的旧档案号录进来的高风险结果把上一胎标成高危，这一胎仍是「正常」；
访视与分娩登记早就对结案档案 409。
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


def test_已结案的档案不收产前筛查(client, admin, org):
    record = _record(client, admin)
    client.post(f"/api/maternal/records/{record}/visits", headers=admin, json={"visit_type": "postpartum"})
    assert client.post(f"/api/maternal/records/{record}/close", headers=admin).status_code == 200
    resp = client.post("/api/maternal/screenings", headers=admin, json={
        "record_id": record, "screen_type": "nipt", "screen_date": "2026-09-26", "result": "high_risk"})
    assert resp.status_code == 409, resp.text                                     # 修前 201，上一胎被标成高危
    rows = client.get("/api/maternal/records", headers=admin).json()
    assert next(r for r in rows if r["id"] == record)["high_risk"] is False
