"""流程实例的清单、流转历史与「我的待办」按实例的机构收口（P0-38 第三批）。

流程引擎的写接口早就按实例的机构判归属：发起（`assert_org_writable`）、推进与终止
（`assert_obj_org_writable(instance)`）——实例属于发起时声明的机构，不挂机构的是全县流程、谁都能办。
三个读接口却什么都不看。2026-09-24 实测，一家与谁都没有关系的新卫生院：

- 医生按实例号读到县里某家机构一条审批流的**流转历史**（每一步的审批意见与经手人姓名）；
- 流程清单吐出全县各家的在办实例（标题、关联单据、所在节点）；
- 「我的待办」把别家机构的实例也算成自己的——只按节点角色筛，于是别家的单子出现在待办里，
  点「推进」才吃 403。这一条不只是看多了，是**待办算错了**：待办该是「我办得了的」。

修法：与写侧同一口径——非全域角色只看本机构与不挂机构的实例（`visible_org_ids`）；
历史按实例的机构判 `assert_org_visible`（不挂机构的照旧放行）；不存在照旧 404。
"""
import pytest


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture(scope="module")
def wf_world(client):
    """甲卫生院发起一条挂在本院的审批流并推进一步（留下审批意见）；另有一条不挂机构的全县流程。"""
    admin = _login(client, "admin", "admin123")
    orgs = {}
    for key, name in (("a", "流程收口甲卫生院"), ("b", "流程收口乙卫生院")):
        orgs[key] = client.post("/api/organizations",
                                json={"name": name, "org_type": "township", "level": "township"},
                                headers=admin).json()["id"]
    for username, role, key in (("p038w_doc_a", "doctor", "a"), ("p038w_doc_b", "doctor", "b"),
                                ("p038w_op_b", "operator", "b")):
        r = client.post("/api/users",
                        json={"username": username, "password": "pw123456", "full_name": f"{username}姓名",
                              "role": role, "org_id": orgs[key]},
                        headers=admin)
        assert r.status_code == 201, r.text
    h = {u: _login(client, u) for u in ("p038w_doc_a", "p038w_doc_b", "p038w_op_b")}

    definition = client.post("/api/workflows/definitions",
                             json={"key": "p038_purchase", "name": "流程收口测试采购审批",
                                   "nodes": [{"key": "apply", "name": "科室申请", "role": "doctor", "next": "check"},
                                             {"key": "check", "name": "科室复核", "role": "doctor", "next": "sign"},
                                             {"key": "sign", "name": "院长签批", "role": "director"}]},
                             headers=admin)
    assert definition.status_code == 201, definition.text
    own = client.post("/api/workflows/instances",
                      json={"definition_key": "p038_purchase", "business_type": "purchase", "business_id": 1,
                            "title": "流程收口甲院采购彩超", "org_id": orgs["a"]},
                      headers=h["p038w_doc_a"])
    assert own.status_code == 201, own.text
    moved = client.post(f"/api/workflows/instances/{own.json()['id']}/advance",
                        json={"comment": "流程收口甲院意见：预算内同意"}, headers=h["p038w_doc_a"])
    assert moved.status_code == 200, moved.text
    county = client.post("/api/workflows/instances",
                         json={"definition_key": "p038_purchase", "business_type": "purchase", "business_id": 2,
                               "title": "流程收口全县联合采购"},
                         headers=h["p038w_doc_b"])
    assert county.status_code == 201, county.text
    return {"admin": admin, "h": h, "own": own.json()["id"], "county": county.json()["id"]}


def _listed(client, headers, path):
    r = client.get(path, headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["tasks"] if isinstance(body, dict) else body
    return {row["id"] for row in rows}


@pytest.mark.parametrize("who", ["p038w_doc_b", "p038w_op_b"])
def test_无关机构读不到别家流程的流转历史(client, wf_world, who):
    r = client.get(f"/api/workflows/instances/{wf_world['own']}/history", headers=wf_world["h"][who])
    assert r.status_code == 403, r.text
    assert "预算内同意" not in r.text


@pytest.mark.parametrize("who", ["p038w_doc_b", "p038w_op_b"])
def test_无关机构的流程清单里没有别家的实例(client, wf_world, who):
    assert wf_world["own"] not in _listed(client, wf_world["h"][who], "/api/workflows/instances?limit=500")


def test_我的待办只算办得了的_别家的单子不进来(client, wf_world):
    """乙院医生角色对得上「科室复核」节点，但推进会被机构归属挡回——它不该出现在乙院的待办里。"""
    h = wf_world["h"]["p038w_doc_b"]
    assert wf_world["own"] not in _listed(client, h, "/api/workflows/my-tasks")
    r = client.post(f"/api/workflows/instances/{wf_world['own']}/advance", json={"comment": ""}, headers=h)
    assert r.status_code == 403, "前提：乙院医生本来就推进不了甲院的实例"


def test_本机构照常看得到读得到办得到(client, wf_world):
    h = wf_world["h"]["p038w_doc_a"]
    assert wf_world["own"] in _listed(client, h, "/api/workflows/instances?limit=500")
    assert wf_world["own"] in _listed(client, h, "/api/workflows/my-tasks")
    r = client.get(f"/api/workflows/instances/{wf_world['own']}/history", headers=h)
    assert r.status_code == 200, r.text
    assert "预算内同意" in r.text


@pytest.mark.parametrize("who", ["p038w_doc_a", "p038w_doc_b"])
def test_不挂机构的全县流程照旧谁都看得到办得到(client, wf_world, who):
    h = wf_world["h"][who]
    assert wf_world["county"] in _listed(client, h, "/api/workflows/instances?limit=500")
    assert wf_world["county"] in _listed(client, h, "/api/workflows/my-tasks")
    assert client.get(f"/api/workflows/instances/{wf_world['county']}/history", headers=h).status_code == 200


def test_全域角色照常看全县(client, wf_world):
    admin = wf_world["admin"]
    assert {wf_world["own"], wf_world["county"]} <= _listed(client, admin, "/api/workflows/instances?limit=500")
    assert {wf_world["own"], wf_world["county"]} <= _listed(client, admin, "/api/workflows/my-tasks")
    assert client.get(f"/api/workflows/instances/{wf_world['own']}/history", headers=admin).status_code == 200


def test_实例不存在照旧404(client, wf_world):
    r = client.get("/api/workflows/instances/987654/history", headers=wf_world["h"]["p038w_doc_b"])
    assert r.status_code == 404, r.text
