"""药品供应风险里只出现在缺药登记上的药品，药名取登记上的写法（P2-1664，第四十九批「集中审方与药事监测」扫描 AM3-6）。

`supply_risk` 给仅缺药登记出现的药品写死 `drug_name: ""`（出参说明写着「照抄现状」）。实测（修前）：登记「胰岛素注射液」
（A10AB01）后，风险行是 `{'drug_code': 'A10AB01', 'drug_name': '', …}`，药事监测页的「药品」一格印「—」——登记表上明明有药名。

修法：数还缺着的登记那条查询里顺手取 `func.min(DrugShortage.drug_name)`（口径同用药地图，P2-354：同编码的药名是各自手填的，
取字典序最小的那个写法，稳定、可复现）；有库存告警的照旧取库存行的名字。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P1664 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org}


def _shortage(client, admin, world, code: str, name: str) -> int:
    resp = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": name, "quantity": 20})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _row(client, admin, code: str) -> dict:
    resp = client.get("/api/medication/supply-risk", headers=admin)
    assert resp.status_code == 200, resp.text
    return next(r for r in resp.json()["risks"] if r["drug_code"] == code)


def test_只有缺药登记的药品_风险行带登记上的药名(client, admin, world):
    _shortage(client, admin, world, "P1664-INS", "胰岛素注射液")
    row = _row(client, admin, "P1664-INS")
    assert (row["drug_name"], row["low_stock_orgs"], row["open_shortages"]) == ("胰岛素注射液", 0, 1), row   # 修前 ''


def test_同编码几条登记药名写法不一_取字典序最小的那个(client, admin, world):
    _shortage(client, admin, world, "P1664-MET", "二甲双胍片")
    _shortage(client, admin, world, "P1664-MET", "二甲双胍缓释片")
    assert _row(client, admin, "P1664-MET")["drug_name"] == min("二甲双胍片", "二甲双胍缓释片")


def test_有库存告警的照旧取库存行的名字(client, admin, world):
    stock = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": world["org"], "drug_code": "P1664-AML", "drug_name": "氨氯地平片（库存）", "quantity": 1, "threshold": 50})
    assert stock.status_code == 200, stock.text
    _shortage(client, admin, world, "P1664-AML", "氨氯地平（登记）")
    row = _row(client, admin, "P1664-AML")
    assert (row["drug_name"], row["risk_level"]) == ("氨氯地平片（库存）", "high"), row
