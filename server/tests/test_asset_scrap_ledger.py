"""物资「整件报废」只改状态：数量原样留着、出入库流水里没有这一笔（P2-232）。

报废有两条路：出入库登记选「报废出库」是记流水、扣数量、扣到 0 才置已报废；物资台账上的「报废」按钮
（`POST /api/mgmt/assets/{id}/scrap`）原先只把状态改成已报废——同一件事落出两种账：流水上看这批物资从没出过库，
台账上却是「已报废、还剩 10 台」。再点一次照样 200。

修法：整件报废记一笔报废出库（剩余数量）、数量清零、置已报废；已报废的再报废 409（与调拨、出入库对已报废
物资同一句）。
"""
import pytest

M = "/api/mgmt/assets"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2232 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _asset(client, admin, org, code, quantity):
    resp = client.post(M, headers=admin, json={"org_id": org, "code": code, "name": "监护仪", "category": "equipment",
                                               "quantity": quantity})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_整件报废记一笔报废出库_数量清零_流水与台账对得上(client, admin, org):
    asset = _asset(client, admin, org, "P2232-A", 10)
    issued = client.post(f"{M}/{asset}/movements", headers=admin, json={"movement_type": "issue", "quantity": 3})
    assert issued.status_code == 201, issued.text
    scrapped = client.post(f"{M}/{asset}/scrap", headers=admin)
    assert scrapped.status_code == 200, scrapped.text
    assert (scrapped.json()["status"], scrapped.json()["quantity"]) == ("scrapped", 0)   # 修前 ("scrapped", 7)
    moves = client.get(f"{M}/{asset}/movements", headers=admin).json()
    # 修前流水只有领用那一笔：剩下的 7 台在流水上从没出过库
    assert [(m["movement_type"], m["quantity"], m["note"]) for m in moves] == [
        ("issue", 3, ""), ("scrap", 7, "整件报废")]


def test_已报废的再报废_409_不重复记账(client, admin, org):
    asset = _asset(client, admin, org, "P2232-B", 2)
    assert client.post(f"{M}/{asset}/scrap", headers=admin).status_code == 200
    again = client.post(f"{M}/{asset}/scrap", headers=admin)
    assert again.status_code == 409 and again.json() == {"detail": "物资已报废"}, again.text   # 修前 200
    assert len(client.get(f"{M}/{asset}/movements", headers=admin).json()) == 1


def test_数量已经领完的整件报废_只改状态不记零数量的流水(client, admin, org):
    asset = _asset(client, admin, org, "P2232-C", 1)
    assert client.post(f"{M}/{asset}/movements", headers=admin,
                       json={"movement_type": "issue", "quantity": 1}).status_code == 201
    scrapped = client.post(f"{M}/{asset}/scrap", headers=admin)
    assert scrapped.status_code == 200 and scrapped.json()["status"] == "scrapped", scrapped.text
    assert [m["movement_type"] for m in client.get(f"{M}/{asset}/movements", headers=admin).json()] == ["issue"]
