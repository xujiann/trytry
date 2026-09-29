"""药品采购单：申请人不得自批；审批与「还待审批」同一条 UPDATE（P2-759，第二十批「状态机迁移」扫描 M4-3）。

`approve_purchase` 原先只判待审批、不比申请人：管理员（或同时授了申请与审批两类权限点的自定义角色）建一张 5000 粒的
药品采购单，自己批 200、自己验收 200，库存加 5000；同一个账号审批自己的物资采购是 403「不得审批本人提出的采购申请」
（物资采购、手术审批、双通道申报、特病申报、用血审批同一口径，P2-398 / P2-399 / P2-505）。审批还是锁外读改写：
一个批准、一个驳回同时到，后提交的把先提交的结论改掉（物资采购同形的已按 P2-403 修过）。
"""
import pytest

from conftest import login

CODE = "P2759"


@pytest.fixture(scope="module")
def world(client, admin):
    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2759 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    supplier = client.post("/api/pharmacy/suppliers", headers=admin, json={"name": "P2759 药业"}).json()["id"]
    for name in ("a", "b"):
        resp = client.post("/api/users", headers=admin, json={
            "username": f"p2759_dir_{name}", "password": "passw0rd1", "full_name": f"P2759 主任{name}",
            "role": "director", "org_id": org})
        assert resp.status_code in (200, 201), resp.text
    return {"org": org, "supplier": supplier,
            "a": login(client, "p2759_dir_a", "passw0rd1"), "b": login(client, "p2759_dir_b", "passw0rd1")}


def _order(client, admin, world):
    resp = client.post("/api/pharmacy/purchase-orders", headers=admin, json={
        "org_id": world["org"], "supplier_id": world["supplier"], "item_type": "drug",
        "item_code": CODE, "item_name": "P2759 氨氯地平片", "quantity": 5000})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _status(order_id):
    from app.database import SessionLocal
    from app.models import PurchaseOrder

    with SessionLocal() as db:
        order = db.get(PurchaseOrder, order_id)
        return order.status, order.approved_by


@pytest.mark.parametrize("query", ["", "?reject=true"], ids=["批准", "驳回"])
def test_申请人审批本人的采购单_403_仍待审批_别人照常审(client, admin, world, query):
    order = _order(client, admin, world)
    resp = client.post(f"/api/pharmacy/purchase-orders/{order}/approve{query}", headers=admin)
    assert resp.status_code == 403, resp.text   # 修前 200：自己批、自己验收，库存照加
    assert resp.json()["detail"] == "不得审批本人提出的采购单"
    assert _status(order) == ("pending", None)
    other = client.post(f"/api/pharmacy/purchase-orders/{order}/approve{query}", headers=world["a"])
    assert other.status_code == 200, other.text
    assert other.json()["status"] == ("rejected" if query else "approved")


def test_锁外读到待审批_这时另一位主任先驳回_批准409不改掉驳回(client, admin, world, monkeypatch):
    from app.database import SessionLocal
    from app.models import PurchaseOrder, User
    from app.routers import pharmacy

    order = _order(client, admin, world)
    real_guard = pharmacy.assert_obj_org_writable
    fired = []

    def rejected_meanwhile(db, user, obj, *args, **kwargs):
        real_guard(db, user, obj, *args, **kwargs)
        if not fired and isinstance(obj, PurchaseOrder):   # 批准那一路读完「待审批」、写之前：另一位主任先驳回
            fired.append(True)
            with SessionLocal() as other:
                row = other.get(PurchaseOrder, obj.id)
                row.status = "rejected"
                row.approved_by = other.query(User.id).filter(User.username == "p2759_dir_b").scalar()
                other.commit()

    monkeypatch.setattr(pharmacy, "assert_obj_org_writable", rejected_meanwhile)
    resp = client.post(f"/api/pharmacy/purchase-orders/{order}/approve", headers=world["a"])
    assert fired
    assert resp.status_code == 409, resp.text   # 修前 200：批准改掉了先提交的驳回
    with SessionLocal() as db:
        b_id = db.query(User.id).filter(User.username == "p2759_dir_b").scalar()
    assert _status(order) == ("rejected", b_id)
