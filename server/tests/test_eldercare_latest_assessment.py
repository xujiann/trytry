"""老年健康「每人取最新一次评估」按评估日期取，不按录入先后（P2-125）。

失能清单、预警（重度失能专案 + 年度复评）与统计都写着「每位老人最近一次评估」，实现却按编号取最后录的那条。
基层补录纸质评估表是常事：先录了今年的（96 分，能力完好），再补一张去年的（30 分，重度失能）——补的那张就成了
「最新一次」：人进了失能清单、报重度失能专案、统计里算一名重度失能，复评提醒还按去年的日期报「已超一年」。

修法：按评估日期取最晚的（没填日期的按录入那天），同一天取后录的。按时间顺序录入时结果与原先一致。
"""
import itertools

import pytest

_CARDS = itertools.count(1)
TODAY = "2026-09-26"


@pytest.fixture()
def elder(client, admin):
    resp = client.post("/api/patients", headers=admin, json={
        "name": "补录评估老人", "id_card": f"33010619450505{next(_CARDS):04d}", "gender": "男", "birth_date": "1945-05-05"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _assess(client, admin, patient_id, score, assessed_date):
    resp = client.post("/api/eldercare/assessments", headers=admin, json={
        "patient_id": patient_id, "adl_score": score, "assessed_date": assessed_date})
    assert resp.status_code == 201, resp.text
    return resp.json()


def _mine(rows, patient_id):
    return [r for r in rows if r["patient_id"] == patient_id]


def test_先录今年再补去年_失能清单与预警按今年那张算(client, admin, elder):
    _assess(client, admin, elder, 96, "2026-08-10")   # 今年：能力完好
    _assess(client, admin, elder, 30, "2025-03-01")   # 补录去年的纸质表：重度失能

    disabled = client.get("/api/eldercare/disabled", headers=admin).json()
    assert _mine(disabled, elder) == []   # 修前：按补录的那张进了失能清单（重度失能）
    alerts = client.get(f"/api/eldercare/alerts?today={TODAY}", headers=admin).json()["alerts"]
    assert _mine(alerts, elder) == []     # 修前：重度失能专案 + 「距上次评估已超一年」各一条


def test_统计按每人评估日期最晚的那张计(client, admin, elder):
    before = client.get("/api/eldercare/stats", headers=admin).json()["by_care_level"]
    _assess(client, admin, elder, 96, "2026-08-10")
    _assess(client, admin, elder, 30, "2025-03-01")
    after = client.get("/api/eldercare/stats", headers=admin).json()["by_care_level"]
    assert after.get("能力完好", 0) == before.get("能力完好", 0) + 1
    assert after.get("重度失能", 0) == before.get("重度失能", 0)   # 修前 +1


def test_按时间顺序录入_结果与原先一致(client, admin, elder):
    _assess(client, admin, elder, 96, "2025-03-01")
    _assess(client, admin, elder, 30, "2026-08-10")   # 今年恶化：重度失能
    (row,) = _mine(client.get("/api/eldercare/disabled", headers=admin).json(), elder)
    assert (row["care_level"], row["adl_score"]) == ("重度失能", 30)
    alerts = _mine(client.get(f"/api/eldercare/alerts?today={TODAY}", headers=admin).json()["alerts"], elder)
    assert [a["alert_type"] for a in alerts] == ["severe_disability"]


def test_同一天两张取后录的_没填日期的按录入那天(client, admin, elder):
    _assess(client, admin, elder, 30, "2020-09-01")
    _assess(client, admin, elder, 70, "2020-09-01")   # 同日更正：轻度
    (row,) = _mine(client.get("/api/eldercare/disabled", headers=admin).json(), elder)
    assert (row["care_level"], row["adl_score"]) == ("轻度失能", 70)

    _assess(client, admin, elder, 96, "")   # 没填日期：按录入那天（晚于 2020-09-01）算最新
    assert _mine(client.get("/api/eldercare/disabled", headers=admin).json(), elder) == []
