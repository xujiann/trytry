"""S10 第十五轮新写路径并发回归：原子自增、条件推进、唯一插入在并发下不丢不重不500。"""
import threading

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


def _race(call, times=8):
    codes, lock, barrier = [], threading.Lock(), threading.Barrier(times)

    def run():
        barrier.wait(timeout=30)
        code = call()
        with lock:
            codes.append(code)

    ths = [threading.Thread(target=run) for _ in range(times)]
    for t in ths:
        t.start()
    for t in ths:
        t.join()
    return sorted(codes)


@pytest.fixture(scope="module")
def orgs(client, admin):
    a = client.post("/api/organizations", json={"name": "并发县医院", "org_type": "lead_hospital", "level": "county"}, headers=admin).json()
    b = client.post("/api/organizations", json={"name": "并发卫生院", "org_type": "township", "level": "township"}, headers=admin).json()
    return a, b


def test_并发履约录入不丢增量(client, admin, orgs):
    """S2 带量履约用原子 UPDATE +delta：8 次 +10 必须精确累加到 80。"""
    a, _ = orgs
    cat = client.post("/api/consortium-drugs", json={"drug_code": "CC1", "drug_name": "并发药"}, headers=admin).json()
    vp = client.post("/api/volume-purchases", json={"catalog_id": cat["id"], "year": "2026", "total_volume": 1000}, headers=admin).json()
    alloc = client.post(f"/api/volume-purchases/{vp['id']}/allocations", json={"org_id": a["id"], "allocated_volume": 500}, headers=admin).json()
    aid = alloc["id"]
    codes = _race(lambda: client.patch(f"/api/volume-allocations/{aid}/fulfill", json={"delta": 10}, headers=admin).status_code)
    assert codes == [200] * 8, codes
    got = client.get(f"/api/volume-purchases/{vp['id']}/allocations", headers=admin).json()
    row = [x for x in got if x["id"] == aid][0]
    assert row["fulfilled_volume"] == 80, row


def test_并发签收只成一单不重复入库(client, admin, orgs):
    """配送 receive 条件 UPDATE：并发签收只有一个 200，其余 409，追溯码只落一条。"""
    a, b = orgs
    from app.database import SessionLocal
    from app.models import DrugStock
    with SessionLocal() as db:
        db.add(DrugStock(org_id=a["id"], drug_code="CC2", drug_name="配送药", quantity=100, threshold=0))
        db.commit()
    order = client.post("/api/distribution-orders", json={"from_org_id": a["id"], "to_org_id": b["id"], "drug_code": "CC2", "qty": 30}, headers=admin).json()
    oid = order["id"]
    client.patch(f"/api/distribution-orders/{oid}/advance", json={"action": "pick"}, headers=admin)
    client.patch(f"/api/distribution-orders/{oid}/advance", json={"action": "ship"}, headers=admin)
    codes = _race(lambda: client.patch(f"/api/distribution-orders/{oid}/advance", json={"action": "receive"}, headers=admin).status_code)
    assert codes.count(200) == 1, codes
    assert codes.count(409) == 7, codes
    traces = client.get(f"/api/drug-traces?trace_code=DIST-{oid}", headers=admin).json()
    assert len(traces) == 1, traces
    # 收货方库存只 +30 一次
    with SessionLocal() as db:
        to = db.query(DrugStock).filter(DrugStock.org_id == b["id"], DrugStock.drug_code == "CC2").first()
        assert to is not None and to.quantity == 30, to.quantity if to else None


def test_并发发卡只成一张(client, admin, orgs):
    """S15 电子健康卡 insert_or_conflict：同一患者并发发卡只成一张，其余 409。"""
    pat = client.post("/api/patients", json={"name": "并发发卡居民", "id_card": "320000199505051234"}, headers=admin).json()
    codes = _race(lambda: client.post("/api/ehealth-cards", json={"patient_id": pat["id"]}, headers=admin).status_code)
    assert codes.count(201) == 1, codes
    assert codes.count(409) == 7, codes
