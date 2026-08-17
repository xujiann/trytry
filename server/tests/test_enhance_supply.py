"""E5 药品统一采购、统一配送与追溯。

守四段业务与 A 案隔离：
- 统一目录（admin）CRUD 与去重；
- 带量采购批次—分配—履约，履约按机构归属校验；
- 中心配送 create→pick→ship→receive 全程，签收自动落追溯码；
- 跨机构越权：township 经办不得拣货发货方在县医院的配送单（非全域 operator）；
- 非法状态流转与目录重复码走 409。
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
    return [
        client.post(
            "/api/organizations",
            json={"name": name, "org_type": org_type, "level": level},
            headers=admin,
        ).json()
        for name, org_type, level in [
            ("采购县医院", "lead_hospital", "county"),
            ("采购东镇卫生院", "township", "township"),
        ]
    ]


@pytest.fixture(scope="module")
def director(client, admin, orgs):
    client.post(
        "/api/users",
        json={"username": "es_dir", "password": "passw0rd1", "role": "director",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login", json={"username": "es_dir", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def county_op(client, admin, orgs):
    """县医院经办（非全域 operator）。"""
    client.post(
        "/api/users",
        json={"username": "es_op_county", "password": "passw0rd1", "role": "operator",
              "org_id": orgs[0]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login",
                       json={"username": "es_op_county", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


@pytest.fixture(scope="module")
def township_op(client, admin, orgs):
    """乡镇卫生院经办（非全域 operator）。"""
    client.post(
        "/api/users",
        json={"username": "es_op_town", "password": "passw0rd1", "role": "operator",
              "org_id": orgs[1]["id"]},
        headers=admin,
    )
    resp = client.post("/api/auth/login",
                       json={"username": "es_op_town", "password": "passw0rd1"})
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


# ---------------- 统一药品目录 ----------------


def test_目录建档去重与维护(client, admin):
    body = {"drug_code": "XLK001", "drug_name": "阿莫西林胶囊", "spec": "0.25g",
            "unit": "盒", "selected": True, "volume_agreed": 1000, "unit_price": 12.5}
    resp = client.post("/api/consortium-drugs", json=body, headers=admin)
    assert resp.status_code == 201, resp.text
    assert resp.json()["drug_code"] == "XLK001"

    # 重复编码 409
    dup = client.post("/api/consortium-drugs", json=body, headers=admin)
    assert dup.status_code == 409

    cid = resp.json()["id"]
    upd = client.patch(f"/api/consortium-drugs/{cid}",
                       json={"unit_price": 9.9, "active": False}, headers=admin)
    assert upd.status_code == 200
    assert upd.json()["unit_price"] == 9.9 and upd.json()["active"] is False

    # active 过滤
    actives = client.get("/api/consortium-drugs?active=true", headers=admin).json()
    assert all(r["active"] for r in actives)
    assert "XLK001" not in [r["drug_code"] for r in actives]


def test_目录仅admin可写(client, director):
    resp = client.post("/api/consortium-drugs",
                       json={"drug_code": "X9", "drug_name": "测试"}, headers=director)
    assert resp.status_code == 403


# ---------------- 带量采购 + 分配 + 履约 ----------------


@pytest.fixture(scope="module")
def catalog(client, admin):
    return client.post(
        "/api/consortium-drugs",
        json={"drug_code": "VP100", "drug_name": "二甲双胍片", "unit": "盒",
              "selected": True, "volume_agreed": 5000, "unit_price": 8.0},
        headers=admin,
    ).json()


def test_带量采购分配与履约(client, admin, director, county_op, orgs, catalog):
    # 建批次：目录必须存在
    bad = client.post("/api/volume-purchases",
                      json={"catalog_id": 999999, "year": "2026", "total_volume": 5000},
                      headers=director)
    assert bad.status_code == 404

    purchase = client.post(
        "/api/volume-purchases",
        json={"catalog_id": catalog["id"], "year": "2026", "total_volume": 5000},
        headers=director,
    ).json()
    pid = purchase["id"]

    # 分配到县医院
    alloc = client.post(f"/api/volume-purchases/{pid}/allocations",
                        json={"org_id": orgs[0]["id"], "allocated_volume": 3000},
                        headers=director)
    assert alloc.status_code == 201, alloc.text
    aid = alloc.json()["id"]

    # upsert：同机构再分配为覆盖而非新增
    again = client.post(f"/api/volume-purchases/{pid}/allocations",
                        json={"org_id": orgs[0]["id"], "allocated_volume": 3500},
                        headers=director)
    assert again.status_code == 201
    assert again.json()["id"] == aid and again.json()["allocated_volume"] == 3500

    rows = client.get(f"/api/volume-purchases/{pid}/allocations", headers=director).json()
    assert len([r for r in rows if r["org_id"] == orgs[0]["id"]]) == 1

    # 履约累加（县医院经办，本机构分配）
    f = client.patch(f"/api/volume-allocations/{aid}/fulfill",
                     json={"delta": 800}, headers=county_op)
    assert f.status_code == 200
    assert f.json()["fulfilled_volume"] == 800
    f2 = client.patch(f"/api/volume-allocations/{aid}/fulfill",
                      json={"delta": 200}, headers=county_op)
    assert f2.json()["fulfilled_volume"] == 1000


def test_履约跨机构越权(client, director, township_op, orgs, catalog):
    """乡镇经办不得给县医院的分配记履约。"""
    purchase = client.post(
        "/api/volume-purchases",
        json={"catalog_id": catalog["id"], "year": "2027", "total_volume": 100},
        headers=director,
    ).json()
    alloc = client.post(f"/api/volume-purchases/{purchase['id']}/allocations",
                        json={"org_id": orgs[0]["id"], "allocated_volume": 100},
                        headers=director).json()
    resp = client.patch(f"/api/volume-allocations/{alloc['id']}/fulfill",
                        json={"delta": 10}, headers=township_op)
    assert resp.status_code == 403


# ---------------- 中心统一配送 ----------------


def test_配送全程与追溯码(client, county_op, township_op, orgs):
    """县医院 → 东镇卫生院：created→picked→shipped→received，签收自动落追溯码。

    S2：发货扣发货方库存、签收增收货方库存。发货前先给县医院备货。"""
    from app.database import SessionLocal
    from app.models import DrugStock
    with SessionLocal() as db:
        db.add(DrugStock(org_id=orgs[0]["id"], drug_code="VP100", drug_name="带量药", quantity=100, threshold=0))
        db.commit()
    create = client.post(
        "/api/distribution-orders",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[1]["id"],
              "drug_code": "VP100", "qty": 50},
        headers=county_op,
    )
    assert create.status_code == 201, create.text
    oid = create.json()["id"]
    assert create.json()["status"] == "created"

    # 拣货、发货由发货方（县医院）推进
    pick = client.patch(f"/api/distribution-orders/{oid}/advance",
                        json={"action": "pick"}, headers=county_op)
    assert pick.status_code == 200 and pick.json()["status"] == "picked"
    ship = client.patch(f"/api/distribution-orders/{oid}/advance",
                        json={"action": "ship"}, headers=county_op)
    assert ship.json()["status"] == "shipped"

    # 签收由收货方（东镇卫生院）确认
    recv = client.patch(f"/api/distribution-orders/{oid}/advance",
                        json={"action": "receive"}, headers=township_op)
    assert recv.status_code == 200 and recv.json()["status"] == "received"

    # 签收自动落入库追溯码，收货方可查
    traces = client.get(f"/api/drug-traces?trace_code=DIST-{oid}", headers=township_op).json()
    assert len(traces) == 1
    t = traces[0]
    assert t["direction"] == "in" and t["ref_type"] == "distribution"
    assert t["ref_id"] == oid and t["org_id"] == orgs[1]["id"]

    # S2 勾稽：发货方 100−50=50，收货方 0+50=50
    from app.database import SessionLocal
    from app.models import DrugStock
    with SessionLocal() as db:
        frm = db.query(DrugStock).filter(DrugStock.org_id == orgs[0]["id"], DrugStock.drug_code == "VP100").first()
        to = db.query(DrugStock).filter(DrugStock.org_id == orgs[1]["id"], DrugStock.drug_code == "VP100").first()
        assert frm.quantity == 50
        assert to is not None and to.quantity == 50


def test_配送非法状态流转(client, county_op, orgs):
    """created 状态不可直接 ship（越过 pick）→ 409。"""
    oid = client.post(
        "/api/distribution-orders",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[1]["id"],
              "drug_code": "VP100", "qty": 5},
        headers=county_op,
    ).json()["id"]
    resp = client.patch(f"/api/distribution-orders/{oid}/advance",
                        json={"action": "ship"}, headers=county_op)
    assert resp.status_code == 409


def test_配送拣货跨机构越权(client, county_op, township_op, orgs):
    """发货方为县医院的配送单，东镇经办不得拣货（拣货锁发货方可写）。"""
    oid = client.post(
        "/api/distribution-orders",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[1]["id"],
              "drug_code": "VP100", "qty": 8},
        headers=county_op,
    ).json()["id"]
    resp = client.patch(f"/api/distribution-orders/{oid}/advance",
                        json={"action": "pick"}, headers=township_op)
    assert resp.status_code == 403


def test_配送单跨机构可见(client, county_op, township_op, orgs):
    """收货方也看得到发到自己这儿的配送单。"""
    client.post(
        "/api/distribution-orders",
        json={"from_org_id": orgs[0]["id"], "to_org_id": orgs[1]["id"],
              "drug_code": "VP100", "qty": 3},
        headers=county_op,
    )
    town_view = client.get("/api/distribution-orders", headers=township_op).json()
    assert town_view, "收货方应看得到本机构为收货方的配送单"
    assert all(
        o["from_org_id"] == orgs[1]["id"] or o["to_org_id"] == orgs[1]["id"]
        for o in town_view
    )


# ---------------- 追溯码 ----------------


def test_追溯码登记与可见性(client, county_op, township_op, orgs):
    reg = client.post(
        "/api/drug-traces",
        json={"trace_code": "TC777", "drug_code": "VP100", "org_id": orgs[0]["id"],
              "direction": "out", "ref_type": "manual"},
        headers=county_op,
    )
    assert reg.status_code == 201, reg.text

    # 县医院经办本机构可查
    mine = client.get("/api/drug-traces?trace_code=TC777", headers=county_op).json()
    assert len(mine) == 1

    # 东镇经办看不到县医院的追溯行
    other = client.get("/api/drug-traces?trace_code=TC777", headers=township_op).json()
    assert other == []


def test_追溯码跨机构写越权(client, township_op, orgs):
    """乡镇经办不得以县医院名义登记追溯码。"""
    resp = client.post(
        "/api/drug-traces",
        json={"trace_code": "TC888", "drug_code": "VP100", "org_id": orgs[0]["id"],
              "direction": "in"},
        headers=township_op,
    )
    assert resp.status_code == 403
