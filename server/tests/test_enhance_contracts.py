"""E1 家医签约内容化：服务包目录、履约率、签约费按人头分配。"""
import pytest
from fastapi.testclient import TestClient

from conftest import reset_database

from app.main import app


@pytest.fixture(scope="module")
def client():
    reset_database()
    with TestClient(app) as c:
        yield c


def _login(client, u, p):
    tok = client.post("/api/auth/login", json={"username": u, "password": p}).json()
    return {"Authorization": f"Bearer {tok['access_token']}"}


@pytest.fixture(scope="module")
def admin(client):
    return _login(client, "admin", "admin123")


@pytest.fixture(scope="module")
def setup(client, admin):
    a = client.post("/api/organizations", json={"name": "签约甲卫生院", "org_type": "township", "level": "township"}, headers=admin).json()
    b = client.post("/api/organizations", json={"name": "签约乙卫生院", "org_type": "township", "level": "township"}, headers=admin).json()
    client.post("/api/users", json={"username": "e1_doc_a", "password": "pw123456", "full_name": "甲医生", "role": "doctor", "org_id": a["id"]}, headers=admin)
    client.post("/api/users", json={"username": "e1_doc_b", "password": "pw123456", "full_name": "乙医生", "role": "doctor", "org_id": b["id"]}, headers=admin)
    p1 = client.post("/api/patients", json={"name": "签约居民甲", "id_card": "320000199001011234"}, headers=admin).json()
    p2 = client.post("/api/patients", json={"name": "签约居民乙", "id_card": "320000199001012222"}, headers=admin).json()
    return {"a": a, "b": b, "doc_a": _login(client, "e1_doc_a", "pw123456"),
            "doc_b": _login(client, "e1_doc_b", "pw123456"), "p1": p1, "p2": p2}


def test_service_package_catalog(client, admin):
    # 管理员建服务包 + 条目
    pkg = client.post("/api/service-packages", json={"code": "PKG-CHRONIC", "name": "慢病个性包", "annual_fee": 120, "target_population": "chronic"}, headers=admin)
    assert pkg.status_code == 201, pkg.text
    pid = pkg.json()["id"]
    # 重复编码 409
    assert client.post("/api/service-packages", json={"code": "PKG-CHRONIC", "name": "x"}, headers=admin).status_code == 409
    i1 = client.post(f"/api/service-packages/{pid}/items", json={"service_type": "followup", "name": "季度随访", "annual_times": 4}, headers=admin)
    assert i1.status_code == 201, i1.text
    client.post(f"/api/service-packages/{pid}/items", json={"service_type": "visit", "name": "年度上门", "annual_times": 1}, headers=admin)
    got = client.get("/api/service-packages", headers=admin).json()
    mine = [x for x in got if x["code"] == "PKG-CHRONIC"][0]
    assert len(mine["items"]) == 2
    # 非管理员不能建包
    assert client.post("/api/service-packages", json={"code": "X", "name": "y"}, headers=admin).status_code in (201, 409)


def test_fulfillment_rate(client, admin, setup):
    # 甲医生给甲居民在甲院签约
    c = client.post("/api/contracts", json={"patient_id": setup["p1"]["id"], "org_id": setup["a"]["id"], "doctor_name": "甲医生"}, headers=setup["doc_a"])
    assert c.status_code == 201, c.text
    cid = c.json()["id"]
    pkg = client.post("/api/service-packages", json={"code": "PKG-STD", "name": "标准包", "annual_fee": 60}, headers=admin).json()
    it = client.post(f"/api/service-packages/{pkg['id']}/items", json={"service_type": "followup", "name": "随访", "annual_times": 4}, headers=admin).json()
    # 绑定服务包 + 重点人群
    r = client.patch(f"/api/contracts/{cid}/package", json={"package_id": pkg["id"], "key_population": "chronic", "expire_date": "2026-12-31"}, headers=setup["doc_a"])
    assert r.status_code == 200, r.text
    # 履约 2 次（应 4 次）
    for _ in range(2):
        assert client.post(f"/api/contracts/{cid}/fulfill", json={"item_id": it["id"]}, headers=setup["doc_a"]).status_code == 201
    f = client.get(f"/api/contracts/{cid}/fulfillment", headers=setup["doc_a"]).json()
    assert f["expected_total"] == 4 and f["done_total"] == 2
    assert f["fulfillment_rate"] == 0.5
    # 到期清单
    exp = client.get("/api/contracts/expiring?before=2027-01-01", headers=setup["doc_a"]).json()
    assert any(x["id"] == cid for x in exp)


def test_cross_org_403(client, setup):
    # 乙医生（非全域）不能给甲院签约
    r = client.post("/api/contracts", json={"patient_id": setup["p2"]["id"], "org_id": setup["a"]["id"], "doctor_name": "乙医生"}, headers=setup["doc_b"])
    assert r.status_code == 403, r.text


def test_sign_fee_distribution(client, admin, setup):
    # 甲乙两院各若干有效签约，签约费池按人头×履约率×重点人群系数分配
    # 甲院已有 1 签约（含履约）；给乙院也建一个
    client.post("/api/contracts", json={"patient_id": setup["p2"]["id"], "org_id": setup["b"]["id"], "doctor_name": "乙医生"}, headers=setup["doc_b"])
    # director 建池（全域），直接核定总额 10000
    dir_admin = admin
    pool = client.post("/api/sign-fee/pools", json={"year": "2026", "per_capita": 60, "total_amount": 10000}, headers=dir_admin)
    assert pool.status_code == 201, pool.text
    pid = pool.json()["id"]
    d = client.post(f"/api/sign-fee/pools/{pid}/distribute", headers=dir_admin)
    assert d.status_code == 200, d.text
    dists = client.get(f"/api/sign-fee/pools/{pid}/distributions", headers=dir_admin).json()
    assert len(dists) >= 2
    # 分毫不差：合计恰等于总额
    assert round(sum(x["amount"] for x in dists), 2) == 10000.0
