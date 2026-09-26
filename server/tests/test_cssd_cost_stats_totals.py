"""消毒供应成本核算的合计、件数、整体单件成本与成本构成按全部批次算，不只算最近 200 批（P2-170）。

成本统计写「按批次汇总总成本与单件成本，并给出成本构成与整体单件成本」；实现先取最近 200 批，合计、件数、
整体单件成本、成本构成都只在这 200 批上算——批次一多，页面卡片上的「成本合计」只剩最近一段，整体单件成本
也跟着变成近期的，早一点的人工、耗材成本从构成里消失。逐批明细照旧列最近 200 批。
"""
import pytest


@pytest.fixture(scope="module")
def seeded(client, admin):
    from app.database import SessionLocal
    from app.models import CssdCostItem, SterilizationBatch, User

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2170 消毒供应中心", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    with SessionLocal() as db:
        operator = db.query(User).filter(User.username == "admin").one().id
        old = SterilizationBatch(batch_no="P2170-OLD", center_org_id=org, item_name="手术器械包", quantity=100)
        db.add(old)
        db.flush()
        db.add(CssdCostItem(batch_id=old.id, cost_type="labor", amount=500, created_by=operator))
        # 之后又做了 200 批，每批 10 件、耗材 10 元
        recent = [SterilizationBatch(batch_no=f"P2170-{i:03d}", center_org_id=org, item_name="换药包", quantity=10)
                  for i in range(200)]
        db.add_all(recent)
        db.flush()
        db.add_all([CssdCostItem(batch_id=b.id, cost_type="material", amount=10, created_by=operator) for b in recent])
        db.commit()
        return {"old": old.id}


def test_合计与构成覆盖全部批次(client, admin, seeded):
    stats = client.get("/api/cssd/cost-stats", headers=admin).json()
    assert len(stats["batches"]) == 200 and seeded["old"] not in {b["batch_id"] for b in stats["batches"]}
    # 修前 2000 / 2000 / 1.0、构成里没有人工：最早那批的 500 元人工与 100 件都不算
    assert (stats["total_cost"], stats["total_quantity"], stats["overall_unit_cost"]) == (2500.0, 2100, 1.19)
    assert stats["by_cost_type"] == {"labor": {"amount": 500.0, "name": "人工"},
                                     "material": {"amount": 2000.0, "name": "耗材"}}


def test_按批次筛只算这一批(client, admin, seeded):
    stats = client.get(f"/api/cssd/cost-stats?batch_id={seeded['old']}", headers=admin).json()
    assert [b["batch_id"] for b in stats["batches"]] == [seeded["old"]]
    assert (stats["total_cost"], stats["total_quantity"], stats["overall_unit_cost"]) == (500.0, 100, 5.0)
    assert stats["by_cost_type"] == {"labor": {"amount": 500.0, "name": "人工"}}
