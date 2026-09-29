"""室内质控按日历读测定时刻：P1-100 之前手填的不补零时刻不再按字符串排错（P2-889，第二十四批「存量行 vs 新规则」扫描 Z3-2）。

P2-687 把「上一点 / 下一点」改成按测定时刻比，比的是字符串（只把 `T` 换成空格）。P1-100（09-24）之前测定时间是自由
文本框，存量里「2026-9-20 8:30」在第 6 位是 '9'，比「2026-09-25 09:00」的 '0' 大——同一年里它比任何补零的写法都「晚」：
- 09-25 录的新点取不到它当上一点，真的 2-2s 只判成「1-2s 警告」，不出失控处理、检验结果照发；
- 它自己被当成「时间上的下一点」改判「失控 2-2s」，回执还写着「这是补录点」；L-J 图把它画在最新一端。
修后按日历读（不补零、斜杠写法照读）；读不成日期的按录入时刻算（与留空按录入时刻同一个取法，P2-171）。
"""
from datetime import datetime

import pytest

from app.database import SessionLocal
from app.models import QcMeasurement


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2889 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _lot(client, admin, org, lot_no):
    resp = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "GLU", "item_name": "葡萄糖", "lot_no": lot_no, "target_value": 100, "sd": 2})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _legacy(lot, value, measured_at, created_at=None):
    with SessionLocal() as db:
        row = QcMeasurement(lot_id=lot, value=value, measured_at=measured_at, operator="存量", warning=value > 104,
                            out_of_control=False, violated_rules="", handled=False)
        if created_at is not None:
            row.created_at = created_at
        db.add(row)
        db.commit()
        return row.id


@pytest.mark.parametrize("legacy_at", ["2026-9-20 8:30", "2026/9/20 8:30", "2026-09-20 08:30"],
                         ids=["不补零", "斜杠", "规范写法对照"])
def test_存量不补零的时刻_新点照样跟它比出2_2s(client, admin, org, legacy_at):
    lot = _lot(client, admin, org, f"P2889-{legacy_at}")
    legacy = _legacy(lot, 104.6, legacy_at)   # z=+2.3 警告
    got = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin,
                      json={"value": 105.0, "measured_at": "2026-09-25 09:00"})   # z=+2.5
    assert got.status_code == 201, got.text
    body = got.json()
    assert (body["out_of_control"], body["violated_rules"]) == (True, "2-2s"), body   # 修前不补零的只判警告
    assert body["alert"] == ""   # 修前：存量点被当成「下一点」改判失控
    with SessionLocal() as db:
        row = db.get(QcMeasurement, legacy)
        assert (row.warning, row.out_of_control) == (True, False)
    points = client.get(f"/api/labqc/lots/{lot}/levey-jennings", headers=admin).json()["points"]
    assert [p["id"] for p in points] == [legacy, body["id"]]   # 修前 L-J 图把存量点画在最新一端


def test_读不成日期的按录入时刻排(client, admin, org):
    lot = _lot(client, admin, org, "P2889-garbage")
    garbage = _legacy(lot, 100.0, "上午", created_at=datetime(2026, 9, 1, 0, 30))   # 录入于本地 9-01 08:30
    later = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin,
                        json={"value": 100.0, "measured_at": "2026-09-02 08:00"}).json()
    earlier = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin,
                          json={"value": 100.0, "measured_at": "2026-08-31 08:00"}).json()
    rows = client.get(f"/api/labqc/lots/{lot}/measurements", headers=admin).json()
    assert [r["id"] for r in rows] == [earlier["id"], garbage, later["id"]]
