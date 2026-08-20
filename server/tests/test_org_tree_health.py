"""机构树体检接口（GET /api/organizations/tree-health）。

配套 ADR-0004（转诊分级审核按机构树 parent_id 逐级上收，非顶层机构缺 parent_id 会把
非全域账号 403）与 ADR-0005（链路收敛为村→乡镇→区市县三级，父子层级错位会让环节名
与实际处理机构对不上）。本接口把两类缺陷一次列清，供运维在建树阶段就发现。

本文件的 client 是**函数级**的：接口返回的是全库汇总（total/roots/referral_ready），
共享库会让断言依赖用例执行顺序——单跑一条就红、换个顺序也红。每条用例自建干净库，
断言才说得清是自己造的那棵树导致的。
"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture()
def client():
    """每条用例一套干净库：本接口断言的是全库汇总，共享库会引入顺序耦合。"""
    reset_database()
    with TestClient(app) as c:
        yield c


def _admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin123"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def _mkorg(client, admin, name, level, org_type, parent_id=None):
    body = {"name": name, "org_type": org_type, "level": level}
    if parent_id is not None:
        body["parent_id"] = parent_id
    r = client.post("/api/organizations", json=body, headers=admin)
    assert r.status_code == 201, r.text
    return r.json()


def _health(client, admin):
    return client.get("/api/organizations/tree-health", headers=admin).json()


def test_健康三级树全部指标干净(client):
    admin = _admin(client)
    county = _mkorg(client, admin, "体检县医院", "county", "lead_hospital")
    town = _mkorg(client, admin, "体检卫生院", "township", "township", county["id"])
    _mkorg(client, admin, "体检村卫生室", "village", "village", town["id"])

    health = _health(client, admin)
    assert health["total"] == 3
    assert health["roots"] == 1                 # 只有顶层 county 是合法树根
    assert health["orphans"] == []
    assert health["broken_chains"] == []
    assert health["max_depth"] == 3
    assert health["referral_ready"] is True


def test_市级四层树合法不误报(client):
    """判据是层级相邻、不是链路长度：city→county→township→village 四层是合法的。

    `spd/platform.py` 的 org_level 把 city 归到 county 处理，转诊逐级校验一次只上收
    一层、根本走不到市级节点——按节点数报"超深"会把正常的市级牵头架构判成故障。
    """
    admin = _admin(client)
    city = _mkorg(client, admin, "体检市医院", "city", "lead_hospital")
    county = _mkorg(client, admin, "市属县医院", "county", "lead_hospital", city["id"])
    town = _mkorg(client, admin, "市属卫生院", "township", "township", county["id"])
    _mkorg(client, admin, "市属村卫生室", "village", "village", town["id"])

    health = _health(client, admin)
    assert health["broken_chains"] == [], "合法的市级四层树不该被报为错位"
    assert health["max_depth"] == 4, "max_depth 只是信息项，四层不等于有问题"
    assert health["referral_ready"] is True


def test_链路终点之上如何挂载不参与判定(client):
    """county/city 是转诊链终点，其上怎么挂与转诊无关——真实医共体形态不该判故障。

    县级公卫机构挂在县医院之下、市级协作医院挂在县医院之下，都是常见架构；
    只要 village→township→county 这段完好，就不该翻转 referral_ready。
    """
    admin = _admin(client)
    county = _mkorg(client, admin, "终点县医院", "county", "lead_hospital")
    town = _mkorg(client, admin, "终点卫生院", "township", "township", county["id"])
    _mkorg(client, admin, "终点村卫生室", "village", "village", town["id"])
    # county 之下再挂 county 级公卫机构、city 级协作医院
    _mkorg(client, admin, "县疾控中心", "county", "public_health", county["id"])
    _mkorg(client, admin, "市协作医院", "city", "lead_hospital", county["id"])

    health = _health(client, admin)
    assert health["broken_chains"] == [], "链路终点之上的挂载不参与转诊阶梯判定"
    assert health["referral_ready"] is True


def test_层级错位报出并翻转referral_ready(client):
    """county→卫生院→服务站(村级)→村室：第四层的父机构是村级，层级不相邻。

    这正是 ADR-0005 收敛掉的那档"服务站"：村室发起的单子第一步"卫生院审核"会由
    服务站完成，推到 accepted 时县级医院从未经手。库里没有孤儿，
    referral_ready 的翻转只可能来自本条错位。
    """
    admin = _admin(client)
    county = _mkorg(client, admin, "错位县医院", "county", "lead_hospital")
    town = _mkorg(client, admin, "错位卫生院", "township", "township", county["id"])
    station = _mkorg(client, admin, "错位服务站", "village", "village", town["id"])
    village = _mkorg(client, admin, "错位村卫生室", "village", "village", station["id"])

    health = _health(client, admin)
    assert health["orphans"] == [], "干净库无孤儿，下面的翻转只能归因于错位"

    broken = {b["id"]: b for b in health["broken_chains"]}
    assert set(broken) == {village["id"]}, "只有父机构为村级的那间村室错位"
    entry = broken[village["id"]]
    assert entry["parent_level"] == "village"
    assert entry["expected_parent_levels"] == ["township"]
    assert entry["chain"] == [
        "错位县医院", "错位卫生院", "错位服务站", "错位村卫生室",
    ], "链路应自根到叶，便于运维一眼看出错在哪一层"
    assert health["referral_ready"] is False, "错位必须让 referral_ready 翻转"


def test_卫生院挂在卫生院下也算错位(client):
    """township 的父机构须是 county/city——挂在另一家卫生院下，第二步收不到县级。"""
    admin = _admin(client)
    county = _mkorg(client, admin, "双院县医院", "county", "lead_hospital")
    town_a = _mkorg(client, admin, "双院卫生院甲", "township", "township", county["id"])
    town_b = _mkorg(client, admin, "双院卫生院乙", "township", "township", town_a["id"])

    health = _health(client, admin)
    broken = {b["id"]: b for b in health["broken_chains"]}
    assert set(broken) == {town_b["id"]}
    assert broken[town_b["id"]]["expected_parent_levels"] == ["city", "county"]
    assert health["referral_ready"] is False


def test_孤儿报出(client):
    admin = _admin(client)
    county = _mkorg(client, admin, "孤儿县医院", "county", "lead_hospital")
    orphan = _mkorg(client, admin, "孤儿村卫生室", "village", "village")

    health = _health(client, admin)
    assert {o["id"] for o in health["orphans"]} == {orphan["id"]}
    assert health["referral_ready"] is False
    # 孤儿无父，不该同时被算成树根、也不该进 broken_chains（那是"有父但错位"）
    assert health["roots"] == 1, f"只有 {county['name']} 是合法树根"
    assert health["broken_chains"] == []


def test_tree_health_requires_admin(client):
    admin = _admin(client)
    org = _mkorg(client, admin, "体检权限院", "township", "township")
    client.post(
        "/api/users",
        json={"username": "tree_doc", "password": "pass123456", "role": "doctor",
              "org_id": org["id"]},
        headers=admin,
    )
    doc = client.post(
        "/api/auth/login", json={"username": "tree_doc", "password": "pass123456"}
    ).json()
    doc = {"Authorization": f"Bearer {doc['access_token']}"}
    resp = client.get("/api/organizations/tree-health", headers=doc)
    assert resp.status_code == 403, resp.text
