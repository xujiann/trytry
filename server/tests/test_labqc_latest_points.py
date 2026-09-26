"""室内质控的测定清单与 L-J 图给最近 500 个点，不是最早 500 个（P2-157）。

两处原先都按编号升序取前 500 个——截掉的恰好是最新那一端。一个批号用满 500 个点（一天两次约八个月），第 501 个点
起新录的、包括刚判出的失控点都不上页面；页面上的「失控处理」按钮按 L-J 数据画，于是这个失控点处理不了，而每次
录入都在提示「尚有 N 个失控点未处理」。
"""
import pytest


@pytest.fixture(scope="module")
def world(client, admin):
    from app.database import SessionLocal
    from app.models import QcMeasurement

    org = client.post("/api/organizations", headers=admin, json={
        "name": "P2157 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]
    lot = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "GLU", "item_name": "葡萄糖", "lot_no": "P2157", "target_value": 5.0, "sd": 0.5})
    assert lot.status_code == 201, lot.text
    lot_id = lot.json()["id"]
    with SessionLocal() as db:   # 八个月来在控的 500 个点
        db.add_all([QcMeasurement(lot_id=lot_id, value=5.0, measured_at="2026-01-01 08:00", operator="检验科")
                    for _ in range(500)])
        db.commit()
    out = client.post(f"/api/labqc/lots/{lot_id}/measurements", headers=admin, json={"value": 7.0})
    assert out.status_code == 201 and out.json()["out_of_control"] is True, out.text   # 第 501 个点：1-3s 失控
    return {"lot": lot_id, "latest": out.json()["id"]}


def test_LJ图给最近500个点_刚失控的点在最后(client, admin, world):
    points = client.get(f"/api/labqc/lots/{world['lot']}/levey-jennings", headers=admin).json()["points"]
    assert len(points) == 500
    assert points[-1]["id"] == world["latest"] and points[-1]["out_of_control"] is True   # 修前：不在这 500 个里
    assert [p["id"] for p in points] == sorted(p["id"] for p in points)   # 仍按录入先后排


def test_测定清单同一口径(client, admin, world):
    rows = client.get(f"/api/labqc/lots/{world['lot']}/measurements", headers=admin).json()
    assert len(rows) == 500 and rows[-1]["id"] == world["latest"]
