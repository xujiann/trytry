"""室内质控 Westgard 的「上一点」按测定时刻取，补录插进中间的点不打乱相邻关系（P2-687，第十七批「最近 / 最新」扫描 U4-1）。

2-2s（连续两点同侧超 2SD）与 R-4s（相邻两点极差超 4SD）比的是**时间上**相邻的两点。原先「上一点」按录入编号取：
漏录的一次事后补录，补录点跟时间上更晚的那点比，之后录的点又跟补录点比——
- 周二 z=+2.4（警告）、补录周一 z=0、周三 z=+2.6：周三按时间紧跟周二，是 2-2s 失控；原先跟补录点比，只记警告，
  没有「失控处理」按钮、不进「尚有 N 个失控点未处理」，病人结果照发；
- 补录一个 z=−2.2 的早先的点，原先跟最新的 z=+2.1 比出 R-4s——凭空一个要处理的失控。
修后：上一点取测定时刻不晚于本点的最后一个；补录插进中间时，时间上的下一点改跟补录点比、重判（已处理的不动），
回执里说明；清单与 L-J 图按测定时刻排。
"""
import pytest

from app.database import SessionLocal
from app.models import QcMeasurement


@pytest.fixture(scope="module")
def org(client, admin):
    return client.post("/api/organizations", headers=admin, json={
        "name": "P2687 县医院", "org_type": "lead_hospital", "level": "county"}).json()["id"]


def _lot(client, admin, org, lot_no):
    resp = client.post("/api/labqc/lots", headers=admin, json={
        "org_id": org, "item_code": "GLU", "item_name": "葡萄糖", "lot_no": lot_no, "target_value": 5.0, "sd": 0.5})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _measure(client, admin, lot, value, measured_at):
    resp = client.post(f"/api/labqc/lots/{lot}/measurements", headers=admin,
                       json={"value": value, "measured_at": measured_at})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _row(measurement_id):
    with SessionLocal() as db:
        m = db.get(QcMeasurement, measurement_id)
        return m.warning, m.out_of_control, m.violated_rules, m.handled


def test_补录之后_时间上紧跟的两点同侧超2SD判失控(client, admin, org):
    lot = _lot(client, admin, org, "P2687-A")
    tuesday = _measure(client, admin, lot, 6.2, "2026-09-22 08:00")          # z=+2.4 警告
    assert (tuesday["warning"], tuesday["out_of_control"]) == (True, False)
    _measure(client, admin, lot, 5.0, "2026-09-21T16:00")                   # 漏录的周一，事后补录（页面的 T 写法）
    wednesday = _measure(client, admin, lot, 6.3, "2026-09-23 08:00")       # z=+2.6
    assert (wednesday["out_of_control"], wednesday["violated_rules"]) == (True, "2-2s")   # 修前：跟补录点比，只记警告


def test_补录的早先的点不跟最新的点比出R4s(client, admin, org):
    lot = _lot(client, admin, org, "P2687-B")
    _measure(client, admin, lot, 5.0, "2026-09-01 08:00")
    _measure(client, admin, lot, 5.0, "2026-09-02 08:00")
    _measure(client, admin, lot, 6.05, "2026-09-05 08:00")                  # z=+2.1
    backfill = _measure(client, admin, lot, 3.9, "2026-09-01 20:00")        # z=−2.2，前后都是 z=0 的点
    assert (backfill["warning"], backfill["out_of_control"], backfill["violated_rules"]) == (True, False, "")  # 修前 R-4s
    assert backfill["alert"] == ""   # 时间上的下一点（9-02，z=0）跟它比仍在控，不用重判


def test_补录插进中间_时间上的下一点改跟它比并在回执里说明(client, admin, org):
    lot = _lot(client, admin, org, "P2687-C")
    first = _measure(client, admin, lot, 5.0, "2026-09-01 08:00")
    later = _measure(client, admin, lot, 6.2, "2026-09-03 08:00")           # z=+2.4，跟 z=0 比只是警告
    backfill = _measure(client, admin, lot, 6.1, "2026-09-02 08:00")        # z=+2.2
    assert (backfill["warning"], backfill["out_of_control"]) == (True, False)   # 修前跟 9-03 比成 2-2s
    assert _row(later["id"])[:3] == (False, True, "2-2s")                    # 修前 9-03 仍记警告
    assert backfill["alert"] == ("这是补录点：插在 2026-09-03 08:00 那一点之前，该点改跟本点比，"
                                 "重判为「失控 2-2s」（原为「1-2s 警告」）")
    for path in ("levey-jennings", "measurements"):   # 按测定时刻排，补录点不再排在最后
        body = client.get(f"/api/labqc/lots/{lot}/{path}", headers=admin).json()
        points = body["points"] if path == "levey-jennings" else body
        assert [p["id"] for p in points] == [first["id"], backfill["id"], later["id"]]


def test_已处理的失控点不因补录改判(client, admin, org):
    lot = _lot(client, admin, org, "P2687-D")
    _measure(client, admin, lot, 5.0, "2026-09-01 08:00")
    handled = _measure(client, admin, lot, 6.7, "2026-09-03 08:00")         # z=+3.4，1-3s 失控
    resp = client.post(f"/api/labqc/measurements/{handled['id']}/handle", headers=admin,
                       json={"reason": "质控品复溶不当", "corrective_action": "重新复溶复测"})
    assert resp.status_code == 200, resp.text
    backfill = _measure(client, admin, lot, 3.9, "2026-09-02 08:00")        # z=−2.2：9-03 跟它比会多出 R-4s
    assert _row(handled["id"]) == (False, True, "1-3s", True)                # 处理记录与原判定都留着
    assert backfill["alert"] == ""
