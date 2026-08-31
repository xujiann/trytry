"""机构协作分组 `/api/org-groups` 八个端点的**特征化网 + 响应契约**。

套路同 `test_rules_contract.py` / `test_dataquality_contract.py`：先钉住**当前**
响应的完整 JSON（dict 相等）与键序 → 再加 `response_model` → 加完逐字节不变
（CLAUDE.md §11）。

建模判断：

- `_group_out` 是**两种键集合**：创建回执与列表行带 `member_count`（恒在，键序
  排最后），更新回执与 of-org 行**没有这个键**（不是 null）——键集合按端点固定、
  不随数据变，按「键集合不同就两个模型」拆两个模型，不用 exclude_unset。
- `lead_org_id` 可空（网格化管理常没有牵头单位）：None 如实透出。
- `joined_at` 是 DateTime 列的 `.isoformat()` 字符串出参；本模块无 Money/Float 列。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def admin(client):
    resp = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def orgs(client, admin):
    created = {}
    for key, payload in {
        "lead": {"name": "分组契约牵头医院", "org_type": "lead_hospital", "level": "county"},
        "member": {"name": "分组契约卫生院", "org_type": "township", "level": "township"},
        "loner": {"name": "分组契约卫生室", "org_type": "village", "level": "village"},
    }.items():
        resp = client.post("/api/organizations", json=payload, headers=admin)
        assert resp.status_code in (200, 201), resp.text
        created[key] = resp.json()
    return created


GROUP_WITH_COUNT_KEY_ORDER = [
    "id", "name", "group_type", "group_type_name", "lead_org_id", "note", "active",
    "member_count",
]
GROUP_KEY_ORDER = GROUP_WITH_COUNT_KEY_ORDER[:-1]


@pytest.fixture(scope="module")
def groups(client, admin, orgs):
    zone = client.post(
        "/api/org-groups",
        json={"name": "契约片区", "group_type": "zone", "lead_org_id": orgs["lead"]["id"],
              "note": "片区备注"},
        headers=admin,
    )
    assert zone.status_code == 201, zone.text
    alliance = client.post(
        "/api/org-groups", json={"name": "契约专科联盟", "group_type": "alliance"}, headers=admin
    )
    assert alliance.status_code == 201, alliance.text
    return {"zone": zone.json(), "alliance": alliance.json()}


def test_建组回执精确形状与键序(groups, orgs):
    body = groups["zone"]
    assert list(body.keys()) == GROUP_WITH_COUNT_KEY_ORDER
    assert body == {
        "id": body["id"],
        "name": "契约片区",
        "group_type": "zone",
        "group_type_name": "片区/分片",
        "lead_org_id": orgs["lead"]["id"],
        "note": "片区备注",
        "active": True,
        "member_count": 0,
    }
    # 缺省分支：牵头机构可空（None 不是 0），备注空串
    assert groups["alliance"] == {
        "id": groups["alliance"]["id"],
        "name": "契约专科联盟",
        "group_type": "alliance",
        "group_type_name": "专科联盟",
        "lead_org_id": None,
        "note": "",
        "active": True,
        "member_count": 0,
    }


def test_列表与回执同形(client, admin, groups):
    rows = client.get("/api/org-groups", headers=admin).json()
    assert rows == [groups["zone"], groups["alliance"]]
    assert client.get("/api/org-groups?group_type=zone", headers=admin).json() == [
        groups["zone"]
    ]
    assert client.get("/api/org-groups?active=false", headers=admin).json() == []


def test_更新回执精确_无成员数键(client, admin, groups, orgs):
    body = client.patch(
        f"/api/org-groups/{groups['zone']['id']}", json={"note": "片区备注改"}, headers=admin
    ).json()
    # 更新回执没有 member_count 这个键（整个不在，不是 null）
    assert list(body.keys()) == GROUP_KEY_ORDER
    assert "member_count" not in body
    assert body == {
        "id": groups["zone"]["id"],
        "name": "契约片区",
        "group_type": "zone",
        "group_type_name": "片区/分片",
        "lead_org_id": orgs["lead"]["id"],
        "note": "片区备注改",
        "active": True,
    }


@pytest.fixture(scope="module")
def zone_members(client, admin, groups, orgs):
    added = []
    for key in ("lead", "member"):
        resp = client.post(
            f"/api/org-groups/{groups['zone']['id']}/members",
            json={"org_id": orgs[key]["id"]},
            headers=admin,
        )
        assert resp.status_code == 201, resp.text
        added.append(resp.json())
    return added


def test_加成员回执精确形状与键序(zone_members, groups, orgs):
    assert list(zone_members[0].keys()) == ["group_id", "org_id"]
    assert zone_members[0] == {"group_id": groups["zone"]["id"], "org_id": orgs["lead"]["id"]}
    assert zone_members[1] == {"group_id": groups["zone"]["id"], "org_id": orgs["member"]["id"]}


def test_成员清单精确形状与键序(client, admin, groups, orgs, zone_members):
    rows = client.get(f"/api/org-groups/{groups['zone']['id']}/members", headers=admin).json()
    assert list(rows[0].keys()) == ["org_id", "org_name", "level", "joined_at"]
    # joined_at 是入组时刻的 isoformat 字符串，值不可预测——钉格式、其余全键钉值
    for row in rows:
        assert "T" in row["joined_at"] and row["joined_at"][:2] == "20"
    assert rows == [
        {"org_id": orgs["lead"]["id"], "org_name": "分组契约牵头医院", "level": "county",
         "joined_at": rows[0]["joined_at"]},
        {"org_id": orgs["member"]["id"], "org_name": "分组契约卫生院", "level": "township",
         "joined_at": rows[1]["joined_at"]},
    ]


def test_列表成员数跟随(client, admin, groups, zone_members):
    rows = client.get("/api/org-groups", headers=admin).json()
    assert rows[0] == {**groups["zone"], "note": "片区备注改", "member_count": 2}
    assert rows[1] == groups["alliance"]


def test_某机构归属分组精确_无成员数键(client, admin, groups, orgs, zone_members):
    rows = client.get(f"/api/org-groups/of-org/{orgs['lead']['id']}", headers=admin).json()
    assert [list(r.keys()) for r in rows] == [GROUP_KEY_ORDER]
    assert rows == [{
        "id": groups["zone"]["id"],
        "name": "契约片区",
        "group_type": "zone",
        "group_type_name": "片区/分片",
        "lead_org_id": orgs["lead"]["id"],
        "note": "片区备注改",
        "active": True,
    }]
    # 未入任何组：空列表分支
    assert client.get(f"/api/org-groups/of-org/{orgs['loner']['id']}", headers=admin).json() == []


COVERAGE_NOTE = (
    "未入组机构不会出现在任何分组视图里；按分组统计前请确认此清单为空，"
    "否则分组之和不等于全域总数"
)


def test_覆盖情况精确形状与键序(client, admin, orgs, groups, zone_members):
    body = client.get("/api/org-groups/coverage?group_type=zone", headers=admin).json()
    assert list(body.keys()) == [
        "group_type", "group_type_name", "groups", "orgs_total", "orgs_grouped",
        "ungrouped", "note",
    ]
    assert body == {
        "group_type": "zone",
        "group_type_name": "片区/分片",
        "groups": 1,
        "orgs_total": 3,
        "orgs_grouped": 2,
        "ungrouped": [{"org_id": orgs["loner"]["id"], "org_name": "分组契约卫生室",
                       "level": "village"}],
        "note": COVERAGE_NOTE,
    }
    # 没有任何该类型分组：全部机构未入组（按机构 id 升序）
    grid = client.get("/api/org-groups/coverage?group_type=grid", headers=admin).json()
    assert grid == {
        "group_type": "grid",
        "group_type_name": "网格",
        "groups": 0,
        "orgs_total": 3,
        "orgs_grouped": 0,
        "ungrouped": [
            {"org_id": orgs["lead"]["id"], "org_name": "分组契约牵头医院", "level": "county"},
            {"org_id": orgs["member"]["id"], "org_name": "分组契约卫生院", "level": "township"},
            {"org_id": orgs["loner"]["id"], "org_name": "分组契约卫生室", "level": "village"},
        ],
        "note": COVERAGE_NOTE,
    }


def test_移除成员回执精确(client, admin, groups, orgs, zone_members):
    resp = client.delete(
        f"/api/org-groups/{groups['zone']['id']}/members/{orgs['member']['id']}", headers=admin
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"removed": True}
    assert client.delete(
        f"/api/org-groups/{groups['zone']['id']}/members/{orgs['member']['id']}", headers=admin
    ).status_code == 404
