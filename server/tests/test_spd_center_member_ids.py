"""专病中心的覆盖机构 / 团队清单不查存在：填错的编号原样存进去、照样算进卫健委工作台的覆盖数（P2-433）。

`org_ids` / `team_ids` 是 JSON 编号清单、没有外键；建档与改档只查了牵头机构与负责人（P1-90），两张清单照单全收。
页面上原先没有这三项的录入框（中心页补上了），补录入框之前先把写侧的口子堵上：清单里有不存在的编号即 404，
报出是哪几个。
"""
import pytest

from conftest import login


@pytest.fixture(scope="module")
def h(client):
    return login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def world(client, h):
    org = client.post("/api/organizations", headers=h, json={
        "name": "P2433 卫生院", "org_type": "township", "level": "township"}).json()["id"]
    team = client.post("/api/spd/teams", headers=h, json={
        "name": "P2433 团队", "org_id": org, "level": "township"})
    assert team.status_code in (200, 201), team.text
    return {"org": org, "team": team.json()["id"]}


def _center(client, h, code, **extra):
    return client.post("/api/spd/centers", headers=h, json={
        "code": code, "name": f"{code} 中心", "program_code": "hypertension", **extra})


def test_建中心_覆盖机构或团队清单里有不存在的编号_404(client, h, world):
    bad_org = _center(client, h, "P2433A", org_ids=[world["org"], 999999])
    assert (bad_org.status_code, bad_org.json()["detail"]) == (404, "覆盖机构不存在（org_ids 中的 [999999]）"), \
        bad_org.text   # 修前 201，999999 原样存进去
    bad_team = _center(client, h, "P2433B", team_ids=[888888])
    assert (bad_team.status_code, bad_team.json()["detail"]) == (404, "团队不存在（team_ids 中的 [888888]）")


def test_改中心同样查_合法的照常存(client, h, world):
    created = _center(client, h, "P2433C", org_ids=[world["org"]], team_ids=[world["team"]])
    assert created.status_code == 201, created.text
    assert (created.json()["org_ids"], created.json()["team_ids"]) == ([world["org"]], [world["team"]])
    cid = created.json()["id"]
    bad = client.patch(f"/api/spd/centers/{cid}", headers=h, json={"team_ids": [world["team"], 777777]})
    assert bad.status_code == 404, bad.text   # 修前 200
    cleared = client.patch(f"/api/spd/centers/{cid}", headers=h, json={"org_ids": [], "team_ids": []})
    assert cleared.status_code == 200 and cleared.json()["org_ids"] == [] and cleared.json()["team_ids"] == []
