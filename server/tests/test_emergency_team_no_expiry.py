"""应急队伍没有效期：改档与建档同一条（P2-136）。

建档时给应急队伍填效期 422「应急队伍无效期，请勿填写」（模型注释：物资效期，队伍留空）；改档（PATCH）却不看类型，
照收——给队伍填个过去的日期，队伍就被判「已过期」，进了应急资源的报废桶与短缺清单。
"""
import pytest


@pytest.fixture(scope="module")
def team(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2136 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    resp = client.post("/api/surveillance/resources", headers=admin, json={
        "org_id": org, "resource_type": "team", "name": "P2136 应急小分队", "quantity": 6, "min_quantity": 4})
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_改档给队伍填效期_422(client, admin, team):
    resp = client.patch(f"/api/surveillance/resources/{team['id']}", headers=admin, json={"expire_date": "2026-01-01"})
    assert resp.status_code == 422, resp.text   # 修前 200、expired=true
    assert resp.json()["detail"] == "应急队伍无效期，请勿填写"
    rows = client.get(f"/api/surveillance/resources?org_id={team['org_id']}&shortage_only=true", headers=admin).json()
    assert team["id"] not in {r["id"] for r in rows}


def test_队伍别的字段照改(client, admin, team):
    resp = client.patch(f"/api/surveillance/resources/{team['id']}", headers=admin, json={"quantity": 8, "contact": "张队"})
    assert resp.status_code == 200, resp.text
    assert (resp.json()["quantity"], resp.json()["contact"]) == (8, "张队")
