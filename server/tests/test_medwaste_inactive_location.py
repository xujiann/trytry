"""停用的医废点位不再收新医废、不再入暂存（P2-49）。

点位停用是「科室撤并」的软删除（`deactivate_location` 的 docstring：不删行，历史医废的来源必须永远
查得到），点位清单默认也只列启用的。可「收集」（`POST /api/medwaste`，填产生点）与「入暂存」
（`POST /api/medwaste/{id}/store`，填暂存间）都只判点位存在、类型对、属于该机构，不看停用——
已撤并的科室照样能产生新医废、已拆掉的暂存间照样能收进医废，而收集是受监管的转移联单起点。
界面上的点位下拉只列启用的，走得到这里的是直接调接口的一方，以及点位在页面打开之后才被停用的那一下。

修法：两处在既有的点位校验之后加一句「已停用」，与同文件其余点位校验同为 422；
启用点位的行为不变（特征化用例钉住）。
"""
import pytest

from conftest import login

M = "/api/medwaste"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", json={"name": "点位停用卫生院", "org_type": "township",
                                                  "level": "township"}, headers=admin).json()["id"]
    r = client.post("/api/users", json={"username": "p249_op", "password": "passw0rd1", "full_name": "p249_op",
                                        "role": "operator", "org_id": org}, headers=admin)
    assert r.status_code == 201, r.text
    op = login(client, "p249_op", "passw0rd1")
    locs = {}
    for key, kind in (("src_on", "source"), ("src_off", "source"), ("sto_on", "storage"), ("sto_off", "storage")):
        r = client.post(f"{M}/locations", json={"org_id": org, "name": f"点位停用{key}", "location_type": kind},
                        headers=op)
        assert r.status_code == 201, r.text
        locs[key] = r.json()["id"]
    for key in ("src_off", "sto_off"):
        r = client.delete(f"{M}/locations/{locs[key]}", headers=op)
        assert r.status_code == 200 and r.json()["active"] is False, r.text
    return {"org": org, "op": op, "locs": locs}


def _collect(client, world, source):
    return client.post(M, json={"org_id": world["org"], "waste_type": "infectious", "weight_kg": 1.5,
                                "collected_date": "2031-05-05", "source_location_id": world["locs"][source]},
                       headers=world["op"])


def test_特征化_启用点位照常收集与入暂存(client, world):
    r = _collect(client, world, "src_on")
    assert r.status_code == 201, r.text
    r = client.post(f"{M}/{r.json()['id']}/store", json={"storage_location_id": world["locs"]["sto_on"]},
                    headers=world["op"])
    assert r.status_code == 200 and r.json()["status"] == "stored", r.text


def test_停用的产生点不再收新医废(client, world):
    r = _collect(client, world, "src_off")
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "停用" in r.json()["detail"]


def test_停用的暂存间不再收医废(client, world):
    waste = _collect(client, world, "src_on")
    assert waste.status_code == 201, waste.text
    r = client.post(f"{M}/{waste.json()['id']}/store", json={"storage_location_id": world["locs"]["sto_off"]},
                    headers=world["op"])
    assert r.status_code == 422, (r.status_code, r.text[:200])
    assert "停用" in r.json()["detail"]
    # 没入成：仍是已收集，换一个启用的暂存间照常能入
    r = client.post(f"{M}/{waste.json()['id']}/store", json={"storage_location_id": world["locs"]["sto_on"]},
                    headers=world["op"])
    assert r.status_code == 200 and r.json()["status"] == "stored", r.text


def test_重新启用之后照常可用(client, world):
    r = client.post(f"{M}/locations/{world['locs']['src_off']}/reactivate", headers=world["op"])
    assert r.status_code == 200, r.text
    try:
        assert _collect(client, world, "src_off").status_code == 201
    finally:
        client.delete(f"{M}/locations/{world['locs']['src_off']}", headers=world["op"])
