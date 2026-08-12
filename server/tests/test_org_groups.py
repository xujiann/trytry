"""阶段六 机构协作分组：横向分组与统计口径。

这一步是唯一会改动既有统计口径的改造，所以用例的重点不在 CRUD，而在
**加了维度之后各处口径还对不对**：分组聚合之和等不等于全域总数、
未入组机构会不会凭空消失、分组与机构同时给出时是不是交集。
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
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def orgs(client, admin):
    """两家县级 + 四家乡镇，稍后分入两个片区（各 1 县 2 乡）。"""
    made = []
    for name, org_type, level in [
        ("分组县医院", "lead_hospital", "county"),
        ("分组县中医院", "lead_hospital", "county"),
        ("分组东镇卫生院", "township", "township"),
        ("分组西镇卫生院", "township", "township"),
        ("分组南镇卫生院", "township", "township"),
        ("分组北镇卫生院", "township", "township"),
    ]:
        made.append(
            client.post(
                "/api/organizations",
                json={"name": name, "org_type": org_type, "level": level},
                headers=admin,
            ).json()
        )
    return made


@pytest.fixture(scope="module")
def zones(client, admin, orgs):
    """两个片区，六家机构全部入组（3 + 3）。"""
    z1 = client.post(
        "/api/org-groups",
        json={"name": "县医院片区", "group_type": "zone", "lead_org_id": orgs[0]["id"]},
        headers=admin,
    ).json()
    z2 = client.post(
        "/api/org-groups",
        json={"name": "中医院片区", "group_type": "zone", "lead_org_id": orgs[1]["id"]},
        headers=admin,
    ).json()
    for org in orgs[:3]:
        client.post(f"/api/org-groups/{z1['id']}/members",
                    json={"org_id": org["id"]}, headers=admin)
    for org in orgs[3:]:
        client.post(f"/api/org-groups/{z2['id']}/members",
                    json={"org_id": org["id"]}, headers=admin)
    return [z1, z2]


# ---------------- 分组本身 ----------------


def test_一家机构可属于多个分组(client, admin, orgs, zones):
    """既在某片区，又在某专科联盟——这是常态，不该被互斥掉。"""
    alliance = client.post(
        "/api/org-groups",
        json={"name": "胸痛专科联盟", "group_type": "alliance"},
        headers=admin,
    ).json()
    resp = client.post(f"/api/org-groups/{alliance['id']}/members",
                       json={"org_id": orgs[2]["id"]}, headers=admin)
    assert resp.status_code == 201
    groups = client.get(f"/api/org-groups/of-org/{orgs[2]['id']}", headers=admin).json()
    types = {g["group_type"] for g in groups}
    assert types == {"zone", "alliance"}


def test_同一分组内重复加入被拒(client, admin, orgs, zones):
    resp = client.post(f"/api/org-groups/{zones[0]['id']}/members",
                       json={"org_id": orgs[0]["id"]}, headers=admin)
    assert resp.status_code == 409


def test_同名分组被拒(client, admin, zones):
    resp = client.post("/api/org-groups",
                       json={"name": "县医院片区", "group_type": "zone"}, headers=admin)
    assert resp.status_code == 409


def test_牵头机构可空(client, admin):
    """网格化管理常常没有"牵头单位"这一说。"""
    resp = client.post("/api/org-groups", json={"name": "东片网格", "group_type": "grid"},
                       headers=admin)
    assert resp.status_code == 201 and resp.json()["lead_org_id"] is None


def test_牵头机构不存在返回404(client, admin):
    resp = client.post("/api/org-groups",
                       json={"name": "空头片区", "lead_org_id": 999999}, headers=admin)
    assert resp.status_code == 404


def test_移出成员(client, admin, orgs):
    group = client.post("/api/org-groups", json={"name": "临时联盟", "group_type": "alliance"},
                        headers=admin).json()
    client.post(f"/api/org-groups/{group['id']}/members",
                json={"org_id": orgs[0]["id"]}, headers=admin)
    assert client.delete(f"/api/org-groups/{group['id']}/members/{orgs[0]['id']}",
                         headers=admin).status_code == 200
    assert client.delete(f"/api/org-groups/{group['id']}/members/{orgs[0]['id']}",
                         headers=admin).status_code == 404


def test_分组管理限管理员(client, admin, orgs):
    client.post("/api/users",
                json={"username": "og_op", "password": "passw0rd1", "role": "operator",
                      "org_id": orgs[0]["id"]}, headers=admin)
    token = client.post("/api/auth/login",
                        json={"username": "og_op", "password": "passw0rd1"}).json()["access_token"]
    op = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/org-groups", json={"name": "越权片区"}, headers=op).status_code == 403
    # 只读允许：分组信息本身不敏感，业务人员需要知道自己归哪个片区
    assert client.get("/api/org-groups", headers=op).status_code == 200


# ---------------- 统计口径（本阶段的重点） ----------------


def _ids(rows):
    return {r["org_id"] for r in rows}


def test_分组聚合之和等于全域总数(client, admin, orgs, zones):
    """加了维度之后各处口径还得对得上——这是本阶段唯一必须守住的不变量。

    六家机构全部入组且两片不重叠，所以两片区各自的结果集之和应当正好等于
    不加筛选的全域结果集。
    """
    period = _current_period()
    for path in (
        f"/api/analytics/efficiency?period={period}",
        "/api/inpatient/stats",
    ):
        joiner = "&" if "?" in path else "?"
        allrows = _rows(client.get(path, headers=admin).json())
        parts = [
            _rows(client.get(f"{path}{joiner}group_id={z['id']}", headers=admin).json())
            for z in zones
        ]
        # 只比对本用例造的机构，避免其他用例留下的机构干扰
        mine = {o["id"] for o in orgs}
        assert _ids(parts[0]) | _ids(parts[1]) == _ids(allrows) & mine, path
        assert not (_ids(parts[0]) & _ids(parts[1])), f"{path} 两片区出现重叠机构"


def test_未入组机构不会凭空消失而是被点名(client, admin, orgs, zones):
    """分组统计的天然陷阱：未入组机构的数据从分组视图里消失且无人察觉。"""
    loner = client.post(
        "/api/organizations",
        json={"name": "未入组卫生室", "org_type": "village", "level": "village"},
        headers=admin,
    ).json()
    data = client.get("/api/org-groups/coverage?group_type=zone", headers=admin).json()
    assert loner["id"] in {o["org_id"] for o in data["ungrouped"]}
    assert "分组之和不等于全域总数" in data["note"]


def test_分组与机构同时给出时取交集(client, admin, orgs, zones):
    period = _current_period()
    z1, z2 = zones[0]["id"], zones[1]["id"]
    inside = orgs[0]["id"]      # 属于 z1
    # 在本片区内 → 命中
    rows = client.get(
        f"/api/analytics/drug-use?period={period}&group_id={z1}&org_id={inside}", headers=admin
    ).json()["orgs"]
    assert {r["org_id"] for r in rows} == {inside}
    # 不在本片区内 → 空集，而不是退化成"只按机构筛"
    rows = client.get(
        f"/api/analytics/drug-use?period={period}&group_id={z2}&org_id={inside}", headers=admin
    ).json()["orgs"]
    assert rows == []


def test_不传范围参数即全域(client, admin, orgs, zones):
    """未启用分组的部署应当完全感觉不到这个功能存在。"""
    period = _current_period()
    rows = client.get(f"/api/analytics/drug-use?period={period}", headers=admin).json()["orgs"]
    assert {o["id"] for o in orgs} <= {r["org_id"] for r in rows}


def test_分组不存在返回404而不是静默全域(client, admin):
    """把不存在的分组当成"没筛选"，会让人看着全县数据以为是某片区的。"""
    period = _current_period()
    for path in (
        f"/api/analytics/drug-use?period={period}&group_id=999999",
        f"/api/analytics/efficiency?period={period}&group_id=999999",
        "/api/inpatient/stats?group_id=999999",
        "/api/quality/clinical-indicators?group_id=999999",
        "/api/performance/orgs?group_id=999999",
    ):
        assert client.get(path, headers=admin).status_code == 404, path


def test_绩效排名在筛选后的集合内产生(client, admin, orgs, zones):
    """片区第一不是全县第一，接口文档与返回都不该让人混淆。"""
    allrows = _rows(client.get("/api/performance/orgs", headers=admin).json())
    part = _rows(client.get(f"/api/performance/orgs?group_id={zones[0]['id']}",
                            headers=admin).json())
    assert len(part) == 3
    assert {r["org_id"] for r in part} <= {r["org_id"] for r in allrows}


def test_临床指标支持分组并回显(client, admin, zones):
    data = client.get(
        f"/api/quality/clinical-indicators?group_id={zones[0]['id']}", headers=admin
    ).json()
    assert data["group_id"] == zones[0]["id"]
    assert data["indicators"]


def test_就医流向不提供分组筛选(client, admin):
    """县外就诊没有县内机构归属，只筛一侧会得到分子分母不同域的比率。

    这里断言的是"参数被忽略"而不是"报错"——多传一个查询参数不该让调用方失败，
    但也绝不能让它以为筛成功了，所以文档口径写在 docstring 里，返回体不含 group_id。
    """
    data = client.get("/api/analytics/patient-flow?group_id=1", headers=admin).json()
    assert "group_id" not in data


def _rows(payload):
    """各统计接口的外壳不统一：有的直接返回列表，有的裹在 orgs/items 里。"""
    if isinstance(payload, list):
        return payload
    for key in ("orgs", "scorecards", "items", "results"):
        if key in payload:
            return payload[key]
    raise AssertionError(f"未知的响应外壳：{list(payload)}")


def _current_period():
    from datetime import datetime

    return datetime.now().strftime("%Y-%m")
