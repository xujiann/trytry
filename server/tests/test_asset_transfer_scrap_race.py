"""物资调拨与整件报废同时到：已报废的物资被划到调入机构名下（P2-1188，第三十四批扫描 L1-8 物资那一半）。

整件报废（`scrap_asset`）在物资这一行的临界区里刷新之后再判、置已报废（P2-232）；调拨划拨（`transfer_asset`）原先锁外判
「未报废」、往对象上赋值再提交，那条 UPDATE 只有 `WHERE id = ?`。调拨读到物资之后、写入之前，报废先提交了，调拨照旧把
机构改掉，两路都 200：库里这件物资已报废、数量 0，却挂在乙院名下；顺序发生时报废之后再调拨是 409。

修法：划拨与「未报废」同一条 UPDATE（`concurrency.move_row`，`WHERE status != 'scrapped'`），抢输的一路改到 0 行、回滚，
文案与顺序请求同一句。这里把「读到之后、写入之前」钉成确定的时序（同 `test_approval_transition_races.py`）：在两者之间
必经的归属校验（`assert_obj_org_writable`）里，经真实接口插一路整件报废并提交——调拨随后判「已报废」用的是先前读到的
那份对象。
"""
import pytest

from app.database import SessionLocal
from app.models import Asset

M = "/api/mgmt/assets"


@pytest.fixture(scope="module")
def orgs(client, admin):
    return [client.post("/api/organizations", headers=admin, json={
        "name": name, "org_type": "township", "level": "township"}).json()["id"]
        for name in ("P21188 甲卫生院", "P21188 乙卫生院")]


def _asset(client, admin, org, code):
    got = client.post(M, headers=admin, json={
        "org_id": org, "code": code, "name": "心电图机", "category": "equipment", "quantity": 1})
    assert got.status_code == 201, got.text
    return got.json()["id"]


def _row(asset_id):
    with SessionLocal() as db:
        asset = db.get(Asset, asset_id)
        return asset.org_id, asset.status, asset.quantity


def test_调拨读到物资还没写时整件报废先提交_调拨409_物资仍在原机构(client, admin, orgs, monkeypatch):
    from app.routers import admin_mgmt

    a_org, b_org = orgs
    asset_id = _asset(client, admin, a_org, "P21188-A")
    real, fired = admin_mgmt.assert_obj_org_writable, []

    def racing(db, user, obj, *args, **kwargs):
        result = real(db, user, obj, *args, **kwargs)
        if not fired:   # 先记上：插进来的报废自己也过这道归属校验
            fired.append("报废")
            fired.append(client.post(f"{M}/{asset_id}/scrap", headers=admin))
        return result

    monkeypatch.setattr(admin_mgmt, "assert_obj_org_writable", racing)
    got = client.post(f"{M}/{asset_id}/transfer", headers=admin, params={"to_org_id": b_org})
    monkeypatch.undo()
    assert fired, "插桩没有触发：调拨不再在读到物资与写入之间做归属校验了，换一个插点"
    scrapped = fired[1]
    assert scrapped.status_code == 200 and scrapped.json()["status"] == "scrapped", scrapped.text
    assert got.status_code == 409, got.text   # 修前 200 {'org_id': 乙院, 'status': 'scrapped'}
    assert got.json() == {"detail": "已报废物资不可调拨"}   # 与顺序请求同一句
    assert _row(asset_id) == (a_org, "scrapped", 0)   # 修前 org_id 是乙院


def test_不并发时照常调拨_报废之后409_调入机构不存在404(client, admin, orgs):
    a_org, b_org = orgs
    asset_id = _asset(client, admin, a_org, "P21188-B")
    moved = client.post(f"{M}/{asset_id}/transfer", headers=admin, params={"to_org_id": b_org})
    assert moved.status_code == 200, moved.text
    assert (moved.json()["org_id"], moved.json()["status"], moved.json()["quantity"]) == (b_org, "in_use", 1)
    missing = client.post(f"{M}/{asset_id}/transfer", headers=admin, params={"to_org_id": 999999})
    assert missing.status_code == 404 and missing.json() == {"detail": "调入机构不存在"}, missing.text
    assert client.post(f"{M}/{asset_id}/scrap", headers=admin).status_code == 200
    late = client.post(f"{M}/{asset_id}/transfer", headers=admin, params={"to_org_id": a_org})
    assert late.status_code == 409 and late.json() == {"detail": "已报废物资不可调拨"}, late.text
    assert _row(asset_id) == (b_org, "scrapped", 0)
