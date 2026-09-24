"""流程「我的待办」按角色筛发生在截断之后：最新 200 条都是别的角色的单子时，待办是空的（P1-82）。

`GET /api/workflows/my-tasks` 原先先取本机构最新 200 条在办实例，再在 Python 里按节点角色筛，
`count` 数的是筛剩的。于是只要最新 200 条恰好都卡在别的角色的节点上（药学审核排着长队、
院长签批压了一批），医生的待办就是空的、计数是 0——他名下该推进的单子一张没少，只是排在
第 201 条之后。「就这么多」与「一张都没有」长得一模一样，正是 P2-8 那一族的形状。

修法：角色这一筛挪进查询（节点要求别的角色的实例排除掉），计数走 `count()`，列表切 `paginate`。
判定一字不改：节点角色为空的人人可办；定义或节点对不上号的（定义被改过、节点已不在）照旧算进
待办——推进会 404 / 409，但那是要有人来终止的卡单；admin 照旧看全部。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal
from app.models import User, WorkflowInstance

CAP = 200  # 原硬编码上限


def _login(client, username, password="pw123456"):
    token = client.post(
        "/api/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _tasks(client, headers, **params):
    r = client.get("/api/workflows/my-tasks", params=params, headers=headers)
    assert r.status_code == 200, r.text
    return r


@pytest.fixture(scope="module")
def world(client):
    """一家卫生院的医生与药师；一条「科室申请 → 药学审核 → 归档（不限角色）」的流程。"""
    admin = _login(client, "admin", "admin123")
    org = client.post("/api/organizations",
                      json={"name": "待办截断卫生院", "org_type": "township", "level": "township"},
                      headers=admin).json()["id"]
    for username, role in (("p182_doc", "doctor"), ("p182_ph", "pharmacist")):
        r = client.post("/api/users", json={"username": username, "password": "pw123456", "full_name": username,
                                            "role": role, "org_id": org}, headers=admin)
        assert r.status_code == 201, r.text
    h = {"doc": _login(client, "p182_doc"), "ph": _login(client, "p182_ph"), "admin": admin}
    r = client.post("/api/workflows/definitions",
                    json={"key": "p182_supply", "name": "待办截断测试领用审批",
                          "nodes": [{"key": "apply", "name": "科室申请", "role": "doctor", "next": "review"},
                                    {"key": "review", "name": "药学审核", "role": "pharmacist", "next": "file"},
                                    {"key": "file", "name": "归档", "next": ""}]},
                    headers=admin)
    assert r.status_code == 201, r.text

    def start(title):
        r = client.post("/api/workflows/instances",
                        json={"definition_key": "p182_supply", "business_type": "supply", "business_id": 1,
                              "title": title, "org_id": org},
                        headers=h["doc"])
        assert r.status_code == 201, r.text
        return r.json()["id"]

    ids = {"apply": start("待办截断·待科室申请")}
    ids["review"] = start("待办截断·待药学审核")
    assert client.post(f"/api/workflows/instances/{ids['review']}/advance", json={}, headers=h["doc"]).status_code == 200
    ids["file"] = start("待办截断·待归档")
    for who in ("doc", "ph"):
        assert client.post(f"/api/workflows/instances/{ids['file']}/advance", json={},
                           headers=h[who]).status_code == 200
    with SessionLocal() as db:
        creator = db.query(User).filter(User.username == "p182_doc").one().id
        orphan = WorkflowInstance(definition_key="p182_gone", business_type="supply", business_id=9,
                                  title="待办截断·定义已不在", org_id=org, current_node="whatever", created_by=creator)
        db.add(orphan)
        db.commit()
        ids["orphan"] = orphan.id
    return {"org": org, "h": h, "ids": ids, "creator": creator}


def _mine(client, world, who):
    ids = world["ids"]
    return {t["id"] for t in _tasks(client, world["h"][who], limit=500).json()["tasks"]} & set(ids.values())


# ---------------------------------------------------------------- 特征化（灌量前）
def test_特征化_按节点角色分人_不限角色的与对不上号的人人都算(client, world):
    ids = world["ids"]
    assert _mine(client, world, "doc") == {ids["apply"], ids["file"], ids["orphan"]}
    assert _mine(client, world, "ph") == {ids["review"], ids["file"], ids["orphan"]}
    assert _mine(client, world, "admin") == set(ids.values())


def test_特征化_逐列与排序(client, world):
    body = _tasks(client, world["h"]["doc"]).json()
    rows = [t for t in body["tasks"] if t["id"] in world["ids"].values()]
    assert [t["id"] for t in rows] == sorted((t["id"] for t in rows), reverse=True), "新的在前"
    apply = next(t for t in rows if t["id"] == world["ids"]["apply"])
    assert (apply["current_node"], apply["current_node_name"], apply["current_node_role"]) == \
        ("apply", "科室申请", "doctor")
    assert apply["org_id"] == world["org"] and apply["status"] == "running"
    orphan = next(t for t in rows if t["id"] == world["ids"]["orphan"])
    assert (orphan["current_node_name"], orphan["current_node_role"]) == ("", "")
    assert body["count"] == len(body["tasks"]), "没超过一页时计数就是列表长度"


# ---------------------------------------------------------------- 缺陷回归
@pytest.fixture(scope="module")
def bulk(client, world):
    """再灌 `CAP + 10` 条卡在药学审核的在办实例——都比医生那张新。"""
    before = {who: _tasks(client, world["h"][who]).json()["count"] for who in ("doc", "ph")}
    with SessionLocal() as db:
        db.execute(insert(WorkflowInstance), [
            {"definition_key": "p182_supply", "business_type": "supply", "business_id": 100 + i,
             "title": f"待办截断·灌量{i}", "org_id": world["org"], "current_node": "review",
             "status": "running", "created_by": world["creator"]}
            for i in range(CAP + 10)
        ])
        db.commit()
    return {"before": before, "n": CAP + 10}


def test_别的角色的单子排满最新一页时_医生的待办照样在(client, world, bulk):
    r = _tasks(client, world["h"]["doc"])
    body = r.json()
    assert body["count"] == bulk["before"]["doc"], "医生的待办被别人的单子挤没了"
    assert world["ids"]["apply"] in {t["id"] for t in body["tasks"]}
    assert r.headers["X-Total-Count"] == str(body["count"])


def test_计数是全部待办而不是取回的那一页(client, world, bulk):
    r = _tasks(client, world["h"]["ph"])
    body = r.json()
    assert body["count"] == bulk["before"]["ph"] + bulk["n"], "药师的待办计数被截断在上限上了"
    assert len(body["tasks"]) == CAP and r.headers["X-Total-Count"] == str(body["count"])
    rest = _tasks(client, world["h"]["ph"], offset=CAP).json()["tasks"]
    assert len(rest) == body["count"] - CAP, "往后翻拿得到剩下的"
    assert not {t["id"] for t in rest} & {t["id"] for t in body["tasks"]}
