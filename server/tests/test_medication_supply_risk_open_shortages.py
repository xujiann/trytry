"""药品供应风险只数「还缺着」的缺药登记（P2-127）。

供应风险 = 库存低于阈值 + 未解决的缺药登记。登记数原先按 `status != 已配送` 算：缺药流转后来加了取药 / 未取药 / 取消
三个终态，这三态全被数成「缺药」——药已经被患者取走的登记让这味药照旧挂「高风险」，只剩结案登记的药品也凭空列成
中风险，而真正在路上的「已配送」反倒不算（药到了，这一条本来也不该算，是碰巧对）。

修法：只数已登记 / 采购中（药还在路上）。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "供应风险口径卫生院", "org_type": "township", "level": "township"}).json()["id"]
    return {"org": org}


def _shortage(client, admin, world, code):
    resp = client.post("/api/medication/shortages", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": f"{code} 药"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _advance(client, admin, sid, times):
    for _ in range(times):
        assert client.post(f"/api/medication/shortages/{sid}/advance", headers=admin).status_code == 200


def _close(client, admin, sid, result):
    resp = client.post(f"/api/medication/shortages/{sid}/close", headers=admin,
                       json={"result": result, "reason": "口径用例"})
    assert resp.status_code == 200, resp.text


def _row(client, admin, code):
    rows = [r for r in client.get("/api/medication/supply-risk", headers=admin).json()["risks"] if r["drug_code"] == code]
    return rows[0] if rows else None


def test_只数已登记与采购中(client, admin, world):
    code = "P2127-MIX"
    _shortage(client, admin, world, code)                                    # 已登记：缺
    _advance(client, admin, _shortage(client, admin, world, code), 1)        # 采购中：缺
    _advance(client, admin, _shortage(client, admin, world, code), 2)        # 已配送：药到了
    sid = _shortage(client, admin, world, code)
    _advance(client, admin, sid, 2)
    _close(client, admin, sid, "collected")                                  # 已取药
    sid = _shortage(client, admin, world, code)
    _advance(client, admin, sid, 2)
    _close(client, admin, sid, "no_show")                                    # 未取药
    _close(client, admin, _shortage(client, admin, world, code), "cancelled")  # 已取消
    row = _row(client, admin, code)
    assert row is not None and row["open_shortages"] == 2   # 修前 5：三个终态都算进来、已配送不算


def test_登记都结案了的药品不再列为供应风险(client, admin, world):
    code = "P2127-DONE"
    sid = _shortage(client, admin, world, code)
    _advance(client, admin, sid, 2)
    _close(client, admin, sid, "collected")
    _close(client, admin, _shortage(client, admin, world, code), "cancelled")
    assert _row(client, admin, code) is None   # 修前：open_shortages 2、中风险


def test_库存告警加一条已取药的登记_不是高风险(client, admin, world):
    code = "P2127-INS"
    stock = client.post("/api/pharmacy/stocks", headers=admin, json={
        "org_id": world["org"], "drug_code": code, "drug_name": "胰岛素(口径)", "quantity": 5, "threshold": 50})
    assert stock.status_code in (200, 201), stock.text
    sid = _shortage(client, admin, world, code)
    _advance(client, admin, sid, 2)
    _close(client, admin, sid, "collected")
    row = _row(client, admin, code)
    assert (row["low_stock_orgs"], row["open_shortages"], row["risk_level"]) == (1, 0, "medium")   # 修前 (1, 1, high)
    _shortage(client, admin, world, code)   # 再缺一次：库存告警 + 还缺着 → 高风险
    assert _row(client, admin, code)["risk_level"] == "high"
