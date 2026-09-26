"""基金池的月度预结只收本池年度内的月份（P2-167）。

年终清算缺省取「各期预结之和」当全年发生额。原先预结不看月份属于哪一年：2041 年度的池子收得下 2040-12、
2042-01——别的年度的医保支付被归集进来，算进这一年的发生额，结余跟着少算。
"""
import pytest


@pytest.fixture(scope="module")
def pool(client, admin):
    resp = client.post("/api/fund/pools", headers=admin, json={
        "year": 2041, "insurance_type": "resident", "total_amount": 100000})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def test_年度外的月份拒收(client, admin, pool):
    for period in ("2040-12", "2042-01"):
        resp = client.post(f"/api/fund/pools/{pool}/periods", headers=admin,
                           json={"period": period, "actual_amount": 30000})
        assert resp.status_code == 422, (period, resp.text)   # 修前 201
        assert "2041" in resp.json()["detail"]
    assert client.get(f"/api/fund/pools/{pool}/periods", headers=admin).json() == []


def test_年度内照收_清算只算本年度(client, admin, pool):
    for period in ("2041-01", "2041-12"):
        resp = client.post(f"/api/fund/pools/{pool}/periods", headers=admin,
                           json={"period": period, "actual_amount": 20000})
        assert resp.status_code == 201, resp.text
    settled = client.post(f"/api/fund/pools/{pool}/settle", headers=admin, json={})
    assert settled.status_code == 201, settled.text
    assert (settled.json()["total_expense"], settled.json()["balance"]) == (40000, 60000)   # 修前 100000 / 0
