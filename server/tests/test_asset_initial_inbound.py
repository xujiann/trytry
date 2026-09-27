"""物资建档的初始数量记一笔入库流水（P2-628，第十三批「拆分之和 vs 总额」扫描 Q3-9）。

建档只写台账、不记流水：入库 + 归还 − 领用 − 报废合不上现存量。整件报废之后流水上是「领 3、报废 7」，从没进过库的
10 件凭空出了库。采购验收生成台账时本就同一事务记一笔入库（`materials.receive_purchase`），手工建档没跟上。
"""
import pytest

M = "/api/mgmt/assets"
SIGN = {"inbound": 1, "return": 1, "issue": -1, "scrap": -1}


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2628 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _moves(client, admin, asset):
    return client.get(f"{M}/{asset}/movements", headers=admin).json()


def test_建档数量记一笔入库(client, admin, org):
    created = client.post(M, headers=admin, json={"org_id": org, "code": "P2628-A", "name": "文件柜",
                                                  "category": "office", "quantity": 10})
    assert created.status_code == 201 and created.json()["quantity"] == 10, created.text
    moves = _moves(client, admin, created.json()["id"])
    assert [(m["movement_type"], m["quantity"], m["note"]) for m in moves] == [("inbound", 10, "建档入库")]   # 修前 []


def test_流水合计与现存量对得上(client, admin, org):
    asset = client.post(M, headers=admin, json={"org_id": org, "code": "P2628-B", "name": "轮椅",
                                                "category": "equipment", "quantity": 6}).json()["id"]
    for kind, quantity in (("issue", 4), ("return", 1), ("inbound", 2), ("scrap", 1)):
        assert client.post(f"{M}/{asset}/movements", headers=admin,
                           json={"movement_type": kind, "quantity": quantity}).status_code == 201
    current = next(a for a in client.get(M, headers=admin, params={"org_id": org}).json() if a["id"] == asset)
    ledger = sum(SIGN[m["movement_type"]] * m["quantity"] for m in _moves(client, admin, asset))
    assert (current["quantity"], ledger) == (4, 4)   # 修前流水合计 -2：建档的 6 件不在流水里
    assert client.post(f"{M}/{asset}/scrap", headers=admin).status_code == 200
    assert sum(SIGN[m["movement_type"]] * m["quantity"] for m in _moves(client, admin, asset)) == 0


def test_编码重复_不落流水(client, admin, org):
    first = client.post(M, headers=admin, json={"org_id": org, "code": "P2628-C", "name": "打印机",
                                                "category": "office", "quantity": 2}).json()["id"]
    again = client.post(M, headers=admin, json={"org_id": org, "code": "P2628-C", "name": "打印机",
                                                "category": "office", "quantity": 5})
    assert (again.status_code, again.json()["detail"]) == (409, "物资编码已存在")
    assert [m["quantity"] for m in _moves(client, admin, first)] == [2]
