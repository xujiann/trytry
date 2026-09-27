"""物资「归还」不超过已领未还的量（P2-627，第十三批「正向 vs 逆向」扫描 Q1-1）。

出入库登记里「入库」「归还」走同一个分支，只加不查：领用 2 件、归还 5 件照样 201，台账多出实物不存在的 3 件；
从没领用过的物资也能「归还」，10 变 15——流水上记成「归还」，事后连来源都查不到。领用一侧早有下界（「出库数量超过
现存量」），归还一侧没有，正向与逆向不对称。

修法：归还不得超过这件物资的已领未还（Σ领用 − Σ归还）；判在物资这一行的锁里（读的是流水合计，压不进一条 UPDATE）。
上线前就借出、系统里没有领用记录的，报错里写明走「入库」并在备注里写来源。入库本身不设上限，照旧。
"""
import pytest

M = "/api/mgmt/assets"


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2627 卫生院", "org_type": "township", "level": "township"}).json()["id"]


def _asset(client, admin, org, code, quantity):
    resp = client.post(M, headers=admin, json={"org_id": org, "code": code, "name": "办公椅", "category": "office",
                                               "quantity": quantity})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _move(client, admin, asset, kind, quantity):
    return client.post(f"{M}/{asset}/movements", headers=admin, json={"movement_type": kind, "quantity": quantity})


def _quantity(client, admin, org, asset):
    return next(a for a in client.get(M, headers=admin, params={"org_id": org}).json() if a["id"] == asset)["quantity"]


REFUSED = "归还数量超过已领未还的量（{}）；上线前借出、系统里没有领用记录的请走「入库」并在备注里写明来源"


def test_归还不超过已领未还(client, admin, org):
    asset = _asset(client, admin, org, "P2627-A", 10)
    assert _move(client, admin, asset, "issue", 2).status_code == 201
    over = _move(client, admin, asset, "return", 5)
    assert (over.status_code, over.json()["detail"]) == (409, REFUSED.format(2)), over.text   # 修前 201、台账 13
    assert _quantity(client, admin, org, asset) == 8
    assert _move(client, admin, asset, "return", 2).json()["asset_quantity"] == 10
    again = _move(client, admin, asset, "return", 1)
    assert (again.status_code, again.json()["detail"]) == (409, REFUSED.format(0))


def test_没领用过的不能归还_入库照旧(client, admin, org):
    asset = _asset(client, admin, org, "P2627-B", 10)
    refused = _move(client, admin, asset, "return", 5)
    assert refused.status_code == 409, refused.text   # 修前 201、10 变 15
    assert _quantity(client, admin, org, asset) == 10
    assert _move(client, admin, asset, "inbound", 5).json()["asset_quantity"] == 15   # 入库不设上限


def test_锁里判_两笔归还交错也不超还(client, admin, org, monkeypatch):
    """确定时序：这一笔锁外判过之后、锁到手之前，另一笔先把已领未还的 2 件还完了——这一笔在锁里重算，只能 409。"""
    from app import concurrency
    from app.routers import admin_mgmt

    asset = _asset(client, admin, org, "P2627-C", 10)
    assert _move(client, admin, asset, "issue", 2).status_code == 201
    real = concurrency.serialized_on
    fired = []

    def other_returns_first(db, model, row_id):
        if model.__name__ == "Asset" and not fired:
            fired.append(True)
            assert _move(client, admin, asset, "return", 2).status_code == 201
        return real(db, model, row_id)

    monkeypatch.setattr(admin_mgmt, "serialized_on", other_returns_first)
    got = _move(client, admin, asset, "return", 2)
    assert (got.status_code, got.json()["detail"]) == (409, REFUSED.format(0)), got.text   # 修前两笔都 201、台账 12
    assert _quantity(client, admin, org, asset) == 10
