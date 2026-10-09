"""物资调拨的调入机构就是物资现属机构：原先 200、机构没变，请求级审计却多一条调拨（P2-1634，第四十七批登记起草时
顺带查出；员工调动的同一形状已由 P2-1272 拦住）。

修后 422「调入机构与物资现属机构不能相同」，物资一字不动；调往别家照旧 200。
"""
from app.database import SessionLocal
from app.models import Asset, Organization


def _world(tag: str):
    """机构名全库唯一：每条用例各带一个后缀。"""
    with SessionLocal() as db:
        county = Organization(name=f"P21634县医院{tag}", org_type="hospital", level="county")
        town = Organization(name=f"P21634卫生院{tag}", org_type="township", level="township")
        db.add_all([county, town])
        db.flush()
        asset = Asset(org_id=county.id, code=f"P21634-ZC-{tag}", name="P21634 监护仪", quantity=1, status="in_use")
        db.add(asset)
        db.commit()
        return county.id, town.id, asset.id


def test_调往物资现属机构_422_物资不动(client, admin):
    county, _town, asset_id = _world("a")
    got = client.post(f"/api/mgmt/assets/{asset_id}/transfer?to_org_id={county}", headers=admin)
    assert got.status_code == 422, got.text   # 修前 200
    assert "不能相同" in got.json()["detail"]
    with SessionLocal() as db:
        asset = db.get(Asset, asset_id)
        assert (asset.org_id, asset.status) == (county, "in_use")


def test_调往别家照旧(client, admin):
    _county, town, asset_id = _world("b")
    got = client.post(f"/api/mgmt/assets/{asset_id}/transfer?to_org_id={town}", headers=admin)
    assert got.status_code == 200, got.text
    assert got.json()["org_id"] == town
