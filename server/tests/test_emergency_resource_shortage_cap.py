"""应急资源「只看缺口与过期」先取最新 500 条再筛：早登记的储备缺口排在 500 条之外，勾了反而一条都看不到（P2-310）。

`GET /api/surveillance/resources?shortage_only=true` 原先先 `order_by(id desc).limit(500)` 取一页，再在这一页里挑「低于
最低储备或已过效期」的——资源多的县（药械按批次逐条登记，几百条很常见），早登记的储备缺口、过期药械排在 500 条之外，
勾了「只看缺口」反而看不到。

修法：判据挪到截断之前，写成 SQL（与 `_resource_out` 的两个派生字段逐字对应）。
"""
import pytest
from sqlalchemy import insert

from app.database import SessionLocal

B = "/api/surveillance"


@pytest.fixture(scope="module")
def world(client, admin):
    from app.models import EmergencyResource

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2310 疾控中心", "org_type": "township", "level": "township"}).json()["id"]
    short = client.post(f"{B}/resources", headers=admin, json={
        "org_id": org, "resource_type": "material", "name": "P2310 N95 口罩", "quantity": 10, "unit": "个",
        "min_quantity": 500})
    assert short.status_code == 201, short.text
    expired = client.post(f"{B}/resources", headers=admin, json={
        "org_id": org, "resource_type": "material", "name": "P2310 过期消毒液", "quantity": 20, "unit": "瓶",
        "expire_date": "2020-01-01"})
    assert expired.status_code == 201, expired.text
    with SessionLocal() as db:   # 之后又登记了 500 条储备充足的
        db.execute(insert(EmergencyResource), [
            {"org_id": org, "resource_type": "material", "name": f"P2310 充足物资 {n}", "quantity": 100,
             "unit": "件", "min_quantity": 10, "expire_date": ""} for n in range(500)])
        db.commit()
    return {"org": org, "short": short.json()["id"], "expired": expired.json()["id"]}


def test_早登记的缺口与过期排在500条之外_只看缺口照样列得出(client, admin, world):
    got = client.get(f"{B}/resources", params={"org_id": world["org"], "shortage_only": True}, headers=admin)
    assert got.status_code == 200, got.text
    assert sorted(r["id"] for r in got.json()) == sorted([world["short"], world["expired"]])   # 修前 []
    assert all(r["below_min"] or r["expired"] for r in got.json())


def test_不勾只看缺口照旧最多500条(client, admin, world):
    got = client.get(f"{B}/resources", params={"org_id": world["org"]}, headers=admin)
    assert got.status_code == 200 and len(got.json()) == 500, got.text
